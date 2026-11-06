#include <ucontext.h>
#include <signal.h>
#include <deque>
#include <jni.h>
#include <unistd.h>
#include <link.h>
#include <set>
#include <dirent.h>
#include <map>
#include <functional>
#include <fcntl.h>
#include <dlfcn.h>
#include <sys/mman.h>
#include <cstdio>
#include <cstdint>
#include <thread>
#include <string>
#include <vector>
#include <sstream>
#include <cstring>
#include <cstdlib>
#include <map>
#include <mutex>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <array>
#include <utility>
#include <unwind.h>
#include <android/log.h>
#include <zlib.h>

// Proper stack unwind (libunwind) for clean caller RVAs - far better than a raw
// stack scan for finding the exact managed method that logged something (e.g.
// the obfuscated credential coroutine behind "Failed to get credentials").
struct UnwindState { uint8_t* base; uint8_t* end; std::string out; int n; };
static _Unwind_Reason_Code UnwindCb(struct _Unwind_Context* ctx, void* arg) {
    auto* st = (UnwindState*)arg;
    uintptr_t pc = _Unwind_GetIP(ctx);
    if (pc && (uint8_t*)pc >= st->base && (uint8_t*)pc < st->end) {
        char b[24]; snprintf(b, sizeof(b), " %llx", (unsigned long long)((uint8_t*)pc - st->base));
        st->out += b;
    }
    if (++st->n > 40) return _URC_END_OF_STACK;
    return _URC_NO_REASON;
}
static std::string UnwindRVAs(uint8_t* base) {
    UnwindState st{ base, base ? base + 0x20000000 : nullptr, "", 0 };
    if (base) _Unwind_Backtrace(UnwindCb, &st);
    return st.out;
}
#include "extern/Dobby/include/dobby.h"
#include "Include/BNMResolve.hpp"
#include "extern/BNM-Android/src/private/Internals.hpp"
#include "Include/httplib.h"
#include "explorer/html_page.hpp"
// --- GameManager edit lock (biglive22, owner 2026-10-13: "make sure your game crashes
// when you try to edit game manager"). The BNM explorer can READ anything, but any
// WRITE / INVOKE / LOOP whose target class chain -- or the live object's real class --
// is GameManager CRASHES the game, so the edit never applies and the attempt ends the
// session. GameManager is how griefers turned creative mode on and broke everyone's maps.
// Judged by the real il2cpp class chain and the resolved object, never request text, so
// spelling the name in pieces or editing by raw address can't get around it.
[[noreturn]] static void GameManagerTrap(const char* where) {
    BNM_FileCheckpointF("[lock] GameManager edit via %s -> crash (anti-grief)", where ? where : "?");
    __android_log_print(ANDROID_LOG_ERROR, "MYDBG", "[lock] GameManager edit blocked -> crash");
    abort();
}
static bool ClassChainIsGameManager(BNM::IL2CPP::Il2CppClass* k) {
    int depth = 0;
    for (BNM::Class cur(k); cur.IsValid() && cur.GetClass() && depth < 32; cur = cur.GetParent(), depth++) {
        const char* n = cur.GetClass()->name;
        if (!n) continue;
        std::string low;
        for (const char* p = n; *p; ++p) low.push_back((char)tolower((unsigned char)*p));
        if (low.find("gamemanager") != std::string::npos) return true;
    }
    return false;
}
static bool ObjectIsGameManager(const void* obj) {
    auto* o = (const BNM::IL2CPP::Il2CppObject*)obj;
    return o && o->klass && ClassChainIsGameManager(o->klass);
}

#include "explorer/luabnm.hpp"

#define LOGD(...) __android_log_print(ANDROID_LOG_DEBUG, "BNMExplorer", __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, "BNMExplorer", __VA_ARGS__)

using namespace BNM;
using namespace BNM::IL2CPP;
using namespace BNM::Structures::Unity;

static int g_port = 8080;

static double nowSeconds() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

static void AttachThread() {
    BNM::AttachIl2Cpp();
}

static std::string jsEsc(const char* s) {
    if (!s) return "";
    std::string o;
    for (const char* p = s; *p; ++p) {
        unsigned char c = (unsigned char)*p;
        if      (c == '"')  o += "\\\"";
        else if (c == '\\') o += "\\\\";
        else if (c == '\n') o += "\\n";
        else if (c == '\r') o += "\\r";
        else if (c == '\t') o += "\\t";
        else if (c < 0x20) { char buf[8]; snprintf(buf, sizeof(buf), "\\u%04x", c); o += buf; }
        else                 o += *p;
    }
    return o;
}

static std::string jsEsc(const std::string& s) { return jsEsc(s.c_str()); }

static std::string typeName(const Il2CppType* t) {
    if (!t) return "?";
    Class c(t);
    if (!c.IsValid()) return "?";
    auto* r = c.GetClass();
    if (!r || !r->name) return "?";
    std::string n = r->name;
    if (r->namespaze && strlen(r->namespaze) > 0)
        n = std::string(r->namespaze) + "." + n;
    return n;
}

struct LoopEntry {
    std::string addr;
    std::string asm_;
    std::string ns;
    std::string cls;
    std::string name;
    std::string ftype;
    bool isProp;
    std::string val;
    int intervalMs;
    std::atomic<bool> active{true};
};

static std::mutex g_loopMtx;
static std::map<std::string, std::shared_ptr<LoopEntry>> g_loops;

struct LogEntry {
    std::string msg;
    int level;
    double timestamp;
};
static std::mutex g_logMtx;
static std::vector<LogEntry> g_logBuffer;
static const size_t LOG_MAX = 500;
static std::atomic<bool> g_logHooked{false};

static void addLogEntry(const std::string& msg, int level) {
    std::lock_guard<std::mutex> lk(g_logMtx);
    if (g_logBuffer.size() >= LOG_MAX) g_logBuffer.erase(g_logBuffer.begin());
    g_logBuffer.push_back({msg, level, nowSeconds()});
}

static void hookDebugLog() {
    if (g_logHooked.exchange(true)) return;
    try {
        Class debugCls("UnityEngine", "Debug");
        if (!debugCls.IsValid()) return;
    } catch(...) {}
}

struct CatcherArg {
    std::string type;
    std::string val;
};

struct CatcherCall {
    double ts;
    std::string instance;
    std::vector<CatcherArg> args;
    std::string ret;
};

struct CatcherEntry {
    std::string id;
    std::string asm_;
    std::string ns;
    std::string cls;
    std::string method;
    std::string retType;
    std::vector<std::string> paramTypes;
    std::vector<std::string> paramNames;
    std::atomic<bool> active{true};
    std::mutex callsMtx;
    std::vector<CatcherCall> calls;
    void* origPtr = nullptr;
    int slotIdx = -1;
    static const size_t MAX_CALLS = 100;
};

static std::mutex g_catcherMtx;
static std::map<std::string, std::shared_ptr<CatcherEntry>> g_catchers;

static std::string logsJson() {
    std::lock_guard<std::mutex> lk(g_logMtx);
    std::ostringstream j;
    j << "[";
    bool first = true;
    for (auto& e : g_logBuffer) {
        if (!first) j << ","; first = false;
        j << "{\"msg\":\"" << jsEsc(e.msg) << "\",\"level\":" << e.level << ",\"t\":" << std::fixed << e.timestamp << "}";
    }
    j << "]";
    return j.str();
}

static std::string jsonVal(const std::string& json, const std::string& key) {
    std::string qk = "\"" + key + "\":";
    auto pos = json.find(qk);
    if (pos == std::string::npos) return "";
    pos += qk.length();
    while (pos < json.length() && (json[pos] == ' ' || json[pos] == '\t')) pos++;
    if (pos < json.length() && json[pos] == '"') {
        pos++;
        size_t end = pos;
        bool esc = false;
        while (end < json.length()) {
            if (json[end] == '\\' && !esc) esc = true;
            else if (json[end] == '"' && !esc) break;
            else esc = false;
            end++;
        }
        if (end >= json.length()) return "";
        std::string raw = json.substr(pos, end - pos);
        std::string out;
        for (size_t i = 0; i < raw.length(); i++) {
            if (raw[i] == '\\' && i + 1 < raw.length()) {
                char n = raw[i+1];
                if      (n == '"')  { out += '"';  i++; }
                else if (n == '\\') { out += '\\'; i++; }
                else if (n == 'n')  { out += '\n'; i++; }
                else if (n == 'r')  { out += '\r'; i++; }
                else out += raw[i];
            } else out += raw[i];
        }
        return out;
    }
    auto end = json.find_first_of(",}", pos);
    if (end == std::string::npos) return "";
    std::string v = json.substr(pos, end - pos);
    while (!v.empty() && (v.back() == ' ' || v.back() == '\t' || v.back() == '\r' || v.back() == '\n')) v.pop_back();
    return v;
}

static MethodBase findObjsMethod() {
    static MethodBase m;
    if (m.IsValid()) return m;
    Class oc("UnityEngine", "Object");
    for (auto& mt : oc.GetMethods(false)) {
        auto* mi = mt.GetInfo();
        if (mi && mi->name && strcmp(mi->name, "FindObjectsOfType") == 0 && mi->parameters_count == 1)
            if (mi->parameters[0] && strstr(typeName(mi->parameters[0]).c_str(), "Type"))
            { m = mt; break; }
    }
    return m;
}

static Il2CppObject* sysTypeOf(const std::string& aqn) {
    Class tc("System", "Type", Image("mscorlib.dll"));
    if (!tc.IsValid()) return nullptr;
    Method<Il2CppObject*> gt(tc.GetMethod("GetType", 1));
    return gt.IsValid() ? gt(CreateMonoString(aqn.c_str())) : nullptr;
}

static bool isObjectAlive(Il2CppObject* obj) {
    if (!obj) return false;
    try {
        Method<bool> m(Class("UnityEngine", "Object").GetMethod("op_Implicit", 1));
        if (m.IsValid()) return m(obj);
        auto* k = obj->klass;
        if (!k || !k->name) return false;
        return true;
    } catch(...) { return false; }
}

static Il2CppObject* resolveComponentByAddr(uintptr_t addr) {
    if (!addr) return nullptr;
    Il2CppObject* obj = (Il2CppObject*)addr;
    if (!isObjectAlive(obj)) return nullptr;
    return obj;
}

static std::string assembliesJson() {
    std::ostringstream j;
    j << "[";
    bool first = true;
    for (auto& img : Image::GetImages()) {
        auto* d = img.GetInfo();
        if (!img.IsValid() || !d || !d->name) continue;
        if (!first) j << ",";
        first = false;
        j << "\"" << jsEsc(d->name) << "\"";
    }
    j << "]";
    return j.str();
}

static std::string classesJson(const std::string& asm_) {
    Image img(asm_);
    if (!img.IsValid()) return "[]";
    std::ostringstream j;
    j << "[";
    bool first = true;
    for (auto& cls : img.GetClasses(false)) {
        auto* r = cls.GetClass();
        if (!cls.IsValid() || !r || !r->name) continue;
        if (!first) j << ",";
        first = false;
        std::string t = "class";
        if (r->enumtype) t = "enum";
        else if (r->byval_arg.valuetype) t = "struct";
        else if (r->flags & 0x20) t = "interface";
        j << "{\"name\":\"" << jsEsc(r->name) << "\",\"ns\":\"" << jsEsc(r->namespaze ? r->namespaze : "") << "\",\"t\":\"" << t << "\"}";
    }
    j << "]";
    return j.str();
}

static std::string allClassesJson() {
    std::ostringstream j;
    j << "[";
    bool first = true;
    for (auto& img : Image::GetImages()) {
        auto* d = img.GetInfo();
        if (!img.IsValid() || !d || !d->name) continue;
        std::string a = d->name;
        for (auto& cls : img.GetClasses(false)) {
            auto* r = cls.GetClass();
            if (!cls.IsValid() || !r || !r->name) continue;
            if (!first) j << ",";
            first = false;
            std::string t = "class";
            if (r->enumtype) t = "enum";
            else if (r->byval_arg.valuetype) t = "struct";
            else if (r->flags & 0x20) t = "interface";
            j << "{\"name\":\"" << jsEsc(r->name) << "\",\"ns\":\"" << jsEsc(r->namespaze ? r->namespaze : "") << "\",\"t\":\"" << t << "\",\"a\":\"" << jsEsc(a) << "\"}";
        }
    }
    j << "]";
    return j.str();
}

static std::string classDetailJson(const std::string& a, const std::string& ns, const std::string& cn) {
    BNM_FileCheckpointF("classDetailJson: start a=%s ns=%s cn=%s", a.c_str(), ns.c_str(), cn.c_str());
    Class cls(ns.c_str(), cn.c_str(), Image(a.c_str()));
    BNM_FileCheckpoint("classDetailJson: Class constructed");
    if (!cls.IsValid()) { BNM_FileCheckpoint("classDetailJson: cls not valid, returning"); return "{}"; }
    BNM_FileCheckpoint("classDetailJson: cls valid, calling GetClass()");
    auto* r = cls.GetClass();
    BNM_FileCheckpointF("classDetailJson: GetClass() returned %p", (void*)r);
    if (!r) return "{}";

    std::ostringstream j;
    j << "{\"name\":\"" << jsEsc(cn) << "\",\"ns\":\"" << jsEsc(ns) << "\",\"asm\":\"" << jsEsc(a) << "\",";
    j << (r->parent && r->parent->name ? "\"parent\":\"" + jsEsc(r->parent->name) + "\"," : "\"parent\":null,");
    BNM_FileCheckpoint("classDetailJson: wrote header, calling GetFields()");

    j << "\"fields\":[";
    bool first = true;
    auto fields = cls.GetFields(false);
    BNM_FileCheckpointF("classDetailJson: GetFields() returned %zu fields", fields.size());
    for (auto& f : fields) {
        auto* fi = f.GetInfo();
        if (!f.IsValid() || !fi || !fi->name) continue;
        if (!first) j << ",";
        first = false;
        j << "{\"name\":\"" << jsEsc(fi->name) << "\",\"type\":\"" << jsEsc(typeName(fi->type)) << "\",\"s\":" << (fi->type && (fi->type->attrs & 0x10) ? "true" : "false") << ",\"off\":" << fi->offset << "}";
    }
    BNM_FileCheckpoint("classDetailJson: fields done, calling GetMethods()");

    j << "],\"methods\":[";
    first = true;
    auto methods = cls.GetMethods(false);
    BNM_FileCheckpointF("classDetailJson: GetMethods() returned %zu methods", methods.size());
    for (auto& m : methods) {
        auto* mi = m.GetInfo();
        if (!m.IsValid() || !mi || !mi->name) continue;
        if (!first) j << ",";
        first = false;
        char ab[32] = {};
        if (mi->methodPointer) snprintf(ab, sizeof(ab), "%llX", (unsigned long long)(uintptr_t)mi->methodPointer);
        j << "{\"name\":\"" << jsEsc(mi->name) << "\",\"ret\":\"" << jsEsc(typeName(mi->return_type)) << "\",\"s\":" << ((mi->flags & 0x10) ? "true" : "false") << ",\"addr\":\"" << ab << "\",\"params\":[";
        for (int i = 0; i < (int)mi->parameters_count; i++) {
            if (i > 0) j << ",";
            j << "{\"n\":\"arg" << i << "\",\"t\":\"" << jsEsc(mi->parameters && mi->parameters[i] ? typeName(mi->parameters[i]) : "?") << "\"}";
        }
        j << "]}";
    }

    j << "],\"props\":[";
    first = true;
    for (auto& p : cls.GetProperties(false)) {
        auto* pi = p._data;
        if (!p.IsValid() || !pi || !pi->name) continue;
        if (!first) j << ",";
        first = false;
        j << "{\"name\":\"" << jsEsc(pi->name) << "\",\"type\":\"" << jsEsc(pi->get && pi->get->return_type ? typeName(pi->get->return_type) : "?") << "\",\"g\":" << (pi->get ? "true" : "false") << ",\"s\":" << (pi->set ? "true" : "false") << "}";
    }
    j << "]}";
    return j.str();
}

struct InvokeResult { bool ok; std::string val, err; };

static InvokeResult invokeMethod(const std::string& body) {
    AttachThread();
    InvokeResult res = {false, "", ""};
    std::string a = jsonVal(body, "asm"), ns = jsonVal(body, "ns"), cn = jsonVal(body, "cls"), mn = jsonVal(body, "method");
    bool isStatic = jsonVal(body, "static") == "true" || jsonVal(body, "static") == "1";
    uintptr_t instAddr = (uintptr_t)strtoull(jsonVal(body, "instance").c_str(), nullptr, 16);

    std::vector<std::string> argT, argV;
    auto ap = body.find("\"args\":[");
    if (ap != std::string::npos) {
        ap += 8;
        auto ae = body.find("]", ap);
        if (ae != std::string::npos) {
            std::string ab = body.substr(ap, ae - ap);
            size_t p = 0;
            while (p < ab.size()) {
                auto ob = ab.find("{", p), cb = ab.find("}", ob);
                if (ob == std::string::npos || cb == std::string::npos) break;
                std::string e = "{" + ab.substr(ob + 1, cb - ob - 1) + "}";
                argT.push_back(jsonVal(e, "t"));
                argV.push_back(jsonVal(e, "v"));
                p = cb + 1;
            }
        }
    }

    if (a.empty() || cn.empty() || mn.empty()) { res.err = "Missing asm/cls/method"; return res; }

    Class cls(ns.c_str(), cn.c_str(), Image(a.c_str()));
    if (!cls.IsValid()) { res.err = "Class not found"; return res; }

    MethodBase mb = cls.GetMethod(mn.c_str(), (int)argT.size());
    if (!mb.IsValid()) mb = cls.GetMethod(mn.c_str());
    if (!mb.IsValid()) {
        for (Class cur = cls.GetParent(); cur.IsValid() && cur.GetClass() && !mb.IsValid(); cur = cur.GetParent()) {
            mb = cur.GetMethod(mn.c_str(), (int)argT.size());
            if (!mb.IsValid()) mb = cur.GetMethod(mn.c_str());
        }
    }
    if (!mb.IsValid()) { res.err = "Method not found"; return res; }
    auto* mi = mb.GetInfo();
    if (!mi || !mi->methodPointer) { res.err = "No pointer"; return res; }
    if (ClassChainIsGameManager(cls.GetClass()) || (mi->klass && ClassChainIsGameManager(mi->klass))
        || (!isStatic && instAddr && ObjectIsGameManager((void*)instAddr))) GameManagerTrap("invoke");

    std::vector<void*> runtimeArgs;
    std::vector<int32_t> vi;
    std::vector<int64_t> vi64;
    std::vector<float> vf;
    std::vector<double> vd;
    std::vector<uint8_t> vb;
    std::vector<int16_t> vs16;
    std::vector<Vector3> vv3;
    std::vector<Vector2> vv2;
    std::vector<Color> vc;
    std::vector<Vector4> vv4;

    for (size_t i = 0; i < argT.size(); i++) {
        auto& t = argT[i]; auto& v = argV[i];
        if (t=="System.Int32"||t=="Int32"||t=="int") {
            vi.push_back(std::stoi(v.empty()?"0":v)); runtimeArgs.push_back(&vi.back());
        } else if (t=="System.Int64"||t=="Int64"||t=="long") {
            vi64.push_back(std::stoll(v.empty()?"0":v)); runtimeArgs.push_back(&vi64.back());
        } else if (t=="System.Single"||t=="Single"||t=="float") {
            vf.push_back(std::stof(v.empty()?"0":v)); runtimeArgs.push_back(&vf.back());
        } else if (t=="System.Double"||t=="Double"||t=="double") {
            vd.push_back(std::stod(v.empty()?"0":v)); runtimeArgs.push_back(&vd.back());
        } else if (t=="System.Boolean"||t=="Boolean"||t=="bool") {
            vb.push_back((uint8_t)(v=="true"||v=="1"?1:0)); runtimeArgs.push_back(&vb.back());
        } else if (t=="System.Byte"||t=="Byte"||t=="byte") {
            vb.push_back((uint8_t)std::stoul(v.empty()?"0":v)); runtimeArgs.push_back(&vb.back());
        } else if (t=="System.Int16"||t=="Int16"||t=="short") {
            vs16.push_back((int16_t)std::stoi(v.empty()?"0":v)); runtimeArgs.push_back(&vs16.back());
        } else if (t=="UnityEngine.Vector3"||t=="Vector3") {
            float x=0,y=0,z=0; sscanf(v.c_str(),"[%f,%f,%f]",&x,&y,&z); vv3.push_back({x,y,z}); runtimeArgs.push_back(&vv3.back());
        } else if (t=="UnityEngine.Vector2"||t=="Vector2") {
            float x=0,y=0; sscanf(v.c_str(),"[%f,%f]",&x,&y); vv2.push_back({x,y}); runtimeArgs.push_back(&vv2.back());
        } else if (t=="UnityEngine.Color"||t=="Color") {
            float r=1,g=1,b=1,aa=1; sscanf(v.c_str(),"[%f,%f,%f,%f]",&r,&g,&b,&aa); vc.push_back({r,g,b,aa}); runtimeArgs.push_back(&vc.back());
        } else if (t=="UnityEngine.Vector4"||t=="Vector4"||t=="UnityEngine.Quaternion"||t=="Quaternion") {
            float x=0,y=0,z=0,w=0; sscanf(v.c_str(),"[%f,%f,%f,%f]",&x,&y,&z,&w); vv4.push_back({x,y,z,w}); runtimeArgs.push_back(&vv4.back());
        } else if (t=="System.String"||t=="String"||t=="string") {
            runtimeArgs.push_back(CreateMonoString(v));
        } else {
            runtimeArgs.push_back((void*)strtoull(v.empty()?"0":v.c_str(), nullptr, 16));
        }
    }

    void* inst = isStatic ? nullptr : (void*)instAddr;
    std::string rtn = typeName(mi->return_type);
    std::ostringstream out;

    Il2CppException* exc = nullptr;
    void* ret = BNM::Internal::il2cppMethods.il2cpp_runtime_invoke(mi, inst, runtimeArgs.empty() ? nullptr : runtimeArgs.data(), &exc);

    if (exc) {
        BNM::Structures::Mono::String* excMsg = nullptr;
        try {
            auto excCls = Class(((Il2CppObject*)exc)->klass);
            if (excCls.IsValid()) {
                auto msgMethod = excCls.GetMethod("get_Message", 0);
                if (msgMethod.IsValid()) {
                    auto* msgMi = msgMethod.GetInfo();
                    if (msgMi && msgMi->methodPointer) {
                        Il2CppException* inner = nullptr;
                        auto* msgRet = (Il2CppObject*)BNM::Internal::il2cppMethods.il2cpp_runtime_invoke(msgMi, exc, nullptr, &inner);
                        if (msgRet && !inner) excMsg = (BNM::Structures::Mono::String*)msgRet;
                    }
                }
            }
        } catch(...) {}
        res.err = excMsg ? excMsg->str() : "IL2CPP exception";
        return res;
    }

    if (rtn=="System.Void"||rtn=="Void"||rtn=="void"||rtn=="?") {
        out << "void";
    } else if (rtn=="System.Single"||rtn=="Single"||rtn=="float") {
        float v = ret ? *(float*)((uint8_t*)ret + sizeof(Il2CppObject)) : 0;
        out << v;
    } else if (rtn=="System.Double"||rtn=="Double"||rtn=="double") {
        double v = ret ? *(double*)((uint8_t*)ret + sizeof(Il2CppObject)) : 0;
        out << v;
    } else if (rtn=="System.Int32"||rtn=="Int32"||rtn=="int") {
        int v = ret ? *(int*)((uint8_t*)ret + sizeof(Il2CppObject)) : 0;
        out << v;
    } else if (rtn=="System.Int64"||rtn=="Int64"||rtn=="long") {
        int64_t v = ret ? *(int64_t*)((uint8_t*)ret + sizeof(Il2CppObject)) : 0;
        out << v;
    } else if (rtn=="System.Boolean"||rtn=="Boolean"||rtn=="bool") {
        bool v = ret ? *(bool*)((uint8_t*)ret + sizeof(Il2CppObject)) : false;
        out << (v?"true":"false");
    } else if (rtn=="System.Byte"||rtn=="Byte"||rtn=="byte") {
        uint8_t v = ret ? *(uint8_t*)((uint8_t*)ret + sizeof(Il2CppObject)) : 0;
        out << (int)v;
    } else if (rtn=="UnityEngine.Vector3"||rtn=="Vector3") {
        Vector3 v = ret ? *(Vector3*)((uint8_t*)ret + sizeof(Il2CppObject)) : Vector3{0,0,0};
        char buf[128]; snprintf(buf,sizeof(buf),"[%.4f, %.4f, %.4f]",v.x,v.y,v.z); out<<buf;
    } else if (rtn=="UnityEngine.Vector2"||rtn=="Vector2") {
        Vector2 v = ret ? *(Vector2*)((uint8_t*)ret + sizeof(Il2CppObject)) : Vector2{0,0};
        char buf[128]; snprintf(buf,sizeof(buf),"[%.4f, %.4f]",v.x,v.y); out<<buf;
    } else if (rtn=="UnityEngine.Color"||rtn=="Color") {
        Color v = ret ? *(Color*)((uint8_t*)ret + sizeof(Il2CppObject)) : Color{0,0,0,0};
        char buf[128]; snprintf(buf,sizeof(buf),"[%.4f, %.4f, %.4f, %.4f]",v.r,v.g,v.b,v.a); out<<buf;
    } else if (rtn=="System.String"||rtn=="String"||rtn=="string") {
        auto* s = (BNM::Structures::Mono::String*)ret;
        out << (s ? s->str() : "null");
    } else {
        char buf[64]; snprintf(buf,sizeof(buf),"0x%llX",(unsigned long long)(uintptr_t)ret);
        out << rtn << " @ " << buf;
    }
    res.ok = true; res.val = out.str();
    return res;
}

static std::string instancesJson(const std::string& a, const std::string& ns, const std::string& cn) {
    AttachThread();
    std::string full = ns.empty() ? cn : ns + "." + cn;
    std::string aNoExt = a;
    auto dp = aNoExt.find(".dll");
    if (dp != std::string::npos) aNoExt = aNoExt.substr(0, dp);

    Il2CppObject* st = sysTypeOf(full + ", " + aNoExt);
    if (!st) st = sysTypeOf(full);
    if (!st) return "{\"error\":\"Class not found / System.Type missing\"}";

    auto fm = findObjsMethod();
    if (!fm.IsValid()) return "{\"error\":\"FindObjectsOfType missing\"}";

    std::ostringstream j;
    j << "{\"instances\":[";
    try {
        auto* arr = Method<Array<Il2CppObject*>*>(fm)(st);
        if (arr && arr->capacity > 0) {
            bool first = true;
            Method<BNM::Structures::Mono::String*> gn(Class("UnityEngine","Object").GetMethod("get_name"));
            for (int i = 0; i < arr->capacity; i++) {
                Il2CppObject* obj = arr->m_Items[i];
                if (!obj) continue;
                if (!first) j << ",";
                first = false;
                std::string name = "obj";
                if (gn.IsValid()) { auto* s = gn[obj](); if (s) name = s->str(); }
                char addr[32]; snprintf(addr, sizeof(addr), "%llX", (unsigned long long)(uintptr_t)obj);
                j << "{\"addr\":\"" << addr << "\",\"name\":\"" << jsEsc(name) << "\"}";
            }
        }
    } catch(...) {}
    j << "]}";
    return j.str();
}

static std::string sceneJson() {
    AttachThread();
    auto fm = findObjsMethod();
    if (!fm.IsValid()) return "[]";

    Il2CppObject* st = sysTypeOf("UnityEngine.GameObject, UnityEngine.CoreModule");
    if (!st) st = sysTypeOf("UnityEngine.GameObject, UnityEngine");
    if (!st) st = sysTypeOf("UnityEngine.GameObject");
    if (!st) return "[]";

    Method<BNM::Structures::Mono::String*> gn(Class("UnityEngine","Object").GetMethod("get_name"));
    Method<bool> ga(Class("UnityEngine","GameObject").GetMethod("get_activeSelf"));
    Method<Il2CppObject*> gt(Class("UnityEngine","GameObject").GetMethod("get_transform"));
    Method<Il2CppObject*> tp(Class("UnityEngine","Transform").GetMethod("get_parent"));
    Method<Il2CppObject*> cg(Class("UnityEngine","Component").GetMethod("get_gameObject"));

    std::ostringstream j;
    j << "[";
    try {
        auto* arr = Method<Array<Il2CppObject*>*>(fm)(st);
        if (arr && arr->capacity > 0) {
            bool first = true;
            for (int i = 0; i < arr->capacity; i++) {
                Il2CppObject* go = arr->m_Items[i];
                if (!go) continue;
                std::string name = "Unknown";
                bool active = false;
                if (gn.IsValid()) { auto* s = gn[go](); if (s) name = s->str(); }
                if (ga.IsValid()) active = ga[go]();
                uintptr_t par = 0;
                if (gt.IsValid() && tp.IsValid() && cg.IsValid()) {
                    Il2CppObject* tr = gt[go]();
                    if (tr) { Il2CppObject* pt = tp[tr](); if (pt) { Il2CppObject* pg = cg[pt](); if (pg) par = (uintptr_t)pg; } }
                }
                if (!first) j << ",";
                first = false;
                j << "{\"addr\":\"" << std::hex << (uintptr_t)go << "\",\"name\":\"" << jsEsc(name) << "\",\"active\":" << (active?"true":"false") << ",\"parent\":\"" << std::hex << par << "\"}";
            }
        }
    } catch(...) {}
    j << "]";
    return j.str();
}

static std::string readField(const std::string& ft, Class& cls, Il2CppObject* inst, const std::string& name, MethodBase* getter, bool isStatic, bool& ok) {
    ok = false;
    if (!inst && !isStatic) return "";
    try {
        if (ft=="System.Single"||ft=="Single"||ft=="float") {
            float v = 0;
            if (getter) { Method<float> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<float> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.Double"||ft=="Double"||ft=="double") {
            double v = 0;
            if (getter) { Method<double> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<double> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.Int32"||ft=="Int32"||ft=="int") {
            int v = 0;
            if (getter) { Method<int> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<int> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.UInt32"||ft=="UInt32"||ft=="uint") {
            uint32_t v = 0;
            if (getter) { Method<uint32_t> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<uint32_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.Int64"||ft=="Int64"||ft=="long") {
            int64_t v = 0;
            if (getter) { Method<int64_t> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<int64_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.UInt64"||ft=="UInt64"||ft=="ulong") {
            uint64_t v = 0;
            if (getter) { Method<uint64_t> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<uint64_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.Int16"||ft=="Int16"||ft=="short") {
            int16_t v = 0;
            if (getter) { Method<int16_t> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<int16_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.UInt16"||ft=="UInt16"||ft=="ushort") {
            uint16_t v = 0;
            if (getter) { Method<uint16_t> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<uint16_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.Byte"||ft=="Byte"||ft=="byte") {
            uint8_t v = 0;
            if (getter) { Method<uint8_t> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<uint8_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.SByte"||ft=="SByte"||ft=="sbyte") {
            int8_t v = 0;
            if (getter) { Method<int8_t> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<int8_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.Boolean"||ft=="Boolean"||ft=="bool") {
            bool v = false;
            if (getter) { Method<bool> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<bool> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return v ? "true" : "false";
        } else if (ft=="System.String"||ft=="String"||ft=="string") {
            BNM::Structures::Mono::String* s = nullptr;
            if (getter) { Method<BNM::Structures::Mono::String*> m(*getter); if(!isStatic) m.SetInstance(inst); s = m(); }
            else { Field<BNM::Structures::Mono::String*> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);s=f();} }
            ok = true; return s ? "\"" + jsEsc(s->str()) + "\"" : "\"\"";
        } else if (ft=="UnityEngine.Vector3"||ft=="Vector3") {
            Vector3 v = {0,0,0};
            if (getter) { Method<Vector3> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<Vector3> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return "[" + std::to_string(v.x) + "," + std::to_string(v.y) + "," + std::to_string(v.z) + "]";
        } else if (ft=="UnityEngine.Color"||ft=="Color") {
            Color c = {0,0,0,0};
            if (getter) { Method<Color> m(*getter); if(!isStatic) m.SetInstance(inst); c = m(); }
            else { Field<Color> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);c=f();} }
            ok = true; return "[" + std::to_string(c.r) + "," + std::to_string(c.g) + "," + std::to_string(c.b) + "," + std::to_string(c.a) + "]";
        } else if (ft=="UnityEngine.Vector2"||ft=="Vector2") {
            Vector2 v = {0,0};
            if (getter) { Method<Vector2> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<Vector2> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return "[" + std::to_string(v.x) + "," + std::to_string(v.y) + "]";
        } else if (ft=="UnityEngine.Vector4"||ft=="Vector4"||ft=="UnityEngine.Quaternion"||ft=="Quaternion") {
            Vector4 v = {0,0,0,0};
            if (getter) { Method<Vector4> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<Vector4> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::string("{\"x\":") + std::to_string(v.x) + ",\"y\":" + std::to_string(v.y) + ",\"z\":" + std::to_string(v.z) + ",\"w\":" + std::to_string(v.w) + "}";
        } else if (ft=="UnityEngine.LayerMask"||ft=="LayerMask") {
            int v = 0;
            if (getter) { Method<int> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<int> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="UnityEngine.Rect"||ft=="Rect") {
            Rect v = {0,0,0,0};
            if (getter) { Method<Rect> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<Rect> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true;
            return std::string("{\"x\":") + std::to_string(v.x) + ",\"y\":" + std::to_string(v.y) + ",\"w\":" + std::to_string(v.w) + ",\"h\":" + std::to_string(v.h) + "}";
        }
    } catch(...) {}
    return "";
}

static void writeField(const std::string& ft, Class& cls, Il2CppObject* inst, const std::string& name, MethodBase* setter, const std::string& v, bool isStatic = false) {
    if (!inst && !isStatic) return;
    if (ClassChainIsGameManager(cls.GetClass()) || (inst && ObjectIsGameManager(inst))) GameManagerTrap("writeField");
    try {
        if (ft=="System.Single"||ft=="Single"||ft=="float") {
            float val = std::stof(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<float> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.Double"||ft=="Double"||ft=="double") {
            double val = std::stod(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<double> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.Int32"||ft=="Int32"||ft=="int") {
            int val = std::stoi(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<int> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.UInt32"||ft=="UInt32"||ft=="uint") {
            uint32_t val = (uint32_t)std::stoul(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<uint32_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.Int64"||ft=="Int64"||ft=="long") {
            int64_t val = std::stoll(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<int64_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.UInt64"||ft=="UInt64"||ft=="ulong") {
            uint64_t val = std::stoull(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<uint64_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.Int16"||ft=="Int16"||ft=="short") {
            int16_t val = (int16_t)std::stoi(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<int16_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.UInt16"||ft=="UInt16"||ft=="ushort") {
            uint16_t val = (uint16_t)std::stoul(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<uint16_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.Byte"||ft=="Byte"||ft=="byte") {
            uint8_t val = (uint8_t)std::stoul(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<uint8_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.SByte"||ft=="SByte"||ft=="sbyte") {
            int8_t val = (int8_t)std::stoi(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<int8_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.Boolean"||ft=="Boolean"||ft=="bool") {
            bool val = v=="true"||v=="1";
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<bool> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.String"||ft=="String"||ft=="string") {
            auto* s = CreateMonoString(v.c_str());
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(s); }
            else { Field<BNM::Structures::Mono::String*> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=s;} }
        } else if (ft=="UnityEngine.Vector3"||ft=="Vector3") {
            float x=0,y=0,z=0; sscanf(v.c_str(),"[%f,%f,%f]",&x,&y,&z); Vector3 val={x,y,z};
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<Vector3> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="UnityEngine.Color"||ft=="Color") {
            float r=1,g=1,b=1,a=1; sscanf(v.c_str(),"[%f,%f,%f,%f]",&r,&g,&b,&a); Color val={r,g,b,a};
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<Color> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="UnityEngine.Vector2"||ft=="Vector2") {
            float x=0,y=0; sscanf(v.c_str(),"[%f,%f]",&x,&y); Vector2 val={x,y};
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<Vector2> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="UnityEngine.Vector4"||ft=="Vector4"||ft=="UnityEngine.Quaternion"||ft=="Quaternion") {
            float x=0,y=0,z=0,w=0;
            sscanf(v.c_str(),"{\"x\":%f,\"y\":%f,\"z\":%f,\"w\":%f}",&x,&y,&z,&w);
            Vector4 val={x,y,z,w};
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<Vector4> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="UnityEngine.LayerMask"||ft=="LayerMask") {
            int val = std::stoi(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<int> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="UnityEngine.Rect"||ft=="Rect") {
            float x=0,y=0,w=0,h=0;
            sscanf(v.c_str(),"{\"x\":%f,\"y\":%f,\"w\":%f,\"h\":%f}",&x,&y,&w,&h);
            Rect val={x,y,w,h};
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<Rect> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        }
    } catch(...) {}
}

static std::string goInfoJson(uintptr_t addr) {
    AttachThread();
    if (!addr) return "{}";
    Il2CppObject* go = (Il2CppObject*)addr;
    if (!isObjectAlive(go)) return "{\"stale\":true}";
    std::string name = "Unknown";
    bool active = false;

    Class goCls("UnityEngine","GameObject");
    Method<BNM::Structures::Mono::String*> gn(Class("UnityEngine","Object").GetMethod("get_name"));
    Method<bool> ga(goCls.GetMethod("get_activeSelf"));
    Method<Il2CppObject*> gt(goCls.GetMethod("get_transform"));

    try {
        if (gn.IsValid()) { auto* s=gn[go](); if(s) name=s->str(); }
        if (ga.IsValid()) active = ga[go]();
    } catch(...) {}

    std::ostringstream j;
    j << "{\"addr\":\"" << std::hex << addr << "\",\"name\":\"" << jsEsc(name) << "\",\"active\":" << (active?"true":"false") << ",\"transform\":{";
    try {
        if (gt.IsValid()) {
            Il2CppObject* tr = gt[go]();
            if (tr) {
                Method<Vector3> gp(Class("UnityEngine","Transform").GetMethod("get_localPosition"));
                Method<Vector3> gr(Class("UnityEngine","Transform").GetMethod("get_localEulerAngles"));
                Method<Vector3> gs(Class("UnityEngine","Transform").GetMethod("get_localScale"));
                Vector3 p=gp.IsValid()?gp[tr]():Vector3{0,0,0};
                Vector3 r=gr.IsValid()?gr[tr]():Vector3{0,0,0};
                Vector3 s=gs.IsValid()?gs[tr]():Vector3{0,0,0};
                j << "\"addr\":\"" << std::hex << (uintptr_t)tr << "\",\"p\":[" << p.x << "," << p.y << "," << p.z << "],\"r\":[" << r.x << "," << r.y << "," << r.z << "],\"s\":[" << s.x << "," << s.y << "," << s.z << "]";
            }
        }
    } catch(...) {}
    j << "},\"scripts\":[";

    try {
        Class componentCls("UnityEngine", "Component");
        Il2CppObject* compReflType = nullptr;
        if (componentCls.IsValid()) compReflType = (Il2CppObject*)componentCls.GetMonoType();
        if (!compReflType) compReflType = sysTypeOf("UnityEngine.Component, UnityEngine.CoreModule");
        if (!compReflType) compReflType = sysTypeOf("UnityEngine.Component");
        Array<Il2CppObject*>* comps = nullptr;
        if (!comps && compReflType) {
            for (auto& mt : goCls.GetMethods(true)) {
                auto* mi = mt.GetInfo();
                if (!mi || !mi->name || mi->is_generic) continue;
                if (strcmp(mi->name, "GetComponents") != 0) continue;
                if (mi->parameters_count == 1) {
                    try {
                        Method<Array<Il2CppObject*>*> m(mt);
                        comps = m[go](compReflType);
                    } catch(...) { LOGE("goInfoJson: GetComponents(Type) threw"); }
                    if (comps && comps->capacity > 0) break;
                    comps = nullptr;
                }
            }
        }
        if (!comps && compReflType) {
            for (auto& mt : goCls.GetMethods(true)) {
                auto* mi = mt.GetInfo();
                if (!mi || !mi->name) continue;
                if (strcmp(mi->name, "GetComponentsInternal") != 0) continue;
                LOGD("goInfoJson: found GetComponentsInternal with %d params", mi->parameters_count);
                if (mi->parameters_count >= 2) {
                    try {
                        if (mi->parameters_count == 6) {
                            Method<Array<Il2CppObject*>*> m(mt);
                            comps = m[go](compReflType, true, false, true, false, nullptr);
                            LOGD("goInfoJson: GetComponentsInternal(6) returned %p, count=%d", comps, comps ? (int)comps->capacity : -1);
                        } else if (mi->parameters_count == 5) {
                            Method<Array<Il2CppObject*>*> m(mt);
                            comps = m[go](compReflType, true, false, true, false);
                            LOGD("goInfoJson: GetComponentsInternal(5) returned %p, count=%d", comps, comps ? (int)comps->capacity : -1);
                        } else if (mi->parameters_count == 4) {
                            Method<Array<Il2CppObject*>*> m(mt);
                            comps = m[go](compReflType, false, true, false);
                            LOGD("goInfoJson: GetComponentsInternal(4) returned %p, count=%d", comps, comps ? (int)comps->capacity : -1);
                        }
                    } catch(...) { LOGE("goInfoJson: GetComponentsInternal threw"); }
                    if (comps && comps->capacity > 0) break;
                    comps = nullptr;
                }
            }
        }
        if (!comps) {
            LOGD("goInfoJson: falling back to FindObjectsOfType");
            Class monoCls("UnityEngine", "MonoBehaviour");
            Il2CppObject* monoType = nullptr;
            if (monoCls.IsValid()) monoType = (Il2CppObject*)monoCls.GetMonoType();
            if (!monoType) monoType = sysTypeOf("UnityEngine.MonoBehaviour, UnityEngine.CoreModule");
            if (!monoType) monoType = sysTypeOf("UnityEngine.MonoBehaviour");

            auto fom = findObjsMethod();
            Method<Il2CppObject*> compGetGO(Class("UnityEngine","Component").GetMethod("get_gameObject"));

            if (fom.IsValid() && monoType && compGetGO.IsValid()) {
                auto* allMonos = Method<Array<Il2CppObject*>*>(fom)(monoType);
                LOGD("goInfoJson: FindObjectsOfType(MonoBehaviour) count=%d", allMonos ? (int)allMonos->capacity : -1);
                if (allMonos && allMonos->capacity > 0) {
                    for (int i = 0; i < allMonos->capacity; i++) {
                        Il2CppObject* c = allMonos->m_Items[i];
                        if (!c) continue;
                        try {
                            Il2CppObject* cgo = compGetGO[c]();
                            if ((uintptr_t)cgo != addr) allMonos->m_Items[i] = nullptr;
                        } catch(...) { allMonos->m_Items[i] = nullptr; }
                    }
                    comps = allMonos;
                }
            }
        }

        LOGD("goInfoJson: final comps=%p, count=%d", comps, comps ? (int)comps->capacity : -1);

        static const char* kStopAtComponent[] = {
                "Component", "Object", nullptr
        };
        static const char* kSkipProps[] = {
                "isActiveAndEnabled", "transform", "gameObject", "tag", "name",
                "hideFlags", "rigidbody", "rigidbody2D", "camera", "light",
                "animation", "constantForce", "renderer", "audio", "networkView",
                "collider", "collider2D", "hingeJoint", "particleSystem", nullptr
        };

        auto isStopClass = [](const char* n) -> bool {
            for (auto** s = kStopAtComponent; *s; s++)
                if (strcmp(n, *s) == 0) return true;
            return false;
        };
        auto isSkipProp = [](const char* n) -> bool {
            for (auto** s = kSkipProps; *s; s++)
                if (strcmp(n, *s) == 0) return true;
            return false;
        };

        auto isKnownBuiltinBase = [](const char* n) -> bool {
            static const char* builtins[] = {
                    "MonoBehaviour", "Behaviour",
                    "Collider", "Collider2D",
                    "BoxCollider", "SphereCollider", "CapsuleCollider", "MeshCollider", "TerrainCollider", "WheelCollider",
                    "BoxCollider2D", "CircleCollider2D", "PolygonCollider2D", "EdgeCollider2D", "CapsuleCollider2D", "CompositeCollider2D",
                    "Rigidbody", "Rigidbody2D",
                    "Renderer", "MeshRenderer", "SkinnedMeshRenderer", "SpriteRenderer", "LineRenderer", "TrailRenderer", "ParticleSystemRenderer",
                    "MeshFilter",
                    "Animator", "Animation",
                    "AudioSource", "AudioListener",
                    "Camera", "Light", "LensFlare", "Projector",
                    "Canvas", "CanvasGroup", "CanvasRenderer",
                    "RectTransform",
                    "Text", "Image", "RawImage", "Button", "Toggle", "Slider", "Scrollbar", "Dropdown", "InputField", "ScrollRect", "Mask", "RectMask2D",
                    "TMP_Text", "TextMeshPro", "TextMeshProUGUI",
                    "NavMeshAgent", "NavMeshObstacle",
                    "ParticleSystem",
                    "Joint", "HingeJoint", "FixedJoint", "SpringJoint", "CharacterJoint", "ConfigurableJoint",
                    "Joint2D", "HingeJoint2D", "FixedJoint2D", "SpringJoint2D", "WheelJoint2D", "SliderJoint2D",
                    "CharacterController",
                    "LODGroup",
                    "OcclusionArea", "OcclusionPortal",
                    "NetworkView",
                    "ConstantForce",
                    nullptr
            };
            for (auto** s = builtins; *s; s++)
                if (strcmp(n, *s) == 0) return true;
            return false;
        };

        if (comps && comps->capacity > 0) {
            bool firstScript = true;

            for (int i = 0; i < comps->capacity; i++) {
                Il2CppObject* comp = comps->m_Items[i];
                if (!comp || !comp->klass) continue;

                auto* klass = comp->klass;

                bool isSupported = false;
                std::string compCategory = "script";
                for (auto* cur = klass; cur; cur = cur->parent) {
                    if (!cur->name) continue;
                    if (strcmp(cur->name, "MonoBehaviour") == 0) { isSupported = true; compCategory = "script"; break; }
                    if (isKnownBuiltinBase(cur->name) && strcmp(cur->name, "MonoBehaviour") != 0 && strcmp(cur->name, "Behaviour") != 0) {
                        isSupported = true; compCategory = "builtin"; break;
                    }
                    if (strcmp(cur->name, "Component") == 0 || strcmp(cur->name, "Object") == 0) break;
                }
                if (!isSupported) continue;

                std::string compName = klass->name ? klass->name : "Unknown";
                std::string compNs = klass->namespaze ? klass->namespaze : "";
                char compAddr[32]; snprintf(compAddr, sizeof(compAddr), "%llX", (unsigned long long)(uintptr_t)comp);
                std::string compAsm = (klass->image && klass->image->name) ? klass->image->name : "";

                bool hasEnabledProp = false;
                bool enabled = false;
                for (auto* cur = klass; cur; cur = cur->parent) {
                    if (!cur->name) continue;
                    if (strcmp(cur->name, "Behaviour") == 0 || strcmp(cur->name, "Renderer") == 0 ||
                        strcmp(cur->name, "Collider") == 0 || strcmp(cur->name, "Collider2D") == 0) {
                        hasEnabledProp = true; break;
                    }
                    if (strcmp(cur->name, "Component") == 0) break;
                }
                if (hasEnabledProp) {
                    Method<bool> getEnabled(Class("UnityEngine","Behaviour").GetMethod("get_enabled"));
                    if (!getEnabled.IsValid()) {
                        Class compClsCheck(klass);
                        auto ep = compClsCheck.GetProperty("enabled");
                        if (ep.IsValid() && ep._data && ep._data->get) {
                            MethodBase egetter(ep._data->get);
                            Method<bool> m(egetter); m.SetInstance(comp);
                            try { enabled = m(); } catch(...) {}
                        }
                    } else {
                        try { enabled = getEnabled[comp](); } catch(...) {}
                    }
                }

                if (!firstScript) j << ",";
                firstScript = false;
                j << "{\"addr\":\"" << compAddr << "\",\"name\":\"" << jsEsc(compName) << "\",\"ns\":\"" << jsEsc(compNs) << "\",\"asm\":\"" << jsEsc(compAsm) << "\",\"category\":\"" << compCategory << "\",\"enabled\":" << (enabled?"true":"false") << ",\"fields\":[";

                LOGD("goInfoJson: found component '%s' category='%s' at %s", compName.c_str(), compCategory.c_str(), compAddr);

                try {
                    Class compCls(klass);
                    bool firstF = true;
                    std::vector<std::string> seen;

                    for (Class cur = compCls; cur.IsValid() && cur.GetClass(); cur = cur.GetParent()) {
                        auto* cn_ = cur.GetClass()->name;
                        if (!cn_) break;
                        if (isStopClass(cn_)) break;

                        for (auto& f : cur.GetFields(false)) {
                            try {
                                auto* fi = f.GetInfo();
                                if (!fi || !fi->type || (fi->type->attrs & 0x10)) continue;
                                std::string fn = fi->name;
                                bool dup = false; for (auto& s : seen) if (s==fn) dup=true;
                                if (dup) continue; seen.push_back(fn);
                                std::string ft = typeName(fi->type);
                                bool ok = false;
                                std::string vs = readField(ft, cur, comp, fn, nullptr, false, ok);
                                if (ok) {
                                    if (!firstF) j << ","; firstF = false;
                                    j << "{\"name\":\"" << jsEsc(fn) << "\",\"type\":\"" << jsEsc(ft) << "\",\"val\":" << vs << ",\"isProp\":false,\"canWrite\":true}";
                                }
                            } catch(...) {}
                        }

                        for (auto& p : cur.GetProperties(false)) {
                            try {
                                auto* pi = p._data;
                                if (!pi || !pi->get) continue;
                                std::string pn = pi->name;
                                if (isSkipProp(pn.c_str())) continue;
                                bool dup = false; for (auto& s : seen) if (s==pn) dup=true;
                                if (dup) continue; seen.push_back(pn);
                                std::string pt = typeName(pi->get->return_type);
                                bool ok = false;
                                MethodBase getter(pi->get);
                                std::string vs = readField(pt, cur, comp, pn, &getter, false, ok);
                                if (ok) {
                                    if (!firstF) j << ","; firstF = false;
                                    j << "{\"name\":\"" << jsEsc(pn) << "\",\"type\":\"" << jsEsc(pt) << "\",\"val\":" << vs << ",\"isProp\":true,\"canWrite\":" << (pi->set?"true":"false") << "}";
                                }
                            } catch(...) {}
                        }
                    }
                } catch(...) {}
                j << "]}";
            }
        }
    } catch(...) {
        LOGE("goInfoJson: exception in script enumeration");
    }
    j << "]}";
    return j.str();
}

static void applyLoopEntry(const std::shared_ptr<LoopEntry>& e) {
    try {
        uintptr_t addr = (uintptr_t)strtoull(e->addr.c_str(), nullptr, 16);
        if (!addr) return;
        Il2CppObject* obj = resolveComponentByAddr(addr);
        if (!obj) { e->active = false; return; }
        if (ObjectIsGameManager(obj)) GameManagerTrap("loop");
        for (Class cur(e->ns.c_str(), e->cls.c_str(), Image(e->asm_.c_str())); cur.IsValid(); cur = cur.GetParent()) {
            if (e->isProp) {
                auto p = cur.GetProperty(e->name.c_str());
                if (p.IsValid() && p._data && p._data->set) {
                    MethodBase s(p._data->set);
                    writeField(e->ftype, cur, obj, e->name, &s, e->val);
                    return;
                }
            } else {
                auto f = cur.GetField(e->name.c_str());
                if (f.IsValid()) { writeField(e->ftype, cur, obj, e->name, nullptr, e->val); return; }
            }
        }
    } catch(...) {}
}

static std::string loopListJson() {
    std::lock_guard<std::mutex> lk(g_loopMtx);
    std::ostringstream j;
    j << "[";
    bool first = true;
    for (auto& kv : g_loops) {
        if (!first) j << ","; first = false;
        auto& e = kv.second;
        j << "{\"id\":\"" << jsEsc(kv.first) << "\",\"cls\":\"" << jsEsc(e->cls) << "\",\"name\":\"" << jsEsc(e->name) << "\",\"val\":\"" << jsEsc(e->val) << "\",\"interval\":" << e->intervalMs << ",\"active\":" << (e->active?"true":"false") << "}";
    }
    j << "]";
    return j.str();
}

#define MAX_CATCHERS 64
static std::shared_ptr<CatcherEntry>* g_catcherSlots[MAX_CATCHERS] = {};
static int g_catcherSlotCount = 0;
static std::mutex g_catcherSlotMtx;
static void (*g_catcherOrigPtrs[MAX_CATCHERS])(Il2CppObject*) = {};

template<int N>
static void catcherHook(Il2CppObject* self) {
    std::shared_ptr<CatcherEntry>* slot = g_catcherSlots[N];
    if (slot && *slot && (*slot)->active) {
        CatcherCall call;
        call.ts = nowSeconds();
        char buf[32]; snprintf(buf, sizeof(buf), "0x%llX", (unsigned long long)(uintptr_t)self);
        call.instance = buf;
        if (self && self->klass) {
            try {
                Method<BNM::Structures::Mono::String*> gn(Class("UnityEngine","Object").GetMethod("get_name"));
                if (gn.IsValid()) { auto* s = gn[self](); if (s) call.instance += " (" + s->str() + ")"; }
            } catch(...) {}
        }
        std::lock_guard<std::mutex> lk((*slot)->callsMtx);
        if ((*slot)->calls.size() >= CatcherEntry::MAX_CALLS) (*slot)->calls.erase((*slot)->calls.begin());
        (*slot)->calls.push_back(std::move(call));
    }
    if (g_catcherOrigPtrs[N]) g_catcherOrigPtrs[N](self);
}

typedef void(*CatcherHookFn)(Il2CppObject*);
static CatcherHookFn g_catcherHookFns[MAX_CATCHERS] = {
        catcherHook<0>,catcherHook<1>,catcherHook<2>,catcherHook<3>,catcherHook<4>,
        catcherHook<5>,catcherHook<6>,catcherHook<7>,catcherHook<8>,catcherHook<9>,
        catcherHook<10>,catcherHook<11>,catcherHook<12>,catcherHook<13>,catcherHook<14>,
        catcherHook<15>,catcherHook<16>,catcherHook<17>,catcherHook<18>,catcherHook<19>,
        catcherHook<20>,catcherHook<21>,catcherHook<22>,catcherHook<23>,catcherHook<24>,
        catcherHook<25>,catcherHook<26>,catcherHook<27>,catcherHook<28>,catcherHook<29>,
        catcherHook<30>,catcherHook<31>,catcherHook<32>,catcherHook<33>,catcherHook<34>,
        catcherHook<35>,catcherHook<36>,catcherHook<37>,catcherHook<38>,catcherHook<39>,
        catcherHook<40>,catcherHook<41>,catcherHook<42>,catcherHook<43>,catcherHook<44>,
        catcherHook<45>,catcherHook<46>,catcherHook<47>,catcherHook<48>,catcherHook<49>,
        catcherHook<50>,catcherHook<51>,catcherHook<52>,catcherHook<53>,catcherHook<54>,
        catcherHook<55>,catcherHook<56>,catcherHook<57>,catcherHook<58>,catcherHook<59>,
        catcherHook<60>,catcherHook<61>,catcherHook<62>,catcherHook<63>
};

static void startServer() {
    httplib::Server svr;

    // Any non-GET (write) request that names GameManager, or whose "addr" resolves to a
    // GameManager object, crashes the game before it is routed (anti-grief; reads are GETs).
    svr.set_pre_routing_handler([](const httplib::Request& req, httplib::Response& res) {
        if (req.method != "GET") {
            std::string low; low.reserve(req.body.size());
            for (char c : req.body) low.push_back((char)tolower((unsigned char)c));
            uintptr_t a = (uintptr_t)strtoull(jsonVal(req.body, "addr").c_str(), nullptr, 16);
            if (low.find("gamemanager") != std::string::npos || (a && ObjectIsGameManager(resolveComponentByAddr(a))))
                GameManagerTrap("http");
        }
        return httplib::Server::HandlerResponse::Unhandled;
    });

    svr.Get("/",[](const httplib::Request&, httplib::Response& res) {
        int n = 0;
        for (auto& img : Image::GetImages()) if (img.IsValid()) n++;
        res.set_content(GetExplorerHTML(n), "text/html");
    });

    svr.Get("/api/assemblies",[](const httplib::Request&, httplib::Response& res) {
        AttachThread();
        res.set_content(assembliesJson(), "application/json");
    });

    svr.Get("/api/classes",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        res.set_content(classesJson(req.get_param_value("a")), "application/json");
    });

    svr.Get("/api/allclasses",[](const httplib::Request&, httplib::Response& res) {
        AttachThread();
        res.set_content(allClassesJson(), "application/json");
    });

    svr.Get("/api/class",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        res.set_content(classDetailJson(req.get_param_value("a"), req.get_param_value("ns"), req.get_param_value("n")), "application/json");
    });

    svr.Get("/api/instances",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        res.set_content(instancesJson(req.get_param_value("a"), req.get_param_value("ns"), req.get_param_value("n")), "application/json");
    });

    svr.Get("/api/scene",[](const httplib::Request&, httplib::Response& res) {
        AttachThread();
        res.set_content(sceneJson(), "application/json");
    });

    svr.Get("/api/scene/inspect",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        auto s = req.get_param_value("addr");
        res.set_content(s.empty() ? "{}" : goInfoJson((uintptr_t)strtoull(s.c_str(), nullptr, 16)), "application/json");
    });

    svr.Get("/api/controller/inspect",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        uintptr_t addr = (uintptr_t)strtoull(req.get_param_value("addr").c_str(), nullptr, 16);
        std::string a = req.get_param_value("asm"), ns = req.get_param_value("ns"), cn = req.get_param_value("cls");

        Class startCls(ns.c_str(), cn.c_str(), Image(a.c_str()));
        if (!startCls.IsValid()) { res.set_content("{}", "application/json"); return; }

        std::ostringstream j;
        j << "{\"addr\":\"" << std::hex << addr << "\",\"name\":\"" << jsEsc(cn) << "\",\"fields\":[";

        bool firstF = true;
        std::vector<std::string> seen;

        for (Class cur = startCls; cur.IsValid() && cur.GetClass(); cur = cur.GetParent()) {
            auto* cn_ = cur.GetClass()->name;
            if (!cn_) break;
            if (strcmp(cn_, "Object") == 0 && cur.GetClass()->namespaze && strcmp(cur.GetClass()->namespaze, "System") == 0) break;

            for (auto& f : cur.GetFields(false)) {
                try {
                    auto* fi = f.GetInfo();
                    if (!fi || !fi->type) continue;
                    bool fStatic = (fi->type->attrs & 0x10) != 0;
                    if (!fStatic && !addr) continue;
                    std::string fn = fi->name;
                    bool dup = false; for (auto& s : seen) if (s==fn) dup=true;
                    if (dup) continue; seen.push_back(fn);
                    std::string ft = typeName(fi->type);
                    bool ok = false;
                    std::string vs = readField(ft, cur, fStatic ? nullptr : (Il2CppObject*)addr, fn, nullptr, fStatic, ok);
                    if (ok) { if (!firstF) j << ","; firstF = false; j << "{\"name\":\"" << jsEsc(fn) << "\",\"type\":\"" << ft << "\",\"val\":" << vs << ",\"isProp\":false,\"canWrite\":true,\"static\":" << (fStatic?"true":"false") << "}"; }
                } catch(...) {}
            }

            for (auto& p : cur.GetProperties(false)) {
                try {
                    auto* pi = p._data;
                    if (!pi || !pi->get) continue;
                    bool pStatic = (pi->get->flags & 0x10) != 0;
                    if (!pStatic && !addr) continue;
                    std::string pn = pi->name;
                    bool dup = false; for (auto& s : seen) if (s==pn) dup=true;
                    if (dup) continue; seen.push_back(pn);
                    std::string pt = typeName(pi->get->return_type);
                    bool ok = false;
                    MethodBase getter(pi->get);
                    std::string vs = readField(pt, cur, pStatic ? nullptr : (Il2CppObject*)addr, pn, &getter, pStatic, ok);
                    if (ok) { if (!firstF) j << ","; firstF = false; j << "{\"name\":\"" << jsEsc(pn) << "\",\"type\":\"" << pt << "\",\"val\":" << vs << ",\"isProp\":true,\"canWrite\":" << (pi->set?"true":"false") << ",\"static\":" << (pStatic?"true":"false") << "}"; }
                } catch(...) {}
            }
        }

        j << "],\"methods\":[";
        bool firstM = true;
        for (Class cur = startCls; cur.IsValid() && cur.GetClass(); cur = cur.GetParent()) {
            auto* cn_ = cur.GetClass()->name;
            if (!cn_) break;
            if (strcmp(cn_, "Object") == 0 && cur.GetClass()->namespaze && strcmp(cur.GetClass()->namespaze, "System") == 0) break;
            for (auto& m : cur.GetMethods(false)) {
                auto* mi = m.GetInfo();
                if (!m.IsValid() || !mi || !mi->name) continue;
                if (!firstM) j << ","; firstM = false;
                j << "{\"name\":\"" << jsEsc(mi->name) << "\",\"ret\":\"" << jsEsc(typeName(mi->return_type)) << "\",\"s\":" << ((mi->flags&0x10)?"true":"false") << ",\"params\":[";
                for (int i = 0; i < (int)mi->parameters_count; i++) {
                    if (i>0) j<<",";
                    j << "{\"n\":\"arg" << i << "\",\"t\":\"" << jsEsc(mi->parameters&&mi->parameters[i]?typeName(mi->parameters[i]):"?") << "\"}";
                }
                j << "]}";
            }
        }
        j << "]}";
        res.set_content(j.str(), "application/json");
    });

    svr.Post("/api/scene/update",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        uintptr_t addr = (uintptr_t)strtoull(jsonVal(req.body, "addr").c_str(), nullptr, 16);
        if (!addr) { res.set_content("{\"ok\":false}", "application/json"); return; }
        std::string type = jsonVal(req.body, "type"), prop = jsonVal(req.body, "prop"), val = jsonVal(req.body, "val");
        try {
            if (type == "gameobject") {
                if (prop == "active") {
                    bool newActive = val == "true";
                    Il2CppObject* go = resolveComponentByAddr(addr);
                    if (go) {
                        Method<void> setActive(Class("UnityEngine","GameObject").GetMethod("SetActive", 1));
                        if (setActive.IsValid()) setActive[go](newActive);
                    }
                } else if (prop == "name") {
                    Il2CppObject* go = resolveComponentByAddr(addr);
                    if (go) {
                        Method<void> m(Class("UnityEngine","Object").GetMethod("set_name",1));
                        if(m.IsValid()) m[go](CreateMonoString(val));
                    }
                }
            } else if (type == "transform") {
                float x=0,y=0,z=0; sscanf(val.c_str(),"[%f,%f,%f]",&x,&y,&z); Vector3 v={x,y,z};
                Il2CppObject* tr = resolveComponentByAddr(addr);
                if (tr) {
                    if (prop=="p") { Method<void> m(Class("UnityEngine","Transform").GetMethod("set_localPosition",1)); if(m.IsValid()) m[tr](v); }
                    else if (prop=="r") { Method<void> m(Class("UnityEngine","Transform").GetMethod("set_localEulerAngles",1)); if(m.IsValid()) m[tr](v); }
                    else if (prop=="s") { Method<void> m(Class("UnityEngine","Transform").GetMethod("set_localScale",1)); if(m.IsValid()) m[tr](v); }
                }
            } else if (type == "script") {
                std::string name = jsonVal(req.body, "prop2");
                std::string ft   = jsonVal(req.body, "ftype");
                bool isProp      = jsonVal(req.body, "isProp") == "true";
                if (prop == "enabled") {
                    try {
                        bool newEnabled = val == "true";
                        Il2CppObject* comp = resolveComponentByAddr(addr);
                        if (comp && comp->klass) {
                            auto setEnabledOnComp = [&](Il2CppObject* c, bool v) {
                                Class compKlass(c->klass);
                                for (Class cur = compKlass; cur.IsValid() && cur.GetClass(); cur = cur.GetParent()) {
                                    auto* cn_ = cur.GetClass()->name;
                                    if (!cn_ || strcmp(cn_, "Component") == 0 || strcmp(cn_, "Object") == 0) break;
                                    auto ep = cur.GetProperty("enabled");
                                    if (ep.IsValid() && ep._data && ep._data->set) {
                                        Method<void> m(ep._data->set); m.SetInstance(c); m(v); return true;
                                    }
                                }
                                return false;
                            };
                            auto getEnabledFromComp = [&](Il2CppObject* c) -> bool {
                                Class compKlass(c->klass);
                                for (Class cur = compKlass; cur.IsValid() && cur.GetClass(); cur = cur.GetParent()) {
                                    auto* cn_ = cur.GetClass()->name;
                                    if (!cn_ || strcmp(cn_, "Component") == 0 || strcmp(cn_, "Object") == 0) break;
                                    auto ep = cur.GetProperty("enabled");
                                    if (ep.IsValid() && ep._data && ep._data->get) {
                                        Method<bool> m(ep._data->get); m.SetInstance(c);
                                        try { return m(); } catch(...) {}
                                    }
                                }
                                return false;
                            };
                            if (newEnabled) {
                                bool already = getEnabledFromComp(comp);
                                if (!already) {
                                    setEnabledOnComp(comp, true);
                                    std::this_thread::sleep_for(std::chrono::milliseconds(50));
                                    comp = resolveComponentByAddr(addr);
                                    if (!comp) { res.set_content("{\"ok\":true,\"warn\":\"enabled but object moved\"}", "application/json"); return; }
                                }
                            } else {
                                setEnabledOnComp(comp, false);
                            }
                        }
                    } catch(...) {}
                } else {
                    std::string compNs  = jsonVal(req.body, "compNs");
                    std::string compCls = jsonVal(req.body, "compCls");
                    std::string compAsm = jsonVal(req.body, "compAsm");
                    Il2CppObject* obj = resolveComponentByAddr(addr);
                    if (!obj) { res.set_content("{\"ok\":false,\"error\":\"stale address\"}", "application/json"); return; }
                    Class startCls = (obj->klass) ? Class(obj->klass) :
                                     Class(compNs.c_str(), compCls.c_str(), compAsm.empty() ? Image() : Image(compAsm.c_str()));
                    for (Class cur = startCls; cur.IsValid(); cur = cur.GetParent()) {
                        if (isProp) {
                            auto p = cur.GetProperty(name.c_str());
                            if (p.IsValid() && p._data && p._data->set) { MethodBase s(p._data->set); writeField(ft, cur, obj, name, &s, val); break; }
                        } else {
                            auto f = cur.GetField(name.c_str());
                            if (f.IsValid()) { writeField(ft, cur, obj, name, nullptr, val); break; }
                        }
                    }
                }
            }
        } catch(...) {}
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Post("/api/instance/update",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        uintptr_t addr = (uintptr_t)strtoull(jsonVal(req.body, "addr").c_str(), nullptr, 16);
        std::string a=jsonVal(req.body,"asm"), ns=jsonVal(req.body,"ns"), cn=jsonVal(req.body,"cls");
        std::string name=jsonVal(req.body,"name"), ft=jsonVal(req.body,"ftype"), val=jsonVal(req.body,"val");
        bool isProp = jsonVal(req.body,"isProp") == "true";
        bool isStaticField = jsonVal(req.body,"isStatic") == "true";
        Il2CppObject* obj = isStaticField ? nullptr : resolveComponentByAddr(addr);
        if (obj || isStaticField) {
            try {
                for (Class cur(ns.c_str(),cn.c_str(),Image(a.c_str())); cur.IsValid(); cur = cur.GetParent()) {
                    if (isProp) {
                        auto p = cur.GetProperty(name.c_str());
                        if (p.IsValid() && p._data && p._data->set) { MethodBase s(p._data->set); writeField(ft,cur,obj,name,&s,val,isStaticField); break; }
                    } else {
                        auto f = cur.GetField(name.c_str());
                        if (f.IsValid()) { writeField(ft,cur,obj,name,nullptr,val,isStaticField); break; }
                    }
                }
            } catch(...) {}
        }
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Post("/api/scene/delete",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        auto s = jsonVal(req.body, "addr");
        if (!s.empty()) {
            uintptr_t addr = (uintptr_t)strtoull(s.c_str(), nullptr, 16);
            Il2CppObject* obj = resolveComponentByAddr(addr);
            if (obj) {
                try { Method<void> m(Class("UnityEngine","Object").GetMethod("Destroy",1)); if(m.IsValid()) m(obj); } catch(...) {}
            }
        }
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Post("/api/invoke",[](const httplib::Request& req, httplib::Response& res) {
        auto r = invokeMethod(req.body);
        std::ostringstream j;
        j << "{\"ok\":" << (r.ok?"true":"false") << ",\"value\":\"" << jsEsc(r.val) << "\",\"error\":\"" << jsEsc(r.err) << "\"}";
        res.set_content(j.str(), "application/json");
    });

    svr.Post("/api/loop/add",[](const httplib::Request& req, httplib::Response& res) {
        auto e = std::make_shared<LoopEntry>();
        e->addr      = jsonVal(req.body, "addr");
        e->asm_      = jsonVal(req.body, "asm");
        e->ns        = jsonVal(req.body, "ns");
        e->cls       = jsonVal(req.body, "cls");
        e->name      = jsonVal(req.body, "name");
        e->ftype     = jsonVal(req.body, "ftype");
        e->isProp    = jsonVal(req.body, "isProp") == "true";
        e->val       = jsonVal(req.body, "val");
        int ms       = 0;
        try { ms = std::stoi(jsonVal(req.body, "interval")); } catch(...) {}
        if (ms < 50) ms = 100;
        e->intervalMs = ms;

        std::string id = e->addr + "_" + e->cls + "_" + e->name;
        {
            std::lock_guard<std::mutex> lk(g_loopMtx);
            auto it = g_loops.find(id);
            if (it != g_loops.end()) it->second->active = false;
            g_loops[id] = e;
        }

        std::thread([e, id]() {
            AttachThread();
            while (e->active) {
                applyLoopEntry(e);
                std::this_thread::sleep_for(std::chrono::milliseconds(e->intervalMs));
            }
            std::lock_guard<std::mutex> lk(g_loopMtx);
            auto it = g_loops.find(id);
            if (it != g_loops.end() && it->second == e) g_loops.erase(it);
        }).detach();

        std::ostringstream j;
        j << "{\"ok\":true,\"id\":\"" << jsEsc(id) << "\"}";
        res.set_content(j.str(), "application/json");
    });

    svr.Post("/api/loop/remove",[](const httplib::Request& req, httplib::Response& res) {
        std::string id = jsonVal(req.body, "id");
        std::lock_guard<std::mutex> lk(g_loopMtx);
        auto it = g_loops.find(id);
        if (it != g_loops.end()) { it->second->active = false; g_loops.erase(it); }
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Post("/api/loop/removeall",[](const httplib::Request&, httplib::Response& res) {
        std::lock_guard<std::mutex> lk(g_loopMtx);
        for (auto& kv : g_loops) kv.second->active = false;
        g_loops.clear();
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Get("/api/loop/list",[](const httplib::Request&, httplib::Response& res) {
        res.set_content(loopListJson(), "application/json");
    });

    svr.Get("/api/logs",[](const httplib::Request&, httplib::Response& res) {
        res.set_content(logsJson(), "application/json");
    });

    svr.Post("/api/logs/clear",[](const httplib::Request&, httplib::Response& res) {
        std::lock_guard<std::mutex> lk(g_logMtx);
        g_logBuffer.clear();
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Post("/api/logs/add",[](const httplib::Request& req, httplib::Response& res) {
        std::string msg = jsonVal(req.body, "msg");
        int level = 0;
        try { level = std::stoi(jsonVal(req.body, "level")); } catch(...) {}
        addLogEntry(msg, level);
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Get("/api/timescale",[](const httplib::Request&, httplib::Response& res) {
        AttachThread();
        float ts = 1.0f;
        try {
            Class timeCls("UnityEngine", "Time");
            if (timeCls.IsValid()) {
                Method<float> get(timeCls.GetMethod("get_timeScale"));
                if (get.IsValid()) ts = get();
            }
        } catch(...) {}
        res.set_content("{\"value\":" + std::to_string(ts) + "}", "application/json");
    });

    svr.Post("/api/timescale",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        float val = 1.0f;
        try { val = std::stof(jsonVal(req.body, "value")); } catch(...) {}
        try {
            Class timeCls("UnityEngine", "Time");
            if (timeCls.IsValid()) {
                Method<void> set(timeCls.GetMethod("set_timeScale", 1));
                if (set.IsValid()) set(val);
            }
        } catch(...) {}
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Post("/api/scene/destroy",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        auto s = jsonVal(req.body, "addr");
        if (!s.empty()) {
            uintptr_t addr = (uintptr_t)strtoull(s.c_str(), nullptr, 16);
            Il2CppObject* obj = resolveComponentByAddr(addr);
            if (obj) {
                try {
                    Method<void> m(Class("UnityEngine","Object").GetMethod("Destroy",1));
                    if (m.IsValid()) m(obj);
                } catch(...) {}
            }
        }
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Post("/api/scene/create",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        std::string name = jsonVal(req.body, "name");
        if (name.empty()) name = "New GameObject";
        std::ostringstream j;
        try {
            Class goCls("UnityEngine", "GameObject");
            Method<Il2CppObject*> ctor;
            for (auto& m : goCls.GetMethods(false)) {
                auto* mi = m.GetInfo();
                if (mi && mi->name && strcmp(mi->name, ".ctor") == 0 && mi->parameters_count == 1) {
                    ctor = Method<Il2CppObject*>(m);
                    break;
                }
            }
            if (ctor.IsValid()) {
                Il2CppObject* newGO = goCls.CreateNewObjectParameters(CreateMonoString(name.c_str()));
                if (newGO) {
                    j << "{\"ok\":true,\"addr\":\"" << std::hex << (uintptr_t)newGO << "\"}";
                    res.set_content(j.str(), "application/json");
                    return;
                }
            }
        } catch(...) {}
        res.set_content("{\"ok\":false,\"error\":\"Failed to create GameObject\"}", "application/json");
    });

    svr.Post("/api/scene/addcomponent",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        uintptr_t addr = (uintptr_t)strtoull(jsonVal(req.body, "addr").c_str(), nullptr, 16);
        std::string typeName_ = jsonVal(req.body, "type");
        if (!addr || typeName_.empty()) { res.set_content("{\"ok\":false}", "application/json"); return; }
        Il2CppObject* go = resolveComponentByAddr(addr);
        if (!go) { res.set_content("{\"ok\":false,\"error\":\"stale address\"}", "application/json"); return; }
        try {
            Il2CppObject* typeObj = sysTypeOf(typeName_);
            if (!typeObj) typeObj = sysTypeOf(typeName_ + ", UnityEngine.CoreModule");
            if (!typeObj) typeObj = sysTypeOf(typeName_ + ", UnityEngine");
            if (!typeObj) typeObj = sysTypeOf(typeName_ + ", Assembly-CSharp");
            if (typeObj) {
                Class goCls("UnityEngine", "GameObject");
                Method<Il2CppObject*> addComp(goCls.GetMethod("AddComponent", 1));
                if (addComp.IsValid()) {
                    Il2CppObject* comp = addComp[go](typeObj);
                    if (comp) {
                        std::ostringstream j;
                        j << "{\"ok\":true,\"addr\":\"" << std::hex << (uintptr_t)comp << "\"}";
                        res.set_content(j.str(), "application/json");
                        return;
                    }
                }
            }
        } catch(...) {}
        res.set_content("{\"ok\":false,\"error\":\"Failed to add component\"}", "application/json");
    });

    svr.Post("/api/scene/removecomponent",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        uintptr_t addr = (uintptr_t)strtoull(jsonVal(req.body, "addr").c_str(), nullptr, 16);
        if (!addr) { res.set_content("{\"ok\":false}", "application/json"); return; }
        Il2CppObject* obj = resolveComponentByAddr(addr);
        if (!obj) { res.set_content("{\"ok\":false,\"error\":\"stale address\"}", "application/json"); return; }
        try {
            Method<void> destroy(Class("UnityEngine","Object").GetMethod("Destroy",1));
            if (destroy.IsValid()) destroy(obj);
        } catch(...) {}
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Get("/api/dump",[](const httplib::Request&, httplib::Response& res) {
        AttachThread();
        std::ostringstream out;
        for (auto& img : Image::GetImages()) {
            auto* d = img.GetInfo();
            if (!img.IsValid() || !d || !d->name) continue;
            out << "// Assembly: " << d->name << "\n\n";
            for (auto& cls : img.GetClasses(false)) {
                auto* r = cls.GetClass();
                if (!cls.IsValid() || !r || !r->name) continue;
                std::string clsKw = "class";
                if (r->enumtype) clsKw = "enum";
                else if (r->byval_arg.valuetype) clsKw = "struct";
                else if (r->flags & 0x20) clsKw = "interface";
                if (r->namespaze && strlen(r->namespaze) > 0) {
                    out << "namespace " << r->namespaze << " {\n";
                }
                std::string indent = (r->namespaze && strlen(r->namespaze) > 0) ? "    " : "";
                std::string parent = "";
                if (r->parent && r->parent->name && strcmp(r->parent->name, "Object") != 0 && strcmp(r->parent->name, "ValueType") != 0)
                    parent = " : " + std::string(r->parent->namespaze && strlen(r->parent->namespaze) > 0 ? std::string(r->parent->namespaze) + "." : "") + r->parent->name;
                out << indent << "public " << clsKw << " " << r->name << parent << " {\n";
                for (auto& f : cls.GetFields(false)) {
                    auto* fi = f.GetInfo();
                    if (!fi || !fi->name) continue;
                    std::string tn = typeName(fi->type);
                    bool isStatic = fi->type && (fi->type->attrs & 0x10);
                    out << indent << "    // offset 0x" << std::hex << fi->offset << std::dec << "\n";
                    out << indent << "    public " << (isStatic ? "static " : "") << tn << " " << fi->name << ";\n";
                }
                for (auto& p : cls.GetProperties(false)) {
                    auto* pi = p._data;
                    if (!pi || !pi->name) continue;
                    std::string tn = pi->get && pi->get->return_type ? typeName(pi->get->return_type) : "?";
                    out << indent << "    public " << tn << " " << pi->name << " {";
                    if (pi->get) out << " get;";
                    if (pi->set) out << " set;";
                    out << " }\n";
                }
                for (auto& m : cls.GetMethods(false)) {
                    auto* mi = m.GetInfo();
                    if (!mi || !mi->name) continue;
                    char ab[32] = {};
                    if (mi->methodPointer) snprintf(ab, sizeof(ab), "0x%llX", (unsigned long long)(uintptr_t)mi->methodPointer);
                    bool isStatic = mi->flags & 0x10;
                    out << indent << "    // " << ab << "\n";
                    out << indent << "    public " << (isStatic ? "static " : "") << typeName(mi->return_type) << " " << mi->name << "(";
                    for (int i = 0; i < (int)mi->parameters_count; i++) {
                        if (i > 0) out << ", ";
                        out << (mi->parameters && mi->parameters[i] ? typeName(mi->parameters[i]) : "?") << " arg" << i;
                    }
                    out << ") {}\n";
                }
                out << indent << "}\n";
                if (r->namespaze && strlen(r->namespaze) > 0) out << "}\n";
                out << "\n";
            }
        }
        res.set_content(out.str(), "text/plain");
    });

    svr.Get("/api/scene/addable",[](const httplib::Request&, httplib::Response& res) {
        AttachThread();
        std::ostringstream j;
        j << "{";
        bool firstCat = true;
        static const struct { const char* cat; const char* ns; const char* name; } known[] = {
                {"Physics","UnityEngine","Rigidbody"},{"Physics","UnityEngine","BoxCollider"},{"Physics","UnityEngine","SphereCollider"},
                {"Physics","UnityEngine","CapsuleCollider"},{"Physics","UnityEngine","MeshCollider"},{"Physics","UnityEngine","CharacterController"},
                {"Physics","UnityEngine","WheelCollider"},{"Physics","UnityEngine","Rigidbody2D"},{"Physics","UnityEngine","BoxCollider2D"},
                {"Physics","UnityEngine","CircleCollider2D"},{"Physics","UnityEngine","PolygonCollider2D"},{"Physics","UnityEngine","CapsuleCollider2D"},
                {"Rendering","UnityEngine","MeshRenderer"},{"Rendering","UnityEngine","MeshFilter"},{"Rendering","UnityEngine","SkinnedMeshRenderer"},
                {"Rendering","UnityEngine","SpriteRenderer"},{"Rendering","UnityEngine","LineRenderer"},{"Rendering","UnityEngine","TrailRenderer"},
                {"Rendering","UnityEngine","Camera"},{"Rendering","UnityEngine","Light"},
                {"Audio","UnityEngine","AudioSource"},{"Audio","UnityEngine","AudioListener"},{"Audio","UnityEngine","AudioReverbZone"},
                {"Animation","UnityEngine","Animator"},{"Animation","UnityEngine","Animation"},{"Animation","UnityEngine","ParticleSystem"},
                {"Navigation","UnityEngine","NavMeshAgent"},{"Navigation","UnityEngine","NavMeshObstacle"},
                {"UI","UnityEngine.UI","Text"},{"UI","UnityEngine.UI","Image"},{"UI","UnityEngine.UI","Button"},
                {"UI","UnityEngine.UI","Toggle"},{"UI","UnityEngine.UI","Slider"},{"UI","UnityEngine.UI","InputField"},
                {"UI","UnityEngine.UI","Canvas"},{"UI","UnityEngine","CanvasGroup"},
                {nullptr,nullptr,nullptr}
        };
        std::map<std::string, std::vector<std::pair<std::string,std::string>>> cats;
        for (int i = 0; known[i].cat; i++) {
            Class c(known[i].ns, known[i].name);
            if (c.IsValid()) cats[known[i].cat].push_back({std::string(known[i].ns) + "." + known[i].name, known[i].name});
        }
        for (auto& img : Image::GetImages()) {
            auto* d = img.GetInfo();
            if (!img.IsValid() || !d || !d->name) continue;
            if (strstr(d->name, "Assembly-CSharp") == nullptr && strstr(d->name, "Assembly-CSharp-firstpass") == nullptr) continue;
            for (auto& cls : img.GetClasses(false)) {
                auto* r = cls.GetClass();
                if (!cls.IsValid() || !r || !r->name) continue;
                bool isMono = false;
                for (auto* cur = r; cur; cur = cur->parent)
                    if (cur->name && strcmp(cur->name, "MonoBehaviour") == 0) { isMono = true; break; }
                if (!isMono) continue;
                std::string ns = r->namespaze ? r->namespaze : "";
                std::string fullName = ns.empty() ? r->name : ns + "." + r->name;
                cats["Scripts"].push_back({fullName, r->name});
            }
        }
        for (auto& kv : cats) {
            if (!firstCat) j << ","; firstCat = false;
            j << "\"" << jsEsc(kv.first) << "\":[";
            bool first = true;
            for (auto& p : kv.second) {
                if (!first) j << ","; first = false;
                j << "{\"full\":\"" << jsEsc(p.first) << "\",\"name\":\"" << jsEsc(p.second) << "\"}";
            }
            j << "]";
        }
        j << "}";
        res.set_content(j.str(), "application/json");
    });

    svr.Post("/api/catcher/hook",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        std::string asm_ = jsonVal(req.body, "asm");
        std::string ns   = jsonVal(req.body, "ns");
        std::string cls  = jsonVal(req.body, "cls");
        std::string meth = jsonVal(req.body, "method");
        std::string id   = asm_ + "|" + ns + "|" + cls + "|" + meth;
        {
            std::lock_guard<std::mutex> lk(g_catcherMtx);
            if (g_catchers.count(id)) {
                res.set_content(std::string("{\"ok\":true,\"exists\":true,\"id\":\"") + jsEsc(id) + "\"}", "application/json");
                return;
            }
        }
        int slot = -1;
        {
            std::lock_guard<std::mutex> lk(g_catcherSlotMtx);
            for (int i = 0; i < MAX_CATCHERS; i++) {
                if (!g_catcherSlots[i]) { slot = i; break; }
            }
        }
        if (slot < 0) { res.set_content("{\"ok\":false,\"error\":\"max catchers reached\"}", "application/json"); return; }
        auto entry = std::make_shared<CatcherEntry>();
        entry->id = id; entry->asm_ = asm_; entry->ns = ns; entry->cls = cls; entry->method = meth;
        entry->slotIdx = slot;
        Class c(ns.c_str(), cls.c_str(), Image(asm_.c_str()));
        if (!c.IsValid()) { res.set_content("{\"ok\":false,\"error\":\"class not found\"}", "application/json"); return; }
        MethodBase mb = c.GetMethod(meth.c_str());
        if (!mb.IsValid()) { res.set_content("{\"ok\":false,\"error\":\"method not found\"}", "application/json"); return; }
        auto* mi = mb.GetInfo();
        if (!mi || !mi->methodPointer) { res.set_content("{\"ok\":false,\"error\":\"no method pointer\"}", "application/json"); return; }
        entry->retType = typeName(mi->return_type);
        for (int i = 0; i < (int)mi->parameters_count; i++) {
            entry->paramTypes.push_back(mi->parameters && mi->parameters[i] ? typeName(mi->parameters[i]) : "?");
            entry->paramNames.push_back("arg" + std::to_string(i));
        }
        {
            std::lock_guard<std::mutex> lk(g_catcherSlotMtx);
            g_catcherSlots[slot] = new std::shared_ptr<CatcherEntry>(entry);
        }
        InvokeHook(mb, g_catcherHookFns[slot], g_catcherOrigPtrs[slot]);
        {
            std::lock_guard<std::mutex> lk(g_catcherMtx);
            g_catchers[id] = entry;
        }
        std::ostringstream j;
        j << "{\"ok\":true,\"id\":\"" << jsEsc(id) << "\"}";
        res.set_content(j.str(), "application/json");
    });

    svr.Post("/api/catcher/unhook",[](const httplib::Request& req, httplib::Response& res) {
        std::string id = jsonVal(req.body, "id");
        std::lock_guard<std::mutex> lk(g_catcherMtx);
        auto it = g_catchers.find(id);
        if (it != g_catchers.end()) {
            it->second->active = false;
            int sl = it->second->slotIdx;
            if (sl >= 0 && sl < MAX_CATCHERS) {
                std::lock_guard<std::mutex> lk2(g_catcherSlotMtx);
                delete g_catcherSlots[sl];
                g_catcherSlots[sl] = nullptr;
                g_catcherOrigPtrs[sl] = nullptr;
            }
            g_catchers.erase(it);
        }
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Post("/api/catcher/clear",[](const httplib::Request& req, httplib::Response& res) {
        std::string id = jsonVal(req.body, "id");
        std::lock_guard<std::mutex> lk(g_catcherMtx);
        auto it = g_catchers.find(id);
        if (it != g_catchers.end()) { std::lock_guard<std::mutex> lk2(it->second->callsMtx); it->second->calls.clear(); }
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Get("/api/catcher/list",[](const httplib::Request&, httplib::Response& res) {
        std::lock_guard<std::mutex> lk(g_catcherMtx);
        std::ostringstream j;
        j << "[";
        bool first = true;
        for (auto& kv : g_catchers) {
            auto& e = kv.second;
            if (!first) j << ","; first = false;
            j << "{\"id\":\"" << jsEsc(e->id) << "\",\"cls\":\"" << jsEsc(e->cls) << "\",\"ns\":\"" << jsEsc(e->ns) << "\",\"method\":\"" << jsEsc(e->method) << "\",\"ret\":\"" << jsEsc(e->retType) << "\",\"active\":" << (e->active?"true":"false") << ",\"count\":";
            { std::lock_guard<std::mutex> lk2(e->callsMtx); j << e->calls.size(); }
            j << ",\"params\":[";
            for (size_t i = 0; i < e->paramTypes.size(); i++) {
                if (i>0) j<<",";
                j << "{\"t\":\"" << jsEsc(e->paramTypes[i]) << "\",\"n\":\"" << jsEsc(e->paramNames[i]) << "\"}";
            }
            j << "]}";
        }
        j << "]";
        res.set_content(j.str(), "application/json");
    });

    svr.Get("/api/catcher/calls",[](const httplib::Request& req, httplib::Response& res) {
        std::string id = req.get_param_value("id");
        std::lock_guard<std::mutex> lk(g_catcherMtx);
        auto it = g_catchers.find(id);
        if (it == g_catchers.end()) { res.set_content("[]", "application/json"); return; }
        auto& e = it->second;
        std::lock_guard<std::mutex> lk2(e->callsMtx);
        std::ostringstream j;
        j << "[";
        bool first = true;
        for (auto& c : e->calls) {
            if (!first) j << ","; first = false;
            j << "{\"ts\":" << std::fixed << c.ts << ",\"instance\":\"" << jsEsc(c.instance) << "\",\"args\":[";
            for (size_t i = 0; i < c.args.size(); i++) {
                if (i>0) j<<",";
                j << "{\"t\":\"" << jsEsc(c.args[i].type) << "\",\"v\":" << c.args[i].val << "}";
            }
            j << "],\"ret\":\"" << jsEsc(c.ret) << "\"}";
        }
        j << "]";
        res.set_content(j.str(), "application/json");
    });

    svr.Post("/api/lua/exec",[](const httplib::Request& req, httplib::Response& res) {
        try {
            std::string code = jsonVal(req.body, "code");
            if (code.empty()) { res.set_content("{\"output\":\"Error: empty code\"}", "application/json"); return; }
            std::string out = executeLuaBNM(code);
            std::ostringstream j;
            j << "{\"output\":\"" << jsEsc(out) << "\"}";
            res.set_content(j.str(), "application/json");
        } catch (...) {
            res.set_content("{\"output\":\"Error: native crash in executor\"}", "application/json");
        }
    });

    // Bind to localhost ONLY (not 0.0.0.0) so the Explorer is reachable only
    // on-device (adb forward tcp:... tcp:8080 still works for debugging) and
    // NOT by anyone else on the player's network — closing a live-game
    // inspect/edit hole in the public build.
    svr.listen("127.0.0.1", g_port);
}

// --- Entitlement flow observation hooks ---------------------------------
// Armed in-process from OnLoaded(), before any HTTP round trip is possible,
// so we don't lose the race against Unity's Awake()/Start() lifecycle (which
// fires within milliseconds of process resume - manually arming hooks over
// curl after the fact is consistently too slow to catch it).
//
// Uses BasicHook (Dobby inline hook on the real function address) rather
// than BNM's InvokeHook/MethodInfo-swap technique or the built-in "catcher"
// system: InvokeHook only catches calls that go through il2cpp API method
// dispatch (reflection/runtime_invoke), not direct compiled call sites,
// which is how normal C# method calls are actually emitted. The built-in
// catcher only forwards `self` (no other args), which would corrupt any
// call to a method that takes real parameters - unsafe for these targets.
//
// Native IL2CPP ABI (see BNM::Method::Call in Method.hpp): non-static
// methods are `Ret(*)(Il2CppObject*, Params..., MethodInfo*)`; static
// methods (UNITY_VER > 174, true here) are `Ret(*)(Params..., MethodInfo*)`.
// Reference-type params (objects, delegates, List<T>) are all pointer-sized
// so treating unknown/obfuscated param types generically as void* is ABI-safe.


// =====================================================================
//  YEEPS 1.19.0 (Unity 2021.3.19f1) — Oculus Platform entitlement bypass
// ---------------------------------------------------------------------
//  Ground truth from on-device logcat (com.TrassGames.Yeeps 1.19.0):
//    * "Entitlement: Beginning entitlement check"
//    * "Entitlement: Received check callback"          <- ENTITLEMENT PASSES
//    * "Entitlement: Failed to get user age group"     <- 400 Invalid OAuth
//    * "Loading Screen: Getting user proof..."
//    * "Entitlement: Failed to get user proof: 400"    <- 400 Invalid OAuth  <== THE GATE
//  After the proof fails the account never goes online, NetworkManager
//  never connects to Photon, and the client stalls on the loading screen.
//
//  The proof / age / nonce results are delivered through the NATIVE Oculus
//  Platform SDK (libovrplatformloader.so) and read by the managed C# entitlement
//  callbacks via P/Invoke of plain C exports. Those exports are name-stable
//  across builds, so we hook them directly with Dobby — no IL2CPP metadata /
//  obfuscated-name resolution needed for the core fix.
//
//  ovrMessageType constants (from the 1.19.0 il2cpp metadata dump,
//  Oculus.Platform.MessageType enum — the member NAMES survive obfuscation):
//     User_GetUserProof            = 578880643
//     UserAgeCategory_Get          = 567009472
//     User_GetLoggedInUser         = 1131361373
//
//  For an errored proof/age message we force ovr_Message_IsError -> false and
//  hand the managed callback a fabricated (non-null) handle whose getter returns
//  a dummy value, so the callback takes its SUCCESS branch. The dummy nonce is
//  then sent to the Yeeps backend (redirected to the self-hosted server via the
//  metadata URL patch), which does not validate it against Meta.
// =====================================================================

#include "extern/Dobby/include/dobby.h"

static const int OVRMSG_User_GetUserProof    = 578880643;
static const int OVRMSG_UserAgeCategory_Get  = 567009472;
// Meta friends ("me/friends") always fails with HTTP 400 for this build, which
// made the game pop "Failed to get friends: 400" for everyone. Treat it as a
// successful, EMPTY friend list instead (temporary in-game friends still work).
static const int OVRMSG_User_GetLoggedInUserFriends = 1484532365;
static const int OVRMSG_User_GetNextUserArrayPage   = 645723971;
static char g_fakeUserArray[8] = {0};

typedef unsigned char ovrBool;
typedef int           (*ovr_GetType_t)(void* msg);
typedef ovrBool       (*ovr_IsError_t)(void* msg);
typedef void*         (*ovr_GetUserProof_t)(void* msg);
typedef const char*   (*ovr_GetNonce_t)(void* proof);
typedef void*         (*ovr_GetAgeMsg_t)(void* msg);
typedef int           (*ovr_GetAgeCat_t)(void* handle);

static ovr_GetType_t      real_GetType     = nullptr;   // called, never hooked
static ovr_IsError_t      real_IsError     = nullptr;
static ovr_GetUserProof_t real_GetUserProof= nullptr;
static ovr_GetNonce_t     real_GetNonce    = nullptr;
static ovr_GetAgeMsg_t    real_GetAgeMsg   = nullptr;
static ovr_GetAgeCat_t    real_GetAgeCat   = nullptr;

// Sentinel handles handed back for errored messages (must be non-null & unique).
static char        g_fakeProofHandle[8] = {0};
static char        g_fakeAgeHandle[8]   = {0};
// A plausible 64-hex-char nonce. The redirected backend accepts any value.
static const char* g_dummyNonce = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90";

static ovrBool hook_ovr_Message_IsError(void* msg) {
    ovrBool err = real_IsError ? real_IsError(msg) : (ovrBool)0;
    if (err && real_GetType) {
        int t = real_GetType(msg);
        if (t == OVRMSG_User_GetUserProof || t == OVRMSG_UserAgeCategory_Get ||
            t == OVRMSG_User_GetLoggedInUserFriends || t == OVRMSG_User_GetNextUserArrayPage) {
            BNM_FileCheckpointF("[ovr] IsError: forcing SUCCESS for msg type %d (real=error)", t);
            return (ovrBool)0;
        }
    }
    return err;
}

static void* hook_ovr_Message_GetUserProof(void* msg) {
    // If the underlying proof message really errored, its data pointer is
    // unsafe — return our sentinel so GetNonce yields the dummy nonce.
    if (real_IsError && real_IsError(msg)) {
        BNM_FileCheckpoint("[ovr] GetUserProof: errored msg -> sentinel handle");
        return (void*)g_fakeProofHandle;
    }
    void* p = real_GetUserProof ? real_GetUserProof(msg) : nullptr;
    return p ? p : (void*)g_fakeProofHandle;
}

static const char* hook_ovr_UserProof_GetNonce(void* proof) {
    if (proof == (void*)g_fakeProofHandle) {
        BNM_FileCheckpoint("[ovr] GetNonce: sentinel -> dummy nonce");
        return g_dummyNonce;
    }
    const char* n = real_GetNonce ? real_GetNonce(proof) : nullptr;
    return n ? n : g_dummyNonce;
}

static void* hook_ovr_Message_GetUserAccountAgeCategory(void* msg) {
    if (real_IsError && real_IsError(msg)) {
        BNM_FileCheckpoint("[ovr] GetAgeCategory msg: errored -> sentinel handle");
        return (void*)g_fakeAgeHandle;
    }
    void* h = real_GetAgeMsg ? real_GetAgeMsg(msg) : nullptr;
    return h ? h : (void*)g_fakeAgeHandle;
}

static int hook_ovr_UserAccountAgeCategory_GetAgeCategory(void* handle) {
    if (handle == (void*)g_fakeAgeHandle) {
        return 3; // Adult (enum: Unknown=0, Child=1, Teen=2, Adult=3)
    }
    return real_GetAgeCat ? real_GetAgeCat(handle) : 3;
}

typedef void*       (*ovr_GetUserArray_t)(void* msg);
typedef size_t      (*ovr_UA_GetSize_t)(void* arr);
typedef ovrBool     (*ovr_UA_HasNext_t)(void* arr);
typedef const char* (*ovr_UA_NextUrl_t)(void* arr);
static ovr_GetUserArray_t real_GetUserArray = nullptr;
static ovr_UA_GetSize_t   real_UA_GetSize   = nullptr;
static ovr_UA_HasNext_t   real_UA_HasNext   = nullptr;
static ovr_UA_NextUrl_t   real_UA_NextUrl   = nullptr;
static void* hook_ovr_Message_GetUserArray(void* msg) {
    if (real_IsError && real_IsError(msg)) return (void*)g_fakeUserArray;   // errored -> empty list
    void* a = real_GetUserArray ? real_GetUserArray(msg) : nullptr;
    return a ? a : (void*)g_fakeUserArray;
}
static size_t hook_ovr_UserArray_GetSize(void* arr) {
    if (arr == (void*)g_fakeUserArray) return 0;
    return real_UA_GetSize ? real_UA_GetSize(arr) : 0;
}
static ovrBool hook_ovr_UserArray_HasNextPage(void* arr) {
    if (arr == (void*)g_fakeUserArray) return (ovrBool)0;
    return real_UA_HasNext ? real_UA_HasNext(arr) : (ovrBool)0;
}
static const char* hook_ovr_UserArray_GetNextUrl(void* arr) {
    if (arr == (void*)g_fakeUserArray) return "";
    return real_UA_NextUrl ? real_UA_NextUrl(arr) : "";
}

// 2.4.0's libovrplatformloader exports are 12-byte stubs
//     adrp xN, page ; ldr xM, [xN, #off] ; br xM
// that jump through a pointer table the loader fills in. An inline Dobby patch
// (16 bytes) overruns the stub into the NEXT export (SIGILL in
// ovr_UserAgeCategory_Get). Swap the table pointer instead: no code is
// touched. The loader can fill or refill the table later, so a keeper thread
// re-applies the swap whenever the slot holds something other than our hook.
struct OvrSlotHook { const char* name; void** slot; void* repl; void** orig; };
static std::vector<OvrSlotHook> g_ovrSlots;
static void** OvrStubSlot(void* fn) {
    auto* w = (const uint32_t*)fn;
    if ((w[0] & 0x9F000000u) != 0x90000000u) return nullptr;            // adrp
    if ((w[1] & 0xFFC00000u) != 0xF9400000u) return nullptr;            // ldr x, [x, #imm]
    if ((w[2] & 0xFFFFFC1Fu) != 0xD61F0000u) return nullptr;            // br x
    uint32_t rd = w[0] & 31, rn = (w[1] >> 5) & 31, rt = w[1] & 31, rb = (w[2] >> 5) & 31;
    if (rd != rn || rt != rb) return nullptr;
    int64_t imm = (int64_t)((((w[0] >> 5) & 0x7FFFF) << 2) | ((w[0] >> 29) & 3));
    if (imm & (1LL << 20)) imm -= (1LL << 21);
    uintptr_t page = ((uintptr_t)fn & ~(uintptr_t)0xFFF) + (uintptr_t)(imm << 12);
    return (void**)(page + ((w[1] >> 10) & 0xFFF) * 8);
}
static void OvrSlotKeeper() {
    for (;;) {
        for (auto& h : g_ovrSlots) {
            void* cur = __atomic_load_n(h.slot, __ATOMIC_ACQUIRE);
            if (cur && cur != h.repl) {
                *h.orig = cur;   // the real function, for the hook to call
                __atomic_store_n(h.slot, h.repl, __ATOMIC_RELEASE);
                BNM_FileCheckpointF("[ovr] slot %s -> hook (real=%p)", h.name, cur);
            }
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
}

static bool g_ovrHooksInstalled = false;
static void InstallOvrPlatformHooks(void* lib) {
    if (g_ovrHooksInstalled) return;
    auto H = [&](const char* name, void* repl, void** orig) {
        void* sym = dlsym(lib, name);
        if (!sym) { BNM_FileCheckpointF("[ovr] dlsym FAILED: %s", name); return; }
        if (void** slot = OvrStubSlot(sym)) {
            g_ovrSlots.push_back({name, slot, repl, orig});
            BNM_FileCheckpointF("[ovr] %s is a stub, slot %p (now %p)", name, (void*)slot, *slot);
            return;
        }
        void* tramp = nullptr;
        if (DobbyHook(sym, (dobby_dummy_func_t)repl, (dobby_dummy_func_t*)&tramp) != 0) {
            BNM_FileCheckpointF("[ovr] DobbyHook FAILED: %s", name);
            return;
        }
        if (orig) *orig = tramp;
        BNM_FileCheckpointF("[ovr] hooked %s @ %p (tramp=%p)", name, sym, tramp);
    };
    // GetType is only called, not hooked.
    real_GetType = (ovr_GetType_t)dlsym(lib, "ovr_Message_GetType");
    BNM_FileCheckpointF("[ovr] ovr_Message_GetType @ %p", (void*)real_GetType);
    H("ovr_Message_IsError",                      (void*)hook_ovr_Message_IsError,                      (void**)&real_IsError);
    H("ovr_Message_GetUserProof",                 (void*)hook_ovr_Message_GetUserProof,                 (void**)&real_GetUserProof);
    H("ovr_UserProof_GetNonce",                   (void*)hook_ovr_UserProof_GetNonce,                   (void**)&real_GetNonce);
    H("ovr_Message_GetUserAccountAgeCategory",    (void*)hook_ovr_Message_GetUserAccountAgeCategory,    (void**)&real_GetAgeMsg);
    H("ovr_UserAccountAgeCategory_GetAgeCategory",(void*)hook_ovr_UserAccountAgeCategory_GetAgeCategory,(void**)&real_GetAgeCat);
    H("ovr_Message_GetUserArray",                 (void*)hook_ovr_Message_GetUserArray,                 (void**)&real_GetUserArray);
    H("ovr_UserArray_GetSize",                    (void*)hook_ovr_UserArray_GetSize,                    (void**)&real_UA_GetSize);
    H("ovr_UserArray_HasNextPage",                (void*)hook_ovr_UserArray_HasNextPage,                (void**)&real_UA_HasNext);
    H("ovr_UserArray_GetNextUrl",                 (void*)hook_ovr_UserArray_GetNextUrl,                 (void**)&real_UA_NextUrl);
    g_ovrHooksInstalled = true;
    if (!g_ovrSlots.empty()) std::thread(OvrSlotKeeper).detach();
    BNM_FileCheckpoint("[ovr] InstallOvrPlatformHooks: DONE");
}

// libovrplatformloader.so is dlopen'd by the app during OVRPlatform init, which
// happens a little after process start. Poll (without forcing a load) until it
// appears, then install the hooks before the entitlement callbacks fire.
static void OvrHookPollThread() {
    // Poll effectively forever (process lifetime). The game may sit on the
    // Terms-of-Service / boot screens for an unbounded time before the game
    // scene loads and OVRPlatform (libovrplatformloader.so) is dlopen'd, so a
    // short timeout can miss it entirely on a fresh install. 100ms polling for
    // up to ~1 hour costs nothing and guarantees we catch the lib whenever it
    // loads, before the entitlement callbacks fire.
    for (int i = 0; i < 36000; i++) { // up to ~1 h
        void* lib = dlopen("libovrplatformloader.so", RTLD_NOLOAD | RTLD_NOW);
        if (lib) {
            BNM_FileCheckpointF("[ovr] libovrplatformloader.so present (attempt %d)", i);
            InstallOvrPlatformHooks(lib);
            return;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    BNM_FileCheckpoint("[ovr] libovrplatformloader.so never appeared (1h timeout)");
}

// ---------------------------------------------------------------------
//  CREDENTIAL-DECRYPT DIAGNOSTICS (1.19.0)
//  The client decrypts e_accessKey/e_secretKey with a symmetric key it
//  holds; "Account: Failed to get credentials" fires when that fails. We
//  don't know 1.19.0's baked key. Hook
//  the .NET crypto (mscorlib, plaintext names) to dump the exact key/IV and
//  the base64 blobs being decoded, so the server can re-encrypt the real
//  creds with the SAME key. il2cpp byte[] layout: length @ +0x18, data @ +0x20.
// ---------------------------------------------------------------------
static std::string HexDumpArr(void* arr) {
    if (!arr) return "(null)";
    try {
        uintptr_t len = *(uintptr_t*)((uint8_t*)arr + 0x18);
        if (len > 4096) return "(len too big)";
        uint8_t* data = (uint8_t*)arr + 0x20;
        static const char* H = "0123456789abcdef";
        std::string s; s.reserve(len * 2);
        for (uintptr_t i = 0; i < len; i++) { s += H[data[i] >> 4]; s += H[data[i] & 0xf]; }
        return s;
    } catch (...) { return "(exc)"; }
}
static void (*orig_setKey)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void hook_setKey(void* self, void* val, IL2CPP::MethodInfo* mi) {
    BNM_FileCheckpointF("CRYPTO set_Key: %s", HexDumpArr(val).c_str());
    if (orig_setKey) orig_setKey(self, val, mi);
}
static void (*orig_setIV)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void hook_setIV(void* self, void* val, IL2CPP::MethodInfo* mi) {
    BNM_FileCheckpointF("CRYPTO set_IV : %s", HexDumpArr(val).c_str());
    if (orig_setIV) orig_setIV(self, val, mi);
}
static void (*orig_rfcCtor)(void*, void*, void*, int, IL2CPP::MethodInfo*) = nullptr;
static void hook_rfcCtor(void* self, void* pwd, void* salt, int iters, IL2CPP::MethodInfo* mi) {
    std::string p;
    try { p = pwd ? ((BNM::Structures::Mono::String*)pwd)->str() : "(null)"; } catch(...) { p="(exc)"; }
    BNM_FileCheckpointF("CRYPTO Rfc2898.ctor: pwd='%s' salt=%s iters=%d", p.c_str(), HexDumpArr(salt).c_str(), iters);
    if (orig_rfcCtor) orig_rfcCtor(self, pwd, salt, iters, mi);
}
// Dump the base64 strings the client decodes (e_accessKey/e_secretKey land here).
static void* (*orig_FromBase64)(void*, IL2CPP::MethodInfo*) = nullptr;
static void* hook_FromBase64(void* s, IL2CPP::MethodInfo* mi) {
    try {
        std::string in = s ? ((BNM::Structures::Mono::String*)s)->str() : "(null)";
        if (in.size() >= 20 && in.size() <= 200)  // creds are 44-130 chars; skip tiny/huge
            BNM_FileCheckpointF("CRYPTO FromBase64('%s')", in.c_str());
    } catch(...) {}
    return orig_FromBase64 ? orig_FromBase64(s, mi) : nullptr;
}
// Dump the DECRYPTED global-metadata.dat from memory. il2cpp decrypts the
// on-disk (encrypted, header 0ac2246e) metadata into a heap buffer with the
// STANDARD magic AF 1B B1 FA during init. Scan /proc/self/maps ANONYMOUS rw
// regions for that magic and write the buffer to /sdcard so Il2CppDumper can
// parse it offline -> full 2.4.0 class/method symbol table (names -> offsets).
static void DumpDecryptedMetadata() {
    const char* OUT = "/sdcard/Android/data/com.TrassGames.Yeeps/files/meta_decrypted.dat";
    FILE* mf = fopen("/proc/self/maps", "r");
    if (!mf) { BNM_FileCheckpoint("MetaDump: cannot open maps"); return; }
    char line[512];
    const unsigned char M0=0xAF,M1=0x1B,M2=0xB1,M3=0xFA;
    int dumped = 0;
    while (fgets(line, sizeof(line), mf)) {
        unsigned long start=0, end=0; char perms[8]={0}; char path[256]={0};
        int n = sscanf(line, "%lx-%lx %7s %*x %*s %*u %255[^\n]", &start, &end, perms, path);
        if (n < 3) continue;
        if (perms[0] != 'r' || perms[1] != 'w') continue;   // heap: rw
        // skip file-backed mappings (incl. the ENCRYPTED global-metadata.dat file)
        if (n >= 4 && path[0] && strchr(path, '/')) continue;
        size_t sz = end - start;
        if (sz < 0x400000 || sz > 0x8000000) continue;       // metadata ~29MB
        unsigned char* p = (unsigned char*)start;
        size_t lim = sz - 4; if (lim > 0x2000000) lim = 0x2000000;
        for (size_t i = 0; i < lim; i++) {
            if (p[i]==M0 && p[i+1]==M1 && p[i+2]==M2 && p[i+3]==M3) {
                int ver = *(int*)(p+i+4);
                if (ver < 16 || ver > 40) continue;           // il2cpp metadata version sanity
                size_t avail = sz - i;
                size_t out = avail > 0x2800000 ? 0x2800000 : avail;  // cap ~40MB
                FILE* of = fopen(OUT, "wb");
                if (of) { fwrite(p+i, 1, out, of); fclose(of); dumped=1;
                    BNM_FileCheckpointF("MetaDump: WROTE %zu bytes ver=%d region=%lx-%lx OUT=%s", out, ver, start, end, OUT); }
                break;
            }
        }
        if (dumped) break;
    }
    fclose(mf);
    if (!dumped) BNM_FileCheckpoint("MetaDump: decrypted AF1BB1FA magic NOT FOUND in anon heap");
}

// More thorough decrypted-metadata dump: (1) dump any mapping whose path
// mentions global-metadata (covers in-place decryption of the file mmap),
// (2) else scan EVERY readable region fully for the il2cpp magic AF1BB1FA.
static void DumpMetaV2() {
    const char* OUT = "/sdcard/Android/data/com.TrassGames.Yeeps/files/meta_decrypted.dat";
    const unsigned char M0=0xAF,M1=0x1B,M2=0xB1,M3=0xFA;
    FILE* mf = fopen("/proc/self/maps", "r");
    if (!mf) { BNM_FileCheckpoint("MetaV2: cannot open maps"); return; }
    char line[1024]; int dumped = 0;
    // Pass 1: the global-metadata file mapping (decrypt-in-place case)
    while (fgets(line, sizeof(line), mf)) {
        unsigned long s=0,e=0; char perms[8]={0}, path[700]={0};
        int n = sscanf(line, "%lx-%lx %7s %*x %*s %*u %699[^\n]", &s,&e,perms,path);
        if (n < 4 || perms[0] != 'r') continue;
        if (strstr(path, "global-metadata")) {
            size_t sz = e - s; unsigned char* p = (unsigned char*)s;
            FILE* of = fopen(OUT, "wb");
            if (of) { fwrite(p, 1, sz, of); fclose(of); dumped = 1;
                BNM_FileCheckpointF("MetaV2: file-map dump %zu bytes magic=%02x%02x%02x%02x (%s)",
                                    sz, p[0],p[1],p[2],p[3], path); }
            break;
        }
    }
    // Pass 2: full scan of every readable region for the decrypted magic
    if (!dumped) {
        rewind(mf);
        while (fgets(line, sizeof(line), mf)) {
            unsigned long s=0,e=0; char perms[8]={0};
            if (sscanf(line, "%lx-%lx %7s", &s,&e,perms) < 3) continue;
            if (perms[0] != 'r') continue;
            size_t sz = e - s;
            if (sz < 0x100000 || sz > 0x10000000) continue;   // 1MB..256MB
            unsigned char* p = (unsigned char*)s;
            for (size_t i = 0; i + 8 < sz; i += 4) {
                if (p[i]==M0 && p[i+1]==M1 && p[i+2]==M2 && p[i+3]==M3) {
                    int ver = *(int*)(p+i+4);
                    if (ver < 16 || ver > 40) continue;
                    size_t avail = sz - i, out = avail > 0x2800000 ? 0x2800000 : avail;
                    FILE* of = fopen(OUT, "wb");
                    if (of) { fwrite(p+i, 1, out, of); fclose(of); dumped = 1;
                        BNM_FileCheckpointF("MetaV2: scan hit %zu bytes ver=%d", out, ver); }
                    break;
                }
            }
            if (dumped) break;
        }
    }
    fclose(mf);
    if (!dumped) BNM_FileCheckpoint("MetaV2: NOT FOUND");
}

static void InstallCryptoKeyDump() {
    try {
        Class sa("System.Security.Cryptography", "SymmetricAlgorithm", Image("mscorlib.dll"));
        if (sa.IsValid()) {
            MethodBase k = sa.GetMethod("set_Key", 1);
            if (k.IsValid()) { BasicHook(k, hook_setKey, orig_setKey); BNM_FileCheckpoint("Crypto: set_Key hooked"); }
            else BNM_FileCheckpoint("Crypto: set_Key NOT FOUND");
            MethodBase v = sa.GetMethod("set_IV", 1);
            if (v.IsValid()) { BasicHook(v, hook_setIV, orig_setIV); BNM_FileCheckpoint("Crypto: set_IV hooked"); }
            else BNM_FileCheckpoint("Crypto: set_IV NOT FOUND");
        } else BNM_FileCheckpoint("Crypto: SymmetricAlgorithm NOT FOUND");
        Class rfc("System.Security.Cryptography", "Rfc2898DeriveBytes", Image("mscorlib.dll"));
        if (rfc.IsValid()) {
            MethodBase c = rfc.GetMethod(".ctor", 3);
            if (c.IsValid()) { BasicHook(c, hook_rfcCtor, orig_rfcCtor); BNM_FileCheckpoint("Crypto: Rfc2898.ctor(3) hooked"); }
        }
        Class conv("System", "Convert", Image("mscorlib.dll"));
        if (conv.IsValid()) {
            MethodBase m = conv.GetMethod("FromBase64String", 1);
            if (m.IsValid()) { BasicHook(m, hook_FromBase64, orig_FromBase64); BNM_FileCheckpoint("Crypto: FromBase64String hooked"); }
        }
    } catch (...) { BNM_FileCheckpoint("InstallCryptoKeyDump: EXCEPTION"); }
}

// ---------------------------------------------------------------------
//  Credential-failure backtrace: hook LogManager.LogEvent(category, message)
//  and, when the credential/online-fetch errors fire, dump the libil2cpp
//  return-address backtrace as FILE OFFSETS (RVAs). 1.19.0 anchor:
//  il2cppBase = &il2cpp_array_new_specific - 0xebdc94. The topmost in-il2cpp
//  frame is the method that logged "Failed to get credentials" — disassemble
//  it to see exactly which response field read returns null.
// ---------------------------------------------------------------------
// While the AccountManager credential coroutine is processing the login
// payload we log every string field it reads, so we can see exactly which
// 3 fields its String.IsNullOrEmpty checks look at (and which is empty).
static std::atomic<bool> g_inCredProc{false};
// The account-data string getter AccountManager uses (RVA 0x1df5f90):
// System.String get(object obj, key, _, _). Logged only during cred processing.
static void* (*orig_credGetter)(void*, void*, void*, void*) = nullptr;
static void* hook_credGetter(void* obj, void* key, void* a2, void* a3) {
    void* ret = orig_credGetter ? orig_credGetter(obj, key, a2, a3) : nullptr;
    if (g_inCredProc.load()) {
        try {
            std::string k = key ? ((BNM::Structures::Mono::String*)key)->str() : "(nullkey)";
            std::string v = ret ? ((BNM::Structures::Mono::String*)ret)->str() : "(NULL)";
            if (k.size() < 60)
                BNM_FileCheckpointF("CREDGET '%s' = '%s'", k.c_str(), (v.size() > 40 ? (v.substr(0,40)+"...").c_str() : v.c_str()));
        } catch (...) {}
    }
    return ret;
}

static void (*orig_LogEventBT)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void hook_LogEventBT(void* category, void* message, IL2CPP::MethodInfo* mi) {
    try {
        std::string msg = message ? ((BNM::Structures::Mono::String*)message)->str() : "";
        if (msg.find("Processing account") != std::string::npos ||
            msg.find("parse account") != std::string::npos ||
            msg.find("fetch online account") != std::string::npos ||
            msg.find("load game account") != std::string::npos ||
            msg.find("ArgumentNull") != std::string::npos) {
            std::string cat = category ? ((BNM::Structures::Mono::String*)category)->str() : "";
            // 1.31.0 base: il2cpp_raise_exception export sits at file vaddr 0x41FC69C.
            void* exp = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_raise_exception");
            uint8_t* base = exp ? ((uint8_t*)exp - 0x41FC69CULL) : nullptr;
            BNM_FileCheckpointF("LOGEVENT-BT [%s] '%s' RVAs:%s", cat.c_str(), msg.c_str(), UnwindRVAs(base).c_str());
        }
    } catch (...) {}
    if (orig_LogEventBT) orig_LogEventBT(category, message, mi);
}
// DIAG (1.31 room load): ArgumentNullException("s") = Int32/DateTime.Parse(null)
// on some room/map field. Backtrace the ctor so the RVAs point at the game
// method doing the parse (disassemble it to find which field key it reads).
static std::string _litStr(uint64_t rva);   // defined with the gate probes
static std::atomic<int> g_anN{0};
static void (*orig_ANECtor)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void hook_ANECtor(void* self, void* paramName, IL2CPP::MethodInfo* mi) {
    try {
        std::string pn = paramName ? ((BNM::Structures::Mono::String*)paramName)->str() : "(null)";
        if (g_anN.fetch_add(1) < 40) {
            void* exp = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_raise_exception");
            uint8_t* base = exp ? ((uint8_t*)exp - 0x41FC69CULL) : nullptr;
            BNM_FileCheckpointF("ANE-CTOR param='%s' RVAs:%s", pn.c_str(), UnwindRVAs(base).c_str());
            if (g_anN.load() == 1) {
                // Keys read by the room-parse method around RVA 0x1c2c100; the
                // GetInt that throws is keyed by slot 0x5dbf248.
                static const uint64_t RK[] = {
                    0x5dbf248,0x5d742f0,0x5dce2f8,0x5da07b8,0x5da2ec0,0x5d7c260,0x5d72ec0,
                    0x5d78f10,0x5d78f98,0x5df93f8,0x5df1c50,0x5d8b180,0x5df96c8,0x5ddf898,
                    0x5ddb128,0x5d75108,0x5d88628,0x5dba718,0x5dfa598,0x5deef70,0x5de7e20,
                    0x5d80120,0x5db29b8,0x5d7df18,0x5dc5b78,0x5dba148,0x5da6c18,0x5dd8b10,
                    0x5dd2e28,0x5da0af8,
                };
                for (uint64_t k : RK)
                    BNM_FileCheckpointF("ROOMKEY %llx '%s'", (unsigned long long)k, _litStr(k).c_str());
            }
        }
    } catch (...) {}
    if (orig_ANECtor) orig_ANECtor(self, paramName, mi);
}
static void InstallANECtor() {
    try {
        Class c("System", "ArgumentNullException", Image("mscorlib.dll"));
        if (!c.IsValid()) { BNM_FileCheckpoint("ANE: class NOT FOUND"); return; }
        MethodBase m = c.GetMethod(".ctor", 1);
        if (m.IsValid()) { BasicHook(m, hook_ANECtor, orig_ANECtor); BNM_FileCheckpoint("ANE: .ctor(1) hooked"); }
        else BNM_FileCheckpoint("ANE: .ctor(1) NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("ANE: EXCEPTION"); }
}
static void InstallLogEventBT() {
    try {
        Class lm("", "LogManager", Image("Assembly-CSharp.dll"));
        if (!lm.IsValid()) { BNM_FileCheckpoint("LogEventBT: LogManager NOT FOUND"); return; }
        MethodBase m = lm.GetMethod("LogEvent", 2);
        if (m.IsValid()) { BasicHook(m, hook_LogEventBT, orig_LogEventBT); BNM_FileCheckpoint("LogEventBT: LogEvent hooked"); }
        else BNM_FileCheckpoint("LogEventBT: LogEvent(2) NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("LogEventBT: EXCEPTION"); }
}

// DIAG: probe the account-process method at RVA 0x287FD94 (it logs "Processing
// account data" then gates success on `[arg1] & 1`). Log the class of each arg
// (and the deciding byte) so we learn what the gate actually tests.
static bool _plausiblePtr(void* p) {
    uintptr_t v = (uintptr_t)p;
    return p && (v & 7) == 0 && v > 0x10000 && v < 0x8000000000ULL;
}
static std::atomic<int> g_apN{0};
static void* (*orig_AP)(void*, void*, void*, void*, void*, void*, void*, void*) = nullptr;
static void* hook_AP(void* a0, void* a1, void* a2, void* a3, void* a4, void* a5, void* a6, void* a7) {
    if (g_apN.fetch_add(1) < 6) {
        try {
            typedef const char* (*cn_t)(void*);
            typedef void* (*oc_t)(void*);
            static auto gn = (cn_t)dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_class_get_name");
            static auto gc = (oc_t)dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_object_get_class");
            const char* c0 = "?"; const char* c1 = "?";
            if (gn && gc) {
                if (_plausiblePtr(a0) && _plausiblePtr(*(void**)a0)) c0 = gn(gc(a0));
                if (_plausiblePtr(a1) && _plausiblePtr(*(void**)a1)) c1 = gn(gc(a1));
            }
            int b1 = _plausiblePtr(a1) ? (*(volatile uint8_t*)a1 & 1) : -1;
            BNM_FileCheckpointF("ACCTPROC a0cls=%s a1cls=%s a1bit0=%d a0=%p a1=%p a2=%p",
                                c0, c1, b1, a0, a1, a2);
        } catch (...) {}
    }
    g_inCredProc.store(true);
    void* r = orig_AP ? orig_AP(a0, a1, a2, a3, a4, a5, a6, a7) : nullptr;
    g_inCredProc.store(false);
    return r;
}
static void InstallAcctProbe() {
    void* exp = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_raise_exception");
    if (!exp) { BNM_FileCheckpoint("[ap] no il2cpp base"); return; }
    void* fn = (uint8_t*)exp - 0x41FC69CULL + 0x287FD94ULL;
    void* tr = nullptr;
    if (DobbyHook(fn, (dobby_dummy_func_t)hook_AP, (dobby_dummy_func_t*)&tr) != 0) {
        BNM_FileCheckpointF("[ap] hook FAILED @ %p", fn); return;
    }
    orig_AP = (void*(*)(void*, void*, void*, void*, void*, void*, void*, void*))tr;
    BNM_FileCheckpointF("[ap] acct-process @ %p hooked", fn);
}

// DIAG: log the KEY (x1) + bool result of each field-read helper used by the
// account parse (RVAs 0x435BB20 / 0x435B538 / 0x435A0D8). The sequence of keys
// (and where it stops / which returns false) pinpoints the failing field.
// Robust il2cpp System.String decode: +0x10 int32 length, +0x14 UTF-16 chars.
// Avoids BNM str() (which was returning "?" here) and never throws.
static std::string _monoStr(void* o) {
    if (!_plausiblePtr(o)) return "(badptr)";
    int32_t len = *(volatile int32_t*)((uint8_t*)o + 0x10);
    if (len < 0 || len > 1024) return std::string("(len=") + std::to_string(len) + ")";
    const volatile uint16_t* cs = (const volatile uint16_t*)((uint8_t*)o + 0x14);
    std::string s;
    for (int i = 0; i < len; i++) {
        uint16_t c = cs[i];
        s += (c >= 32 && c < 127) ? (char)c : '.';
    }
    return s;
}
static std::string _hex(void* p, int n) {
    std::string s;
    if (!_plausiblePtr(p)) return "(bad)";
    char b[4];
    for (int i = 0; i < n; i++) { snprintf(b, 4, "%02x", ((volatile uint8_t*)p)[i]); s += b; if ((i&7)==7) s += ' '; }
    return s;
}
// The two fatal gates: if either returns false, the parse reports failure.
// RVA 0x257570C (gate A) and 0x256C0C8 (gate B). Log result + arg struct bytes.
static uintptr_t g_il2cppBase = 0;   // set in InstallFieldReadProbe
#define _retOff() ((uintptr_t)__builtin_return_address(0) - g_il2cppBase)
// Read an il2cpp string-literal referenced as `ldr x8,[base+rva]; ldr x1,[x8]`
// i.e. String* = *(*(base+rva)). Used to decode the obfuscated field-name keys.
static std::string _litStr(uint64_t rva) {
    if (!g_il2cppBase) return "(nobase)";
    void** slot = (void**)(g_il2cppBase + rva);
    if (!_plausiblePtr(slot)) return "(badslot)";
    void* entry = *slot;
    if (!_plausiblePtr(entry)) return "(badentry)";
    void* s = *(void**)entry;
    return _monoStr(s);
}
static std::atomic<int> g_gA{0}, g_gB{0};
static uint64_t (*orig_GA)(void*, void*, void*, void*, void*, void*, void*, void*) = nullptr;
static uint64_t hook_GA(void* a0, void* a1, void* a2, void* a3, void* a4, void* a5, void* a6, void* a7) {
    uint64_t r = orig_GA ? orig_GA(a0, a1, a2, a3, a4, a5, a6, a7) : 0;
    int n = g_gA.fetch_add(1);
    if (n < 60)
        BNM_FileCheckpointF("GATE-A(257570c) r=%d a0=[%s] a2=[%s]", (int)(r&1), _hex(a0,16).c_str(), _hex(a2,16).c_str());
    if (n == 0) {
        // One-shot: decode EVERY field-name string literal Gate A references
        // (all `ldr x1,[xN]` key slots extracted from the disassembly), so we
        // get the complete set of account fields the parser reads in one run.
        static const uint64_t KSLOTS[] = {
            0x5dd5d98,0x5daf478,0x5e001a8,0x5dd0bd0,0x5dac638,0x5d7ac00,0x5d78af0,0x5dea088,
            0x5dc6ea0,0x5d85bb0,0x5da2168,0x5ddc1b0,0x5df6e78,0x5dcaaa0,0x5dcf420,0x5e06770,
            0x5dcb628,0x5de4700,0x5dcef20,0x5d854a0,0x5dd3070,0x5dd5c30,0x5dbfe50,0x5d95990,
            0x5d8b7a8,0x5da3a60,0x5d806c8,0x5da5260,0x5d84d30,0x5db1398,0x5dbe210,0x5dcba20,
            0x5dc37e0,0x5dba538,0x5d731f0,0x5de4b00,0x5d73af0,0x5e04968,0x5d89cc0,0x5dc0588,
            0x5d720a0,0x5e06a80,0x5db8e20,0x5dbc2e0,0x5d93a18,0x5da9ad0,0x5daf998,0x5daba90,
            0x5dd7590,
        };
        for (uint64_t s : KSLOTS)
            BNM_FileCheckpointF("GAFIELD %llx '%s'", (unsigned long long)s, _litStr(s).c_str());
    }
    return r;
}
static std::atomic<int> g_gBfield{0};
static uint64_t (*orig_GB)(void*, void*, void*, void*, void*, void*, void*, void*) = nullptr;
static uint64_t hook_GB(void* a0, void* a1, void* a2, void* a3, void* a4, void* a5, void* a6, void* a7) {
    uint64_t r = orig_GB ? orig_GB(a0, a1, a2, a3, a4, a5, a6, a7) : 0;
    if (g_gB.fetch_add(1) < 60)
        BNM_FileCheckpointF("GATE-B(256c0c8) r=%d a0=[%s]", (int)(r&1), _hex(a0,24).c_str());
    if (g_gBfield.fetch_add(1) == 0) {
        // One-shot: decode the field-name literals Gate B (the validator) reads.
        static const uint64_t GBS[] = {
            0x5d806c8,0x5da5260,0x5d84d30,0x5db2530,0x5e02b08,0x5d89cc0,
            0x5dc0588,0x5d720a0,0x5e04968,0x5e06a80,0x5db8e20,
        };
        for (uint64_t s : GBS)
            BNM_FileCheckpointF("GBFIELD %llx '%s'", (unsigned long long)s, _litStr(s).c_str());
    }
    // Gate B is the client-side integrity/tamper validation of the account data.
    // It hash-validates fields we legitimately modify on our private server, so
    // it fails. Force it to pass, but ONLY during the account-process coroutine
    // (g_inCredProc) so any other callers get the real result. orig_GB still runs
    // first (side effects preserved); we only override the boolean.
    if (g_inCredProc.load() && (r & 1) == 0) {
        BNM_FileCheckpointF("GATE-B FORCED pass (was %d)", (int)(r & 1));
        return 1;
    }
    return r;
}
// IsNullOrEmpty (RVA 0x3CDA1D0). Log value+result but ONLY for calls made from
// inside Gate B (the validator, 0x256c0c8..0x256cc00) -> pinpoints which string
// check fails validation.
static std::atomic<int> g_ineN{0};
static uint64_t (*orig_INE)(void*, void*) = nullptr;
static uint64_t hook_INE(void* a0, void* a1) {
    uintptr_t off = _retOff();
    uint64_t r = orig_INE ? orig_INE(a0, a1) : 0;
    if (g_inCredProc.load() && off >= 0x256c0c8 && off <= 0x256cc00 && g_ineN.fetch_add(1) < 60) {
        std::string v = a0 ? _monoStr(a0) : std::string("(null)");
        BNM_FileCheckpointF("GBNULL off=%llx '%s' -> %d", (unsigned long long)off, v.c_str(), (int)(r & 1));
    }
    return r;
}
// Gate A (0x257570c) requires 5 keys via Dictionary.ContainsKey (0x43554f0):
//   bool ContainsKey(this dict=x0, string key=x1, MethodInfo*=x2)
// Log key+result during cred-proc; the first key that returns 0 is the field our
// server is missing (which is what makes Gate A return false -> parse failure).
static std::atomic<int> g_ckN{0};
static uint64_t (*orig_CK)(void*, void*, void*) = nullptr;
static uint64_t hook_CK(void* a0, void* a1, void* a2) {
    uint64_t r = orig_CK ? orig_CK(a0, a1, a2) : 0;
    // Only log ContainsKey calls made from inside Gate A (0x257570c): its 5
    // required-key checks have return addrs in base+[0x2575a50 .. 0x2575ac4].
    uintptr_t ret = (uintptr_t)__builtin_return_address(0);
    if (g_il2cppBase && g_inCredProc.load()) {
        uintptr_t off = ret - g_il2cppBase;
        if (off >= 0x2575a40 && off <= 0x2575ad0) {
            std::string v = a1 ? _monoStr(a1) : std::string("(null)");
            BNM_FileCheckpointF("GAKEY off=%llx '%s' -> %d",
                                (unsigned long long)off, v.c_str(), (int)(r & 1));
        } else if (g_ckN.fetch_add(1) < 40) {
            // fallback diag: show raw return offset + key so we can locate Gate A's
            // real call offsets if the range above misses (Dobby trampoline shift).
            std::string v = a1 ? _monoStr(a1) : std::string("(null)");
            BNM_FileCheckpointF("CKANY off=%llx '%s' -> %d",
                                (unsigned long long)off, v.c_str(), (int)(r & 1));
        }
    }
    return r;
}
// ---- Gate A value-validation getters -------------------------------------
// Gate A returns false if any required typed read fails. Data-dependent fail
// exits found by disasm: GetString (0x4356550) -> null@0x2576100; TryGetValue
// (0x4355440) -> false@0x257604c; a get (0x4355298) -> null@0x2575ccc; list get
// (0x435d128). Log each call made from INSIDE Gate A (return addr in Gate A's
// body) with its key + result; the LAST line before "GATE-A r=0" is the field
// whose value our server response has in the wrong shape/type.
static std::atomic<int> g_gN{0};
static void* (*orig_GS)(void*, void*, void*, void*) = nullptr;   // GetString 0x4356550
static void* hook_GS(void* a0, void* a1, void* a2, void* a3) {
    uintptr_t off = _retOff();
    void* r = orig_GS ? orig_GS(a0, a1, a2, a3) : nullptr;
    if (g_inCredProc.load() && off >= 0x2575a40 && off <= 0x25766d0 && g_gN.fetch_add(1) < 80) {
        std::string k = a1 ? _monoStr(a1) : std::string("(nullkey)");
        BNM_FileCheckpointF("GS off=%llx '%s' -> %s", (unsigned long long)off, k.c_str(), r ? "ok" : "NULL");
    }
    return r;
}
static void* (*orig_GSn)(void*, void*, void*) = nullptr;         // GetString? 0x435b538
static void* hook_GSn(void* a0, void* a1, void* a2) {
    uintptr_t off = _retOff();
    void* r = orig_GSn ? orig_GSn(a0, a1, a2) : nullptr;
    if (g_inCredProc.load() && off >= 0x2575a40 && off <= 0x25766d0 && g_gN.fetch_add(1) < 80) {
        std::string k = a1 ? _monoStr(a1) : std::string("(nullkey)");
        BNM_FileCheckpointF("GSn off=%llx '%s' -> %s", (unsigned long long)off, k.c_str(), r ? "ok" : "NULL");
    }
    return r;
}
static uint64_t (*orig_TGV)(void*, void*, void*, void*) = nullptr; // TryGetValue 0x4355440
static uint64_t hook_TGV(void* a0, void* a1, void* a2, void* a3) {
    uintptr_t off = _retOff();
    uint64_t r = orig_TGV ? orig_TGV(a0, a1, a2, a3) : 0;
    if (g_inCredProc.load() && off >= 0x2575a40 && off <= 0x25766d0 && g_gN.fetch_add(1) < 80) {
        std::string k = a1 ? _monoStr(a1) : std::string("(nullkey)");
        BNM_FileCheckpointF("TGV off=%llx '%s' -> %d", (unsigned long long)off, k.c_str(), (int)(r & 1));
    }
    return r;
}
static void* (*orig_GL)(void*, void*, void*, void*) = nullptr;   // list/array get 0x435d128
static void* hook_GL(void* a0, void* a1, void* a2, void* a3) {
    uintptr_t off = _retOff();
    void* r = orig_GL ? orig_GL(a0, a1, a2, a3) : nullptr;
    if (g_inCredProc.load() && off >= 0x2575a40 && off <= 0x25766d0 && g_gN.fetch_add(1) < 80) {
        std::string k = a1 ? _monoStr(a1) : std::string("(nullkey)");
        BNM_FileCheckpointF("GL off=%llx '%s' -> %s", (unsigned long long)off, k.c_str(), r ? "ok" : "NULL");
    }
    return r;
}
static void* (*orig_G1)(void*, void*) = nullptr;                 // get 0x4355298 (no key)
static void* hook_G1(void* a0, void* a1) {
    uintptr_t off = _retOff();
    void* r = orig_G1 ? orig_G1(a0, a1) : nullptr;
    if (g_inCredProc.load() && off >= 0x2575a40 && off <= 0x25766d0 && g_gN.fetch_add(1) < 80)
        BNM_FileCheckpointF("G1 off=%llx -> %s", (unsigned long long)off, r ? "ok" : "NULL");
    return r;
}
// segmentationData sub-field getters (run only after TGV('segmentationData')
// succeeds): 0x4359e88 (SA, first sub-read) and 0x435560c (SB, next three).
static void* (*orig_SA)(void*, void*, void*) = nullptr;
static void* hook_SA(void* a0, void* a1, void* a2) {
    uintptr_t off = _retOff();
    void* r = orig_SA ? orig_SA(a0, a1, a2) : nullptr;
    if (g_inCredProc.load() && off >= 0x2575a40 && off <= 0x25766d0 && g_gN.fetch_add(1) < 80) {
        std::string k = a1 ? _monoStr(a1) : std::string("(nullkey)");
        BNM_FileCheckpointF("SA off=%llx '%s' -> %s", (unsigned long long)off, k.c_str(), r ? "ok" : "NULL");
    }
    return r;
}
static void* (*orig_SB)(void*, void*, void*) = nullptr;
static void* hook_SB(void* a0, void* a1, void* a2) {
    uintptr_t off = _retOff();
    void* r = orig_SB ? orig_SB(a0, a1, a2) : nullptr;
    if (g_inCredProc.load() && off >= 0x2575a40 && off <= 0x25766d0 && g_gN.fetch_add(1) < 80) {
        std::string k = a1 ? _monoStr(a1) : std::string("(nullkey)");
        BNM_FileCheckpointF("SB off=%llx '%s' -> %s", (unsigned long long)off, k.c_str(), r ? "ok" : "NULL");
    }
    return r;
}
static void InstallFieldReadProbe() {
    void* exp = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_raise_exception");
    if (!exp) return;
    uint8_t* base = (uint8_t*)exp - 0x41FC69CULL;
    g_il2cppBase = (uintptr_t)base;
    // Only the account-parse-local gate predicates are hooked now. The hot
    // generic getters (0x4356550/0x435560c/...) were removed: inline-hooking
    // them destabilised the whole game and crashed it right as the
    // segmentationData sub-fields were read. GATE-A/GATE-B run only during the
    // account parse, so they are safe and still tell us if the parse passed.
    struct { uint64_t rva; void* hook; void** orig; } T[] = {
        {0x257570CULL, (void*)hook_GA,  (void**)&orig_GA},
        {0x256C0C8ULL, (void*)hook_GB,  (void**)&orig_GB},
        {0x3CDA1D0ULL, (void*)hook_INE, (void**)&orig_INE},
    };
    for (auto& t : T) {
        void* fn = base + t.rva; void* tr = nullptr;
        if (DobbyHook(fn, (dobby_dummy_func_t)t.hook, (dobby_dummy_func_t*)&tr) == 0) {
            *t.orig = tr; BNM_FileCheckpointF("[gate] hooked 0x%llx", (unsigned long long)t.rva);
        } else BNM_FileCheckpointF("[gate] FAILED 0x%llx", (unsigned long long)t.rva);
    }
}
// Hook the account-data string getter by RVA (il2cppBase + 0x1df5f90).
static void InstallCredGetterHook() {
    try {
        void* ans = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_array_new_specific");
        if (!ans) { BNM_FileCheckpoint("CredGetter: no il2cpp anchor"); return; }
        uint8_t* base = (uint8_t*)ans - 0xebdc94;
        void* target = base + 0x1df5f90;
        BasicHook(target, (void*)hook_credGetter, orig_credGetter);
        BNM_FileCheckpointF("CredGetter: hooked getter @ %p", target);
    } catch (...) { BNM_FileCheckpoint("CredGetter: EXCEPTION"); }
}

// ---------------------------------------------------------------------
//  GameAnalytics kill-switch. The baked-in GameAnalytics SDK
//  (GameAnalyticsSDK.GameAnalytics, Assembly-CSharp.dll) opens a local
//  ga.sqlite3, starts a session and ships events to api.gameanalytics.com
//  (AWS us-east-1) -> player telemetry leaks to a GA account we don't own.
//  We no-op the single entry point GameAnalytics.Initialize(): with no
//  init there is no session, no DB and no network submission, and every
//  event method (NewDesignEvent/NewProgressionEvent/...) bails internally
//  on the un-initialized state, so gameplay is unaffected.
// ---------------------------------------------------------------------
static void (*orig_GA_Initialize)(IL2CPP::MethodInfo*) = nullptr;
static void hook_GA_Initialize([[maybe_unused]] IL2CPP::MethodInfo* mi) {
    static std::atomic<bool> logged{false};
    if (!logged.exchange(true))
        BNM_FileCheckpoint("GA: Initialize() suppressed - no telemetry");
    // intentionally do nothing: GA never starts
}
static void InstallGameAnalyticsBlock() {
    try {
        Class ga("GameAnalyticsSDK", "GameAnalytics", Image("Assembly-CSharp.dll"));
        if (!ga.IsValid()) { BNM_FileCheckpoint("GA: class NOT FOUND"); return; }
        MethodBase m = ga.GetMethod("Initialize", 0);
        if (m.IsValid()) { BasicHook(m, hook_GA_Initialize, orig_GA_Initialize);
                           BNM_FileCheckpoint("GA: Initialize() hooked (telemetry disabled)"); }
        else BNM_FileCheckpoint("GA: Initialize(0) NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("GA: EXCEPTION installing block"); }
}

// ---------------------------------------------------------------------
//  Clean version label. ApplicationVersionDisplayUI.Start() builds the
//  on-screen string as  "v" + "{0}({1})"  (app version + a compatibility
//  version), e.g. "v1.19.0-blaze(1.19.0)". We let Start() run, then
//  overwrite versionText (field @ 0x18) with just Application.version
//  ("1.19.0-blaze") so the label reads exactly the manifest version.
// ---------------------------------------------------------------------
static void* (*p_gchandle_new)(void*, bool) = nullptr;   // il2cpp GC handles (Remote client)
static void* g_uiFontHandle = nullptr;    // pinned UI Font (from the version label)
static void (*orig_AVDU_Start)(void*, void*) = nullptr;
#ifdef PLAY_BUILD
static const char* kBigVersionLabel = "playyeeps";      // player-facing build name (playyeeps line)
#else
// Player-facing build name: "<game version>-blaze" from Application.version at runtime
// ("2.31.4" -> "2.31.4-blaze", "2.32.0_lab1" -> "2.32.0-blaze"), so one lib fits every base.
static std::string g_versionLabel;
static const char* LiveVersionLabel() {
    if (g_versionLabel.empty()) {
        try {
            Method<BNM::Structures::Mono::String*> gv(Class("UnityEngine", "Application", Image("UnityEngine.CoreModule.dll")).GetMethod("get_version", 0));
            std::string v = gv.IsValid() && gv() ? gv()->str() : "";
            size_t u = v.find('_'); if (u != std::string::npos) v = v.substr(0, u);
            if (!v.empty()) g_versionLabel = v + "-blaze";   // cache only a real read
        } catch (...) {}
    }
    return g_versionLabel.empty() ? "live-blaze" : g_versionLabel.c_str();
}
#define kBigVersionLabel LiveVersionLabel()
#endif
static void hook_AVDU_Start(void* self, void* mi) {
    if (orig_AVDU_Start) orig_AVDU_Start(self, mi);
    try {
        if (!self) return;
        void* txt = *(void**)((uint8_t*)self + 0x18);   // Text versionText
        if (!txt) return;
        Class app("UnityEngine", "Application", Image("UnityEngine.CoreModule.dll"));
        Method<BNM::Structures::Mono::String*> gv(app.GetMethod("get_version", 0));
        if (!gv.IsValid()) return;
        auto* ver = gv();
        if (!ver) return;
        Class txtc("UnityEngine.UI", "Text", Image("UnityEngine.UI.dll"));
        // Cache the game's UI font for server announcements (Remote client).
        if (!g_uiFontHandle && p_gchandle_new) {
            Method<Il2CppObject*> gf(txtc.GetMethod("get_font", 0));
            Il2CppObject* f = gf.IsValid() ? gf[(Il2CppObject*)txt]() : nullptr;
            if (f) g_uiFontHandle = p_gchandle_new(f, false);
        }
        Method<void> st(txtc.GetMethod("set_text", 1));
        if (!st.IsValid()) return;
        st.SetInstance((IL2CPP::Il2CppObject*)txt);
        st(CreateMonoString(kBigVersionLabel));   // big APK: owner's label instead of the version
    } catch (...) {}
}
// Other version texts (loading / error screens show "v2.4.0-blaze (2.4.0)"):
// any UI text starting with "v<version>" becomes the big APK's label.
static bool StartsWithVersion(void* str) {
    if (!str) return false;
    int len = *(int*)((uint8_t*)str + 0x10);
    const char16_t* c = (const char16_t*)((uint8_t*)str + 0x14);
    // "v" + the game's own version (Application.version, e.g. 2.31.4 / 2.32.0_lab1).
    static std::u16string pre;
    if (pre.empty()) {
        try {
            Method<BNM::Structures::Mono::String*> gv(Class("UnityEngine", "Application", Image("UnityEngine.CoreModule.dll")).GetMethod("get_version", 0));
            std::string v = gv.IsValid() && gv() ? gv()->str() : "";
            size_t u = v.find('_'); if (u != std::string::npos) v = v.substr(0, u);
            if (!v.empty()) { pre = u"v"; for (char ch : v) pre.push_back((char16_t)ch); }   // cache only a real read
        } catch (...) {}
        if (pre.empty()) return false;
    }
    const int n = (int)pre.size();
    if (len < n) return false;
    for (int i = 0; i < n; i++) if (c[i] != pre[i]) return false;
    return true;
}
// live83 (owner 2026-10-29): our Photon app is free tier (20 CCU). When the
// "Trouble connecting to multiplayer" box is caused by MaxCcuReached, show the
// owner's message instead of the connection log. hook_InternalLog sets the flag
// from the game's "[Network] Connect failed (MaxCcuReached)" lines.
static std::atomic<bool> g_maxCcu{false};
static const char* kMaxCcuTitle = "Server 1 is full!";
static const char* kMaxCcuBody = "20 player max per server!\n\n"
                                 "Get Server 2: live.your-domain.example/download/server2\n\n"
                                 "use /online in the discord to see how many players are on\n\n"
                                 "or try the 1.0 apk while you wait";
static bool MonoStrContains(void* str, const char16_t* needle) {
    if (!str) return false;
    int len = *(int*)((uint8_t*)str + 0x10);
    const char16_t* c = (const char16_t*)((uint8_t*)str + 0x14);
    int n = 0; while (needle[n]) n++;
    for (int i = 0; i + n <= len; i++) {
        int j = 0; while (j < n && c[i + j] == needle[j]) j++;
        if (j == n) return true;
    }
    return false;
}
static void* MaxCcuSwap(void* v) {
    if (!v) return v;
    if (MonoStrContains(v, u"Last error: MaxCcuReached")) { g_maxCcu = true; return CreateMonoString(kMaxCcuBody); }
    if (g_maxCcu && MonoStrContains(v, u"Trouble connecting to multiplayer")) return CreateMonoString(kMaxCcuTitle);
    return v;
}
static void (*orig_UIText_set)(void*, void*, void*) = nullptr;
static void hook_UIText_set(void* self, void* v, void* mi) {
    if (StartsWithVersion(v)) v = CreateMonoString(kBigVersionLabel);
    else v = MaxCcuSwap(v);
    orig_UIText_set(self, v, mi);
}
static void (*orig_TMP_set)(void*, void*, void*) = nullptr;
static void hook_TMP_set(void* self, void* v, void* mi) {
    if (StartsWithVersion(v)) v = CreateMonoString(kBigVersionLabel);
    else v = MaxCcuSwap(v);
    orig_TMP_set(self, v, mi);
}
static void InstallBigVersionTexts() {
    try {
        MethodBase a = Class("UnityEngine.UI", "Text", Image("UnityEngine.UI.dll")).GetMethod("set_text", 1);
        if (a.IsValid()) BasicHook(a, hook_UIText_set, orig_UIText_set);
        MethodBase b = Class("TMPro", "TMP_Text", Image("Unity.TextMeshPro.dll")).GetMethod("set_text", 1);
        if (b.IsValid()) BasicHook(b, hook_TMP_set, orig_TMP_set);
        BNM_FileCheckpointF("VerLabel: big texts hooked ui=%d tmp=%d", a.IsValid(), b.IsValid());
    } catch (...) { BNM_FileCheckpoint("VerLabel: big texts EXCEPTION"); }
}
static void InstallVersionLabelClean() {
    try {
        Class c("", "ApplicationVersionDisplayUI", Image("Assembly-CSharp.dll"));
        if (!c.IsValid()) { BNM_FileCheckpoint("VerLabel: class NOT FOUND"); return; }
        MethodBase m = c.GetMethod("Start", 0);
        if (m.IsValid()) { BasicHook(m, hook_AVDU_Start, orig_AVDU_Start);
                           BNM_FileCheckpoint("VerLabel: Start() hooked (clean version)"); }
        else BNM_FileCheckpoint("VerLabel: Start(0) NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("VerLabel: EXCEPTION"); }
}

// ---------------------------------------------------------------------
//  Backend redirect. In 1.19.0 the API base URL was plaintext in
//  global-metadata.dat and redirected by an in-place string patch. In
//  1.31.0 it is decrypted at runtime, so we instead hook the name-stable
//  framework entry points every HTTP request funnels through, LOG the full
//  (decrypted) URL for diagnosis, and rewrite the Yeeps execute-api gateway
//  host to the self-hosted server. The server strips the /xxxxxxxxxxxxxxxxxx
//  prefix (main.py strip_apk_prefix) and routes normally, exactly as the
//  1.19.0-blaze client's patched URL did.
// ---------------------------------------------------------------------
// ---------------------------------------------------------------------
//  Build identity + integrity tag. Computed once at startup from the
//  native libraries actually loaded on the headset and sent to the server
//  in the path of every API-gateway request (login etc.) as
//  /c/<kBuildId>.<fnv64(libil2cpp.so)>.<fnv64(libBNMExplorer.so)>/
//  The server records it per account and blocks wrong/old/modified builds.
//  Bump kBuildId for every APK you distribute.
// ---------------------------------------------------------------------
#ifdef PLAY_BUILD
static const char* kBuildId = "play3";       // playyeeps line: clean stock 2.31.4 -> play.your-domain.example
#elif defined(BNM_LIVE_BASE)
static const char* kBuildId = "live92";       // live92 = live91 + Yeeps TV: 1x1 computer screens play the /client/tv.txt channel with sound from the nearest computer (owner 2026-11-01). live91 = live90 + server2 voice FIXED: the data voice id maps to the build's own voice app (server2 voice was being forced onto its realtime app) (owner 2026-10-31). live90 = live89 + voice retry FIXED (watch every VoiceFollowClient, re-enable AutoConnectAndJoin every 30 s; the flip happens in OnVoiceStateChanged, so live88/89 never saw it) (owner 2026-10-31). live89 = live88 + error popups off (Application.CallLogCallback drops Error/Assert/Exception; devlog keeps them) + GameAnalytics nag silenced (owner 2026-10-31). live88 = live87 + voice auto-retry after MaxCcu (VoiceFollowClient) + MaxCcu box points to the other server (owner 2026-10-31). live87 = live86 + MontagemDomer disk (replaces No: data_live87 + disks87, owner 2026-10-30). live86 = live85 + Orbit disk (replaces guh: data_live86 + disks86) + mod repo link renamed (owner 2026-10-30). live85 = live85 = live84 + mod update hook only ticks for the local MasterPlayer (Player.LateUpdate is shared with remote PuppetPlayers). live84 = live83 + player mod loader (mods folder .so via YeepsModApi, owner 2026-10-29). live83 = live82 + "Max players reached!" text on the MaxCcuReached multiplayer error box (owner 2026-10-29). live82 = live81 minus Jeff Gnome + review button handler hooked -> localhost:8080 mod menu; (owner 2026-10-28: removed, games not loading). live81 = live80 + Jeff Gnome cosmetic (jeffgnome.bundle on turkey head/body/arms + firefighter backpack). live80 = live79 + Lost Chocolate disk (FluffyPillow clip renamed LostChocolate, plays Sugary Spire "Lost Chocolate", owner's label art; key stays record_fluffyPillow): data_live80.unity3d + sharedassets2_disks80. live79 notes: 2.32.0 PUBLIC base (Downloads/Yeeps-2.32.0-public-957.apk, il2cpp 367ca975, versionCode 957; name map V4) + data RE-BAKED on 957's OWN data (data_live79.unity3d: photon, blocks+cat, label, branding/TOS, BLAZE screen, FrostOverYeti cosmetic, trading cards, 5 disks incl Hackers rename) + sharedassets2_disks957 (957 sa2 + disk FSBs) + runtime version label. [live78 reused 949 data -> CRASHED; this re-bakes on 957 data]
#else
static const char* kBuildId = "big51";
#endif
static std::string g_buildTag = std::string(kBuildId) + ".pending.pending";

static uint64_t Fnv1aFile(const char* path) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return 0;
    uint64_t h = 1469598103934665603ULL;
    std::vector<uint8_t> buf(1 << 20);
    ssize_t n;
    while ((n = read(fd, buf.data(), buf.size())) > 0)
        for (ssize_t i = 0; i < n; i++) { h ^= buf[i]; h *= 1099511628211ULL; }
    close(fd);
    return h;
}
struct LibPaths { std::string il2cpp, bnm; };
static int FindLibsCb(struct dl_phdr_info* info, size_t, void* data) {
    auto* lp = (LibPaths*)data;
    if (!info->dlpi_name) return 0;
    std::string n = info->dlpi_name;
    if (n.size() >= 12 && n.compare(n.size() - 12, 12, "libil2cpp.so") == 0) lp->il2cpp = n;
    if (n.size() >= 17 && n.compare(n.size() - 17, 17, "libBNMExplorer.so") == 0) lp->bnm = n;
    return 0;
}
static void ComputeBuildTag() {
    LibPaths lp;
    dl_iterate_phdr(FindLibsCb, &lp);
    char tag[96];
    snprintf(tag, sizeof(tag), "%s.%016llx.%016llx", kBuildId,
             (unsigned long long)(lp.il2cpp.empty() ? 0 : Fnv1aFile(lp.il2cpp.c_str())),
             (unsigned long long)(lp.bnm.empty() ? 0 : Fnv1aFile(lp.bnm.c_str())));
    g_buildTag = tag;
    BNM_FileCheckpointF("BuildTag: %s", tag);
}

// ---------------------------------------------------------------------
//  Headset device key (131b14+). The game API has no player login of its
//  own, so every API URL carries /k/<key>/ and the server ties each account
//  to the headset that owns it (vr-server services/device_auth.py).
//  key = sha256("blaze-device-v1|" + Android ID): ANDROID_ID is per device +
//  per signing key, survives reinstalls, and is never sent raw. If it can't
//  be read, a random key is kept in the app's files dir instead.
// ---------------------------------------------------------------------
static JavaVM* g_vm = nullptr;
static std::string g_deviceKey;

namespace sha256mini {
static const uint32_t K[64] = {
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
static inline uint32_t rotr(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }
static std::string hex(const std::string& msg) {
    uint32_t h[8] = {0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    std::vector<uint8_t> m(msg.begin(), msg.end());
    uint64_t bits = (uint64_t)m.size() * 8;
    m.push_back(0x80);
    while (m.size() % 64 != 56) m.push_back(0);
    for (int i = 7; i >= 0; i--) m.push_back((uint8_t)(bits >> (i * 8)));
    for (size_t off = 0; off < m.size(); off += 64) {
        uint32_t w[64];
        for (int i = 0; i < 16; i++)
            w[i] = (uint32_t)m[off + 4*i] << 24 | (uint32_t)m[off + 4*i + 1] << 16 |
                   (uint32_t)m[off + 4*i + 2] << 8 | (uint32_t)m[off + 4*i + 3];
        for (int i = 16; i < 64; i++) {
            uint32_t s0 = rotr(w[i-15], 7) ^ rotr(w[i-15], 18) ^ (w[i-15] >> 3);
            uint32_t s1 = rotr(w[i-2], 17) ^ rotr(w[i-2], 19) ^ (w[i-2] >> 10);
            w[i] = w[i-16] + s0 + w[i-7] + s1;
        }
        uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
        for (int i = 0; i < 64; i++) {
            uint32_t S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
            uint32_t ch = (e & f) ^ (~e & g);
            uint32_t t1 = hh + S1 + ch + K[i] + w[i];
            uint32_t S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
            uint32_t mj = (a & b) ^ (a & c) ^ (b & c);
            uint32_t t2 = S0 + mj;
            hh = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
        }
        h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
    }
    char out[65];
    for (int i = 0; i < 8; i++) snprintf(out + i * 8, 9, "%08x", h[i]);
    return std::string(out, 64);
}
}  // namespace sha256mini

static std::string ReadAndroidId() {
    if (!g_vm) return "";
    JNIEnv* env = nullptr;
    bool attached = false;
    if (g_vm->GetEnv((void**)&env, JNI_VERSION_1_6) != JNI_OK) {
        if (g_vm->AttachCurrentThread(&env, nullptr) != JNI_OK) return "";
        attached = true;
    }
    std::string id;
    auto clear = [&]() { if (env->ExceptionCheck()) { env->ExceptionClear(); return true; } return false; };
    do {
        jclass at = env->FindClass("android/app/ActivityThread");
        if (clear() || !at) break;
        jmethodID cur = env->GetStaticMethodID(at, "currentApplication", "()Landroid/app/Application;");
        if (clear() || !cur) break;
        jobject app = env->CallStaticObjectMethod(at, cur);
        if (clear() || !app) break;
        jclass ctx = env->FindClass("android/content/Context");
        if (clear() || !ctx) break;
        jmethodID gcr = env->GetMethodID(ctx, "getContentResolver", "()Landroid/content/ContentResolver;");
        if (clear() || !gcr) break;
        jobject cr = env->CallObjectMethod(app, gcr);
        if (clear() || !cr) break;
        jclass sec = env->FindClass("android/provider/Settings$Secure");
        if (clear() || !sec) break;
        jmethodID gs = env->GetStaticMethodID(sec, "getString",
                           "(Landroid/content/ContentResolver;Ljava/lang/String;)Ljava/lang/String;");
        if (clear() || !gs) break;
        jstring name = env->NewStringUTF("android_id");
        auto js = (jstring)env->CallStaticObjectMethod(sec, gs, cr, name);
        if (clear() || !js) break;
        const char* c = env->GetStringUTFChars(js, nullptr);
        if (c) { id = c; env->ReleaseStringUTFChars(js, c); }
    } while (false);
    if (attached) g_vm->DetachCurrentThread();
    return id;
}

static std::string RandomKeyFile() {
    // fallback: a random key kept in /data/data/<package>/files
    char pkg[128] = {0};
    int fd = open("/proc/self/cmdline", O_RDONLY);
    if (fd >= 0) { read(fd, pkg, sizeof(pkg) - 1); close(fd); }
    std::string path = std::string("/data/data/") + pkg + "/files/blaze_device_key";
    std::string key;
    fd = open(path.c_str(), O_RDONLY);
    if (fd >= 0) {
        char buf[65] = {0};
        if (read(fd, buf, 64) == 64) key.assign(buf, 64);
        close(fd);
    }
    if (key.size() == 64) return key;
    uint8_t rnd[32] = {0};
    fd = open("/dev/urandom", O_RDONLY);
    if (fd >= 0) { read(fd, rnd, sizeof(rnd)); close(fd); }
    key = sha256mini::hex(std::string((const char*)rnd, sizeof(rnd)));
    fd = open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd >= 0) { write(fd, key.data(), key.size()); close(fd); }
    return key;
}

static void ComputeDeviceKey() {
    std::string aid = ReadAndroidId();
    if (!aid.empty()) {
        g_deviceKey = sha256mini::hex("blaze-device-v1|" + aid);
        BNM_FileCheckpoint("DeviceKey: from android id");
    } else {
        g_deviceKey = RandomKeyFile();
        BNM_FileCheckpoint("DeviceKey: random (android id unavailable)");
    }
}

#ifdef PLAY_BUILD
static const char* kSelfBase = "https://play.your-domain.example/xxxxxxxxxxxxxxxxxx";
#else
static const char* kSelfBase = "https://live.your-domain.example/xxxxxxxxxxxxxxxxxx";
#endif
static bool RewriteBackendUrl(const std::string& in, std::string& out) {
    // Yeeps uses THREE AWS backends, all of which must go to our proxy:
    //   *.execute-api.*.amazonaws.com  -> API Gateway (login/account)
    //   lambda.*.amazonaws.com         -> direct Lambda g2_* calls (room/game data)
    //   dynamodb.*.amazonaws.com       -> direct DynamoDB (room map nodes, etc.)
    // Asset CDNs (s3/cloudfront) are left alone so real game assets still load.
    auto sch = in.find("://");
    if (sch == std::string::npos) return false;
    size_t hostStart = sch + 3;
    size_t pathStart = in.find('/', hostStart);
    std::string host = (pathStart == std::string::npos)
                     ? in.substr(hostStart) : in.substr(hostStart, pathStart - hostStart);
    std::string path = (pathStart == std::string::npos) ? "/" : in.substr(pathStart);
    // Live 2.31.4 routes its game API (questLogIntoAccount, g2_*, ...) through a
    // CloudFront distribution instead of execute-api; it must be redirected too,
    // or login hits the real backend (and leaks to Yeeps). Real asset CDNs are
    // other hosts and are left alone.
    bool apiCdn = (host == "d21p0qgqisid3s.cloudfront.net");
    // Any other Yeeps / Trass web host the game might call (none seen so far, but the
    // metadata names yeeps.com and yeeps.junipercreates.com): never let it reach them.
    if (host.find("junipercreates") != std::string::npos || host.find("trassgames") != std::string::npos
        || host == "yeeps.com" || (host.size() > 10 && host.compare(host.size() - 10, 10, ".yeeps.com") == 0))
        apiCdn = true;
    bool aws = host.find(".amazonaws.com") != std::string::npos;
    if (!aws && !apiCdn) return false;
    bool match = apiCdn
              || (host.find(".execute-api.") != std::string::npos)
              || (host.rfind("lambda.", 0) == 0)
              || (host.rfind("dynamodb.", 0) == 0);
    if (!match) return false;
    // API-gateway URLs (login etc.) are rewritten whole, so they carry the
    // build tag. Lambda/DynamoDB are host-only SDK endpoints the SDK builds
    // paths onto; leave those untagged.
    std::string base = std::string(kSelfBase);
    if (g_deviceKey.size() == 64) base += "/k/" + g_deviceKey;   // who's asking (device_auth.py)
    if (apiCdn || host.find(".execute-api.") != std::string::npos)
        out = base + "/c/" + g_buildTag + path;
    else
        out = base + path;   // keep path + query
    return true;
}
static std::atomic<int> g_urlLogN{0};
// Leak watch (biglive20): every outside web host the game talks to is logged once
// ("[url-ext]"), with no cap, so a call that still goes to Yeeps/Trass/a third party
// shows up in the remote log however late in the session it happens.
static std::mutex g_extHostMtx;
static std::set<std::string> g_extHosts;
static void LogExternalHost(const std::string& u, const char* tag) {
    auto sch = u.find("://");
    if (sch == std::string::npos) return;
    std::string scheme = u.substr(0, sch);
    if (scheme != "http" && scheme != "https" && scheme != "ws" && scheme != "wss") return;
    size_t hs = sch + 3, pe = u.find_first_of("/?#", hs);
    std::string host = u.substr(hs, pe == std::string::npos ? std::string::npos : pe - hs);
    if (host.empty() || host == "localhost" || host.find("your-domain.example") != std::string::npos) return;
    {
        std::lock_guard<std::mutex> lk(g_extHostMtx);
        if (g_extHosts.size() > 200 || !g_extHosts.insert(host).second) return;
    }
    BNM_FileCheckpointF("[url-ext] %s %s", tag, u.substr(0, 160).c_str());
}
static void* MaybeRewrite(void* urlObj, [[maybe_unused]] const char* tag) {
    try {
        if (!urlObj) return urlObj;
        std::string u = ((BNM::Structures::Mono::String*)urlObj)->str();
        std::string nu;
        bool rw = RewriteBackendUrl(u, nu);
        // 2.4.0 diagnosis: log the first 300 URLs (rewritten or not).
        if (g_urlLogN.fetch_add(1) < 300)
            BNM_FileCheckpointF("[url] %s %s%s%s", tag, u.substr(0, 180).c_str(),
                                rw ? " -> " : "", rw ? nu.substr(0, 120).c_str() : "");
        if (rw) return CreateMonoString(nu.c_str());
        LogExternalHost(u, tag);
    } catch (...) {}
    return urlObj;
}
static void (*orig_UWR_seturl)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void hook_UWR_seturl(void* self, void* url, IL2CPP::MethodInfo* mi) {
    void* pass = MaybeRewrite(url, "UWR.set_url");
    if (orig_UWR_seturl) orig_UWR_seturl(self, pass, mi);
}
static void (*orig_Uri_ctor)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void hook_Uri_ctor(void* self, void* uriStr, IL2CPP::MethodInfo* mi) {
    void* pass = MaybeRewrite(uriStr, "Uri.ctor");
    if (orig_Uri_ctor) orig_Uri_ctor(self, pass, mi);
}
static void (*orig_Uri_ctorKind)(void*, void*, int32_t, IL2CPP::MethodInfo*) = nullptr;
static void hook_Uri_ctorKind(void* self, void* uriStr, int32_t kind, IL2CPP::MethodInfo* mi) {
    void* pass = MaybeRewrite(uriStr, "Uri.ctor2");
    if (orig_Uri_ctorKind) orig_Uri_ctorKind(self, pass, kind, mi);
}
static intptr_t (*orig_UWR_internalSetUrl)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static intptr_t hook_UWR_internalSetUrl(void* self, void* url, IL2CPP::MethodInfo* mi) {
    void* pass = MaybeRewrite(url, "UWR.InternalSetUrl");
    return orig_UWR_internalSetUrl ? orig_UWR_internalSetUrl(self, pass, mi) : 0;
}
// ---------------------------------------------------------------------
//  Photon room joins carry the headset key too (131b15+). Photon Cloud
//  calls our custom-auth URL with the client's AuthGetParameters, so we add
//  dk=<device key> right before the client authenticates; the server then
//  refuses a join that claims someone else's account (device_auth.py).
// ---------------------------------------------------------------------
static void AddDeviceKeyToAuth(void* authValues) {
    try {
        if (!authValues || g_deviceKey.size() != 64) return;
        Class av("Photon.Realtime", "AuthenticationValues");
        Method<BNM::Structures::Mono::String*> getP(av.GetMethod("get_AuthGetParameters", 0));
        Method<void> setP(av.GetMethod("set_AuthGetParameters", 1));
        if (!getP.IsValid() || !setP.IsValid()) return;
        auto* cur = getP[(Il2CppObject*)authValues]();
        std::string q = cur ? cur->str() : "";
        if (q.find("dk=") != std::string::npos) return;
        if (!q.empty()) q += "&";
        q += "dk=" + g_deviceKey;
        setP[(Il2CppObject*)authValues](CreateMonoString(q.c_str()));
    } catch (...) {}
}
// Live 2.31.4 base: its PhotonServerSettings carries Yeeps' own Photon AppId, so
// players would land on Yeeps' Photon cloud (never seeing ours, and talking to
// Yeeps). Force our app -- the one the 2.4 build ships and whose custom auth
// calls our server.
static const char* kOurPhotonAppId = "00000000-0000-0000-0000-000000000005";
// Our VOICE app (PhotonServerSettings AppIdVoice, set by tools/data_photon.py). live46:
// it was being forced onto the realtime app too, so every player used 2 of the realtime
// app's 20 CCU (12 players = "Max CCU of 20 reached"). Our own ids now pass through.
static const char* kOurVoiceAppId = "00000000-0000-0000-0000-000000000002";
// The voice id BAKED IN THE DATA (PhotonServerSettings AppIdVoice, tools/data_photon.py).
// NOT swapped by build_server2.sh. live91 (owner 2026-10-31): the server2 build swaps
// kOurVoiceAppId to its own voice app, but the game still reads THIS id from the data;
// it matched neither of server2's ids and was forced onto the REALTIME app, so server2
// voice never used its voice app (and each player took 2 of the realtime app's 20 CCU).
static const char* kDataVoiceAppId = "00000000-0000-0000-0000-000000000002";
static void* OurAppId(void* appId) {
#ifdef BNM_LIVE_BASE
    try {
        std::string was = appId ? ((BNM::Structures::Mono::String*)appId)->str() : "";
        if (was == kDataVoiceAppId && was != kOurVoiceAppId) {        // server2: data voice id -> our voice app
            static std::atomic<int> vN{0};
            if (vN.fetch_add(1) < 4) BNM_FileCheckpointF("[auth] voice AppId %s -> %.8s", was.substr(0, 8).c_str(), kOurVoiceAppId);
            return CreateMonoString(kOurVoiceAppId);
        }
        if (was == kOurPhotonAppId || was == kOurVoiceAppId) {
            static std::atomic<int> keptN{0};
            if (keptN.fetch_add(1) < 4) BNM_FileCheckpointF("[auth] Photon AppId %s kept (ours)", was.substr(0, 8).c_str());
            return appId;
        }
        static std::atomic<int> logN{0};
        if (logN.fetch_add(1) < 4) BNM_FileCheckpointF("[auth] Photon AppId %s -> ours", was.substr(0, 8).c_str());
        return CreateMonoString(kOurPhotonAppId);
    } catch (...) {}
#endif
    return appId;
}
static bool (*orig_OpAuth)(void*, void*, void*, void*, void*, bool, IL2CPP::MethodInfo*) = nullptr;
static bool hook_OpAuth(void* self, void* appId, void* appVer, void* av, void* region, bool stats, IL2CPP::MethodInfo* mi) {
    AddDeviceKeyToAuth(av);
    return orig_OpAuth ? orig_OpAuth(self, OurAppId(appId), appVer, av, region, stats, mi) : false;
}
static bool (*orig_OpAuthOnce)(void*, void*, void*, void*, void*, int32_t, int32_t, IL2CPP::MethodInfo*) = nullptr;
static bool hook_OpAuthOnce(void* self, void* appId, void* appVer, void* av, void* region, int32_t enc, int32_t proto, IL2CPP::MethodInfo* mi) {
    AddDeviceKeyToAuth(av);
    return orig_OpAuthOnce ? orig_OpAuthOnce(self, OurAppId(appId), appVer, av, region, enc, proto, mi) : false;
}
static void InstallPhotonAuthKey() {
    try {
        Class lbp("Photon.Realtime", "LoadBalancingPeer");
        if (!lbp.IsValid()) { BNM_FileCheckpoint("[auth] LoadBalancingPeer NOT FOUND"); return; }
        MethodBase a = lbp.GetMethod("OpAuthenticate", 5);
        if (a.IsValid()) { BasicHook(a, hook_OpAuth, orig_OpAuth); BNM_FileCheckpoint("[auth] OpAuthenticate hooked"); }
        else BNM_FileCheckpoint("[auth] OpAuthenticate(5) NOT FOUND");
        MethodBase b = lbp.GetMethod("OpAuthenticateOnce", 6);
        if (b.IsValid()) { BasicHook(b, hook_OpAuthOnce, orig_OpAuthOnce); BNM_FileCheckpoint("[auth] OpAuthenticateOnce hooked"); }
        else BNM_FileCheckpoint("[auth] OpAuthenticateOnce(6) NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("[auth] EXC"); }
}

static MethodBase FindOverload(Class c, const char* name, std::vector<std::string> types);
static void InstallBackendRedirect() {
    try {
        Class uwr("UnityEngine.Networking", "UnityWebRequest");
        if (uwr.IsValid()) {
            MethodBase m = uwr.GetMethod("set_url", 1);
            if (m.IsValid()) { BasicHook(m, hook_UWR_seturl, orig_UWR_seturl);
                               BNM_FileCheckpoint("[net] UnityWebRequest.set_url hooked"); }
            else BNM_FileCheckpoint("[net] UWR.set_url(1) NOT FOUND");
            // set_url AND set_uri both end in InternalSetUrl: one place no web request skips
            // (biglive20: fetchGlobalFatalError was still reaching real Yeeps).
            MethodBase isu = FindOverload(uwr, "InternalSetUrl", {"System.String"});
            if (isu.IsValid()) { BasicHook(isu, hook_UWR_internalSetUrl, orig_UWR_internalSetUrl);
                                 BNM_FileCheckpoint("[net] UnityWebRequest.InternalSetUrl hooked"); }
            else BNM_FileCheckpoint("[net] UWR.InternalSetUrl(string) NOT FOUND");
        } else BNM_FileCheckpoint("[net] UnityWebRequest class NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("[net] EXC UWR"); }
    try {
        Class uri("System", "Uri");
        MethodBase k = uri.IsValid() ? FindOverload(uri, ".ctor", {"System.String", "System.UriKind"}) : MethodBase();
        if (k.IsValid()) { BasicHook(k, hook_Uri_ctorKind, orig_Uri_ctorKind);
                           BNM_FileCheckpoint("[net] System.Uri.ctor(string, UriKind) hooked"); }
        else BNM_FileCheckpoint("[net] Uri.ctor(string, UriKind) NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("[net] EXC Uri2"); }
    try {
        Class uri("System", "Uri");
        if (uri.IsValid()) {
            MethodBase m = uri.GetMethod(".ctor", 1);
            if (m.IsValid()) { BasicHook(m, hook_Uri_ctor, orig_Uri_ctor);
                               BNM_FileCheckpoint("[net] System.Uri.ctor(1) hooked"); }
            else BNM_FileCheckpoint("[net] Uri.ctor(1) NOT FOUND");
        } else BNM_FileCheckpoint("[net] System.Uri class NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("[net] EXC Uri"); }
}

// ---------------------------------------------------------------------
//  DIAGNOSTIC: LitJson ReadValue trace. The 1.31 account parse uses
//  ThirdParty.Json.LitJson.JsonMapper (AWSSDK.Core.dll). ReadValue(Type,
//  JsonReader) is the recursive workhorse that converts each JSON value to
//  the target member type and throws on a type mismatch. We log the target
//  type FullName of every ReadValue to logcat (tag JSONRV); the LAST one
//  logged right before "Failed to parse account data" is the field whose
//  value type the 1.19-shape server response doesn't satisfy for 1.31.
// ---------------------------------------------------------------------
static std::atomic<int> g_rvN{0};
static void* (*orig_JM_ReadValue)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void* hook_JM_ReadValue(void* typeObj, void* reader, IL2CPP::MethodInfo* mi) {
    int n = ++g_rvN;
    try {
        if (typeObj) {
            Method<BNM::Structures::Mono::String*> gfn(Class("System","Type").GetMethod("get_FullName", 0));
            if (gfn.IsValid()) {
                auto* s = gfn[(IL2CPP::Il2CppObject*)typeObj]();
                __android_log_print(ANDROID_LOG_ERROR, "JSONRV", "RV#%d %s", n, s ? s->str().c_str() : "?");
            }
        }
    } catch (...) {}
    return orig_JM_ReadValue ? orig_JM_ReadValue(typeObj, reader, mi) : nullptr;
}
static void InstallReadValueTrace() {
    try {
        Class jm("ThirdParty.Json.LitJson", "JsonMapper", Image("AWSSDK.Core.dll"));
        if (!jm.IsValid()) { BNM_FileCheckpoint("[json] JsonMapper class NOT FOUND"); return; }
        MethodBase m = jm.GetMethod("ReadValue", 2);
        if (m.IsValid()) { BasicHook(m, hook_JM_ReadValue, orig_JM_ReadValue);
                           BNM_FileCheckpoint("[json] JsonMapper.ReadValue(2) traced"); }
        else BNM_FileCheckpoint("[json] ReadValue(2) NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("[json] EXC ReadValue"); }
}

// ---------------------------------------------------------------------
//  DIAGNOSTIC: trace string->number/bool Parse(String). The 1.19-shape
//  server response carries many empty-string fields (banExpireTimestamp,
//  oculusNumber, loginStreakPartition, timestamps). If 1.31 parses one of
//  these as a number, Parse("") throws FormatException -> the swallowed
//  "Failed to parse account data". We log every Parse arg (tag PARSE); the
//  LAST value before the fatal pinpoints the failing field's value.
// ---------------------------------------------------------------------
static int32_t (*orig_i32Parse)(void*, IL2CPP::MethodInfo*) = nullptr;
static int32_t hook_i32Parse(void* s, IL2CPP::MethodInfo* mi) {
    try { __android_log_print(ANDROID_LOG_ERROR, "PARSE", "Int32 '%s'", s ? ((BNM::Structures::Mono::String*)s)->str().c_str() : "(null)"); } catch(...){}
    return orig_i32Parse ? orig_i32Parse(s, mi) : 0;
}
static int64_t (*orig_i64Parse)(void*, IL2CPP::MethodInfo*) = nullptr;
static int64_t hook_i64Parse(void* s, IL2CPP::MethodInfo* mi) {
    try { __android_log_print(ANDROID_LOG_ERROR, "PARSE", "Int64 '%s'", s ? ((BNM::Structures::Mono::String*)s)->str().c_str() : "(null)"); } catch(...){}
    return orig_i64Parse ? orig_i64Parse(s, mi) : 0;
}
static float (*orig_f32Parse)(void*, IL2CPP::MethodInfo*) = nullptr;
static float hook_f32Parse(void* s, IL2CPP::MethodInfo* mi) {
    try { __android_log_print(ANDROID_LOG_ERROR, "PARSE", "Single '%s'", s ? ((BNM::Structures::Mono::String*)s)->str().c_str() : "(null)"); } catch(...){}
    return orig_f32Parse ? orig_f32Parse(s, mi) : 0;
}
static double (*orig_f64Parse)(void*, IL2CPP::MethodInfo*) = nullptr;
static double hook_f64Parse(void* s, IL2CPP::MethodInfo* mi) {
    try { __android_log_print(ANDROID_LOG_ERROR, "PARSE", "Double '%s'", s ? ((BNM::Structures::Mono::String*)s)->str().c_str() : "(null)"); } catch(...){}
    return orig_f64Parse ? orig_f64Parse(s, mi) : 0;
}
static bool (*orig_boolParse)(void*, IL2CPP::MethodInfo*) = nullptr;
static bool hook_boolParse(void* s, IL2CPP::MethodInfo* mi) {
    try { __android_log_print(ANDROID_LOG_ERROR, "PARSE", "Bool '%s'", s ? ((BNM::Structures::Mono::String*)s)->str().c_str() : "(null)"); } catch(...){}
    return orig_boolParse ? orig_boolParse(s, mi) : false;
}
static void InstallParseTrace() {
    struct { const char* ns; const char* cls; void* hook; void** orig; } T[] = {
        {"System","Int32",  (void*)hook_i32Parse,  (void**)&orig_i32Parse},
        {"System","Int64",  (void*)hook_i64Parse,  (void**)&orig_i64Parse},
        {"System","Single", (void*)hook_f32Parse,  (void**)&orig_f32Parse},
        {"System","Double", (void*)hook_f64Parse,  (void**)&orig_f64Parse},
        {"System","Boolean",(void*)hook_boolParse, (void**)&orig_boolParse},
    };
    for (auto& t : T) {
        try {
            Class c(t.ns, t.cls);
            if (!c.IsValid()) { BNM_FileCheckpointF("[parse] %s class NOT FOUND", t.cls); continue; }
            MethodBase m = c.GetMethod("Parse", 1);
            if (m.IsValid()) { BasicHook(m, t.hook, *t.orig);
                               BNM_FileCheckpointF("[parse] %s.Parse(1) hooked", t.cls); }
            else BNM_FileCheckpointF("[parse] %s.Parse(1) NOT FOUND", t.cls);
        } catch (...) { BNM_FileCheckpointF("[parse] EXC %s", t.cls); }
    }
}

// ---------------------------------------------------------------------
//  DIAGNOSTIC: intercept il2cpp_raise_exception. Parser-agnostic — catches
//  the real exception (type + message) the account parse throws before the
//  game swallows it into "Failed to parse account data". The RAISE line
//  logged right before the fatal names the failing type/value (e.g. an
//  InvalidCastException or FormatException on a specific field value).
// ---------------------------------------------------------------------
// The public export il2cpp_raise_exception (file vaddr 0x41FC69C in this 1.31.0
// libil2cpp.so) is a thunk that tail-calls the INTERNAL throw routine at vaddr
// 0xFBDFFC — the function managed `throw` statements actually call. Managed
// exceptions bypass the export, so we hook the internal routine instead,
// addressing it relative to the export's resolved runtime address.
static constexpr uintptr_t IL2CPP_RAISE_EXPORT_VADDR   = 0x41FC69CULL;
static constexpr uintptr_t IL2CPP_RAISE_INTERNAL_VADDR = 0xFBDFFCULL;
static thread_local bool g_inRaise = false;
static void (*orig_internal_raise)(void*, void*) = nullptr;
static void hook_internal_raise(void* ex, void* a1) {
    if (!g_inRaise) {
        g_inRaise = true;
        try {
            if (ex) {
                typedef const char* (*clsname_t)(void*);
                typedef void* (*objclass_t)(void*);
                static auto getName  = (clsname_t)dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_class_get_name");
                static auto getClass = (objclass_t)dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_object_get_class");
                const char* cn = (getName && getClass) ? getName(getClass(ex)) : nullptr;
                std::string msg = "?";
                try {
                    auto* o = (IL2CPP::Il2CppObject*)ex;
                    Class ec(o->klass);
                    if (ec.IsValid()) {
                        auto gm = ec.GetMethod("get_Message", 0);
                        if (gm.IsValid()) { Method<BNM::Structures::Mono::String*> m(gm); auto* s = m[o](); if (s) msg = s->str(); }
                    }
                } catch (...) {}
                __android_log_print(ANDROID_LOG_ERROR, "RAISE", "%s: %s", cn ? cn : "?", msg.c_str());
            }
        } catch (...) {}
        g_inRaise = false;   // reset BEFORE orig: Raise unwinds and never returns
    }
    if (orig_internal_raise) orig_internal_raise(ex, a1);
}
static void InstallRaiseTrace() {
    void* exp = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_raise_exception");
    if (!exp) { BNM_FileCheckpoint("[raise] export NOT FOUND"); return; }
    uintptr_t base = (uintptr_t)exp - IL2CPP_RAISE_EXPORT_VADDR;
    void* internalRaise = (void*)(base + IL2CPP_RAISE_INTERNAL_VADDR);
    void* tramp = nullptr;
    if (DobbyHook(internalRaise, (dobby_dummy_func_t)hook_internal_raise, (dobby_dummy_func_t*)&tramp) != 0) {
        BNM_FileCheckpointF("[raise] DobbyHook FAILED @ %p", internalRaise); return;
    }
    orig_internal_raise = (void(*)(void*, void*))tramp;
    BNM_FileCheckpointF("[raise] internal Raise hooked @ %p (base=%p)", internalRaise, (void*)base);
}

// ---------------------------------------------------------------------
//  CAPTURE: dump large HTTP response bodies (the account JSON is ~30KB) to a
//  file, so the REAL 1.31 backend's questLogIntoAccount response can be read
//  off-device and diffed against the self-hosted server's response.
// ---------------------------------------------------------------------
static void DumpRespFile(const std::string& s) {
    FILE* f = fopen("/sdcard/Android/data/com.TrassGames.Yeeps/files/resp_dump.txt", "a");
    if (!f) f = fopen("/data/local/tmp/resp_dump.txt", "a");
    if (f) { fprintf(f, "\n===RESP len=%zu===\n%s\n", s.size(), s.c_str()); fclose(f); }
}
static void* (*orig_DH_get_text)(void*, IL2CPP::MethodInfo*) = nullptr;
static void* hook_DH_get_text(void* self, IL2CPP::MethodInfo* mi) {
    void* ret = orig_DH_get_text ? orig_DH_get_text(self, mi) : nullptr;
    try {
        if (ret) {
            std::string s = ((BNM::Structures::Mono::String*)ret)->str();
            if (s.size() > 1500) DumpRespFile(s);
        }
    } catch (...) {}
    return ret;
}
static void InstallRespCapture() {
    try {
        Class dh("UnityEngine.Networking", "DownloadHandler");
        if (!dh.IsValid()) { BNM_FileCheckpoint("[resp] DownloadHandler NOT FOUND"); return; }
        MethodBase m = dh.GetMethod("get_text", 0);
        if (m.IsValid()) { BasicHook(m, hook_DH_get_text, orig_DH_get_text);
                           BNM_FileCheckpoint("[resp] DownloadHandler.get_text hooked"); }
        else BNM_FileCheckpoint("[resp] get_text NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("[resp] EXC"); }
}

// ---------------------------------------------------------------------
//  il2cpp-attach entry point (BNM). Kept minimal for 1.19.0: the BNM
//  Explorer HTTP server (so the game can be inspected/edited live) plus
//  the Debug.Log capture that feeds the Explorer's /logs view. The core
//  entitlement fix runs from JNI_OnLoad and does not depend on il2cpp.
// ---------------------------------------------------------------------
// ---------------------------------------------------------------------
//  1.31 account-validation bypass (release). The account coroutine
//  (RVA 0x287FD94) runs a client-side integrity check, "Gate B"
//  (RVA 0x256C0C8), over account fields we legitimately serve differently
//  on the private server, so it reports "Failed to validate account data".
//  Run the real check (side effects kept) and force it to pass, but only
//  while the account coroutine is active; other callers get the real result.
//  RVAs are libil2cpp file offsets; base = il2cpp_raise_exception - 0x41FC69C.
// ---------------------------------------------------------------------
static std::atomic<bool> g_inAcctCoroutine{false};
static void* (*orig_AcctCoroutine)(void*, void*, void*, void*, void*, void*, void*, void*) = nullptr;
static void* hook_AcctCoroutine(void* a0, void* a1, void* a2, void* a3, void* a4, void* a5, void* a6, void* a7) {
    g_inAcctCoroutine.store(true);
    void* r = orig_AcctCoroutine ? orig_AcctCoroutine(a0, a1, a2, a3, a4, a5, a6, a7) : nullptr;
    g_inAcctCoroutine.store(false);
    return r;
}
static uint64_t (*orig_AcctValidate)(void*, void*, void*, void*, void*, void*, void*, void*) = nullptr;
static uint64_t hook_AcctValidate(void* a0, void* a1, void* a2, void* a3, void* a4, void* a5, void* a6, void* a7) {
    uint64_t r = orig_AcctValidate ? orig_AcctValidate(a0, a1, a2, a3, a4, a5, a6, a7) : 0;
    if (g_inAcctCoroutine.load() && (r & 1) == 0) return 1;
    return r;
}
static void InstallAccountValidateBypass() {
    void* exp = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_raise_exception");
    if (!exp) { BNM_FileCheckpoint("AcctBypass: no il2cpp base"); return; }
    uint8_t* base = (uint8_t*)exp - 0x41FC69CULL;
    void* tr = nullptr;
    if (DobbyHook(base + 0x287FD94ULL, (dobby_dummy_func_t)hook_AcctCoroutine, (dobby_dummy_func_t*)&tr) == 0)
        orig_AcctCoroutine = (void*(*)(void*, void*, void*, void*, void*, void*, void*, void*))tr;
    tr = nullptr;
    if (DobbyHook(base + 0x256C0C8ULL, (dobby_dummy_func_t)hook_AcctValidate, (dobby_dummy_func_t*)&tr) == 0)
        orig_AcctValidate = (uint64_t(*)(void*, void*, void*, void*, void*, void*, void*, void*))tr;
    BNM_FileCheckpointF("AcctBypass: coroutine=%d validate=%d", orig_AcctCoroutine != nullptr, orig_AcctValidate != nullptr);
}

// =====================================================================
//  Remote client (build 131b2+): server announcements + remote disk audio.
//  Server side: yeeps-proxy vr-server services/remote.py, GET /client/feed.txt
//    A<TAB>id<TAB>seconds<TAB>text      (| = newline)
//    D<TAB>clipName<TAB>ogg|mp3|wav<TAB>url
//    S<TAB>slot<TAB>WxHxFrames<TAB>url     (picture screen, 131b3+)
//    F<TAB>accountID<TAB>1<TAB>-           (treat as friend, 131b6+)
//  Everything Unity-facing runs on the main thread, from a per-frame tick
//  (AccountOnlineManager.Update). Only stable UnityEngine APIs are used.
// =====================================================================
static const char* kFeedUrl = "https://live.your-domain.example/client/feed.txt";

static void* (*p_gchandle_target)(void*) = nullptr;
static void (*p_gchandle_free)(void*) = nullptr;
static void* Pin(void* obj) {                 // keep a native-held object alive
    if (!obj || !p_gchandle_new) return nullptr;
    return p_gchandle_new(obj, false);
}
static void Unpin(void* h) { if (h && p_gchandle_free) p_gchandle_free(h); }

struct PendingReq { void* req = nullptr; void* handle = nullptr; void* op = nullptr; void* opHandle = nullptr; std::string clip; std::string ver; };
static PendingReq g_feedReq;
static std::vector<PendingReq> g_audioReqs;
static double g_nextFeedAt = 0;

static std::mutex g_diskMtx;
static std::map<std::string, void*> g_diskClipHandle;      // clip name -> pinned AudioClip handle
static std::map<std::string, std::string> g_diskVersion;   // clip name -> url (incl ?v=)
static std::map<void*, std::string> g_clipNameCache;       // original clip ptr -> name

struct Ann { std::string id, text; double seconds; };
static std::vector<Ann> g_annQueue;
static std::set<std::string> g_annSeen;
static void* g_annGO = nullptr;           // pinned GameObject handle
static void FollowHead(Il2CppObject* go);
static double g_annUntil = 0;

static std::string MonoStr(void* s) {
    if (!s) return "";
    try { return ((BNM::Structures::Mono::String*)s)->str(); } catch (...) { return ""; }
}
static bool Alive(void* obj) {
    if (!obj) return false;
    try {
        Method<bool> m(Class("UnityEngine", "Object").GetMethod("op_Implicit", 1));
        return m.IsValid() ? m((Il2CppObject*)obj) : true;
    } catch (...) { return false; }
}
// Pick an overload by exact parameter type names (e.g. Get(string) vs Get(Uri)).
static MethodBase FindOverload(Class c, const char* name, std::vector<std::string> types) {
    for (auto& m : c.GetMethods(false)) {
        auto* mi = m.GetInfo();
        if (!mi || !mi->name || strcmp(mi->name, name) != 0 || mi->parameters_count != types.size()) continue;
        bool ok = true;
        for (size_t i = 0; i < types.size() && ok; i++)
            ok = mi->parameters && mi->parameters[i] && typeName(mi->parameters[i]) == types[i];
        if (ok) return m;
    }
    return MethodBase();
}
static std::atomic<int> g_remoteLogN{0};
#define RLOG(...) do { if (g_remoteLogN.fetch_add(1) < 500) BNM_FileCheckpointF(__VA_ARGS__); } while (0)   // big: was 40
extern "C" int stb_vorbis_decode_memory(const unsigned char* mem, int len, int* channels, int* sample_rate, short** output);
static void* (*p_object_new)(void*) = nullptr;
static void* (*p_array_new)(void*, uintptr_t) = nullptr;
static void* (*p_value_box)(void*, void*) = nullptr;
static std::map<std::string, void*>* g_rtMatsPtr = nullptr;   // -> g_rtMats (runtime blocks, 240b25)
// biglive26: build the cucumber-cat material on demand in PictureMaterial (the
// data Resources.Load path can miss and RtBuildAll may not have run yet).
static Il2CppObject* RtPictureMaterial(const char* name, Il2CppObject* tex);
static Il2CppObject* RtCatTexture();

// This build's engine code is stripped: no UnityWebRequest.Get, no
// request.isDone, no UnityWebRequestMultimedia. So: new UnityWebRequest(url,
// "GET") + DownloadHandlerBuffer, poll the AsyncOperation from SendWebRequest().
static bool StartGet(const std::string& url, PendingReq& out) {
    if (!p_object_new) return false;
    Class uwr("UnityEngine.Networking", "UnityWebRequest");
    Method<void> ctor(FindOverload(uwr, ".ctor", {"System.String", "System.String"}));
    Class dhb("UnityEngine.Networking", "DownloadHandlerBuffer");
    Method<void> dctor(FindOverload(dhb, ".ctor", {}));
    if (!ctor.IsValid() || !dctor.IsValid()) { RLOG("Remote: UnityWebRequest/DownloadHandlerBuffer ctor NOT FOUND"); return false; }
    auto* req = (Il2CppObject*)p_object_new(uwr.GetClass());
    auto* dh = (Il2CppObject*)p_object_new(dhb.GetClass());
    if (!req || !dh) return false;
    ctor[req](CreateMonoString(url.c_str()), CreateMonoString("GET"));
    dctor[dh]();
    Method<void>(uwr.GetMethod("set_downloadHandler", 1))[req](dh);
    Il2CppObject* op = Method<Il2CppObject*>(uwr.GetMethod("SendWebRequest", 0))[req]();
    if (!op) return false;
    out.req = req; out.handle = Pin(req); out.op = op; out.opHandle = Pin(op);
    return true;
}
static bool ReqDone(PendingReq& p) {
    Il2CppObject* op = p.opHandle && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(p.opHandle) : nullptr;
    if (!op) return true;
    Method<bool> d(Class("UnityEngine", "AsyncOperation").GetMethod("get_isDone", 0));
    return !d.IsValid() || d[op]();
}
static bool ReqOk(void* r) {
    Method<Il2CppObject*> e(Class("UnityEngine.Networking", "UnityWebRequest").GetMethod("get_error", 0));
    return e.IsValid() && e[(Il2CppObject*)r]() == nullptr;
}
static std::string ReqText(void* r) {
    Il2CppObject* dh = Method<Il2CppObject*>(Class("UnityEngine.Networking", "UnityWebRequest").GetMethod("get_downloadHandler", 0))[(Il2CppObject*)r]();
    return dh ? MonoStr(Method<Il2CppObject*>(Class("UnityEngine.Networking", "DownloadHandler").GetMethod("get_text", 0))[dh]()) : "";
}
static void ReqDispose(PendingReq& p) {
    try {
        Method<void> d(Class("UnityEngine.Networking", "UnityWebRequest").GetMethod("Dispose", 0));
        if (p.req && d.IsValid()) d[(Il2CppObject*)p.req]();
    } catch (...) {}
    Unpin(p.handle); Unpin(p.opHandle);
    p = PendingReq{};
}

// ---- disk audio: base64 text -> OGG/WAV bytes -> mono float PCM (background)
struct DecodedDisk { std::string clip, ver; std::vector<float> pcm; int rate = 0; };
static std::mutex g_decMtx;
static std::vector<DecodedDisk> g_decoded;
static std::vector<uint8_t> B64Decode(const std::string& in) {
    static int T[256]; static bool init = false;
    if (!init) { for (int i = 0; i < 256; i++) T[i] = -1;
                 const char* a = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
                 for (int i = 0; i < 64; i++) T[(uint8_t)a[i]] = i; init = true; }
    std::vector<uint8_t> out; out.reserve(in.size() * 3 / 4);
    int val = 0, bits = -8;
    for (uint8_t c : in) { if (T[c] < 0) continue; val = (val << 6) + T[c]; bits += 6;
                           if (bits >= 0) { out.push_back((uint8_t)((val >> bits) & 0xFF)); bits -= 8; } }
    return out;
}
static bool DecodeToMono(const std::vector<uint8_t>& b, std::vector<float>& pcm, int& rate) {
    if (b.size() > 4 && memcmp(b.data(), "OggS", 4) == 0) {
        int ch = 0, sr = 0; short* out = nullptr;
        int frames = stb_vorbis_decode_memory(b.data(), (int)b.size(), &ch, &sr, &out);
        if (frames <= 0 || !out || ch <= 0) { if (out) free(out); return false; }
        pcm.resize(frames);
        for (int i = 0; i < frames; i++) { int s = 0; for (int c = 0; c < ch; c++) s += out[i * ch + c]; pcm[i] = (s / (float)ch) / 32768.f; }
        free(out); rate = sr; return true;
    }
    if (b.size() > 44 && memcmp(b.data(), "RIFF", 4) == 0 && memcmp(b.data() + 8, "WAVE", 4) == 0) {
        size_t pos = 12; int ch = 0, sr = 0, bps = 0; const uint8_t* data = nullptr; uint32_t dlen = 0;
        while (pos + 8 <= b.size()) {
            uint32_t len; memcpy(&len, b.data() + pos + 4, 4);
            if (memcmp(b.data() + pos, "fmt ", 4) == 0) { ch = b[pos+10] | (b[pos+11] << 8); memcpy(&sr, b.data() + pos + 12, 4); bps = b[pos+22] | (b[pos+23] << 8); }
            else if (memcmp(b.data() + pos, "data", 4) == 0) { data = b.data() + pos + 8; dlen = (uint32_t)std::min<size_t>(len, b.size() - pos - 8); break; }
            pos += 8 + len + (len & 1);
        }
        if (!data || ch <= 0 || bps != 16) return false;
        size_t frames = dlen / (2 * ch); pcm.resize(frames);
        for (size_t i = 0; i < frames; i++) { int s = 0; for (int c = 0; c < ch; c++) { int16_t v; memcpy(&v, data + (i * ch + c) * 2, 2); s += v; } pcm[i] = (s / (float)ch) / 32768.f; }
        rate = sr; return true;
    }
    return false;
}
static void BuildClipOnMain(DecodedDisk& d) {
    if (!p_array_new || d.pcm.empty()) return;
    Class ac("UnityEngine", "AudioClip");
    Method<Il2CppObject*> create(FindOverload(ac, "Create", {"System.String", "System.Int32", "System.Int32", "System.Int32", "System.Boolean"}));
    Method<bool> setData(FindOverload(ac, "SetData", {"System.Single[]", "System.Int32"}));
    if (!create.IsValid() || !setData.IsValid()) { RLOG("Remote: AudioClip.Create/SetData NOT FOUND"); return; }
    Il2CppObject* clip = create(CreateMonoString(d.clip.c_str()), (int)d.pcm.size(), 1, d.rate, false);
    if (!clip) return;
    void* arr = p_array_new(Class("System", "Single").GetClass(), d.pcm.size());
    if (!arr) return;
    memcpy((uint8_t*)arr + 0x20, d.pcm.data(), d.pcm.size() * sizeof(float));   // Il2CppArray data at +0x20
    setData[clip]((Il2CppObject*)arr, 0);
    std::lock_guard<std::mutex> lk(g_diskMtx);
    Unpin(g_diskClipHandle[d.clip]);
    g_diskClipHandle[d.clip] = Pin(clip);
    g_diskVersion[d.clip] = d.ver;
    RLOG("Remote: disk %s ready (%d samples @ %d Hz)", d.clip.c_str(), (int)d.pcm.size(), d.rate);
}

// ---- picture screens (build 131b3+): S<TAB>slot<TAB>WxHxN<TAB>url
// The server converts the uploaded PNG/GIF into a small binary (base64):
//   "BSCR" u16 w, u16 h, u16 frames, u16 delayMs[frames], then zlib(RGB24
//   frames, bottom row first). Each frame becomes a Texture2D; the block's own
//   material (ResourceManager "blazescreen/mat_<slot>") shows the current frame,
//   so every placed copy of the block updates together.
struct DecodedScreen { std::string slot, ver; int w = 0, h = 0, n = 0; std::vector<int> delays; std::vector<uint8_t> pix; };
static std::mutex g_scrMtx;
static std::vector<DecodedScreen> g_scrDecoded;
static std::vector<PendingReq> g_scrReqs;
struct ScreenState {
    std::string ver;
    DecodedScreen cur;                 // current picture: all frames, CPU side
    void* tex = nullptr;               // pinned Texture2D, pixels rewritten in place
    void* buf = nullptr;               // pinned managed byte[] (one frame)
    int texW = 0, texH = 0;
    size_t frame = 0; double nextAt = 0, reapplyAt = 0;
    void* mat = nullptr;               // pinned Material handle
};
static std::map<std::string, ScreenState> g_screens;

static bool DecodeScreen(const std::vector<uint8_t>& b, DecodedScreen& d) {
    if (b.size() < 10 || memcmp(b.data(), "BSCR", 4) != 0) return false;
    auto u16 = [&](size_t o) { return (int)(b[o] | (b[o + 1] << 8)); };
    d.w = u16(4); d.h = u16(6); d.n = u16(8);
    if (d.w <= 0 || d.h <= 0 || d.n <= 0 || d.w > 1024 || d.h > 1024 || d.n > 120) return false;
    size_t off = 10 + 2 * (size_t)d.n;
    if (b.size() < off) return false;
    for (int i = 0; i < d.n; i++) d.delays.push_back(std::max(20, u16(10 + 2 * i)));
    uLongf want = (uLongf)d.w * d.h * 3 * d.n;
    d.pix.resize(want);
    uLongf got = want;
    if (uncompress(d.pix.data(), &got, b.data() + off, (uLong)(b.size() - off)) != Z_OK || got != want) return false;
    return true;
}
static Il2CppObject* ScreenMaterial(ScreenState& st, const std::string& slot) {
    Il2CppObject* m = st.mat && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(st.mat) : nullptr;
    if (m && Alive(m)) return m;
    Unpin(st.mat); st.mat = nullptr;
    Method<Il2CppObject*> load(FindOverload(Class("UnityEngine", "Resources"), "Load", {"System.String"}));
    if (!load.IsValid()) return nullptr;
    m = load(CreateMonoString(("blazescreen/mat_" + slot).c_str()));
    if (!m && g_rtMatsPtr && g_rtMatsPtr->count(slot)) m = (Il2CppObject*)p_gchandle_target((*g_rtMatsPtr)[slot]);
    if (m) st.mat = Pin(m);
    return m;
}
// The game renders map objects with its own COPIES of their materials (made
// when it batches/relights them), so setting the texture on our material alone
// only showed up when the game rebuilt those copies. Every few seconds, find
// every live material named blazeScreen_<slot>... and update all of them.
static std::map<std::string, std::vector<void*>> g_scrMatCopies;   // slot -> pinned materials
static bool g_tvWantMats = false;   // live92 Yeeps TV: also look for blazeScreen_tv copies
static std::map<void*, std::string> g_matNameSeen;                  // material ptr -> slot ("" = not ours)
static double g_nextMatScan = 0, g_nextSeenReset = 0;
static void ScanScreenMaterials(double now) {
    if (now < g_nextMatScan) return;
    // big: far more materials are loaded than in stock 2.4, and listing them all
    // (plus reading every name) cost frame spikes - scan every 30 s, re-read names every 10 min
    // FindObjectsOfTypeAll(Material) itself is the 6-40 ms hitch (thousands of
    // materials in the big APK): every 30 s only while a screen still has no
    // material copy, otherwise every 3 minutes
    bool missing = false;
    for (auto& kv : g_screens) if (g_scrMatCopies[kv.first].empty()) { missing = true; break; }
    if (g_tvWantMats && g_scrMatCopies["tv"].empty()) missing = true;
    g_nextMatScan = now + (missing ? 30.0 : 180.0);
    if (now >= g_nextSeenReset) { g_matNameSeen.clear(); g_nextSeenReset = now + 600.0; }
    static Class matC("UnityEngine", "Material");
    static Method<Il2CppObject*> findAll(FindOverload(Class("UnityEngine", "Resources"), "FindObjectsOfTypeAll", {"System.Type"}));
    if (!findAll.IsValid()) return;
    auto* arr = (uint8_t*)findAll((Il2CppObject*)matC.GetMonoType());
    if (!arr) return;
    uintptr_t n = *(uintptr_t*)(arr + 0x18);
    static Method<Il2CppObject*> getName(Class("UnityEngine", "Object").GetMethod("get_name", 0));
    for (uintptr_t i = 0; i < n; i++) {
        void* m = ((void**)(arr + 0x20))[i];
        if (!m) continue;
        auto it = g_matNameSeen.find(m);
        if (it != g_matNameSeen.end()) continue;
        std::string slot;
        std::string name = MonoStr(getName[(Il2CppObject*)m]());
        if (name.rfind("blazeScreen_", 0) == 0) {
            slot = name.substr(12);
            size_t cut = slot.find_first_of(" (");
            if (cut != std::string::npos) slot = slot.substr(0, cut);
            auto& v = g_scrMatCopies[slot];
            v.push_back(Pin(m));
            if (slot != "tv") g_screens[slot].reapplyAt = 0;     // push the current frame to it now
            static int found = 0;
            if (found++ < 12) BNM_FileCheckpointF("Remote: screen material copy '%s' -> %s (%d total)", name.c_str(), slot.c_str(), (int)v.size());
        }
        g_matNameSeen[m] = slot;
    }
    // drop destroyed copies
    for (auto& kv : g_scrMatCopies) {
        auto& v = kv.second;
        for (size_t i = 0; i < v.size();) {
            Il2CppObject* m = p_gchandle_target ? (Il2CppObject*)p_gchandle_target(v[i]) : nullptr;
            if (!m || !Alive(m)) { Unpin(v[i]); v.erase(v.begin() + i); } else i++;
        }
    }
}
static void SetScreenTexture(ScreenState& st, const std::string& slot, Il2CppObject* tex) {
    if (!tex) return;
    Method<void> set(FindOverload(Class("UnityEngine", "Material"), "SetTexture", {"System.String", "UnityEngine.Texture"}));
    if (!set.IsValid()) return;
    Il2CppObject* m = ScreenMaterial(st, slot);
    if (m) set[m](CreateMonoString("_MainTex"), tex);
    for (void* h : g_scrMatCopies[slot]) {
        Il2CppObject* c = p_gchandle_target ? (Il2CppObject*)p_gchandle_target(h) : nullptr;
        if (c && c != m) { try { set[c](CreateMonoString("_MainTex"), tex); } catch (...) {} }
    }
}
// One texture per screen; each GIF frame is written INTO it (LoadRawTextureData
// + Apply). The game renders our blocks through material copies / property
// blocks that keep a reference to the texture they saw, so swapping texture
// objects never showed up -- rewriting the same texture's pixels does.
static Il2CppObject* EnsureLiveTexture(ScreenState& st, int w, int h) {
    Il2CppObject* tex = st.tex && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(st.tex) : nullptr;
    if (tex && Alive(tex) && st.texW == w && st.texH == h) return tex;
    if (tex) { try { Method<void>(Class("UnityEngine", "Object").GetMethod("Destroy", 1))(tex); } catch (...) {} }
    Unpin(st.tex); st.tex = nullptr; Unpin(st.buf); st.buf = nullptr;
    Class t2("UnityEngine", "Texture2D");
    Method<void> ctor(FindOverload(t2, ".ctor", {"System.Int32", "System.Int32", "UnityEngine.TextureFormat", "System.Boolean"}));
    if (!ctor.IsValid() || !p_object_new || !p_array_new) return nullptr;
    tex = (Il2CppObject*)p_object_new(t2.GetClass());
    if (!tex) return nullptr;
    ctor[tex](w, h, 3 /* RGB24 */, false);
    try {
        Class tx("UnityEngine", "Texture");
        Method<void>(tx.GetMethod("set_wrapMode", 1))[tex](1 /* Clamp */);
        Method<void>(tx.GetMethod("set_filterMode", 1))[tex](1 /* Bilinear */);
    } catch (...) {}
    void* arr = p_array_new(Class("System", "Byte").GetClass(), (size_t)w * h * 3);
    if (!arr) return nullptr;
    st.tex = Pin(tex); st.buf = Pin(arr); st.texW = w; st.texH = h;
    return tex;
}
static bool UploadFrame(ScreenState& st, size_t i) {
    const DecodedScreen& d = st.cur;
    Il2CppObject* tex = EnsureLiveTexture(st, d.w, d.h);
    void* arr = st.buf && p_gchandle_target ? p_gchandle_target(st.buf) : nullptr;
    if (!tex || !arr || (int)i >= d.n) return false;
    Class t2("UnityEngine", "Texture2D");
    Method<void> load(FindOverload(t2, "LoadRawTextureData", {"System.Byte[]"}));
    Method<void> apply(FindOverload(t2, "Apply", {"System.Boolean", "System.Boolean"}));
    if (!load.IsValid() || !apply.IsValid()) return false;
    size_t n = (size_t)d.w * d.h * 3;
    memcpy((uint8_t*)arr + 0x20, d.pix.data() + n * i, n);
    load[tex]((Il2CppObject*)arr);
    apply[tex](false, false);   // stays writable for the next frame
    return true;
}
static void ScreensTick(double now) {
    bool anyScreen = false;
    for (auto& kv : g_screens) if (!kv.second.ver.empty()) anyScreen = true;
    if (anyScreen || g_tvWantMats) { try { ScanScreenMaterials(now); } catch (...) {} }
    for (size_t i = 0; i < g_scrReqs.size();) {
        auto& p = g_scrReqs[i];
        if (!ReqDone(p)) { i++; continue; }
        if (ReqOk(p.req)) {
            std::string b64 = ReqText(p.req);
            std::string slot = p.clip, ver = p.ver;
            std::thread([b64, slot, ver]() {
                DecodedScreen d; d.slot = slot; d.ver = ver;
                if (DecodeScreen(B64Decode(b64), d)) { std::lock_guard<std::mutex> lk(g_scrMtx); g_scrDecoded.push_back(std::move(d)); }
                else BNM_FileCheckpointF("Remote: screen %s decode failed", slot.c_str());
            }).detach();
        } else RLOG("Remote: screen %s download failed", p.clip.c_str());
        ReqDispose(p);
        g_scrReqs.erase(g_scrReqs.begin() + i);
    }
    {
        std::vector<DecodedScreen> ready;
        { std::lock_guard<std::mutex> lk(g_scrMtx); ready.swap(g_scrDecoded); }
        for (auto& d : ready) {
            auto& st = g_screens[d.slot];
            st.ver = d.ver; st.cur = std::move(d);
            st.frame = 0; st.nextAt = 0; st.reapplyAt = 0;
            bool ok = false;
            try { ok = UploadFrame(st, 0); } catch (...) {}
            BNM_FileCheckpointF("Remote: screen %s ready (%d frames, %dx%d) upload=%d", st.cur.slot.c_str(), st.cur.n, st.cur.w, st.cur.h, ok);
        }
    }
    for (auto& kv : g_screens) {
        const std::string& slot = kv.first;
        auto& st = kv.second;
        if (st.ver.empty() || st.cur.n <= 0) continue;
        if (st.cur.n > 1 && now >= st.nextAt) {
            st.frame = (st.frame + 1) % st.cur.n;
            st.nextAt = now + st.cur.delays[st.frame % st.cur.delays.size()] / 1000.0;
            try { UploadFrame(st, st.frame); } catch (...) {}
        }
        if (now >= st.reapplyAt) {
            st.reapplyAt = now + 2.0;
            Il2CppObject* t = st.tex && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(st.tex) : nullptr;
            try { SetScreenTexture(st, slot, t); } catch (...) {}
        }
    }
}
static void ClearScreen(const std::string& slot) {
    auto it = g_screens.find(slot);
    if (it == g_screens.end()) return;
    auto& st = it->second;
    Il2CppObject* t = st.tex && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(st.tex) : nullptr;
    if (t) { try { Method<void>(Class("UnityEngine", "Object").GetMethod("Destroy", 1))(t); } catch (...) {} }
    Unpin(st.tex); st.tex = nullptr; Unpin(st.buf); st.buf = nullptr;
    st.ver.clear(); st.cur = DecodedScreen{};
}

// The game re-assigns every map object's materials (paint colour etc.) through
// MapObject.SetSharedMaterials, which replaced the screen's picture material
// with the shared decoration atlas. For our screen blocks, keep slot 1 (the
// picture surface) on the screen's own material; slot 0 (the frame) still
// takes the game's colour material, so painting keeps working.
// Only the BLAZE Screen gets the picture material. Swapping the material on the
// 3x3 Screen (tag-0 decoration) crashed every client loading a big public room
// (official_1 / wild_0) - left alone it just renders plain.
static const std::map<std::string, std::string> kScreenKeys = {
    {"3x2x1_blazeScreenRole", "blaze"}};
// Static picture blocks (ported from 131b15): slot 1 = a fixed picture material
// shipped in the data (build_blocks_240.py), loaded once by its Resources path.
static const std::map<std::string, std::string> kPictureKeys = {
    {"1x1_stuffedCucumberCat", "blazecat/mat_cucumbercat"}};
static std::map<std::string, void*> g_pictureMats;     // path -> pinned material
static Il2CppObject* PictureMaterial(const std::string& path) {
    auto f = g_pictureMats.find(path);
    if (f != g_pictureMats.end() && p_gchandle_target) {
        auto* m = (Il2CppObject*)p_gchandle_target(f->second);
        if (m && Alive(m)) return m;
        Unpin(f->second); g_pictureMats.erase(f);
    }
    Method<Il2CppObject*> load(FindOverload(Class("UnityEngine", "Resources"), "Load", {"System.String"}));
    if (!load.IsValid()) return nullptr;
    Il2CppObject* m = load(CreateMonoString(path.c_str()));
    if (!m && path == "blazecat/mat_cucumbercat") {
        // The data material at this Resources path can miss at runtime; build the
        // cat material directly so the block is never left white (biglive26).
        if (g_rtMatsPtr && g_rtMatsPtr->count("cat"))
            m = (Il2CppObject*)p_gchandle_target((*g_rtMatsPtr)["cat"]);
        if (!m || !Alive(m)) {
            try { m = RtPictureMaterial("cucumberCat", RtCatTexture()); } catch (...) { m = nullptr; }
        }
    }
    if (m) g_pictureMats[path] = Pin(m);
    return m;
}
static void (*orig_MO_SetSharedMaterials)(void*, void*, void*) = nullptr;
static void hook_MO_SetSharedMaterials(void* self, void* arr, void* mi) {
    void* pass = arr;
    try {
        if (self && arr && p_array_new) {
            Class mo("", "MapObject", Image("Assembly-CSharp.dll"));
            std::string key = MonoStr(Method<Il2CppObject*>(mo.GetMethod("get_key", 0))[(Il2CppObject*)self]());
            if (key.empty()) {
                Il2CppObject* go = Method<Il2CppObject*>(Class("UnityEngine", "Component").GetMethod("get_gameObject", 0))[(Il2CppObject*)self]();
                if (go) key = MonoStr(Method<Il2CppObject*>(Class("UnityEngine", "Object").GetMethod("get_name", 0))[go]());
                size_t c = key.find("(Clone)"); if (c != std::string::npos) key = key.substr(0, c);
            }
            auto pk = kPictureKeys.find(key);
            if (pk != kPictureKeys.end()) {
                uintptr_t n = *(uintptr_t*)((uint8_t*)arr + 0x18);     // Il2CppArray max_length
                Il2CppObject* m = PictureMaterial(pk->second);
                if (n >= 2 && m) {
                    void* copy = p_array_new(Class("UnityEngine", "Material").GetClass(), n);
                    if (copy) {
                        memcpy((uint8_t*)copy + 0x20, (uint8_t*)arr + 0x20, n * sizeof(void*));
                        ((void**)((uint8_t*)copy + 0x20))[1] = m;
                        pass = copy;
                    }
                }
                static int plogged = 0;
                if (plogged++ < 6) BNM_FileCheckpointF("Remote: picture material for %s (n=%d, mat=%d)", key.c_str(), (int)n, m != nullptr);
            }
            auto it = kScreenKeys.find(key);
            if (it != kScreenKeys.end()) {
                uintptr_t n = *(uintptr_t*)((uint8_t*)arr + 0x18);     // Il2CppArray max_length
                Il2CppObject* m = ScreenMaterial(g_screens[it->second], it->second);
                if (n >= 2 && m) {
                    void* copy = p_array_new(Class("UnityEngine", "Material").GetClass(), n);
                    if (copy) {
                        memcpy((uint8_t*)copy + 0x20, (uint8_t*)arr + 0x20, n * sizeof(void*));
                        ((void**)((uint8_t*)copy + 0x20))[1] = m;
                        pass = copy;
                    }
                }
                static int logged = 0;
                if (logged++ < 6) BNM_FileCheckpointF("Remote: screen materials for %s (n=%d, mat=%d)", key.c_str(), (int)n, m != nullptr);
            }
        }
    } catch (...) {}
    if (orig_MO_SetSharedMaterials) orig_MO_SetSharedMaterials(self, pass, mi);
}

// ---- "everyone is a friend" (131b6+): F<TAB>accountID lines from the feed.
// Meta friends always fail (400) on this server, so friends lists were empty
// and friends-only private worlds couldn't be joined. The server publishes who
// is online; FriendManager.IsFriend() also accepts them, and they're added to
// the game's friend list so they show in the friends UI.
static std::set<std::string> g_feedFriends;
static bool g_feedFriendsDirty = false;
static std::mutex g_feedFriendsMtx;
static bool (*orig_FM_IsFriend)(void*, void*) = nullptr;
static bool hook_FM_IsFriend(void* accountID, void* mi) {
    bool r = orig_FM_IsFriend ? orig_FM_IsFriend(accountID, mi) : false;
    if (r || !accountID) return r;
    std::string id = MonoStr(accountID);
    std::lock_guard<std::mutex> lk(g_feedFriendsMtx);
    return g_feedFriends.count(id) > 0;
}
static double g_nextFriendSync = 0;          // friend list refresh: every 3 minutes
static void SyncFeedFriendsIntoGame() {
    double now = nowSeconds();
    std::set<std::string> want;
    { std::lock_guard<std::mutex> lk(g_feedFriendsMtx);
      if (!g_feedFriendsDirty || now < g_nextFriendSync) return;
      g_feedFriendsDirty = false; want = g_feedFriends; }
    g_nextFriendSync = now + 180.0;
    Class fm("", "FriendManager", Image("Assembly-CSharp.dll"));
    Method<Il2CppObject*> getList(fm.GetMethod("get_friendAccountIDs", 0));
    if (!getList.IsValid()) return;
    auto* list = (uint8_t*)getList();
    if (!list) return;
    std::string self;
    try { self = MonoStr(Method<Il2CppObject*>(Class("", "AccountManager", Image("Assembly-CSharp.dll")).GetMethod("get_accountID", 0))()); } catch (...) {}
    std::set<std::string> have;
    auto* items = *(uint8_t**)(list + 0x10);
    int size = *(int*)(list + 0x18);
    Il2CppClass* strK = Class("System", "String").GetClass();
    if (items && size > 0 && size < 100000)
        for (int i = 0; i < size; i++) {
            auto* e = ((Il2CppObject**)(items + 0x20))[i];
            if (e && e->klass == strK) have.insert(MonoStr(e));
        }
    Method<void> add(Class(((Il2CppObject*)list)->klass).GetMethod("Add", 1));
    if (!add.IsValid()) return;
    int added = 0;
    for (auto& id : want) {
        if (id == self || have.count(id)) continue;
        add[(Il2CppObject*)list](CreateMonoString(id.c_str()));
        added++;
    }
    static int logged = 0;
    if (added && logged++ < 5) BNM_FileCheckpointF("Friends: added %d online players to the friend list", added);
}

// ---- floating video (131b8+): parsed from the feed's V line, applied by
// VideoTick on the main thread. See the video player below RemoteTick.
struct VideoWanted { std::string id, segBase, audioUrl; int w = 0, h = 0, fps = 10, nSeg = 0, segFrames = 60, frames = 0; bool loop = true; };
static VideoWanted g_vidWanted;
static bool g_vidWantedSet = false;
static std::mutex g_vidReqMtx;

static void ParseFeed(const std::string& body) {
    std::set<std::string> seenScreens;
    std::set<std::string> feedFriends;
    size_t pos = 0;
    while (pos < body.size()) {
        size_t nl = body.find('\n', pos);
        std::string line = body.substr(pos, nl == std::string::npos ? std::string::npos : nl - pos);
        pos = nl == std::string::npos ? body.size() : nl + 1;
        std::vector<std::string> f;
        size_t a = 0, b;
        while ((b = line.find('\t', a)) != std::string::npos && f.size() < 3) { f.push_back(line.substr(a, b - a)); a = b + 1; }
        f.push_back(line.substr(a));
        if (f.size() < 4) continue;
        if (f[0] == "A" && !g_annSeen.count(f[1])) {
            g_annSeen.insert(f[1]);
            std::string t = f[3];
            for (auto& c : t) if (c == '|') c = '\n';
            double secs = 10; try { secs = std::stod(f[2]); } catch (...) {}
            g_annQueue.push_back({f[1], t, secs});
        } else if (f[0] == "F") {
            feedFriends.insert(f[1]);
        } else if (f[0] == "S") {
            seenScreens.insert(f[1]);
            if (g_screens[f[1]].ver == f[3]) continue;
            bool inflight = false;
            for (auto& p : g_scrReqs) if (p.clip == f[1] && p.ver == f[3]) inflight = true;
            if (inflight) continue;
            PendingReq pr; pr.clip = f[1]; pr.ver = f[3];
            std::string url = f[3];
            size_t q = url.find('?');
            url = (q == std::string::npos ? url : url.substr(0, q)) + ".b64" + (q == std::string::npos ? "" : url.substr(q));
            if (StartGet(url, pr)) { g_scrReqs.push_back(pr); RLOG("Remote: downloading screen %s", f[1].c_str()); }
        } else if (f[0] == "D") {
            std::lock_guard<std::mutex> lk(g_diskMtx);
            if (g_diskVersion[f[1]] == f[3]) continue;          // already have this version
            bool inflight = false;
            for (auto& p : g_audioReqs) if (p.clip == f[1] && p.ver == f[3]) inflight = true;
            if (inflight) continue;
            PendingReq pr; pr.clip = f[1]; pr.ver = f[3];
            std::string url = f[3];
            size_t q = url.find('?');
            url = (q == std::string::npos ? url : url.substr(0, q)) + ".b64" + (q == std::string::npos ? "" : url.substr(q));
            if (StartGet(url, pr)) { g_audioReqs.push_back(pr); RLOG("Remote: downloading disk %s", f[1].c_str()); }
        }
    }
    for (auto& kv : g_screens)
        if (!seenScreens.count(kv.first) && !kv.second.ver.empty()) ClearScreen(kv.first);
    { std::lock_guard<std::mutex> lk(g_feedFriendsMtx); g_feedFriends.swap(feedFriends); g_feedFriendsDirty = true; }
}

// This build has no TextMesh / TextMeshPro (stripped), so announcements are a
// small world-space Canvas with a UI Text (the game's own text type). The font
// is taken from the game's version label (cached in hook_AVDU_Start).
static void* g_lilitaHandle = nullptr;
static Il2CppObject* AnnouncementFont() {
    // Lilita One (added to the game data as ResourceManager "blaze/lilitaone").
    Il2CppObject* f = g_lilitaHandle && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_lilitaHandle) : nullptr;
    if (f && Alive(f)) return f;
    try {
        Method<Il2CppObject*> load(FindOverload(Class("UnityEngine", "Resources"), "Load", {"System.String"}));
        f = load.IsValid() ? load(CreateMonoString("blaze/lilitaone")) : nullptr;
        if (f) { g_lilitaHandle = Pin(f); return f; }
    } catch (...) {}
    f = g_uiFontHandle && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_uiFontHandle) : nullptr;
    if (f) return f;
    // Use any font the game already has loaded. (GetBuiltinResource for
    // LegacyRuntime.ttf / Arial.ttf is stripped in this build and logged an error.)
    Il2CppObject* fontType = sysTypeOf("UnityEngine.Font, UnityEngine.TextRenderingModule");
    Method<Il2CppObject*> findAll(FindOverload(Class("UnityEngine", "Resources"), "FindObjectsOfTypeAll", {"System.Type"}));
    if (fontType && findAll.IsValid()) {
        auto* arr = (uint8_t*)findAll(fontType);
        if (arr && *(uintptr_t*)(arr + 0x18) > 0) {
            f = ((Il2CppObject**)(arr + 0x20))[0];
            if (f && p_gchandle_new) g_uiFontHandle = Pin(f);
        }
    }
    return f;
}
static void ShowAnnouncement(const Ann& a) {
    try {
        Class goc("UnityEngine", "GameObject");
        Method<Il2CppObject*> addComp(goc.GetMethod("AddComponent", 1));
        Method<Il2CppObject*> getTr(goc.GetMethod("get_transform", 0));
        Il2CppObject* canvasType = sysTypeOf("UnityEngine.Canvas, UnityEngine.UIModule");
        Il2CppObject* textType = sysTypeOf("UnityEngine.UI.Text, UnityEngine.UI");
        Il2CppObject* font = AnnouncementFont();
        if (!addComp.IsValid() || !canvasType || !textType) { RLOG("Remote: announcement UI types NOT FOUND"); return; }
        if (!font) { RLOG("Remote: announcement font NOT FOUND"); return; }
        Il2CppObject* go = goc.CreateNewObjectParameters(CreateMonoString("BlazeAnnouncement"));
        if (!go) return;
        Il2CppObject* canvas = addComp[go](canvasType);   // also swaps Transform -> RectTransform
        if (!canvas) { RLOG("Remote: AddComponent(Canvas) failed"); return; }
        Class cv("UnityEngine", "Canvas");
        Method<void>(cv.GetMethod("set_renderMode", 1))[canvas](2);        // RenderMode.WorldSpace
        Method<void>(cv.GetMethod("set_sortingOrder", 1))[canvas](1000);
        Class rt("UnityEngine", "RectTransform");
        Class tr("UnityEngine", "Transform");
        Il2CppObject* rootT = getTr[go]();
        Method<void>(rt.GetMethod("set_sizeDelta", 1))[rootT](Vector2(900.f, 300.f));
        Method<void>(tr.GetMethod("set_localScale", 1))[rootT](Vector3(0.0018f, 0.0018f, 0.0018f));
        Il2CppObject* child = goc.CreateNewObjectParameters(CreateMonoString("Text"));
        Il2CppObject* text = child ? addComp[child](textType) : nullptr;
        if (!text) { RLOG("Remote: AddComponent(Text) failed"); return; }
        Il2CppObject* childT = getTr[child]();
        Method<void>(FindOverload(tr, "SetParent", {"UnityEngine.Transform", "System.Boolean"}))[childT](rootT, false);
        Method<void>(rt.GetMethod("set_sizeDelta", 1))[childT](Vector2(900.f, 300.f));
        Class tx("UnityEngine.UI", "Text");
        Method<void>(tx.GetMethod("set_font", 1))[text](font);
        Method<void>(tx.GetMethod("set_text", 1))[text](CreateMonoString(a.text.c_str()));
        Method<void>(tx.GetMethod("set_fontSize", 1))[text](52);
        Method<void>(tx.GetMethod("set_alignment", 1))[text](4);            // TextAnchor.MiddleCenter
        Method<void>(tx.GetMethod("set_horizontalOverflow", 1))[text](0);   // Wrap
        Method<void>(tx.GetMethod("set_verticalOverflow", 1))[text](1);     // Overflow
        Class gr("UnityEngine.UI", "Graphic");
        Method<void>(gr.GetMethod("set_color", 1))[text](Color(1.f, 1.f, 1.f, 1.f));   // white
        Method<void>(gr.GetMethod("set_raycastTarget", 1))[text](false);
        // dark outline so white text stays readable on bright backgrounds
        try {
            Il2CppObject* outlineType = sysTypeOf("UnityEngine.UI.Outline, UnityEngine.UI");
            Il2CppObject* ol = outlineType ? addComp[child](outlineType) : nullptr;
            if (ol) {
                Class sh("UnityEngine.UI", "Shadow");
                Method<void>(sh.GetMethod("set_effectColor", 1))[ol](Color(0.f, 0.f, 0.f, 0.85f));
                Method<void>(sh.GetMethod("set_effectDistance", 1))[ol](Vector2(2.5f, -2.5f));
            }
        } catch (...) {}
        g_annGO = Pin(go);
        g_annUntil = nowSeconds() + a.seconds;
        FollowHead(go);
        RLOG("Remote: announcement shown (%s)", a.id.c_str());
    } catch (...) { RLOG("Remote: announcement EXCEPTION"); }
}
// The head camera's Transform. Camera.main is null in this rig (the VR camera
// object is named "MainCamera" but not tagged), so find it by name; cached.
static void* g_headTHandle = nullptr;
static Il2CppObject* HeadTransform() {
    Il2CppObject* t = g_headTHandle && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_headTHandle) : nullptr;
    if (t && Alive(t)) return t;
    Unpin(g_headTHandle); g_headTHandle = nullptr; t = nullptr;
    Class goc("UnityEngine", "GameObject");
    Method<Il2CppObject*> find(FindOverload(goc, "Find", {"System.String"}));
    Method<Il2CppObject*> getTr(goc.GetMethod("get_transform", 0));
    for (const char* n : {"MainCamera", "CenterEyeAnchor"}) {
        Il2CppObject* go = find.IsValid() ? find(CreateMonoString(n)) : nullptr;
        if (go) { t = getTr[go](); break; }
    }
    if (!t) {
        Il2CppObject* cam = Method<Il2CppObject*>(Class("UnityEngine", "Camera").GetMethod("get_main", 0))();
        if (cam) t = Method<Il2CppObject*>(Class("UnityEngine", "Component").GetMethod("get_transform", 0))[cam]();
    }
    if (t) g_headTHandle = Pin(t);
    return t;
}
static void FollowHead(Il2CppObject* go) {
    try {
        Il2CppObject* ct = HeadTransform();
        Il2CppObject* gt = Method<Il2CppObject*>(Class("UnityEngine", "GameObject").GetMethod("get_transform", 0))[go]();
        if (!ct || !gt) return;
        Class tr("UnityEngine", "Transform");
        Vector3 p = Method<Vector3>(tr.GetMethod("get_position", 0))[ct]();
        Vector3 fw = Method<Vector3>(tr.GetMethod("get_forward", 0))[ct]();
        Quaternion q = Method<Quaternion>(tr.GetMethod("get_rotation", 0))[ct]();
        Vector3 np(p.x + fw.x * 1.4f, p.y + fw.y * 1.4f + 0.15f, p.z + fw.z * 1.4f);
        Method<void>(tr.GetMethod("set_position", 1))[gt](np);
        Method<void>(tr.GetMethod("set_rotation", 1))[gt](q);
    } catch (...) {}
}

// =====================================================================
//  Floating video (131b8+). A full-length video + audio on a world-space
//  panel in front of every player, looping until the server stops sending
//  the V line. Frames reuse the picture-screen "BSCR" segment format (so
//  DecodeScreen decodes them) and audio reuses the disk OGG->PCM path. The
//  displayed frame is keyed to the AudioSource's playback time, so picture
//  and sound stay in sync and loop together. Segments are kept COMPRESSED
//  and the current one decoded on demand, so memory stays ~one segment.
struct VideoPlayer {
    std::string id, segBase, audioUrl;
    int w = 0, h = 0, fps = 10, nSeg = 0, segFrames = 60; bool loop = true, audioWanted = false;
    // download (sequential; playback starts after segment 0, the rest stream in)
    int segNext = 0; PendingReq segReq; bool segReqActive = false;
    PendingReq audioReq; bool audioReqActive = false;
    std::vector<std::vector<uint8_t>> segBlobs;   // compressed BSCR per segment
    std::vector<int> segStart;                    // prefix frame counts (nSeg+1)
    int totalFrames = 0; bool allSegs = false;
    // audio decode (worker thread)
    bool audioDecoding = false, audioDecoded = false;
    std::vector<float> pcm; int pcmRate = 0;
    double loadStart = 0;
    // built objects (all pinned)
    bool built = false;
    void* go = nullptr; void* rawImg = nullptr; void* tex = nullptr; void* buf = nullptr;
    void* audioSrc = nullptr; void* clip = nullptr;
    // display
    DecodedScreen cache; int cacheSeg = -1; int curFrame = -1; double startAt = 0;
};
static VideoPlayer g_vp;
static std::mutex g_vpAudMtx;
static bool g_vpAudDone = false, g_vpAudOk = false;
static std::vector<float> g_vpAudPcm; static int g_vpAudRate = 0;

static void VideoFreeObjects() {
    try {
        Il2CppObject* src = g_vp.audioSrc && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_vp.audioSrc) : nullptr;
        if (src && Alive(src)) { try { Method<void>(Class("UnityEngine", "AudioSource").GetMethod("Stop", 0))[src](); } catch (...) {} }
        Il2CppObject* go = g_vp.go && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_vp.go) : nullptr;
        if (go && Alive(go)) { try { Method<void>(Class("UnityEngine", "Object").GetMethod("Destroy", 1))(go); } catch (...) {} }
        Il2CppObject* tex = g_vp.tex && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_vp.tex) : nullptr;
        if (tex && Alive(tex)) { try { Method<void>(Class("UnityEngine", "Object").GetMethod("Destroy", 1))(tex); } catch (...) {} }
    } catch (...) {}
    Unpin(g_vp.go); Unpin(g_vp.rawImg); Unpin(g_vp.tex); Unpin(g_vp.buf); Unpin(g_vp.audioSrc); Unpin(g_vp.clip);
    g_vp.go = g_vp.rawImg = g_vp.tex = g_vp.buf = g_vp.audioSrc = g_vp.clip = nullptr;
}
static void StopVideo() {
    bool had = !g_vp.id.empty();
    if (g_vp.segReqActive) { ReqDispose(g_vp.segReq); }
    if (g_vp.audioReqActive) { ReqDispose(g_vp.audioReq); }
    VideoFreeObjects();
    g_vp = VideoPlayer{};
    { std::lock_guard<std::mutex> lk(g_vpAudMtx); g_vpAudDone = false; g_vpAudOk = false; g_vpAudPcm.clear(); }
    if (had) BNM_FileCheckpoint("Video: stopped");
}
static void StartVideoLoad(const VideoWanted& w) {
    StopVideo();
    g_vp.id = w.id; g_vp.w = w.w; g_vp.h = w.h; g_vp.fps = w.fps > 0 ? w.fps : 10;
    g_vp.nSeg = w.nSeg; g_vp.loop = w.loop; g_vp.segBase = w.segBase; g_vp.audioUrl = w.audioUrl;
    g_vp.segFrames = w.segFrames > 0 ? w.segFrames : 60;
    g_vp.totalFrames = w.frames > 0 ? w.frames : w.nSeg * g_vp.segFrames;   // known up front -> progressive
    g_vp.audioWanted = (!w.audioUrl.empty() && w.audioUrl != "-");
    g_vp.segStart.clear(); g_vp.segStart.push_back(0);
    g_vp.loadStart = nowSeconds();
    BNM_FileCheckpointF("Video: loading %s (%dx%d@%d, %d segs, loop=%d, audio=%d)",
                        w.id.c_str(), w.w, w.h, g_vp.fps, w.nSeg, (int)w.loop, (int)g_vp.audioWanted);
}
static bool VideoBuildTexture() {
    Class t2("UnityEngine", "Texture2D");
    Method<void> ctor(FindOverload(t2, ".ctor", {"System.Int32", "System.Int32", "UnityEngine.TextureFormat", "System.Boolean"}));
    if (!ctor.IsValid() || !p_object_new || !p_array_new) return false;
    Il2CppObject* tex = (Il2CppObject*)p_object_new(t2.GetClass());
    if (!tex) return false;
    ctor[tex](g_vp.w, g_vp.h, 3 /* RGB24 */, false);
    try {
        Class tx("UnityEngine", "Texture");
        Method<void>(tx.GetMethod("set_wrapMode", 1))[tex](1);
        Method<void>(tx.GetMethod("set_filterMode", 1))[tex](1);
    } catch (...) {}
    void* arr = p_array_new(Class("System", "Byte").GetClass(), (size_t)g_vp.w * g_vp.h * 3);
    if (!arr) return false;
    g_vp.tex = Pin(tex); g_vp.buf = Pin(arr);
    return true;
}
static void VideoUploadFrame(int local) {
    Il2CppObject* tex = g_vp.tex && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_vp.tex) : nullptr;
    void* arr = g_vp.buf && p_gchandle_target ? p_gchandle_target(g_vp.buf) : nullptr;
    if (!tex || !arr || local < 0 || local >= g_vp.cache.n) return;
    Class t2("UnityEngine", "Texture2D");
    Method<void> load(FindOverload(t2, "LoadRawTextureData", {"System.Byte[]"}));
    Method<void> apply(FindOverload(t2, "Apply", {"System.Boolean", "System.Boolean"}));
    if (!load.IsValid() || !apply.IsValid()) return;
    size_t n = (size_t)g_vp.w * g_vp.h * 3;
    memcpy((uint8_t*)arr + 0x20, g_vp.cache.pix.data() + n * local, n);
    load[tex]((Il2CppObject*)arr);
    apply[tex](false, false);
}
static void VideoBuild(double now) {
    try {
        if (!VideoBuildTexture()) { BNM_FileCheckpoint("Video: texture build FAILED"); StopVideo(); return; }
        Il2CppObject* clip = nullptr;
        if (g_vp.audioWanted && !g_vp.pcm.empty() && p_array_new) {
            Class ac("UnityEngine", "AudioClip");
            Method<Il2CppObject*> create(FindOverload(ac, "Create", {"System.String", "System.Int32", "System.Int32", "System.Int32", "System.Boolean"}));
            Method<bool> setData(FindOverload(ac, "SetData", {"System.Single[]", "System.Int32"}));
            if (create.IsValid() && setData.IsValid()) {
                clip = create(CreateMonoString("BlazeVideoAudio"), (int)g_vp.pcm.size(), 1, g_vp.pcmRate > 0 ? g_vp.pcmRate : 44100, false);
                if (clip) {
                    void* a = p_array_new(Class("System", "Single").GetClass(), g_vp.pcm.size());
                    if (a) { memcpy((uint8_t*)a + 0x20, g_vp.pcm.data(), g_vp.pcm.size() * sizeof(float)); setData[clip]((Il2CppObject*)a, 0); }
                    g_vp.clip = Pin(clip);
                }
            }
        }
        Class goc("UnityEngine", "GameObject");
        Method<Il2CppObject*> addComp(goc.GetMethod("AddComponent", 1));
        Method<Il2CppObject*> getTr(goc.GetMethod("get_transform", 0));
        Il2CppObject* canvasType = sysTypeOf("UnityEngine.Canvas, UnityEngine.UIModule");
        Il2CppObject* rawType = sysTypeOf("UnityEngine.UI.RawImage, UnityEngine.UI");
        if (!addComp.IsValid() || !canvasType || !rawType) { BNM_FileCheckpoint("Video: UI types NOT FOUND"); StopVideo(); return; }
        Il2CppObject* go = goc.CreateNewObjectParameters(CreateMonoString("BlazeVideo"));
        if (!go) { StopVideo(); return; }
        Il2CppObject* canvas = addComp[go](canvasType);
        if (!canvas) { StopVideo(); return; }
        Class cv("UnityEngine", "Canvas");
        Method<void>(cv.GetMethod("set_renderMode", 1))[canvas](2);        // WorldSpace
        Method<void>(cv.GetMethod("set_sortingOrder", 1))[canvas](999);
        Class rt("UnityEngine", "RectTransform");
        Class tr("UnityEngine", "Transform");
        Il2CppObject* rootT = getTr[go]();
        Method<void>(rt.GetMethod("set_sizeDelta", 1))[rootT](Vector2(800.f, 450.f));
        Method<void>(tr.GetMethod("set_localScale", 1))[rootT](Vector3(0.0024f, 0.0024f, 0.0024f));
        Il2CppObject* child = goc.CreateNewObjectParameters(CreateMonoString("RawImage"));
        Il2CppObject* raw = child ? addComp[child](rawType) : nullptr;
        if (!raw) { StopVideo(); return; }
        Il2CppObject* childT = getTr[child]();
        Method<void>(FindOverload(tr, "SetParent", {"UnityEngine.Transform", "System.Boolean"}))[childT](rootT, false);
        Method<void>(rt.GetMethod("set_sizeDelta", 1))[childT](Vector2(800.f, 450.f));
        Il2CppObject* tex = g_vp.tex && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_vp.tex) : nullptr;
        try { Method<void>(Class("UnityEngine.UI", "RawImage").GetMethod("set_texture", 1))[raw](tex); } catch (...) {}
        try { Method<void>(Class("UnityEngine.UI", "Graphic").GetMethod("set_raycastTarget", 1))[raw](false); } catch (...) {}
        g_vp.go = Pin(go); g_vp.rawImg = Pin(raw);
        if (clip) {
            Il2CppObject* asType = sysTypeOf("UnityEngine.AudioSource, UnityEngine.AudioModule");
            Il2CppObject* src = asType ? addComp[go](asType) : nullptr;
            if (src) {
                Class as("UnityEngine", "AudioSource");
                try { Method<void>(as.GetMethod("set_clip", 1))[src](clip); } catch (...) {}
                try { Method<void>(as.GetMethod("set_loop", 1))[src](g_vp.loop); } catch (...) {}
                try { Method<void>(as.GetMethod("set_spatialBlend", 1))[src](0.0f); } catch (...) {}   // 2D: audible everywhere
                try { Method<void>(as.GetMethod("set_volume", 1))[src](1.0f); } catch (...) {}
                g_vp.audioSrc = Pin(src);
                try { Method<void>(as.GetMethod("Play", 0))[src](); } catch (...) {}
            }
        }
        g_vp.built = true; g_vp.startAt = now; g_vp.curFrame = -1;
        if (!g_vp.segBlobs.empty() && DecodeScreen(g_vp.segBlobs[0], g_vp.cache)) { g_vp.cacheSeg = 0; VideoUploadFrame(0); }
        FollowHead(go);
        BNM_FileCheckpointF("Video: PLAYING %s (%d frames @%dfps, %dx%d, audio=%d)",
                            g_vp.id.c_str(), g_vp.totalFrames, g_vp.fps, g_vp.w, g_vp.h, (int)(clip != nullptr));
    } catch (...) { BNM_FileCheckpoint("Video: build EXCEPTION"); StopVideo(); }
}
static void VideoPump(double now) {
    // audio: one request, decoded on a worker thread
    if (g_vp.audioWanted && !g_vp.audioReqActive && !g_vp.audioDecoded && !g_vp.audioDecoding && g_vp.pcm.empty()) {
        if (StartGet(g_vp.audioUrl, g_vp.audioReq)) g_vp.audioReqActive = true;
        else g_vp.audioWanted = false;
    }
    if (g_vp.audioReqActive && ReqDone(g_vp.audioReq)) {
        if (ReqOk(g_vp.audioReq.req)) {
            std::string b64 = ReqText(g_vp.audioReq.req);
            g_vp.audioDecoding = true;
            { std::lock_guard<std::mutex> lk(g_vpAudMtx); g_vpAudDone = false; }
            std::thread([b64]() {
                std::vector<float> pcm; int rate = 0; bool ok = DecodeToMono(B64Decode(b64), pcm, rate);
                std::lock_guard<std::mutex> lk(g_vpAudMtx);
                g_vpAudPcm.swap(pcm); g_vpAudRate = rate; g_vpAudOk = ok; g_vpAudDone = true;
            }).detach();
        } else { g_vp.audioWanted = false; }   // audio failed -> play silent
        ReqDispose(g_vp.audioReq); g_vp.audioReqActive = false;
    }
    if (g_vp.audioDecoding) {
        std::lock_guard<std::mutex> lk(g_vpAudMtx);
        if (g_vpAudDone) {
            if (g_vpAudOk) { g_vp.pcm.swap(g_vpAudPcm); g_vp.pcmRate = g_vpAudRate; g_vp.audioDecoded = true; }
            else g_vp.audioWanted = false;
            g_vp.audioDecoding = false; g_vpAudDone = false; g_vpAudPcm.clear();
        }
    }
    // segments: sequential download, kept compressed
    if (!g_vp.allSegs) {
        if (!g_vp.segReqActive && g_vp.segNext < g_vp.nSeg) {
            std::string url = g_vp.segBase + std::to_string(g_vp.segNext) + ".b64";
            if (StartGet(url, g_vp.segReq)) g_vp.segReqActive = true;
        }
        if (g_vp.segReqActive && ReqDone(g_vp.segReq)) {
            if (ReqOk(g_vp.segReq.req)) {
                std::vector<uint8_t> blob = B64Decode(ReqText(g_vp.segReq.req));
                int n = (blob.size() >= 10 && memcmp(blob.data(), "BSCR", 4) == 0) ? (blob[8] | (blob[9] << 8)) : 0;
                if (n > 0) { g_vp.segBlobs.push_back(std::move(blob)); g_vp.segNext++; }
                else { BNM_FileCheckpointF("Video: segment %d bad -> abort", g_vp.segNext); ReqDispose(g_vp.segReq); g_vp.segReqActive = false; StopVideo(); return; }
                if (g_vp.segNext >= g_vp.nSeg) g_vp.allSegs = true;
            } else { BNM_FileCheckpointF("Video: segment %d download FAILED -> abort", g_vp.segNext); ReqDispose(g_vp.segReq); g_vp.segReqActive = false; StopVideo(); return; }
            ReqDispose(g_vp.segReq); g_vp.segReqActive = false;
        }
    }
    // build as soon as segment 0 is in and audio is ready (or gave up / timed out);
    // later segments keep streaming while it plays
    bool audioReady = !g_vp.audioWanted || g_vp.audioDecoded;
    if (!audioReady && !g_vp.segBlobs.empty() && (now - g_vp.loadStart) > 15.0) { g_vp.audioWanted = false; audioReady = true; }
    if (!g_vp.built && !g_vp.segBlobs.empty() && g_vp.totalFrames > 0 && audioReady) VideoBuild(now);
}
static std::string g_vpDoneId;   // live61: a play-once video that already finished here (don't replay it)
static void VideoDisplayTick(double now) {
    Il2CppObject* go = g_vp.go && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_vp.go) : nullptr;
    if (!go || !Alive(go)) { VideoFreeObjects(); g_vp.built = false; g_vp.startAt = now; return; }   // room reload destroyed it -> rebuild
    double t;
    Il2CppObject* src = g_vp.audioSrc && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_vp.audioSrc) : nullptr;
    if (src && Alive(src)) { try { t = (double)Method<float>(Class("UnityEngine", "AudioSource").GetMethod("get_time", 0))[src](); } catch (...) { t = now - g_vp.startAt; } }
    else t = now - g_vp.startAt;
    // live61 (owner 2026-10-18 "make the videos stop at the end"): a play-once video
    // disappears when this headset's playback reaches its end
    if (!g_vp.loop && g_vp.totalFrames > 0 && t * g_vp.fps >= g_vp.totalFrames + g_vp.fps * 0.5) {
        g_vpDoneId = g_vp.id;
        BNM_FileCheckpoint("Video: finished (play once)");
        StopVideo();
        return;
    }
    if (g_vp.totalFrames > 0) {
        int want = (int)(t * g_vp.fps);
        if (g_vp.loop) { want %= g_vp.totalFrames; if (want < 0) want += g_vp.totalFrames; }
        else if (want >= g_vp.totalFrames) want = g_vp.totalFrames - 1;
        int seg = want / g_vp.segFrames, local = want % g_vp.segFrames;
        // a segment that hasn't streamed in yet: hold the current frame
        if (want != g_vp.curFrame && seg < (int)g_vp.segBlobs.size()) {
            if (seg != g_vp.cacheSeg) {
                DecodedScreen d;
                if (DecodeScreen(g_vp.segBlobs[seg], d)) { g_vp.cache = std::move(d); g_vp.cacheSeg = seg; }
            }
            if (g_vp.cacheSeg == seg && local < g_vp.cache.n) {
                try { VideoUploadFrame(local); } catch (...) {}
                g_vp.curFrame = want;
            }
        }
    }
    FollowHead(go);
}
// 131b9: dedicated 3 s poll of the active video so Load/Stop land within
// seconds (the general feed is 30 s). Body is one V2 line or empty (= stop):
//   V2 id WxHxFPS nSeg segFrames frames loop audioUrl segBase
// A FAILED request changes nothing (only a real empty answer stops the video).
static const char* kVideoStateUrl = "https://live.your-domain.example/client/video.txt";
static PendingReq g_vidPollReq;
// big: the feed / video-state polls run on a background thread with plain HTTP
// (UnityWebRequest + by-name method lookups on the main thread cost 7-36 ms each)
static std::mutex g_bgPollMu;
// Map builders (owner 2026-10-18): accounts allowed to keep a shared map in
// creative. Fetched from /client/creative_builders by BgPollThread; the owner is
// always allowed even before the first fetch. CreativeGuardTick lets any builder
// who is in the room keep creative on (tagged with creativeBy=<their account>).
static std::mutex g_buildersMu;
static std::set<std::string> g_builders{"o_OWNER0000000001"};
static bool IsCreativeBuilder(const std::string& acct) {
    if (acct.empty()) return false;
    std::lock_guard<std::mutex> lk(g_buildersMu);
    for (const auto& b : g_builders) if (acct.rfind(b, 0) == 0) return true;   // userId may carry a suffix
    return false;
}
static std::string g_bgVideoBody, g_bgFeedBody, g_bgBuildersBody, g_bgTvBody;
static bool g_bgTvNew = false;
static bool g_bgVideoNew = false, g_bgFeedNew = false, g_bgBuildersNew = false;
static void BgPollThread() {
    double nextVid = 0, nextFeed = 0, nextTv = 0;
    for (;;) {
        double now = std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
        try {
            if (now >= nextVid || now >= nextFeed || now >= nextTv) {
                httplib::Client cli("live.your-domain.example", 80);
                cli.set_connection_timeout(8); cli.set_read_timeout(8);
                if (now >= nextVid) {
                    nextVid = now + 12.0;
                    auto r = cli.Get("/client/video.txt");
                    if (r && r->status == 200) { std::lock_guard<std::mutex> lk(g_bgPollMu); g_bgVideoBody = r->body; g_bgVideoNew = true; }
                }
                if (now >= nextTv) {
                    nextTv = now + 10.0;
                    auto r = cli.Get("/client/tv.txt");
                    if (r && r->status == 200) { std::lock_guard<std::mutex> lk(g_bgPollMu); g_bgTvBody = r->body; g_bgTvNew = true; }
                }
                if (now >= nextFeed) {
                    nextFeed = now + 30.0;
                    auto r = cli.Get("/client/feed.txt");
                    if (r && r->status == 200) { std::lock_guard<std::mutex> lk(g_bgPollMu); g_bgFeedBody = r->body; g_bgFeedNew = true; }
                    auto rb = cli.Get("/client/creative_builders");
                    if (rb && rb->status == 200) { std::lock_guard<std::mutex> lk(g_bgPollMu); g_bgBuildersBody = rb->body; g_bgBuildersNew = true; }
                }
            }
        } catch (...) {}
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }
}
static double g_nextVidPollAt = 0;
static void ParseVideoState(const std::string& body) {
    VideoWanted vw; bool seen = false;
    size_t pos = 0;
    while (pos < body.size() && !seen) {
        size_t nl = body.find('\n', pos);
        std::string line = body.substr(pos, nl == std::string::npos ? std::string::npos : nl - pos);
        pos = nl == std::string::npos ? body.size() : nl + 1;
        if (line.rfind("V2\t", 0) != 0) continue;
        std::vector<std::string> g; size_t a = 0, b;
        while ((b = line.find('\t', a)) != std::string::npos) { g.push_back(line.substr(a, b - a)); a = b + 1; }
        g.push_back(line.substr(a));
        if (g.size() < 9) continue;
        int w = 0, h = 0, fps = 10; sscanf(g[2].c_str(), "%dx%dx%d", &w, &h, &fps);
        vw.id = g[1]; vw.w = w; vw.h = h; vw.fps = fps > 0 ? fps : 10;
        vw.nSeg = atoi(g[3].c_str()); vw.segFrames = atoi(g[4].c_str()); vw.frames = atoi(g[5].c_str());
        vw.loop = (g[6] != "0"); vw.audioUrl = g[7]; vw.segBase = g[8];
        seen = vw.w > 0 && vw.h > 0 && vw.nSeg > 0;
    }
    std::lock_guard<std::mutex> lk(g_vidReqMtx);
    g_vidWanted = seen ? vw : VideoWanted{};
    g_vidWantedSet = true;
}
static void VideoTick(double now) {
    {   // big: body fetched by BgPollThread
        std::string body; bool fresh = false;
        { std::lock_guard<std::mutex> lk(g_bgPollMu); if (g_bgVideoNew) { body.swap(g_bgVideoBody); g_bgVideoNew = false; fresh = true; } }
        if (fresh) { try { ParseVideoState(body); } catch (...) {} }
    }
    VideoWanted w; bool set;
    { std::lock_guard<std::mutex> lk(g_vidReqMtx); w = g_vidWanted; set = g_vidWantedSet; }
    if (set && w.id != g_vp.id) {
        if (w.id.empty()) { StopVideo(); g_vpDoneId.clear(); }
        else if (w.id != g_vpDoneId) StartVideoLoad(w);
    }
    if (g_vp.id.empty()) return;
    VideoPump(now);                                  // keeps streaming segments after playback starts
    if (g_vp.built && !g_vp.id.empty()) VideoDisplayTick(now);
}

// =====================================================================
//  Yeeps TV (live92, owner 2026-11-01): every 1x1 computer block shows the
//  same video channel, with sound from the nearest computer.
//  - TvPatchPrefab (from RtInject, at map-object load) gives the computer's
//    screen sub-mesh real UVs and our "blazeScreen_tv" picture material, so the
//    frames we write into one texture show on every computer (incl. the game's
//    batched material copies, found by ScanScreenMaterials under slot "tv").
//  - The server (services/tv.py) publishes /client/tv.txt:
//      T1 id WxHxFPS nSeg segFrames frames startMs segBase audBase
//    Playback time = wall clock - startMs, so everyone sees the same spot.
//  - Segments (BSCR frames + an OGG chunk of the same 10 s) stream a few ahead
//    and are freed behind playback, so long videos never pile up in memory.
struct TvSeg { std::vector<uint8_t> blob; DecodedScreen dec; bool decoding = false, decoded = false; };
struct TvAud { std::vector<float> pcm; int rate = 0; bool decoding = false, decoded = false, failed = false; };
struct TvState {
    std::string id, segBase, audBase;
    int w = 0, h = 0, fps = 12, nSeg = 0, segFrames = 120, frames = 0; long long startMs = 0;
    std::map<int, std::shared_ptr<TvSeg>> segs;
    std::map<int, std::shared_ptr<TvAud>> auds;
    PendingReq vReq, aReq; int vReqSeg = -1, aReqSeg = -1;
    int curFrame = -1, audSeg = -1;
    void* audClip = nullptr;
};
static TvState g_tv;
static std::mutex g_tvMtx;                 // guards TvSeg/TvAud decode results
static void* g_tvMat = nullptr;            // our picture material (pinned)
static void* g_tvTex = nullptr; static void* g_tvBuf = nullptr; static int g_tvTexW = 0, g_tvTexH = 0;
static void* g_tvAudGo = nullptr; static void* g_tvAudSrc = nullptr;
static bool g_tvPrefabDone = false;
static double g_tvNextMatPush = 0, g_tvNextAudPlace = 0;

static long long EpochMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
}
static Il2CppObject* TvTex(int w, int h) {
    Il2CppObject* tex = g_tvTex && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_tvTex) : nullptr;
    if (tex && Alive(tex) && g_tvTexW == w && g_tvTexH == h) return tex;
    Unpin(g_tvTex); g_tvTex = nullptr; Unpin(g_tvBuf); g_tvBuf = nullptr;
    Class t2("UnityEngine", "Texture2D");
    Method<void> ctor(FindOverload(t2, ".ctor", {"System.Int32", "System.Int32", "UnityEngine.TextureFormat", "System.Boolean"}));
    if (!ctor.IsValid() || !p_object_new || !p_array_new) return nullptr;
    tex = (Il2CppObject*)p_object_new(t2.GetClass());
    if (!tex) return nullptr;
    ctor[tex](w, h, 3 /* RGB24 */, false);
    try {
        Class tx("UnityEngine", "Texture");
        Method<void>(tx.GetMethod("set_wrapMode", 1))[tex](1);
        Method<void>(tx.GetMethod("set_filterMode", 1))[tex](1);
        Method<void>(Class("UnityEngine", "Object").GetMethod("DontDestroyOnLoad", 1))(tex);
    } catch (...) {}
    void* arr = p_array_new(Class("System", "Byte").GetClass(), (size_t)w * h * 3);
    if (!arr) return nullptr;
    memset((uint8_t*)arr + 0x20, 0, (size_t)w * h * 3);
    g_tvTex = Pin(tex); g_tvBuf = Pin(arr); g_tvTexW = w; g_tvTexH = h;
    try {
        Method<void>(FindOverload(t2, "LoadRawTextureData", {"System.Byte[]"}))[tex]((Il2CppObject*)arr);
        Method<void>(FindOverload(t2, "Apply", {"System.Boolean", "System.Boolean"}))[tex](false, false);
    } catch (...) {}
    g_tvNextMatPush = 0;
    return tex;
}
static void TvPushTexture() {   // our material + every game-made copy of it
    Il2CppObject* tex = g_tvTex && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_tvTex) : nullptr;
    if (!tex) return;
    static Method<void> set(FindOverload(Class("UnityEngine", "Material"), "SetTexture", {"System.String", "UnityEngine.Texture"}));
    if (!set.IsValid()) return;
    Il2CppObject* m = g_tvMat && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_tvMat) : nullptr;
    if (m && Alive(m)) set[m](CreateMonoString("_MainTex"), tex);
    for (void* h : g_scrMatCopies["tv"]) {
        Il2CppObject* c = p_gchandle_target ? (Il2CppObject*)p_gchandle_target(h) : nullptr;
        if (c && c != m) { try { set[c](CreateMonoString("_MainTex"), tex); } catch (...) {} }
    }
}
static void TvUpload(const DecodedScreen& d, int local) {
    Il2CppObject* tex = TvTex(d.w, d.h);
    void* arr = g_tvBuf && p_gchandle_target ? p_gchandle_target(g_tvBuf) : nullptr;
    if (!tex || !arr || local < 0 || local >= d.n) return;
    static Method<void> load(FindOverload(Class("UnityEngine", "Texture2D"), "LoadRawTextureData", {"System.Byte[]"}));
    static Method<void> apply(FindOverload(Class("UnityEngine", "Texture2D"), "Apply", {"System.Boolean", "System.Boolean"}));
    if (!load.IsValid() || !apply.IsValid()) return;
    size_t n = (size_t)d.w * d.h * 3;
    memcpy((uint8_t*)arr + 0x20, d.pix.data() + n * local, n);
    load[tex]((Il2CppObject*)arr);
    apply[tex](false, false);
}
static void TvBlank() {
    if (!g_tvTex) return;
    Il2CppObject* tex = TvTex(g_tvTexW, g_tvTexH);
    void* arr = g_tvBuf && p_gchandle_target ? p_gchandle_target(g_tvBuf) : nullptr;
    if (!tex || !arr) return;
    memset((uint8_t*)arr + 0x20, 0, (size_t)g_tvTexW * g_tvTexH * 3);
    try {
        Method<void>(FindOverload(Class("UnityEngine", "Texture2D"), "LoadRawTextureData", {"System.Byte[]"}))[tex]((Il2CppObject*)arr);
        Method<void>(FindOverload(Class("UnityEngine", "Texture2D"), "Apply", {"System.Boolean", "System.Boolean"}))[tex](false, false);
    } catch (...) {}
}

// Called for every Resources.LoadAll result until the computer prefab is found.
static void TvPatchPrefab(void* arr) {
    if (g_tvPrefabDone || !arr) return;
    uintptr_t n = *(uintptr_t*)((uint8_t*)arr + 0x18);
    static Method<Il2CppObject*> getName(Class("UnityEngine", "Object").GetMethod("get_name", 0));
    Class goC("UnityEngine", "GameObject"), compC("UnityEngine", "Component"), objC("UnityEngine", "Object");
    for (uintptr_t i = 0; i < n; i++) {
        auto* e = (Il2CppObject*)((void**)((uint8_t*)arr + 0x20))[i];
        if (!e || MonoStr(getName[e]()) != "1x1_computer") continue;
        static int tries = 0;
        if (++tries > 8) { g_tvPrefabDone = true; return; }
        try {
            const char* kn = e->klass && e->klass->name ? e->klass->name : "?";
            bool isGo = e->klass == goC.GetClass();
            if (!isGo && !Class(e->klass).GetMethod("get_gameObject", 0).IsValid()) { BNM_FileCheckpointF("TV: 1x1_computer element is %s (not a GameObject/Component), skipping", kn); continue; }
            Il2CppObject* go = isGo ? e : Method<Il2CppObject*>(compC.GetMethod("get_gameObject", 0))[e]();
            Class mfC("UnityEngine", "MeshFilter"), mrC("UnityEngine", "MeshRenderer"), meshC("UnityEngine", "Mesh");
            // the prefab is not in a scene, so it counts as inactive: include inactive children
            Method<Il2CppObject*> getComp(FindOverload(goC, "GetComponentInChildren", {"System.Type", "System.Boolean"}));
            Il2CppObject* mf = go ? getComp[go]((Il2CppObject*)mfC.GetMonoType(), true) : nullptr;
            Il2CppObject* mr = go ? getComp[go]((Il2CppObject*)mrC.GetMonoType(), true) : nullptr;
            Il2CppObject* mesh = mf ? Method<Il2CppObject*>(mfC.GetMethod("get_sharedMesh", 0))[mf]() : nullptr;
            if (!mf || !mr || !mesh) { BNM_FileCheckpointF("TV: computer prefab (%s) parts missing go=%d mf=%d mr=%d mesh=%d", kn, go != nullptr, mf != nullptr, mr != nullptr, mesh != nullptr); continue; }
            g_tvPrefabDone = true;
            Il2CppObject* m2 = Method<Il2CppObject*>(FindOverload(objC, "Instantiate", {"UnityEngine.Object"}))(mesh);
            if (!m2) return;
            Method<void>(objC.GetMethod("DontDestroyOnLoad", 1))(m2);
            auto* verts = (uint8_t*)Method<Il2CppObject*>(meshC.GetMethod("get_vertices", 0))[m2]();
            auto* uvs = (uint8_t*)Method<Il2CppObject*>(meshC.GetMethod("get_uv", 0))[m2]();
            auto* tris = (uint8_t*)Method<Il2CppObject*>(FindOverload(meshC, "GetTriangles", {"System.Int32"}))[m2](0);
            if (!verts || !uvs || !tris) { BNM_FileCheckpoint("TV: mesh arrays missing"); return; }
            uintptr_t nv = *(uintptr_t*)(verts + 0x18), nuv = *(uintptr_t*)(uvs + 0x18), nt = *(uintptr_t*)(tris + 0x18);
            float* V = (float*)(verts + 0x20); float* U = (float*)(uvs + 0x20); int* T = (int*)(tris + 0x20);
            float x0 = 1e9f, x1 = -1e9f, y0 = 1e9f, y1 = -1e9f;
            for (uintptr_t k = 0; k < nt; k++) { int v = T[k]; if (v < 0 || (uintptr_t)v >= nv) continue;
                x0 = std::min(x0, V[v * 3]); x1 = std::max(x1, V[v * 3]); y0 = std::min(y0, V[v * 3 + 1]); y1 = std::max(y1, V[v * 3 + 1]); }
            if (nuv != nv || x1 <= x0 || y1 <= y0) { BNM_FileCheckpointF("TV: bad screen bounds nv=%d nuv=%d", (int)nv, (int)nuv); return; }
            for (uintptr_t k = 0; k < nt; k++) { int v = T[k]; if (v < 0 || (uintptr_t)v >= nv) continue;
                U[v * 2] = (V[v * 3] - x0) / (x1 - x0); U[v * 2 + 1] = (V[v * 3 + 1] - y0) / (y1 - y0); }
            Method<void>(FindOverload(meshC, "set_uv", {"UnityEngine.Vector2[]"}))[m2]((Il2CppObject*)uvs);
            Method<void>(objC.GetMethod("set_name", 1))[m2](CreateMonoString("1x1_wiring_computer_tv"));
            Method<void>(mfC.GetMethod("set_sharedMesh", 1))[mf](m2);
            Il2CppObject* tex = TvTex(128, 96);
            Il2CppObject* mat = RtPictureMaterial("blazeScreen_tv", tex);
            if (!mat) return;
            g_tvMat = Pin(mat);
            Class rC("UnityEngine", "Renderer");
            auto* mats = (uint8_t*)Method<Il2CppObject*>(rC.GetMethod("get_sharedMaterials", 0))[mr]();
            if (mats && *(uintptr_t*)(mats + 0x18) >= 1) {
                ((void**)(mats + 0x20))[0] = mat;
                Method<void>(FindOverload(rC, "set_sharedMaterials", {"UnityEngine.Material[]"}))[mr]((Il2CppObject*)mats);
            }
            // When powered the game swaps the screen to its own lit material, kept in
            // a Material field on one of the block's components: point those at ours.
            try {
                std::string body = mats && *(uintptr_t*)(mats + 0x18) >= 2 ? MonoStr(getName[(Il2CppObject*)((void**)(mats + 0x20))[1]]()) : "";
                Method<Il2CppObject*> getComps(FindOverload(goC, "GetComponentsInChildren", {"System.Type", "System.Boolean"}));
                auto* comps = (uint8_t*)getComps[go]((Il2CppObject*)compC.GetMonoType(), true);
                Il2CppClass* matK = Class("UnityEngine", "Material").GetClass();
                uintptr_t nc = comps ? *(uintptr_t*)(comps + 0x18) : 0;
                for (uintptr_t ci = 0; ci < nc; ci++) {
                    auto* c = (Il2CppObject*)((void**)(comps + 0x20))[ci];
                    if (!c) continue;
                    for (Il2CppClass* k = c->klass; k; k = k->parent) {
                        if (!k->image || strstr(k->image->name ? k->image->name : "", "Assembly-CSharp") == nullptr) break;
                        for (auto& f : Class(k).GetFields(false)) {
                            auto* fi = f.GetInfo();
                            if (!fi || !fi->type || (fi->type->attrs & 0x10) || fi->offset <= 0) continue;
                            if (Class(fi->type).GetClass() != matK) continue;
                            auto* val = *(Il2CppObject**)((uint8_t*)c + fi->offset);
                            std::string vn = val ? MonoStr(getName[val]()) : "null";
                            bool swap = val && vn != body;
                            BNM_FileCheckpointF("TV: %s.%s = %s%s", k->name, fi->name, vn.c_str(), swap ? " -> blazeScreen_tv" : "");
                            if (swap) *(Il2CppObject**)((uint8_t*)c + fi->offset) = mat;
                        }
                    }
                }
            } catch (...) { BNM_FileCheckpoint("TV: material field scan EXCEPTION"); }
            BNM_FileCheckpointF("TV: computer prefab patched (screen %.3f..%.3f x %.3f..%.3f, %d tris)", x0, x1, y0, y1, (int)nt / 3);
        } catch (...) { BNM_FileCheckpoint("TV: prefab patch EXCEPTION"); }
        return;
    }
}

static void TvStop() {
    if (g_tv.vReqSeg >= 0) ReqDispose(g_tv.vReq);
    if (g_tv.aReqSeg >= 0) ReqDispose(g_tv.aReq);
    Il2CppObject* src = g_tvAudSrc && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_tvAudSrc) : nullptr;
    if (src && Alive(src)) { try { Method<void>(Class("UnityEngine", "AudioSource").GetMethod("Stop", 0))[src](); } catch (...) {} }
    Il2CppObject* clip = g_tv.audClip && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_tv.audClip) : nullptr;
    if (clip) { try { Method<void>(Class("UnityEngine", "Object").GetMethod("Destroy", 1))(clip); } catch (...) {} }
    Unpin(g_tv.audClip);
    bool had = !g_tv.id.empty();
    g_tv = TvState{};
    TvBlank();
    if (had) BNM_FileCheckpoint("TV: stopped");
}
static void TvParse(const std::string& body) {
    TvState w; bool seen = false;
    size_t a = body.find("T1\t");
    if (a != std::string::npos) {
        std::string line = body.substr(a, body.find('\n', a) == std::string::npos ? std::string::npos : body.find('\n', a) - a);
        std::vector<std::string> g; size_t p = 0, b;
        while ((b = line.find('\t', p)) != std::string::npos) { g.push_back(line.substr(p, b - p)); p = b + 1; }
        g.push_back(line.substr(p));
        if (g.size() >= 9) {
            sscanf(g[2].c_str(), "%dx%dx%d", &w.w, &w.h, &w.fps);
            w.id = g[1]; w.nSeg = atoi(g[3].c_str()); w.segFrames = atoi(g[4].c_str()); w.frames = atoi(g[5].c_str());
            while (!g[8].empty() && (g[8].back() == '\r' || g[8].back() == '\n' || g[8].back() == ' ')) g[8].pop_back();
            w.startMs = atoll(g[6].c_str()); w.segBase = g[7]; w.audBase = g[8] == "-" ? "" : g[8];
            seen = w.w > 0 && w.h > 0 && w.fps > 0 && w.nSeg > 0 && w.segFrames > 0;
        }
    }
    g_tvWantMats = g_tvPrefabDone;
    if (!seen) { if (!g_tv.id.empty()) TvStop(); return; }
    if (w.id == g_tv.id && w.startMs == g_tv.startMs) return;
    TvStop();
    g_tv.id = w.id; g_tv.w = w.w; g_tv.h = w.h; g_tv.fps = w.fps; g_tv.nSeg = w.nSeg; g_tv.segFrames = w.segFrames;
    g_tv.frames = w.frames; g_tv.startMs = w.startMs; g_tv.segBase = w.segBase; g_tv.audBase = w.audBase;
    BNM_FileCheckpointF("TV: channel %s (%dx%d@%d, %d segs, audio=%d)", w.id.c_str(), w.w, w.h, w.fps, w.nSeg, (int)!w.audBase.empty());
}
// The game draws placed blocks through its own combined batches, so the prefab
// material alone never reached the screen. Each computer near the player gets a
// thin panel of ours (not batched) just in front of its screen instead.
static std::map<void*, void*> g_tvPanels;   // computer transform (raw) -> pinned panel GameObject
static void TvEnsurePanel(Il2CppObject* compT) {
    auto it = g_tvPanels.find(compT);
    if (it != g_tvPanels.end()) {
        Il2CppObject* p = p_gchandle_target ? (Il2CppObject*)p_gchandle_target(it->second) : nullptr;
        if (p && Alive(p)) return;
        Unpin(it->second); g_tvPanels.erase(it);
    }
    Il2CppObject* mat = g_tvMat && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_tvMat) : nullptr;
    if (!mat) return;
    Class goc("UnityEngine", "GameObject"), tr("UnityEngine", "Transform"), objC("UnityEngine", "Object");
    static Method<Il2CppObject*> prim(goc.GetMethod("CreatePrimitive", 1));
    if (!prim.IsValid()) return;
    Il2CppObject* q = prim(5 /* Quad */);
    if (!q) return;
    Method<void>(objC.GetMethod("set_name", 1))[q](CreateMonoString("BlazeTVPanel"));
    Il2CppObject* col = Method<Il2CppObject*>(FindOverload(goc, "GetComponent", {"System.Type"}))[q]((Il2CppObject*)Class("UnityEngine", "Collider").GetMonoType());
    if (col) Method<void>(FindOverload(objC, "DestroyImmediate", {"UnityEngine.Object"}))(col);
    Il2CppObject* mr = Method<Il2CppObject*>(FindOverload(goc, "GetComponent", {"System.Type"}))[q]((Il2CppObject*)Class("UnityEngine", "Renderer").GetMonoType());
    if (mr) Method<void>(Class("UnityEngine", "Renderer").GetMethod("set_sharedMaterial", 1))[mr](mat);
    Il2CppObject* qt = Method<Il2CppObject*>(goc.GetMethod("get_transform", 0))[q]();
    Method<void>(FindOverload(tr, "SetParent", {"UnityEngine.Transform", "System.Boolean"}))[qt](compT, false);
    Method<void>(tr.GetMethod("set_localPosition", 1))[qt](Vector3(0.f, 0.f, -0.241f));
    Method<void>(tr.GetMethod("set_localRotation", 1))[qt](Quaternion(0.f, 0.f, 0.f, 1.f));
    Method<void>(tr.GetMethod("set_localScale", 1))[qt](Vector3(0.39f, 0.39f, 1.f));
    g_tvPanels[compT] = Pin(q);
    static int n = 0;
    if (n++ < 6) BNM_FileCheckpoint("TV: panel added to a computer");
}
// Nearest computer within earshot -> move the 3D AudioSource there.
static void TvPlaceAudio(Il2CppObject* src) {
    Il2CppObject* head = HeadTransform();
    if (!head) return;
    Class tr("UnityEngine", "Transform"), compC("UnityEngine", "Component");
    Vector3 hp = Method<Vector3>(tr.GetMethod("get_position", 0))[head]();
    static Method<Il2CppObject*> overlap(FindOverload(Class("UnityEngine", "Physics"), "OverlapSphere", {"UnityEngine.Vector3", "System.Single"}));
    static Method<Il2CppObject*> getName(Class("UnityEngine", "Object").GetMethod("get_name", 0));
    if (!overlap.IsValid()) return;
    auto* cols = (uint8_t*)overlap(hp, 14.0f);
    float best = 1e9f; Vector3 bp;
    if (cols) {
        uintptr_t n = *(uintptr_t*)(cols + 0x18);
        for (uintptr_t i = 0; i < n && i < 512; i++) {
            auto* c = (Il2CppObject*)((void**)(cols + 0x20))[i];
            if (!c) continue;
            Il2CppObject* t = Method<Il2CppObject*>(compC.GetMethod("get_transform", 0))[c]();
            if (!t) continue;
            std::string nm = MonoStr(getName[t]());
            if (nm.rfind("1x1_computer", 0) != 0) {
                Il2CppObject* par = Method<Il2CppObject*>(tr.GetMethod("get_parent", 0))[t]();
                if (!par || MonoStr(getName[par]()).rfind("1x1_computer", 0) != 0) continue;
                t = par;
            }
            try { TvEnsurePanel(t); } catch (...) {}
            Vector3 p = Method<Vector3>(tr.GetMethod("get_position", 0))[t]();
            float d = (p.x - hp.x) * (p.x - hp.x) + (p.y - hp.y) * (p.y - hp.y) + (p.z - hp.z) * (p.z - hp.z);
            if (d < best) { best = d; bp = p; }
        }
    }
    Class as("UnityEngine", "AudioSource");
    Il2CppObject* go = g_tvAudGo && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_tvAudGo) : nullptr;
    if (best < 1e8f && go) {
        Il2CppObject* gt = Method<Il2CppObject*>(Class("UnityEngine", "GameObject").GetMethod("get_transform", 0))[go]();
        Method<void>(tr.GetMethod("set_position", 1))[gt](bp);
        Method<void>(as.GetMethod("set_volume", 1))[src](1.0f);
    } else {
        Method<void>(as.GetMethod("set_volume", 1))[src](0.0f);
    }
}
static Il2CppObject* TvAudioSource() {
    Il2CppObject* src = g_tvAudSrc && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_tvAudSrc) : nullptr;
    if (src && Alive(src)) return src;
    Unpin(g_tvAudSrc); g_tvAudSrc = nullptr; Unpin(g_tvAudGo); g_tvAudGo = nullptr;
    Class goc("UnityEngine", "GameObject");
    Il2CppObject* go = goc.CreateNewObjectParameters(CreateMonoString("BlazeTVAudio"));
    Il2CppObject* asType = sysTypeOf("UnityEngine.AudioSource, UnityEngine.AudioModule");
    if (!go || !asType) return nullptr;
    Method<void>(Class("UnityEngine", "Object").GetMethod("DontDestroyOnLoad", 1))(go);
    src = Method<Il2CppObject*>(goc.GetMethod("AddComponent", 1))[go](asType);
    if (!src) return nullptr;
    Class as("UnityEngine", "AudioSource");
    try {
        Method<void>(as.GetMethod("set_spatialBlend", 1))[src](1.0f);
        Method<void>(as.GetMethod("set_rolloffMode", 1))[src](1 /* Linear */);
        Method<void>(as.GetMethod("set_minDistance", 1))[src](1.5f);
        Method<void>(as.GetMethod("set_maxDistance", 1))[src](14.0f);
        Method<void>(as.GetMethod("set_volume", 1))[src](0.0f);
    } catch (...) {}
    g_tvAudGo = Pin(go); g_tvAudSrc = Pin(src);
    return src;
}
static void TvPump(int seg) {
    // drop what playback has passed
    for (auto it = g_tv.segs.begin(); it != g_tv.segs.end();) { if (it->first < seg && !it->second->decoding) it = g_tv.segs.erase(it); else ++it; }
    for (auto it = g_tv.auds.begin(); it != g_tv.auds.end();) { if (it->first < seg && !it->second->decoding) it = g_tv.auds.erase(it); else ++it; }
    // video: keep seg..seg+2 downloaded, decoded on a worker thread
    if (g_tv.vReqSeg >= 0 && ReqDone(g_tv.vReq)) {
        int k = g_tv.vReqSeg;
        if (ReqOk(g_tv.vReq.req)) {
            auto s = std::make_shared<TvSeg>();
            s->blob = B64Decode(ReqText(g_tv.vReq.req));
            s->decoding = true;
            g_tv.segs[k] = s;
            std::thread([s]() { DecodedScreen d; bool ok = DecodeScreen(s->blob, d);
                std::lock_guard<std::mutex> lk(g_tvMtx); if (ok) s->dec = std::move(d); s->blob.clear(); s->blob.shrink_to_fit(); s->decoded = ok; s->decoding = false; }).detach();
        }
        ReqDispose(g_tv.vReq); g_tv.vReqSeg = -1;
    }
    if (g_tv.vReqSeg < 0) {
        for (int k = seg; k < std::min(seg + 3, g_tv.nSeg); k++) {
            if (g_tv.segs.count(k)) continue;
            if (StartGet(g_tv.segBase + std::to_string(k) + ".b64", g_tv.vReq)) g_tv.vReqSeg = k;
            break;
        }
    }
    if (g_tv.audBase.empty()) return;
    if (g_tv.aReqSeg >= 0 && ReqDone(g_tv.aReq)) {
        int k = g_tv.aReqSeg;
        auto a = std::make_shared<TvAud>();
        if (ReqOk(g_tv.aReq.req)) {
            std::string b64 = ReqText(g_tv.aReq.req);
            a->decoding = true;
            std::thread([a, b64]() { std::vector<float> pcm; int rate = 0; bool ok = DecodeToMono(B64Decode(b64), pcm, rate);
                std::lock_guard<std::mutex> lk(g_tvMtx); a->pcm.swap(pcm); a->rate = rate; a->decoded = ok; a->failed = !ok; a->decoding = false; }).detach();
        } else a->failed = true;
        g_tv.auds[k] = a;
        ReqDispose(g_tv.aReq); g_tv.aReqSeg = -1;
    }
    if (g_tv.aReqSeg < 0) {
        for (int k = seg; k < std::min(seg + 2, g_tv.nSeg); k++) {
            if (g_tv.auds.count(k)) continue;
            if (StartGet(g_tv.audBase + std::to_string(k) + ".b64", g_tv.aReq)) g_tv.aReqSeg = k;
            break;
        }
    }
}
static void TvTick(double now) {
    {
        std::string body; bool fresh = false;
        { std::lock_guard<std::mutex> lk(g_bgPollMu); if (g_bgTvNew) { body.swap(g_bgTvBody); g_bgTvNew = false; fresh = true; } }
        if (fresh) { try { TvParse(body); } catch (...) {} }
    }
    if (now >= g_tvNextMatPush && g_tvPrefabDone) { g_tvNextMatPush = now + 2.0; try { TvPushTexture(); } catch (...) {} }
    if (g_tv.id.empty()) return;
    double t = (double)(EpochMs() - g_tv.startMs) / 1000.0;
    int frame = (int)(t * g_tv.fps);
    if (t < 0 || frame >= g_tv.frames) { if (t >= 0 && frame >= g_tv.frames + g_tv.fps) TvStop(); return; }
    int seg = frame / g_tv.segFrames, local = frame % g_tv.segFrames;
    TvPump(seg);
    // picture
    if (frame != g_tv.curFrame) {
        auto it = g_tv.segs.find(seg);
        if (it != g_tv.segs.end()) {
            std::lock_guard<std::mutex> lk(g_tvMtx);
            if (it->second->decoded && local < it->second->dec.n) {
                try { TvUpload(it->second->dec, local); } catch (...) {}
                if (g_tv.curFrame < 0) BNM_FileCheckpointF("TV: first frame (seg %d)", seg);
                g_tv.curFrame = frame;
            }
        }
    }
    // sound: each 10 s chunk starts at its offset into the segment
    Il2CppObject* src = TvAudioSource();
    if (!src) return;
    Class as("UnityEngine", "AudioSource");
    if (now >= g_tvNextAudPlace) { g_tvNextAudPlace = now + 1.0; try { TvPlaceAudio(src); } catch (...) {} }
    if (g_tv.audSeg != seg) {
        auto it = g_tv.auds.find(seg);
        if (it == g_tv.auds.end()) return;
        std::vector<float> pcm; int rate = 0;
        { std::lock_guard<std::mutex> lk(g_tvMtx); if (it->second->decoding) return; if (it->second->decoded) { pcm.swap(it->second->pcm); rate = it->second->rate; } }
        g_tv.audSeg = seg;
        if (pcm.empty() || rate <= 0) return;
        double off = t - (double)seg * g_tv.segFrames / g_tv.fps;
        Class ac("UnityEngine", "AudioClip");
        static Method<Il2CppObject*> create(FindOverload(ac, "Create", {"System.String", "System.Int32", "System.Int32", "System.Int32", "System.Boolean"}));
        static Method<bool> setData(FindOverload(ac, "SetData", {"System.Single[]", "System.Int32"}));
        if (!create.IsValid() || !setData.IsValid()) return;
        Il2CppObject* clip = create(CreateMonoString("BlazeTV"), (int)pcm.size(), 1, rate, false);
        if (!clip) return;
        void* arr = p_array_new(Class("System", "Single").GetClass(), pcm.size());
        if (!arr) return;
        memcpy((uint8_t*)arr + 0x20, pcm.data(), pcm.size() * sizeof(float));
        setData[clip]((Il2CppObject*)arr, 0);
        Il2CppObject* old = g_tv.audClip && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_tv.audClip) : nullptr;
        Method<void>(as.GetMethod("set_clip", 1))[src](clip);
        float clipLen = (float)pcm.size() / rate;
        if (off > 0 && off < clipLen - 0.05) Method<void>(as.GetMethod("set_time", 1))[src]((float)off);
        if (off < clipLen - 0.05) Method<void>(as.GetMethod("Play", 0))[src]();
        if (old) { try { Method<void>(Class("UnityEngine", "Object").GetMethod("Destroy", 1))(old); } catch (...) {} }
        Unpin(g_tv.audClip); g_tv.audClip = Pin(clip);
    }
}

// big APK: 2.4 remembers its last room in PlayerPrefs and falls back to ch2_hub;
// neither is in the big APK's world (the 1.31 world, see the 240 server's
// services/big_world.py). Any saved ch2_* room becomes 1.31's spawn room
// (official_1) before the first room load.
static std::string BigUrlDecode(const std::string& s) {
    std::string o;
    for (size_t i = 0; i < s.size(); i++) {
        if (s[i] == '%' && i + 2 < s.size()) { o += (char)strtol(s.substr(i + 1, 2).c_str(), nullptr, 16); i += 2; }
        else if (s[i] == '+') o += ' ';
        else o += s[i];
    }
    return o;
}
static void BigFixLastRoom() {
    try {
        const char* dir = "/data/data/com.TrassGames.Yeeps/shared_prefs";
        DIR* d = opendir(dir);
        if (!d) { RLOG("[bigroom] shared_prefs not readable"); return; }
        std::vector<std::string> keys;
        std::string names;
        while (dirent* e = readdir(d)) {
            std::string fn = e->d_name;
            if (fn.find("playerprefs") == std::string::npos) continue;
            FILE* f = fopen((std::string(dir) + "/" + fn).c_str(), "r");
            if (!f) continue;
            std::string txt; char buf[16384]; size_t r;
            while ((r = fread(buf, 1, sizeof buf, f)) > 0) txt.append(buf, r);
            fclose(f);
            size_t p = 0;
            while ((p = txt.find("<string name=\"", p)) != std::string::npos) {
                size_t q = txt.find('"', p + 14);
                if (q == std::string::npos) break;
                size_t v0 = txt.find('>', q);
                size_t v1 = v0 == std::string::npos ? v0 : txt.find("</string>", v0);
                if (v1 == std::string::npos) break;
                std::string name = BigUrlDecode(txt.substr(p + 14, q - p - 14));
                std::string val = BigUrlDecode(txt.substr(v0 + 1, v1 - v0 - 1));
                if (names.size() < 1500) names += name + " ";
                if (val.rfind("ch2_", 0) == 0 && val.size() < 64 && val.find_first_of(" {[\"") == std::string::npos) {
                    keys.push_back(name);
                    RLOG("[bigroom] pref '%s' = %s -> official_1", name.c_str(), val.c_str());
                }
                p = v1;
            }
        }
        closedir(d);
        RLOG("[bigroom] string prefs: %s", names.c_str());
        if (keys.empty()) { RLOG("[bigroom] no saved ch2 room"); return; }
        Class pp("UnityEngine", "PlayerPrefs");
        Method<void> set(FindOverload(pp, "SetString", {"System.String", "System.String"}));
        Method<void> save(pp.GetMethod("Save", 0));
        for (auto& k : keys) set(CreateMonoString(k), CreateMonoString("official_1"));
        if (save.IsValid()) save();
        RLOG("[bigroom] %d saved room(s) set to official_1", (int)keys.size());
    } catch (...) { RLOG("[bigroom] EXCEPTION"); }
}

static void DevlogPush(const std::string& line);

// big: which of the script classes only live's data uses does 2.4's CODE still
// have, and what are their fields? One "[cls]" devlog line per class found
// (own fields: name:type:attrs, "+SF" = [SerializeField]), parents included once,
// plus a "[cls] summary". Feeds the next port step (new MonoScripts for them).
#include "big_probe_classes.h"
// type of a field as "List`1<MobAttack>", "arr<UnityEngine.Transform>", ...; queues
// Assembly-CSharp data classes / structs it uses so their fields get dumped too
static std::string BigTypeDesc(const Il2CppType* t, std::vector<IL2CPP::Il2CppClass*>* nested, int depth = 0) {
    if (!t || depth > 4) return "?";
    if (t->type == 0x1d)                                         // SZARRAY
        return "arr<" + BigTypeDesc(t->data.type, nested, depth + 1) + ">";
    if (t->type == 0x15 && t->data.generic_class) {              // GENERICINST
        auto* gc = t->data.generic_class;
        std::string n = typeName(t);
        auto* inst = gc->context.class_inst;
        n += "<";
        for (unsigned i = 0; inst && i < inst->type_argc; i++)
            n += (i ? "," : "") + BigTypeDesc(inst->type_argv[i], nested, depth + 1);
        return n + ">";
    }
    std::string n = typeName(t);
    Class c(t);
    auto* k = c.IsValid() ? c.GetClass() : nullptr;
    if (k) {
        if (k->enumtype) return n + "!E";
        bool isObj = false;
        for (auto* q = k; q; q = q->parent)
            if (q->name && q->namespaze && !strcmp(q->name, "Object") && !strcmp(q->namespaze, "UnityEngine")) { isObj = true; break; }
        if (isObj) return n + "!O";
        if (nested && k->image && k->image->name &&
            (strstr(k->image->name, "Assembly-CSharp") || strstr(k->image->name, "Unity.XR.Interaction.Toolkit")))
            nested->push_back(k);
    }
    return n;
}

static void BigClassProbe() {
    try {
        typedef bool (*fha_t)(void*, void*);
        static auto fieldHasAttr = (fha_t)BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_field_has_attribute");
        Class sfCls("UnityEngine", "SerializeField", Image("UnityEngine.CoreModule.dll"));
        if (!sfCls.IsValid()) sfCls = Class("UnityEngine", "SerializeField");
        void* sfK = sfCls.IsValid() ? (void*)sfCls.GetClass() : nullptr;
        DevlogPush(std::string("[cls] attr check: has_attribute=") + (fieldHasAttr ? "yes" : "NO") + " SerializeField=" + (sfK ? "yes" : "NO"));
        std::set<std::string> done;
        std::vector<IL2CPP::Il2CppClass*> nested;
        int found = 0, missing = 0;
        std::string miss;
        auto dumpOne = [&](IL2CPP::Il2CppClass* k) {
            std::string key = std::string(k->namespaze ? k->namespaze : "") + "." + (k->name ? k->name : "?");
            if (done.count(key)) return;
            done.insert(key);
            std::string line = "[cls] " + key + " parent=" + (k->parent && k->parent->name ? k->parent->name : "-") +
                               " size=" + std::to_string(k->instance_size) + " flags=" + std::to_string(k->flags) +
                               " vt=" + std::to_string((int)k->byval_arg.valuetype) + " |";
            Class c(k);
            for (auto& f : c.GetFields(false)) {
                auto* fi = f.GetInfo();
                if (!f.IsValid() || !fi || !fi->name || !fi->type) continue;
                unsigned a = fi->type->attrs;
                if (a & 0x10) continue;                       // static
                line += " " + std::string(fi->name) + ":" + BigTypeDesc(fi->type, &nested) + ":" + std::to_string(a & 0xFFFF);
                if (sfK && fieldHasAttr && fieldHasAttr((void*)fi, sfK)) line += "+SF";
                line += ";";
                if (line.size() > 1700) { DevlogPush(line); line = "[cls+] " + key + " |"; }   // devlog lines cap at 2000
            }
            DevlogPush(line);
        };
        for (auto& e : kBigProbeClasses) {
            Class c(e[1], e[2], Image(e[0]));
            if (!c.IsValid() || !c.GetClass()) {
                missing++;
                miss += std::string(e[2]) + " ";
                if (miss.size() > 1700) { DevlogPush("[cls] missing: " + miss); miss.clear(); }
                continue;
            }
            found++;
            for (auto* k = c.GetClass(); k && k->name; k = k->parent) {
                std::string n = k->name;
                if (n == "MonoBehaviour" || n == "Behaviour" || n == "Component" || n == "Object" || n == "ScriptableObject") break;
                if ((n == "MapObject" || n == "Item") && k != c.GetClass()) break;   // bases are probed on their own
                dumpOne(k);
            }
        }
        // data classes / structs the fields use (MobAttack, LootTable entries...)
        for (size_t i = 0; i < nested.size() && i < 2000; i++) {
            for (auto* k = nested[i]; k && k->name; k = k->parent) {
                std::string n = k->name;
                if (n == "Object" || n == "ValueType") break;
                dumpOne(k);
            }
        }
        DevlogPush("[cls] summary found=" + std::to_string(found) + " missing=" + std::to_string(missing) + " nested=" + std::to_string(nested.size()));
        DevlogPush("[cls] missing: " + miss);
    } catch (...) { DevlogPush("[cls] EXCEPTION"); }
}

// ---------------------------------------------------------------------
//  Cosmetic bundles v2 (live54, owner 2026-10-18). Several bundles, each
//  downloaded from our server at launch and loaded once (AssetBundle.LoadFromFile
//  + GetAllAssetNames + LoadAsset(string); LoadAllAssets is stripped in 2.32).
//  Every prefab is recorded by name with the mesh + materials of its renderer
//  (skinned or MeshFilter/MeshRenderer), so swaps always use the bundle's own
//  material set (the other AI re-exports change material names/counts).
//  Every 2 s:
//    - SkinnedMeshRenderers showing a replaced cosmetic mesh get the bundle mesh
//      (Frost pieces on the yeti / alienUFO back tube, Frost Over backpack on the
//      lifeguard backpack); renderer bones are kept (bind poses match 1:1);
//    - MeshFilters of the game's magnifying glass (magnifyingGlass2 + LODs) get the
//      Icy Magnifying Glass (the "_invisible" item gets the ghost version), plus one
//      ice-ray particle instance parented to the glass (the glass is role-locked to
//      Staff Member A on the server, so every glass in the game is his);
//    - the ice pillows (1x1x1_icePillow / 2x2x2_icePillow, baked in data with the
//      game's ice material) get the bundle's see-through ice materials.
//  Files live under new names so live50-53 keep the old frostoveryeti.bundle.
// ---------------------------------------------------------------------
static const char* kCosHost = "live.your-domain.example";
struct CosBundle { const char* path; const char* file; };
static const CosBundle kCosBundles[] = {
    {"/client/cosmetics/frost2.bundle", "/sdcard/Android/data/com.TrassGames.Yeeps/files/frost2.bundle"},
    {"/client/cosmetics/frostpack.bundle", "/sdcard/Android/data/com.TrassGames.Yeeps/files/frostpack.bundle"},
};
static const int kCosBundleN = sizeof(kCosBundles) / sizeof(kCosBundles[0]);
static std::atomic<int> g_cosState{0};      // 0 idle, 1 downloading, 2 files ready, 3 failed, 4 loaded
static std::atomic<bool> g_cosFileOk[8];
struct CosPrefab { void* mesh = nullptr; std::vector<void*> mats; void* go = nullptr; };
static std::map<std::string, CosPrefab> g_cosPrefabs;   // prefab (GameObject) name -> mesh/mats handles
static std::map<std::string, void*> g_cosMatsByName;    // every bundle material by name
static std::map<void*, int> g_cosMeshKind;              // game mesh ptr -> 0 not ours / 1 handled
static std::vector<void*> g_cosBundleHandles;

static const std::map<std::string, const char*>& CosSkinSwaps() {
    static const std::map<std::string, const char*> m = {
        {"cosmetic_yeti_head", "frostOverYeti_head"}, {"cosmetic_yeti_head_LOD1", "frostOverYeti_head"},
        {"cosmetic_yeti_torso_overLayer", "frostOverYeti_torso"}, {"cosmetic_yeti_torso_overLayer_LOD1", "frostOverYeti_torso"},
        {"cosmetic_yeti_arms_overLayer", "frostOverYeti_arms"}, {"cosmetic_yeti_arms_overLayer_LOD1", "frostOverYeti_arms"},
        {"cosmetic_alienUFO_back_tube", "frostOverYeti_backpack"}, {"cosmetic_alienUFO_back_tube_LOD1", "frostOverYeti_backpack"},
        {"cosmetic_lifeguard_back_floaty", "frostOverYetiBackpack"}, {"cosmetic_lifeguard_back_floaty_LOD1", "frostOverYetiBackpack"}};
    return m;
}

static void CosDownloadThread() {
    int ok = 0;
    for (int i = 0; i < kCosBundleN; i++) {
        bool got = false;
        try {
            httplib::Client cli(kCosHost, 80);
            cli.set_connection_timeout(10); cli.set_read_timeout(30);
            auto r = cli.Get(kCosBundles[i].path);
            if (r && r->status == 200 && r->body.size() > 1000) {
                std::string tmp = std::string(kCosBundles[i].file) + ".tmp";
                if (FILE* f = fopen(tmp.c_str(), "wb")) {
                    fwrite(r->body.data(), 1, r->body.size(), f); fclose(f);
                    rename(tmp.c_str(), kCosBundles[i].file);
                    got = true;
                    DevlogPush(std::string("[cos] ") + kCosBundles[i].path + " downloaded (" + std::to_string(r->body.size()) + " bytes)");
                }
            } else DevlogPush(std::string("[cos] ") + kCosBundles[i].path + " download failed status=" + (r ? std::to_string(r->status) : "none"));
        } catch (...) { DevlogPush("[cos] download EXCEPTION"); }
        if (!got) { if (FILE* f = fopen(kCosBundles[i].file, "rb")) { fclose(f); got = true; } }   // copy from an earlier launch
        g_cosFileOk[i] = got;
        ok += got;
    }
    g_cosState = ok ? 2 : 3;
}

static void CosLoadBundles() {
    Class abC("UnityEngine", "AssetBundle");
    Method<Il2CppObject*> loadFile(FindOverload(abC, "LoadFromFile", {"System.String"}));
    Method<Array<BNM::Structures::Mono::String*>*> names(abC.GetMethod("GetAllAssetNames", 0));
    MethodBase loadAssetMb;
    for (auto& mt : abC.GetMethods(false)) {
        auto* mi = mt.GetInfo();
        if (mi && mi->name && strcmp(mi->name, "LoadAsset") == 0 && mi->parameters_count == 1
            && typeName(mi->return_type) == "UnityEngine.Object") { loadAssetMb = mt; break; }
    }
    Method<Il2CppObject*> loadAsset(loadAssetMb);
    Method<BNM::Structures::Mono::String*> gn(Class("UnityEngine", "Object").GetMethod("get_name", 0));
    Class goC("UnityEngine", "GameObject");
    Method<Il2CppObject*> getComp(FindOverload(goC, "GetComponent", {"System.Type"}));
    Class smrC("UnityEngine", "SkinnedMeshRenderer"), mfC("UnityEngine", "MeshFilter"), mrC("UnityEngine", "MeshRenderer");
    Method<Il2CppObject*> smrMesh(smrC.GetMethod("get_sharedMesh", 0));
    Method<Il2CppObject*> mfMesh(mfC.GetMethod("get_sharedMesh", 0));
    Method<Array<Il2CppObject*>*> getMats(Class("UnityEngine", "Renderer").GetMethod("get_sharedMaterials", 0));
    if (!loadFile.IsValid() || !names.IsValid() || !loadAsset.IsValid() || !gn.IsValid() || !getComp.IsValid()
        || !smrMesh.IsValid() || !mfMesh.IsValid() || !getMats.IsValid()) {
        DevlogPush("[cos] AssetBundle API missing"); g_cosState = 3; return;
    }
    auto nameOf = [&](Il2CppObject* o) { auto* s = o ? gn[o]() : nullptr; return s ? s->str() : std::string(); };
    int prefabs = 0;
    for (int b = 0; b < kCosBundleN; b++) {
        if (!g_cosFileOk[b]) continue;
        Il2CppObject* ab = loadFile(CreateMonoString(kCosBundles[b].file));
        if (!ab) { DevlogPush(std::string("[cos] LoadFromFile null: ") + kCosBundles[b].file); continue; }
        g_cosBundleHandles.push_back(Pin(ab));
        auto* list = names[ab]();
        if (!list) continue;
        for (int i = 0; i < (int)list->capacity; i++) {
            auto* nm = list->m_Items[i];
            if (!nm) continue;
            Il2CppObject* o = loadAsset[ab](nm);
            if (!o || !o->klass || !o->klass->name) continue;
            std::string kn = o->klass->name;
            if (kn == "Material") { g_cosMatsByName[nameOf(o)] = Pin(o); continue; }
            if (kn != "GameObject") continue;
            CosPrefab p;
            p.go = Pin(o);
            Il2CppObject* r = getComp[o]((Il2CppObject*)smrC.GetMonoType());
            Il2CppObject* mesh = nullptr;
            if (r) mesh = smrMesh[r]();
            else {
                Il2CppObject* mf = getComp[o]((Il2CppObject*)mfC.GetMonoType());
                r = getComp[o]((Il2CppObject*)mrC.GetMonoType());
                if (mf) mesh = mfMesh[mf]();
            }
            if (mesh) p.mesh = Pin(mesh);
            if (r) if (auto* mats = getMats[r]())
                for (int j = 0; j < (int)mats->capacity; j++) if (mats->m_Items[j]) {
                    p.mats.push_back(Pin(mats->m_Items[j]));
                    g_cosMatsByName[nameOf(mats->m_Items[j])] = p.mats.back();
                }
            g_cosPrefabs[nameOf(o)] = p;
            prefabs++;
        }
    }
    g_cosState = 4;
    std::string have;
    for (auto& kv : g_cosPrefabs) have += kv.first + "(" + std::to_string(kv.second.mats.size()) + ") ";
    std::string mh;
    for (auto& kv : g_cosMatsByName) mh += kv.first + " ";
    DevlogPush("[cos] bundles loaded: " + std::to_string(prefabs) + " prefabs: " + have + "| mats: " + mh);
}

static Il2CppObject* CosTarget(void* h) { return h && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(h) : nullptr; }

static Il2CppObject* CosMatArray(const std::vector<Il2CppObject*>& mats) {
    static void* matClass = Class("UnityEngine", "Material").GetClass();
    if (!p_array_new || mats.empty()) return nullptr;
    void* ma = p_array_new(matClass, mats.size());
    if (!ma) return nullptr;
    for (size_t j = 0; j < mats.size(); j++) ((void**)((uint8_t*)ma + 0x20))[j] = mats[j];
    return (Il2CppObject*)ma;
}

static bool CosPrefabMats(const CosPrefab& p, std::vector<Il2CppObject*>& out) {
    out.clear();
    for (auto* h : p.mats) { auto* m = CosTarget(h); if (!m) return false; out.push_back(m); }
    return !out.empty();
}

static void CosmeticTick(double now) {
    static bool started = false;
    if (!started) { started = true; g_cosState = 1; std::thread(CosDownloadThread).detach(); }
    int st = g_cosState.load();
    if (st == 2) { try { CosLoadBundles(); } catch (...) { g_cosState = 3; DevlogPush("[cos] load EXCEPTION"); } return; }
    if (st != 4) return;
    static double next = 0;
    if (now < next) return;
    next = now + 1.0;   // live66: 3 s -> 1 s (the game re-applies remote players' cosmetics ~every second)
    static Class smrC("UnityEngine", "SkinnedMeshRenderer"), mfC("UnityEngine", "MeshFilter"), mrC("UnityEngine", "MeshRenderer");
    static Class compC("UnityEngine", "Component"), trC("UnityEngine", "Transform"), goC("UnityEngine", "GameObject"), objC("UnityEngine", "Object");
    static Method<Il2CppObject*> smrMesh(smrC.GetMethod("get_sharedMesh", 0));
    static Method<void> smrSetMesh(smrC.GetMethod("set_sharedMesh", 1));
    static Method<Il2CppObject*> mfMesh(mfC.GetMethod("get_sharedMesh", 0));
    static Method<void> mfSetMesh(mfC.GetMethod("set_sharedMesh", 1));
    static Method<void> setMats(Class("UnityEngine", "Renderer").GetMethod("set_sharedMaterials", 1));
    static Method<BNM::Structures::Mono::String*> gn(objC.GetMethod("get_name", 0));
    static Method<Il2CppObject*> getTr(compC.GetMethod("get_transform", 0));
    static Method<Il2CppObject*> getGo(compC.GetMethod("get_gameObject", 0));
    static Method<Il2CppObject*> trRoot(trC.GetMethod("get_root", 0));
    static Method<Il2CppObject*> getCompGo(FindOverload(goC, "GetComponent", {"System.Type"}));
    static Method<Il2CppObject*> inst(FindOverload(objC, "Instantiate", {"UnityEngine.Object", "UnityEngine.Transform"}));
    static Method<Il2CppObject*> trFind(FindOverload(trC, "Find", {"System.String"}));
    static Method<void> setName(objC.GetMethod("set_name", 1));
    auto fm = findObjsMethod();
    if (!fm.IsValid() || !smrMesh.IsValid() || !smrSetMesh.IsValid() || !setMats.IsValid() || !gn.IsValid()) return;
    auto nameOf = [&](Il2CppObject* o) { auto* s = o ? gn[o]() : nullptr; return s ? s->str() : std::string(); };
    std::vector<Il2CppObject*> mats;
    int swapped = 0, glass = 0, ice = 0, refixed = 0;
    static Method<Array<Il2CppObject*>*> getMats(Class("UnityEngine", "Renderer").GetMethod("get_sharedMaterials", 0));
    static Method<void> offscreen(smrC.GetMethod("set_updateWhenOffscreen", 1));
    // live66: bundle mesh -> its prefab. The game re-applies ITS materials onto a renderer
    // that already shows our mesh (remote players' cosmetics refresh ~every second); with
    // the wrong materials the Frost mesh drew nothing ("disappears for other players").
    static std::map<void*, const CosPrefab*> bundleMesh;
    if (bundleMesh.empty())
        for (auto& kv : g_cosPrefabs) if (Il2CppObject* bm = CosTarget(kv.second.mesh)) bundleMesh[bm] = &kv.second;
    // 1) skinned cosmetics
    if (auto* arr = Method<Array<Il2CppObject*>*>(fm)((Il2CppObject*)smrC.GetMonoType())) {
        for (int i = 0; i < (int)arr->capacity; i++) {
            Il2CppObject* r = arr->m_Items[i];
            if (!r) continue;
            Il2CppObject* m = smrMesh[r]();
            if (!m) continue;
            auto bmIt = bundleMesh.find(m);
            if (bmIt != bundleMesh.end()) {          // already ours: keep our materials on it
                if (!CosPrefabMats(*bmIt->second, mats) || !getMats.IsValid()) continue;
                auto* cur = getMats[r]();
                bool same = cur && (int)cur->capacity == (int)mats.size();
                for (size_t j = 0; same && j < mats.size(); j++) same = cur->m_Items[j] == mats[j];
                if (!same) if (Il2CppObject* ma = CosMatArray(mats)) { setMats[r](ma); refixed++; }
                continue;
            }
            auto k = g_cosMeshKind.find(m);
            if (k != g_cosMeshKind.end()) continue;
            auto sw = CosSkinSwaps().find(nameOf(m));
            if (sw == CosSkinSwaps().end()) { g_cosMeshKind[m] = 0; continue; }
            auto pf = g_cosPrefabs.find(sw->second);
            if (pf == g_cosPrefabs.end() || !CosPrefabMats(pf->second, mats)) { g_cosMeshKind[m] = 0; continue; }
            Il2CppObject* nm = CosTarget(pf->second.mesh);
            Il2CppObject* ma = CosMatArray(mats);
            if (!nm || !ma) continue;
            smrSetMesh[r](nm);
            setMats[r](ma);
            if (offscreen.IsValid()) offscreen[r](true);   // bounds follow the new mesh (no wrong culling)
            swapped++;
        }
    }
    // 2) magnifying glass + ice pillows (MeshFilter / MeshRenderer)
    if (mfMesh.IsValid() && mfSetMesh.IsValid() && getTr.IsValid() && getGo.IsValid() && trRoot.IsValid() && getCompGo.IsValid())
    if (auto* arr = Method<Array<Il2CppObject*>*>(fm)((Il2CppObject*)mfC.GetMonoType())) {
        for (int i = 0; i < (int)arr->capacity; i++) {
            Il2CppObject* f = arr->m_Items[i];
            if (!f) continue;
            Il2CppObject* m = mfMesh[f]();
            if (!m) continue;
            auto k = g_cosMeshKind.find(m);
            if (k != g_cosMeshKind.end() && k->second == 0) continue;
            std::string mn = nameOf(m);
            bool isGlass = false;   // Icy Magnifying Glass dropped by the owner 2026-10-18 (normal glasses stay)
            bool isIce = false;   // ice pillows removed by the owner 2026-10-18
            if (!isGlass && !isIce) { g_cosMeshKind[m] = 0; continue; }
            g_cosMeshKind[m] = 1;
            Il2CppObject* go = getGo[f]();
            Il2CppObject* mr = go ? getCompGo[go]((Il2CppObject*)mrC.GetMonoType()) : nullptr;
            if (!mr) continue;
            if (isIce) {
                Il2CppObject* body = CosTarget(g_cosMatsByName.count("Cosmetic_IceCube") ? g_cosMatsByName["Cosmetic_IceCube"] : nullptr);
                Il2CppObject* crack = CosTarget(g_cosMatsByName.count("Cosmetic_IceBlock") ? g_cosMatsByName["Cosmetic_IceBlock"] : nullptr);
                if (!body) continue;
                Il2CppObject* ma = CosMatArray({body, crack ? crack : body});
                if (ma) { setMats[mr](ma); ice++; }
                continue;
            }
            // glass: which item? (the invisible one gets the ghost mesh)
            Il2CppObject* tr = getTr[f]();
            Il2CppObject* root = tr ? trRoot[tr]() : nullptr;
            std::string rn = nameOf(root ? getGo[root]() : nullptr);
            bool invis = rn.find("invisible") != std::string::npos;
            auto pf = g_cosPrefabs.find(invis ? "icyMagnifyingGlass_invisible" : "icyMagnifyingGlass");
            if (pf == g_cosPrefabs.end() || !CosPrefabMats(pf->second, mats)) continue;
            Il2CppObject* nm = CosTarget(pf->second.mesh);
            Il2CppObject* ma = CosMatArray(mats);
            if (!nm || !ma) continue;
            mfSetMesh[f](nm);
            setMats[mr](ma);
            glass++;
            // one ice ray per glass, on the LOD0 visual (same space as the glass mesh)
            if (!invis && mn == "magnifyingGlass2" && inst.IsValid() && trFind.IsValid() && setName.IsValid()) {
                if (!trFind[tr](CreateMonoString("blazeIceRay"))) {
                    auto ray = g_cosPrefabs.find("icyMagnifyingGlass_iceRay");
                    Il2CppObject* src = ray != g_cosPrefabs.end() ? CosTarget(ray->second.go) : nullptr;
                    if (src) if (Il2CppObject* rgo = inst(src, tr)) setName[rgo](CreateMonoString("blazeIceRay"));
                }
            }
        }
    }
    static int logged = 0;
    if ((swapped || glass || ice || refixed) && logged++ < 40)
        DevlogPush("[cos] applied: skinned " + std::to_string(swapped) + ", materials re-fixed " + std::to_string(refixed));
}

// ---------------------------------------------------------------------
//  Dev trading-card artwork (live55, owner 2026-10-18). Cards staffcarda,
//  staffcardb, yeepswholarpstuff, bluemagnifyingglass (+ _legendary = gold) are
//  item-style cards in the data; their owner-made full-card textures (the game's
//  tradingCardTexture layout: back on the left, front on the right) come from
//  /client/cards/<key>.rgb (512x512 RGB24, rows bottom-up). Every second every
//  TradingCardItem whose key (string field @816, class dump live54) is one of
//  ours gets:
//    - a copy of the playingCard mesh whose front UVs point at the art
//      (submesh 0 = back/edges keeps its UVs: same layout as the game texture;
//       1 = frame -> gold border (gold) or the card face (normal); 2 = face
//       background and 3 = picture quad -> the card face rectangle),
//    - one material (copy of the card back material) with the art on all 4
//      submeshes, and its name/stars overlay (InfoText) hidden: the art has them.
// ---------------------------------------------------------------------
static const char* kCardArtKeys[] = {"staffcarda", "staffcardb", "yeepswholarpstuff", "bluemagnifyingglass"};
struct CardArt { std::atomic<int> state{0}; std::string bytes; void* tex = nullptr; void* mat = nullptr; };
static std::map<std::string, CardArt*> g_cardArt;     // full key -> art
static void* g_cardMesh[2] = {nullptr, nullptr};      // normal / gold mesh copies
static const int kCardItemKeyOff = 816;

static void CardArtDownload(std::string key, CardArt* a) {
    try {
        httplib::Client cli(kCosHost, 80);
        cli.set_connection_timeout(10); cli.set_read_timeout(30);
        auto r = cli.Get(("/client/cards/" + key + ".rgb").c_str());
        if (r && r->status == 200 && r->body.size() == 512 * 512 * 3) { a->bytes = r->body; a->state = 2; return; }
        DevlogPush("[card] art download failed for " + key + " status=" + (r ? std::to_string(r->status) : "none"));
    } catch (...) {}
    a->state = 3;
}

static Il2CppObject* CardArtMaterial(const std::string& key, Il2CppObject* baseMat) {
    auto it = g_cardArt.find(key);
    if (it == g_cardArt.end()) {
        auto* a = new CardArt(); a->state = 1; g_cardArt[key] = a;
        std::thread(CardArtDownload, key, a).detach();
        return nullptr;
    }
    CardArt* a = it->second;
    if (a->state == 2 && baseMat) {
        a->state = 3;   // one attempt
        Class t2("UnityEngine", "Texture2D"), matC("UnityEngine", "Material");
        Method<void> ctor(FindOverload(t2, ".ctor", {"System.Int32", "System.Int32", "UnityEngine.TextureFormat", "System.Boolean"}));
        Method<void> load(FindOverload(t2, "LoadRawTextureData", {"System.Byte[]"}));
        Method<void> apply(FindOverload(t2, "Apply", {"System.Boolean", "System.Boolean"}));
        Method<void> mctor(FindOverload(matC, ".ctor", {"UnityEngine.Material"}));
        Method<void> setTex(FindOverload(matC, "SetTexture", {"System.String", "UnityEngine.Texture"}));
        if (!ctor.IsValid() || !load.IsValid() || !apply.IsValid() || !mctor.IsValid() || !setTex.IsValid() || !p_object_new || !p_array_new) {
            DevlogPush("[card] texture/material api missing: ctor=" + std::to_string(ctor.IsValid()) + " load=" + std::to_string(load.IsValid())
                       + " mctor=" + std::to_string(mctor.IsValid()) + " setTex=" + std::to_string(setTex.IsValid()));
            return nullptr;
        }
        auto* t = (Il2CppObject*)p_object_new(t2.GetClass());
        ctor[t](512, 512, 3 /* RGB24 */, true);
        void* arr = p_array_new(Class("System", "Byte").GetClass(), a->bytes.size());
        memcpy((uint8_t*)arr + 0x20, a->bytes.data(), a->bytes.size());
        load[t]((Il2CppObject*)arr);
        apply[t](true, true);
        Method<void>(Class("UnityEngine", "Object").GetMethod("DontDestroyOnLoad", 1))(t);
        auto* m = (Il2CppObject*)p_object_new(matC.GetClass());
        mctor[m](baseMat);
        setTex[m](CreateMonoString("_MainTex"), t);
        setTex[m](CreateMonoString("_BaseMap"), t);
        a->tex = Pin(t); a->mat = Pin(m);
        a->bytes.clear(); a->bytes.shrink_to_fit();
        a->state = 4;
        DevlogPush("[card] art ready: " + key);
    }
    return a->state == 4 ? CosTarget(a->mat) : nullptr;
}

// copy of the game's playingCard mesh with the front UVs remapped (see above)
static Il2CppObject* CardArtMesh(Il2CppObject* src, bool gold) {
    if (Il2CppObject* m = CosTarget(g_cardMesh[gold])) return m;
    Class meshC("UnityEngine", "Mesh"), objC("UnityEngine", "Object");
    Method<Il2CppObject*> inst(FindOverload(objC, "Instantiate", {"UnityEngine.Object"}));
    Method<Array<Vector2>*> getUv(meshC.GetMethod("get_uv", 0));
    Method<void> setUv(FindOverload(meshC, "set_uv", {"UnityEngine.Vector2[]"}));
    Method<Array<int>*> getTris(FindOverload(meshC, "GetTriangles", {"System.Int32"}));
    Method<int> subCount(meshC.GetMethod("get_subMeshCount", 0));
    if (!inst.IsValid() || !getUv.IsValid() || !setUv.IsValid() || !getTris.IsValid() || !subCount.IsValid()) {
        static bool once = false;
        if (!once) { once = true; DevlogPush("[card] mesh api missing: inst=" + std::to_string(inst.IsValid()) + " uv=" + std::to_string(getUv.IsValid())
                                             + "/" + std::to_string(setUv.IsValid()) + " tris=" + std::to_string(getTris.IsValid()) + " sub=" + std::to_string(subCount.IsValid())); }
        return nullptr;
    }
    Il2CppObject* m = inst(src);
    if (!m) return nullptr;
    auto* uv = getUv[m]();
    if (!uv || subCount[m]() < 4) { DevlogPush("[card] mesh copy unusable (uv/submeshes)"); return nullptr; }
    // rectangles in UV space (v from the bottom) of the 1024 art, measured from the owner's PNGs
    struct Rc { float u0, v0, u1, v1; };
    const Rc face = {612 / 1024.f, 1 - 473 / 1024.f, 923 / 1024.f, 1 - 38 / 1024.f};
    const Rc border = {588 / 1024.f, 1 - 498 / 1024.f, 947 / 1024.f, 1 - 13 / 1024.f};
    float pic = 0.0633f / 0.0886f;                          // picture quad = this share of the face height, centred
    float cv = (face.v0 + face.v1) / 2, hh = (face.v1 - face.v0) * pic / 2;
    const Rc picture = {face.u0, cv - hh, face.u1, cv + hh};
    const Rc rects[4] = {{0, 0, 1, 1}, gold ? border : face, face, picture};
    std::vector<char> done(uv->capacity, 0);
    for (int s = 1; s < 4; s++) {
        auto* tris = getTris[m](s);
        if (!tris) continue;
        const Rc& r = rects[s];
        for (int i = 0; i < (int)tris->capacity; i++) {
            int vi = tris->m_Items[i];
            if (vi < 0 || vi >= (int)uv->capacity || done[vi]) continue;
            done[vi] = 1;
            Vector2& t = uv->m_Items[vi];
            t.x = r.u0 + t.x * (r.u1 - r.u0);
            t.y = r.v0 + t.y * (r.v1 - r.v0);
        }
    }
    setUv[m](uv);
    Method<void>(objC.GetMethod("DontDestroyOnLoad", 1))(m);
    g_cardMesh[gold] = Pin(m);
    DevlogPush(std::string("[card] mesh copy ready (") + (gold ? "gold" : "normal") + ")");
    return m;
}

static void CardArtTick(double now) {
    static double next = 0;
    if (now < next) return;
    next = now + 1.0;
    static Class tci("", "TradingCardItem", Image("Assembly-CSharp.dll"));
    static Class compC("UnityEngine", "Component"), goC("UnityEngine", "GameObject"), trC("UnityEngine", "Transform");
    static Class mfC("UnityEngine", "MeshFilter"), mrC("UnityEngine", "MeshRenderer"), objC("UnityEngine", "Object");
    static Method<Array<Il2CppObject*>*> kids(FindOverload(compC, "GetComponentsInChildren", {"System.Type", "System.Boolean"}));
    static Method<Il2CppObject*> getGo(compC.GetMethod("get_gameObject", 0));
    static Method<Il2CppObject*> getTr(compC.GetMethod("get_transform", 0));
    static Method<Il2CppObject*> getComp(FindOverload(goC, "GetComponent", {"System.Type"}));
    static Method<Il2CppObject*> mfMesh(mfC.GetMethod("get_sharedMesh", 0));
    static Method<void> mfSetMesh(mfC.GetMethod("set_sharedMesh", 1));
    static Method<Array<Il2CppObject*>*> getMats(Class("UnityEngine", "Renderer").GetMethod("get_sharedMaterials", 0));
    static Method<void> setMats(Class("UnityEngine", "Renderer").GetMethod("set_sharedMaterials", 1));
    static Method<Il2CppObject*> trFind(FindOverload(trC, "Find", {"System.String"}));
    static Method<void> setActive(goC.GetMethod("SetActive", 1));
    static Method<BNM::Structures::Mono::String*> gn(objC.GetMethod("get_name", 0));
    auto fm = findObjsMethod();
    if (!tci.IsValid() || !fm.IsValid() || !trFind.IsValid() || !getTr.IsValid() || !getGo.IsValid() || !getComp.IsValid() || !mfMesh.IsValid()
        || !mfSetMesh.IsValid() || !getMats.IsValid() || !setMats.IsValid()) {
        static bool once = false;
        if (!once) { once = true; DevlogPush(std::string("[card] api missing: cls=") + std::to_string(tci.IsValid()) + " find=" + std::to_string(trFind.IsValid())
                                             + " mats=" + std::to_string(getMats.IsValid()) + " setMesh=" + std::to_string(mfSetMesh.IsValid())); }
        return;
    }
    auto* arr = Method<Array<Il2CppObject*>*>(fm)((Il2CppObject*)tci.GetMonoType());
    static int cntLog = 0;
    if (arr && arr->capacity > 0 && cntLog < 5) { cntLog++; DevlogPush("[card] " + std::to_string((int)arr->capacity) + " trading card item(s) in scene"); }
    if (!arr) return;
    static int seenLog = 0;
    for (int i = 0; i < (int)arr->capacity; i++) {
        Il2CppObject* card = arr->m_Items[i];
        if (!card) continue;
        // card key: string @816, else the config's first field (TradingCardConfig @824,
        // key = first serialized string), else the item's custom-parameter text (Item @176)
        auto strAt = [](void* o, int off) -> std::string {
            if (!o) return "";
            auto* s = *(BNM::Structures::Mono::String**)((uint8_t*)o + off);
            if (!s || !s->klass || !s->klass->name || strcmp(s->klass->name, "String") != 0) return "";
            return s->str();
        };
        std::string key = strAt(card, kCardItemKeyOff);
        if (key.empty()) key = strAt(*(void**)((uint8_t*)card + 824), 0x10);
        std::string params = strAt(card, 176);
        if (key.empty() && !params.empty()) {
            for (auto* k : kCardArtKeys) {
                if (params.find(std::string(k) + "_legendary") != std::string::npos) { key = std::string(k) + "_legendary"; break; }
                if (params.find(k) != std::string::npos) key = k;
            }
        }
        if (seenLog < 15) { seenLog++; DevlogPush("[card] item key '" + key + "' (@816 '" + strAt(card, kCardItemKeyOff) + "', params '" + params + "')"); }
        bool gold = key.size() > 10 && key.compare(key.size() - 10, 10, "_legendary") == 0;
        std::string base = gold ? key.substr(0, key.size() - 10) : key;
        bool ours = false;
        for (auto* k : kCardArtKeys) ours = ours || base == k;
        if (!ours) continue;
        // the card's LOD renderers (Item_TradingCard prefab; GetComponentsInChildren is stripped in 2.32)
        Il2CppObject* cardTr = getTr[card]();
        if (!cardTr) continue;
        for (const char* path : {"ScaleContainer/HeldOffset/LOD0", "ScaleContainer/HeldOffset/LOD1", "ScaleContainer/HeldOffset/LOD2"}) {
            Il2CppObject* lt = trFind[cardTr](CreateMonoString(path));
            Il2CppObject* lgo = lt ? getGo[lt]() : nullptr;
            Il2CppObject* f = lgo ? getComp[lgo]((Il2CppObject*)mfC.GetMonoType()) : nullptr;
            Il2CppObject* m = f ? mfMesh[f]() : nullptr;
            if (!m) continue;
            auto* mn = gn[m]();
            std::string meshName = mn ? mn->str() : "";
            bool isOurs = CosTarget(g_cardMesh[0]) == m || CosTarget(g_cardMesh[1]) == m;
            if (!isOurs && meshName.rfind("playingCard", 0) != 0) continue;
            Il2CppObject* go = getGo[f]();
            Il2CppObject* r = go ? getComp[go]((Il2CppObject*)mrC.GetMonoType()) : nullptr;
            if (!r) continue;
            auto* cur = getMats[r]();
            Il2CppObject* baseMat = cur && cur->capacity > 0 ? cur->m_Items[0] : nullptr;
            Il2CppObject* mat = CardArtMaterial(key, baseMat);
            if (!mat) continue;
            if (!isOurs) { if (Il2CppObject* nm = CardArtMesh(m, gold)) mfSetMesh[f](nm); else continue; }
            if (!cur || cur->capacity != 4 || cur->m_Items[0] != mat || cur->m_Items[3] != mat)
                if (Il2CppObject* ma = CosMatArray({mat, mat, mat, mat})) setMats[r](ma);
        }
        if (setActive.IsValid())
            if (Il2CppObject* t = trFind[cardTr](CreateMonoString("ScaleContainer/HeldOffset/InfoText")))
                if (Il2CppObject* tg = getGo[t]()) setActive[tg](false);
    }
}

// ---------------------------------------------------------------------
//  Frost badge (live55, owner 2026-10-18): the Staff Member A icon next to the
//  name of anyone wearing the FrostOverYeti outfit (like the lab rat badge).
//  Nametag (class dump live54) = world-space UI: root with HorizontalLayoutGroup,
//  nameText @32 (UI.Text). Wearers are found by their skinned renderers showing a
//  Frost bundle mesh (head/torso/arms); a nametag belongs to a wearer when it
//  floats within 1.2 m (horizontal) of that wearer's Frost renderer. A child
//  Image "blazeFrostBadge" (icon from /client/cards/frostbadge.rgb, 128x128
//  RGBA32) is added to the nametag's layout and shown only for wearers.
// ---------------------------------------------------------------------
struct BoundsS { Vector3 center; Vector3 extents; };
static std::atomic<int> g_badgeState{0};
static std::string g_badgeBytes;
static void* g_badgeSprite = nullptr;

static Il2CppObject* FrostBadgeSprite() {
    if (Il2CppObject* s = CosTarget(g_badgeSprite)) return s;
    int st = g_badgeState.load();
    if (st == 0) {
        g_badgeState = 1;
        std::thread([]() {
            try {
                httplib::Client cli(kCosHost, 80);
                cli.set_connection_timeout(10); cli.set_read_timeout(20);
                auto r = cli.Get("/client/cards/frostbadge.rgb");
                if (r && r->status == 200 && r->body.size() == 128 * 128 * 4) { g_badgeBytes = r->body; g_badgeState = 2; return; }
            } catch (...) {}
            g_badgeState = 3;
        }).detach();
        return nullptr;
    }
    if (st != 2) return nullptr;
    g_badgeState = 3;
    Class t2("UnityEngine", "Texture2D"), spC("UnityEngine", "Sprite");
    Method<void> ctor(FindOverload(t2, ".ctor", {"System.Int32", "System.Int32", "UnityEngine.TextureFormat", "System.Boolean"}));
    Method<void> load(FindOverload(t2, "LoadRawTextureData", {"System.Byte[]"}));
    Method<void> apply(FindOverload(t2, "Apply", {"System.Boolean", "System.Boolean"}));
    Method<Il2CppObject*> create(FindOverload(spC, "Create", {"UnityEngine.Texture2D", "UnityEngine.Rect", "UnityEngine.Vector2"}));
    if (!ctor.IsValid() || !load.IsValid() || !apply.IsValid() || !create.IsValid() || !p_object_new || !p_array_new) {
        DevlogPush("[badge] texture/sprite api missing"); return nullptr;
    }
    auto* t = (Il2CppObject*)p_object_new(t2.GetClass());
    ctor[t](128, 128, 4 /* RGBA32 */, false);
    void* arr = p_array_new(Class("System", "Byte").GetClass(), g_badgeBytes.size());
    memcpy((uint8_t*)arr + 0x20, g_badgeBytes.data(), g_badgeBytes.size());
    load[t]((Il2CppObject*)arr);
    apply[t](false, true);
    Method<void>(Class("UnityEngine", "Object").GetMethod("DontDestroyOnLoad", 1))(t);
    Il2CppObject* sp = create(t, Rect(0, 0, 128, 128), Vector2(0.5f, 0.5f));
    if (!sp) { DevlogPush("[badge] Sprite.Create null"); return nullptr; }
    Method<void>(Class("UnityEngine", "Object").GetMethod("DontDestroyOnLoad", 1))(sp);
    g_badgeSprite = Pin(sp);
    g_badgeState = 4;
    DevlogPush("[badge] sprite ready");
    return sp;
}

static void FrostBadgeTick(double now) {
    static double next = 0;
    if (now < next || g_cosState.load() != 4) return;
    next = now + 0.5;
    Il2CppObject* sprite = FrostBadgeSprite();
    if (!sprite) return;
    static Class ntC("", "Nametag", Image("Assembly-CSharp.dll"));
    static Class smrC("UnityEngine", "SkinnedMeshRenderer"), compC("UnityEngine", "Component"), trC("UnityEngine", "Transform");
    static Class goC("UnityEngine", "GameObject"), objC("UnityEngine", "Object");
    static Class imgC("UnityEngine.UI", "Image", Image("UnityEngine.UI.dll"));
    static Class leC("UnityEngine.UI", "LayoutElement", Image("UnityEngine.UI.dll"));
    static Class rtC("UnityEngine", "RectTransform");
    static Method<Il2CppObject*> smrMesh(smrC.GetMethod("get_sharedMesh", 0));
    static Method<BoundsS> rBounds(Class("UnityEngine", "Renderer").GetMethod("get_bounds", 0));
    static Method<Il2CppObject*> getTr(compC.GetMethod("get_transform", 0));
    static Method<Il2CppObject*> getGo(compC.GetMethod("get_gameObject", 0));
    static Method<Vector3> trPos(trC.GetMethod("get_position", 0));
    static Method<Il2CppObject*> trFind(FindOverload(trC, "Find", {"System.String"}));
    static Method<void> setParent(FindOverload(trC, "SetParent", {"UnityEngine.Transform", "System.Boolean"}));
    static Method<void> setSib(trC.GetMethod("SetAsLastSibling", 0));
    static Method<void> goCtor(FindOverload(goC, ".ctor", {"System.String"}));
    static Method<Il2CppObject*> addComp(FindOverload(goC, "AddComponent", {"System.Type"}));
    static Method<void> setActive(goC.GetMethod("SetActive", 1));
    static Method<bool> activeSelf(goC.GetMethod("get_activeSelf", 0));
    static Method<void> setLayer(goC.GetMethod("set_layer", 1));
    static Method<int> getLayer(goC.GetMethod("get_layer", 0));
    static Method<void> setSprite(imgC.GetMethod("set_sprite", 1));
    static Method<void> setAspect(imgC.GetMethod("set_preserveAspect", 1));
    static Method<Vector2> sizeDelta(rtC.GetMethod("get_sizeDelta", 0));
    static Method<void> setSizeDelta(rtC.GetMethod("set_sizeDelta", 1));
    static Method<void> lePW(leC.GetMethod("set_preferredWidth", 1)), lePH(leC.GetMethod("set_preferredHeight", 1));
    static Method<void> leMW(leC.GetMethod("set_minWidth", 1)), leMH(leC.GetMethod("set_minHeight", 1));
    auto fm = findObjsMethod();
    if (!ntC.IsValid() || !imgC.IsValid() || !fm.IsValid() || !smrMesh.IsValid() || !rBounds.IsValid() || !trFind.IsValid()
        || !setParent.IsValid() || !goCtor.IsValid() || !addComp.IsValid() || !setSprite.IsValid() || !p_object_new) return;
    // Frost wearers: positions of renderers that show a Frost bundle mesh
    std::vector<Vector3> wearers;
    std::set<void*> frostMeshes;
    for (const char* n : {"frostOverYeti_head", "frostOverYeti_torso", "frostOverYeti_arms"}) {
        auto pf = g_cosPrefabs.find(n);
        if (pf != g_cosPrefabs.end()) if (auto* m = CosTarget(pf->second.mesh)) frostMeshes.insert(m);
    }
    if (auto* arr = Method<Array<Il2CppObject*>*>(fm)((Il2CppObject*)smrC.GetMonoType()))
        for (int i = 0; i < (int)arr->capacity; i++) {
            Il2CppObject* r = arr->m_Items[i];
            if (r && frostMeshes.count(smrMesh[r]())) wearers.push_back(rBounds[r]().center);
        }
    auto* tags = Method<Array<Il2CppObject*>*>(fm)((Il2CppObject*)ntC.GetMonoType());
    if (!tags) return;
    static int logged = 0;
    for (int i = 0; i < (int)tags->capacity; i++) {
        Il2CppObject* tag = tags->m_Items[i];
        if (!tag) continue;
        Il2CppObject* tr = getTr[tag]();
        if (!tr) continue;
        Vector3 p = trPos[tr]();
        bool wears = false;
        for (auto& w : wearers) { float dx = w.x - p.x, dz = w.z - p.z; if (dx * dx + dz * dz < 1.44f && fabsf(w.y - p.y) < 2.5f) { wears = true; break; } }
        Il2CppObject* badgeTr = trFind[tr](CreateMonoString("blazeFrostBadge"));
        if (!badgeTr && wears) {
            auto* go = (Il2CppObject*)p_object_new(goC.GetClass());
            goCtor[go](CreateMonoString("blazeFrostBadge"));
            Il2CppObject* img = addComp[go]((Il2CppObject*)imgC.GetMonoType());
            if (!img) continue;
            setSprite[img](sprite);
            if (setAspect.IsValid()) setAspect[img](true);
            badgeTr = getTr[img]();
            setParent[badgeTr](tr, false);
            if (setLayer.IsValid() && getLayer.IsValid()) setLayer[go](getLayer[getGo[tag]()]());
            // square, as tall as the name text
            float h = 40.f;
            auto* text = *(Il2CppObject**)((uint8_t*)tag + 32);
            if (text && sizeDelta.IsValid()) { Vector2 sd = sizeDelta[getTr[text]()](); if (sd.y > 1.f) h = sd.y; }
            if (setSizeDelta.IsValid()) setSizeDelta[badgeTr](Vector2(h, h));
            if (leC.IsValid()) if (Il2CppObject* le = addComp[go]((Il2CppObject*)leC.GetMonoType())) {
                if (lePW.IsValid()) lePW[le](h);
                if (lePH.IsValid()) lePH[le](h);
                if (leMW.IsValid()) leMW[le](h);
                if (leMH.IsValid()) leMH[le](h);
            }
            if (setSib.IsValid()) setSib[badgeTr]();
            if (logged++ < 10) DevlogPush("[badge] added to a nametag (size " + std::to_string((int)h) + ")");
        }
        if (badgeTr) {
            Il2CppObject* bgo = getGo[badgeTr]();
            if (bgo && activeSelf.IsValid() && activeSelf[bgo]() != wears) setActive[bgo](wears);
        }
    }
}

// ---------------------------------------------------------------------
//  Single dancing skeletons (live59, owner 2026-10-18: "do the apk change for single
//  skellys"). The game's DancingSkeleton is a local visual (no PhotonView): parties
//  spawn it on every client from a Halloween door. Here the Photon bot queues spawns
//  on our server (/bot/skel) for its Photon room; every live59 client in that room
//  polls /client/skel about once a second and instantiates the game's own
//  DancingSkeleton prefab (found in memory by name; the scene's party manager keeps
//  it loaded) at the position, turned to the given yaw, destroyed after its ttl.
//  Works in any room and mode (nothing is placed in the map). "[skel]" devlog lines.
// ---------------------------------------------------------------------
struct SkSpawn { long id; float x, y, z, yaw, ttl; int style; unsigned seed; char slug[64]; };   // style 0 salsa 1 fast 2 slow 3 spin 4 bounce 5 gangnam 6 random 7 Mixamo dance <slug>
static std::mutex g_skMu;
static std::vector<SkSpawn> g_skQueue;
static std::string g_skRoom;        // current Photon room name (set on the main thread)
static void* g_skPrefab = nullptr;  // pinned DancingSkeleton prefab

// Mixamo dances (style 7): /client/dances/<slug>.bin made by fbx-work/mixamo_to_dance.py
//   "BDNC" u16 ver u16 fps u16 frames u16 nBones f32 hipHeight
//   per frame: f32 hips dr,du,df (in hip heights)  nBones x i8 (r,u,f)
static const char* kSkClipBones[] = {
    "Spine", "Spine/Spine1", "Spine/Spine1/Spine2", "Spine/Spine1/Spine2/Neck", "Spine/Spine1/Spine2/Neck/Head",
    "Spine/Spine1/Spine2/LeftShoulder", "Spine/Spine1/Spine2/LeftShoulder/LeftArm",
    "Spine/Spine1/Spine2/LeftShoulder/LeftArm/LeftForeArm", "Spine/Spine1/Spine2/LeftShoulder/LeftArm/LeftForeArm/LeftHand",
    "Spine/Spine1/Spine2/RightShoulder", "Spine/Spine1/Spine2/RightShoulder/RightArm",
    "Spine/Spine1/Spine2/RightShoulder/RightArm/RightForeArm", "Spine/Spine1/Spine2/RightShoulder/RightArm/RightForeArm/RightHand",
    "LeftUpLeg", "LeftUpLeg/LeftLeg", "LeftUpLeg/LeftLeg/LeftFoot",
    "RightUpLeg", "RightUpLeg/RightLeg", "RightUpLeg/RightLeg/RightFoot"};
static const int kSkClipN = 19;
struct SkClip { int fps = 30, frames = 0, nb = 0; float hipH = 1; std::vector<float> hips; std::vector<int8_t> dirs; };
static std::mutex g_skClipMu;
static std::map<std::string, std::shared_ptr<SkClip>> g_skClips;

static std::shared_ptr<SkClip> SkClipGet(const std::string& slug) {
    std::lock_guard<std::mutex> lk(g_skClipMu);
    auto it = g_skClips.find(slug);
    return it == g_skClips.end() ? nullptr : it->second;
}

static bool SkClipFetch(const std::string& slug) {   // poll thread
    if (SkClipGet(slug)) return true;
    try {
        httplib::Client cli(kCosHost, 80);
        cli.set_connection_timeout(5); cli.set_read_timeout(15);
        auto r = cli.Get(("/client/dances/" + slug + ".bin").c_str());
        if (!r || r->status != 200 || r->body.size() < 16 || r->body.compare(0, 4, "BDNC") != 0) {
            DevlogPush("[skel] dance " + slug + " download failed"); return false;
        }
        const char* d = r->body.data();
        auto c = std::make_shared<SkClip>();
        uint16_t ver, fps, frames, nb; float hh;
        memcpy(&ver, d + 4, 2); memcpy(&fps, d + 6, 2); memcpy(&frames, d + 8, 2); memcpy(&nb, d + 10, 2); memcpy(&hh, d + 12, 4);
        size_t per = 12 + 3 * (size_t)nb;
        if (!frames || nb != kSkClipN || r->body.size() < 16 + per * frames) { DevlogPush("[skel] dance " + slug + " bad file"); return false; }
        c->fps = fps ? fps : 30; c->frames = frames; c->nb = nb; c->hipH = hh;
        c->hips.resize(3 * (size_t)frames); c->dirs.resize(3 * (size_t)nb * frames);
        for (int f = 0; f < frames; f++) {
            const char* q = d + 16 + per * f;
            memcpy(&c->hips[3 * f], q, 12);
            memcpy(&c->dirs[3 * (size_t)nb * f], q + 12, 3 * (size_t)nb);
        }
        std::lock_guard<std::mutex> lk(g_skClipMu);
        g_skClips[slug] = c;
        DevlogPush("[skel] dance " + slug + " loaded (" + std::to_string(frames) + " frames)");
        return true;
    } catch (...) { return false; }
}

static void SkPollThread() {
    long since = 0;
    std::string lastRoom;
    for (;;) {
        std::this_thread::sleep_for(std::chrono::milliseconds(900));
        std::string room;
        { std::lock_guard<std::mutex> lk(g_skMu); room = g_skRoom; }
        if (room.empty()) continue;
        if (room != lastRoom) { lastRoom = room; since = -1; }   // -1: server answers with its newest id only
        try {
            httplib::Client cli(kCosHost, 80);
            cli.set_connection_timeout(3); cli.set_read_timeout(3);
            std::string path = "/client/skel?room=";
            for (char c : room) {   // url-encode the room name
                if (isalnum((unsigned char)c) || c == '-' || c == '_' || c == '.') path += c;
                else { char b[4]; snprintf(b, sizeof b, "%%%02X", (unsigned char)c); path += b; }
            }
            path += "&since=" + std::to_string(since);
            auto r = cli.Get(path.c_str());
            if (!r || r->status != 200) continue;
            std::istringstream in(r->body);
            std::string line;
            std::vector<SkSpawn> got;
            while (std::getline(in, line)) {
                SkSpawn s{};
                if (line.rfind("last ", 0) == 0) { since = atol(line.c_str() + 5); continue; }
                s.style = 0; s.seed = 0; s.slug[0] = 0;
                if (sscanf(line.c_str(), "%ld %f %f %f %f %f %d %u %63s", &s.id, &s.x, &s.y, &s.z, &s.yaw, &s.ttl, &s.style, &s.seed, s.slug) >= 6) {
                    if (s.style == 7 && (!s.slug[0] || !SkClipFetch(s.slug))) s.style = 0;   // no dance file -> salsa
                    got.push_back(s);
                    if (s.id > since) since = s.id;
                }
            }
            if (!got.empty()) { std::lock_guard<std::mutex> lk(g_skMu); g_skQueue.insert(g_skQueue.end(), got.begin(), got.end()); }
        } catch (...) {}
    }
}

static Il2CppObject* SkPrefab() {
    if (Il2CppObject* p = CosTarget(g_skPrefab)) return p;
    static double nextTry = 0;
    double now = nowSeconds();
    if (now < nextTry) return nullptr;
    nextTry = now + 10;
    static Method<Il2CppObject*> findAll(FindOverload(Class("UnityEngine", "Resources"), "FindObjectsOfTypeAll", {"System.Type"}));
    static Method<BNM::Structures::Mono::String*> gn(Class("UnityEngine", "Object").GetMethod("get_name", 0));
    static Method<Il2CppObject*> getScene(Class("UnityEngine", "GameObject").GetMethod("get_scene", 0));
    if (!findAll.IsValid() || !gn.IsValid()) { DevlogPush("[skel] FindObjectsOfTypeAll missing"); return nullptr; }
    auto* arr = (Array<Il2CppObject*>*)findAll((Il2CppObject*)Class("UnityEngine", "GameObject").GetMonoType());
    if (!arr) return nullptr;
    Il2CppObject* best = nullptr;
    for (int i = 0; i < (int)arr->capacity; i++) {
        Il2CppObject* g = arr->m_Items[i];
        if (!g) continue;
        auto* s = gn[g]();
        if (s && s->str() == "DancingSkeleton") { best = g; break; }
    }
    if (!best) { DevlogPush("[skel] DancingSkeleton prefab not in memory yet"); return nullptr; }
    g_skPrefab = Pin(best);
    DevlogPush("[skel] DancingSkeleton prefab found");
    return best;
}

// style 5 "Gangnam Style" (owner 2026-10-18): no clip for it, so
// the salsa Animation is switched off and the Mixamo bones are aimed every frame (Mixamo
// bones point along their local +Y in Unity): 4 beats of horse riding (bounce, wrists
// crossed on the reins, alternating knee lifts) then 4 beats of lasso (right arm circling
// over the head), ~132 BPM.
enum { SKB_SPINE, SKB_HEAD, SKB_LARM, SKB_LFORE, SKB_RARM, SKB_RFORE, SKB_LUP, SKB_LLEG, SKB_RUP, SKB_RLEG, SKB_N };
static const char* kSkBones[SKB_N] = {
    "dancingSkeletonArmature/Hips/Spine",
    "dancingSkeletonArmature/Hips/Spine/Spine1/Spine2/Neck",
    "dancingSkeletonArmature/Hips/Spine/Spine1/Spine2/LeftShoulder/LeftArm",
    "dancingSkeletonArmature/Hips/Spine/Spine1/Spine2/LeftShoulder/LeftArm/LeftForeArm",
    "dancingSkeletonArmature/Hips/Spine/Spine1/Spine2/RightShoulder/RightArm",
    "dancingSkeletonArmature/Hips/Spine/Spine1/Spine2/RightShoulder/RightArm/RightForeArm",
    "dancingSkeletonArmature/Hips/LeftUpLeg", "dancingSkeletonArmature/Hips/LeftUpLeg/LeftLeg",
    "dancingSkeletonArmature/Hips/RightUpLeg", "dancingSkeletonArmature/Hips/RightUpLeg/RightLeg"};
// style 6 = a random dance made up from a seed (owner 2026-10-18 "add a huge amount of
// random dances"): 4-6 poses picked from a move library (directions in the skeleton's
// own right/up/forward frame, right side mirrored), blended on the beat, plus bob / sway /
// spin. Every seed is a different dance.
struct SkDance { int nPose, beatsPer; float bpm, bob, sway, spin; Vector3 pose[6][SKB_N]; };
struct SkLive { void* go; int style; double t0, until; float x, y, z, yaw; void* bones[SKB_N]; SkDance dance;
                std::shared_ptr<SkClip> clip; std::vector<void*> cbones; float hipScale; };

static SkDance SkMakeDance(unsigned seed) {
    unsigned st = seed ? seed : 12345u;
    auto rnd = [&]() { st = st * 1664525u + 1013904223u; return (st >> 8) / 16777216.f; };
    auto pick = [&](int n) { return (int)(rnd() * n) % n; };
    // left-side options in (r, u, f); the right side mirrors r
    static const Vector3 kArm[] = {{-0.2f, -1, 0}, {-1, 0, 0}, {-0.6f, 0.8f, 0}, {-0.1f, 1, 0}, {-0.1f, 0, 1},
                                   {-0.15f, -0.5f, 0.8f}, {-0.25f, -0.7f, -0.6f}, {-0.7f, 0.2f, 0.6f}};
    static const Vector3 kFore[] = {{0, 0, 0} /* straight */, {0, 1, 0}, {0.9f, 0, 0.4f}, {0, 0, 1}, {0, -1, 0},
                                    {0.5f, -0.6f, -0.1f}, {-0.3f, 1, 0.3f}};
    static const Vector3 kUpLeg[] = {{-0.08f, -1, 0}, {0, -0.3f, 0.95f}, {-0.6f, -0.8f, 0}, {0, -0.9f, -0.4f}, {-0.2f, -0.9f, 0.3f}};
    static const Vector3 kLeg[] = {{0, -1, 0}, {0, -0.7f, -0.7f}, {0, -0.2f, 1}};
    SkDance d{};
    d.nPose = 4 + pick(3);
    d.beatsPer = 1 + pick(2);
    d.bpm = 100 + rnd() * 40;
    d.bob = rnd() < 0.6f ? 0.03f + rnd() * 0.1f : 0.f;
    d.sway = rnd() < 0.5f ? 0.05f + rnd() * 0.1f : 0.f;
    d.spin = rnd() < 0.2f ? (rnd() < 0.5f ? -1.f : 1.f) * (60 + rnd() * 120) : 0.f;
    bool mirror = rnd() < 0.5f;
    for (int p = 0; p < d.nPose; p++) {
        if (mirror && (p % 2) == 1) {        // odd poses = the previous pose mirrored left/right
            Vector3* a = d.pose[p - 1]; Vector3* b = d.pose[p];
            auto mr = [](Vector3 v) { return Vector3(-v.x, v.y, v.z); };
            b[SKB_SPINE] = mr(a[SKB_SPINE]); b[SKB_HEAD] = mr(a[SKB_HEAD]);
            b[SKB_LARM] = mr(a[SKB_RARM]); b[SKB_RARM] = mr(a[SKB_LARM]);
            b[SKB_LFORE] = mr(a[SKB_RFORE]); b[SKB_RFORE] = mr(a[SKB_LFORE]);
            b[SKB_LUP] = mr(a[SKB_RUP]); b[SKB_RUP] = mr(a[SKB_LUP]);
            b[SKB_LLEG] = mr(a[SKB_RLEG]); b[SKB_RLEG] = mr(a[SKB_LLEG]);
            continue;
        }
        Vector3* P = d.pose[p];
        P[SKB_SPINE] = Vector3((rnd() - 0.5f) * 0.35f, 1, (rnd() - 0.5f) * 0.35f);
        P[SKB_HEAD] = Vector3((rnd() - 0.5f) * 0.4f, 1, (rnd() - 0.4f) * 0.4f);
        Vector3 la = kArm[pick(8)], ra = kArm[pick(8)];
        Vector3 lf = kFore[pick(7)], rf = kFore[pick(7)];
        P[SKB_LARM] = la; P[SKB_RARM] = Vector3(-ra.x, ra.y, ra.z);
        P[SKB_LFORE] = (lf.x == 0 && lf.y == 0 && lf.z == 0) ? la : lf;
        Vector3 rfm(-rf.x, rf.y, rf.z);
        P[SKB_RFORE] = (rf.x == 0 && rf.y == 0 && rf.z == 0) ? P[SKB_RARM] : rfm;
        int lift = pick(3);                  // 0 none, 1 left, 2 right (one leg at a time)
        Vector3 ul = kUpLeg[1 + pick(4)], ll = kLeg[pick(3)];
        P[SKB_LUP] = lift == 1 ? ul : kUpLeg[0];
        P[SKB_LLEG] = lift == 1 ? ll : kLeg[0];
        P[SKB_RUP] = lift == 2 ? Vector3(-ul.x, ul.y, ul.z) : Vector3(0.08f, -1, 0);
        P[SKB_RLEG] = lift == 2 ? ll : kLeg[0];
    }
    return d;
}
static std::vector<SkLive> g_skLive;   // spawned skeletons that spin / bounce / dance Guangdong style

static Quaternion SkQMul(const Quaternion& a, const Quaternion& b) {
    return Quaternion(a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y,
                      a.w * b.y - a.x * b.z + a.y * b.w + a.z * b.x,
                      a.w * b.z + a.x * b.y - a.y * b.x + a.z * b.w,
                      a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z);
}
static Vector3 SkNorm(Vector3 v) { float l = sqrtf(v.x * v.x + v.y * v.y + v.z * v.z); return l > 1e-6f ? Vector3(v.x / l, v.y / l, v.z / l) : Vector3(0, 1, 0); }
static Quaternion SkFromTo(Vector3 a, Vector3 b) {
    a = SkNorm(a); b = SkNorm(b);
    float d = a.x * b.x + a.y * b.y + a.z * b.z;
    Vector3 c(a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x);
    if (d < -0.9999f) { c = fabsf(a.x) < 0.9f ? Vector3(0, -a.z, a.y) : Vector3(-a.z, 0, a.x); c = SkNorm(c); return Quaternion(c.x, c.y, c.z, 0.f); }
    float s = sqrtf((1.f + d) * 2.f), inv = 1.f / s;
    return Quaternion(c.x * inv, c.y * inv, c.z * inv, s * 0.5f);
}
static void SkAim(Il2CppObject* bone, Vector3 dir) {
    static Class trC("UnityEngine", "Transform");
    static Method<Vector3> getUp(trC.GetMethod("get_up", 0));
    static Method<Quaternion> getRot(trC.GetMethod("get_rotation", 0));
    static Method<void> setRot(trC.GetMethod("set_rotation", 1));
    if (!bone || !getUp.IsValid() || !getRot.IsValid() || !setRot.IsValid()) return;
    Quaternion q = SkQMul(SkFromTo(getUp[bone](), dir), getRot[bone]());
    setRot[bone](q);
}
static void SkRandomDance(SkLive& k, Il2CppObject* tr, double t) {
    static Class trC("UnityEngine", "Transform");
    static Method<void> setPos(trC.GetMethod("set_position", 1));
    static Method<void> setRot(trC.GetMethod("set_rotation", 1));
    const SkDance& d = k.dance;
    double beat = t * d.bpm / 60.0;
    double pp = beat / d.beatsPer;
    int i0 = ((int)floor(pp)) % d.nPose, i1 = (i0 + 1) % d.nPose;
    float f = (float)(pp - floor(pp));
    float sm = f * f * (3 - 2 * f);                     // smoothstep between poses
    float ph = (float)(beat - floor(beat));
    float yawNow = k.yaw + (float)(t * d.spin);
    float yr = yawNow * 3.14159265f / 180.f;
    Vector3 F(sinf(yr), 0, cosf(yr)), R(cosf(yr), 0, -sinf(yr));
    float hop = d.bob * sinf(ph * 3.14159265f);
    float side = d.sway * sinf((float)(beat * 3.14159265));
    if (setPos.IsValid()) setPos[tr](Vector3(k.x + R.x * side, k.y + hop, k.z + R.z * side));
    if (d.spin != 0 && setRot.IsValid()) { float h = yr * 0.5f; setRot[tr](Quaternion(0.f, sinf(h), 0.f, cosf(h))); }
    for (int b = 0; b < SKB_N; b++) {
        Vector3 a = d.pose[i0][b], c = d.pose[i1][b];
        Vector3 v(a.x + (c.x - a.x) * sm, a.y + (c.y - a.y) * sm, a.z + (c.z - a.z) * sm);
        SkAim(CosTarget(k.bones[b]), Vector3(R.x * v.x + F.x * v.z, v.y, R.z * v.x + F.z * v.z));
    }
}

static void SkPlayClip(SkLive& k, Il2CppObject* tr, double t) {
    static Class trC("UnityEngine", "Transform");
    static Method<void> setPos(trC.GetMethod("set_position", 1));
    const SkClip& c = *k.clip;
    double fr = t * c.fps;
    int i0 = ((int)floor(fr)) % c.frames, i1 = (i0 + 1) % c.frames;
    float s = (float)(fr - floor(fr));
    float yr = k.yaw * 3.14159265f / 180.f;
    Vector3 F(sinf(yr), 0, cosf(yr)), R(cosf(yr), 0, -sinf(yr));
    const float* h0 = &c.hips[3 * i0]; const float* h1 = &c.hips[3 * i1];
    float dr = (h0[0] + (h1[0] - h0[0]) * s) * k.hipScale, du = (h0[1] + (h1[1] - h0[1]) * s) * k.hipScale,
          df = (h0[2] + (h1[2] - h0[2]) * s) * k.hipScale;
    float hl = sqrtf(dr * dr + df * df);
    if (hl > 1.5f) { dr *= 1.5f / hl; df *= 1.5f / hl; }     // don't wander off
    if (du > 1.0f) du = 1.0f; if (du < -1.0f) du = -1.0f;
    if (setPos.IsValid()) setPos[tr](Vector3(k.x + R.x * dr + F.x * df, k.y + du, k.z + R.z * dr + F.z * df));
    const int8_t* a = &c.dirs[3 * (size_t)c.nb * i0];
    const int8_t* b = &c.dirs[3 * (size_t)c.nb * i1];
    for (int i = 0; i < c.nb && i < (int)k.cbones.size(); i++) {
        float vr = a[3 * i] + (b[3 * i] - a[3 * i]) * s, vu = a[3 * i + 1] + (b[3 * i + 1] - a[3 * i + 1]) * s,
              vf = a[3 * i + 2] + (b[3 * i + 2] - a[3 * i + 2]) * s;
        SkAim(CosTarget(k.cbones[i]), Vector3(R.x * vr + F.x * vf, vu, R.z * vr + F.z * vf));
    }
}

static void SkGuangdong(SkLive& k, Il2CppObject* tr, double t) {
    static Class trC("UnityEngine", "Transform");
    static Method<void> setPos(trC.GetMethod("set_position", 1));
    const double bps = 132.0 / 60.0;
    double beat = t * bps;
    float ph = (float)(beat - floor(beat));               // 0..1 inside the beat
    bool lasso = ((int)floor(beat / 4.0)) % 2 == 1;
    float yr = k.yaw * 3.14159265f / 180.f;
    Vector3 F(sinf(yr), 0, cosf(yr)), R(cosf(yr), 0, -sinf(yr)), U(0, 1, 0);
    auto mix = [](Vector3 a, float fa, Vector3 b, float fb, Vector3 c, float fc) {
        return Vector3(a.x * fa + b.x * fb + c.x * fc, a.y * fa + b.y * fb + c.y * fc, a.z * fa + b.z * fb + c.z * fc); };
    // the gallop: up on every beat, small side step every other beat
    float hop = 0.12f * sinf(ph * 3.14159265f);
    float side = 0.08f * sinf((float)(beat * 3.14159265));
    if (setPos.IsValid()) setPos[tr](Vector3(k.x + R.x * side, k.y + hop, k.z + R.z * side));
    auto B = [&](int i) { return CosTarget(k.bones[i]); };
    SkAim(B(SKB_SPINE), mix(U, 1.f, F, -0.12f, R, 0.f));
    SkAim(B(SKB_HEAD), mix(U, 1.f, F, 0.15f * sinf(ph * 6.2832f), R, 0.f));
    bool leftUp = ((int)floor(beat)) % 2 == 0;            // knees take turns
    float lift = sinf(ph * 3.14159265f);
    SkAim(B(SKB_LUP), mix(U, -1.f, F, leftUp ? 0.9f * lift : 0.05f, R, -0.12f));
    SkAim(B(SKB_LLEG), mix(U, -1.f, F, leftUp ? -0.5f * lift : 0.f, R, 0.f));
    SkAim(B(SKB_RUP), mix(U, -1.f, F, !leftUp ? 0.9f * lift : 0.05f, R, 0.12f));
    SkAim(B(SKB_RLEG), mix(U, -1.f, F, !leftUp ? -0.5f * lift : 0.f, R, 0.f));
    float bob = 0.18f * sinf(ph * 6.2832f);
    // left arm: always on the reins (upper arm forward-down, forearm across to the right)
    SkAim(B(SKB_LARM), mix(F, 0.8f, U, -0.45f + bob, R, 0.2f));
    SkAim(B(SKB_LFORE), mix(F, 0.35f, R, 0.9f, U, bob));
    if (!lasso) {
        SkAim(B(SKB_RARM), mix(F, 0.8f, U, -0.45f + bob, R, -0.2f));
        SkAim(B(SKB_RFORE), mix(F, 0.35f, R, -0.9f, U, bob));
    } else {                                             // lasso: right arm up, forearm circling
        float a = (float)(t * 6.2832 * 1.6);
        SkAim(B(SKB_RARM), mix(U, 1.f, R, 0.25f, F, 0.f));
        SkAim(B(SKB_RFORE), mix(U, 0.45f, F, 0.9f * cosf(a), R, 0.9f * sinf(a)));
    }
}

static void SkMotionTick(double now) {
    if (g_skLive.empty()) return;
    static Class goC("UnityEngine", "GameObject"), trC("UnityEngine", "Transform");
    static Method<Il2CppObject*> getTr(goC.GetMethod("get_transform", 0));
    static Method<void> setPos(trC.GetMethod("set_position", 1));
    static Method<void> setRot(trC.GetMethod("set_rotation", 1));
    for (size_t i = 0; i < g_skLive.size();) {
        auto& k = g_skLive[i];
        Il2CppObject* go = CosTarget(k.go);
        if (!go || !Alive(go) || now > k.until) {
            Unpin(k.go);
            for (auto* b : k.bones) Unpin(b);
            for (auto* b : k.cbones) Unpin(b);
            g_skLive.erase(g_skLive.begin() + i); continue;
        }
        double t = now - k.t0;
        try {
            Il2CppObject* tr = getTr[go]();
            if (k.style == 5) SkGuangdong(k, tr, t);
            else if (k.style == 6) SkRandomDance(k, tr, t);
            else if (k.style == 7 && k.clip) SkPlayClip(k, tr, t);
            else if (k.style == 3 && setRot.IsValid()) {          // spin: one turn every 2 s
                float h = (float)((k.yaw + t * 180.0) * 3.14159265 / 360.0);
                setRot[tr](Quaternion(0.f, sinf(h), 0.f, cosf(h)));
            } else if (k.style == 4 && setPos.IsValid()) {   // bounce: hop 0.6 m twice a second
                float hop = 0.6f * fabsf(sinf((float)(t * 3.14159265 * 2.0)));
                setPos[tr](Vector3(k.x, k.y + hop, k.z));
            }
        } catch (...) {}
        i++;
    }
}

static void SkeletonTick(double now) {
    SkMotionTick(now);
    static bool started = false;
    if (!started) { started = true; std::thread(SkPollThread).detach(); }
    static double nextRoom = 0;
    if (now >= nextRoom) {
        nextRoom = now + 2.0;
        static BNM::Class pn("Photon.Pun", "PhotonNetwork", Image("PhotonUnityNetworking.dll"));
        static BNM::Class roomInfo("Photon.Realtime", "RoomInfo", Image("PhotonRealtime.dll"));
        static Method<Il2CppObject*> curRoom(pn.GetMethod("get_CurrentRoom", 0));
        static Method<Il2CppObject*> rname(roomInfo.GetMethod("get_Name", 0));
        std::string nm;
        if (curRoom.IsValid() && rname.IsValid()) if (Il2CppObject* r = curRoom()) nm = MonoStr(rname[r]());
        std::lock_guard<std::mutex> lk(g_skMu);
        g_skRoom = nm;
    }
    std::vector<SkSpawn> todo;
    { std::lock_guard<std::mutex> lk(g_skMu); todo.swap(g_skQueue); }
    if (todo.empty()) return;
    Il2CppObject* prefab = SkPrefab();
    if (!prefab) return;
    static Class objC("UnityEngine", "Object");
    static Method<Il2CppObject*> inst(FindOverload(objC, "Instantiate", {"UnityEngine.Object", "UnityEngine.Vector3", "UnityEngine.Quaternion"}));
    static Method<void> destroyT(FindOverload(objC, "Destroy", {"UnityEngine.Object", "System.Single"}));
    static Method<void> setActive(Class("UnityEngine", "GameObject").GetMethod("SetActive", 1));
    if (!inst.IsValid()) { DevlogPush("[skel] Instantiate(Object, Vector3, Quaternion) missing"); return; }
    static int logged = 0;
    for (auto& s : todo) {
        float h = s.yaw * 3.14159265f / 360.f;   // half angle (yaw in degrees)
        Quaternion q(0.f, sinf(h), 0.f, cosf(h));
        Il2CppObject* go = inst(prefab, Vector3(s.x, s.y, s.z), q);
        if (!go) continue;
        if (setActive.IsValid()) setActive[go](true);
        // live62: on its own the skeleton froze on one pose - the party normally drives it.
        // Turn its DancingSkeleton script off and loop the salsa clip on its Animation
        // (VisualContainer; the prefab has no default clip).
        try {
            static Class goC("UnityEngine", "GameObject"), compC("UnityEngine", "Component"), trC("UnityEngine", "Transform");
            static Class dsC("", "DancingSkeleton", Image("Assembly-CSharp.dll"));
            static Class animC("UnityEngine", "Animation");
            static Method<Il2CppObject*> getComp(FindOverload(goC, "GetComponent", {"System.Type"}));
            static Method<Il2CppObject*> getTr(goC.GetMethod("get_transform", 0));
            static Method<Il2CppObject*> getGo(compC.GetMethod("get_gameObject", 0));
            static Method<Il2CppObject*> trFind(FindOverload(trC, "Find", {"System.String"}));
            static Method<void> setEnabled(Class("UnityEngine", "Behaviour").GetMethod("set_enabled", 1));
            static Method<void> setWrap(animC.GetMethod("set_wrapMode", 1));
            static Method<bool> play(FindOverload(animC, "Play", {"System.String"}));
            if (dsC.IsValid() && getComp.IsValid() && setEnabled.IsValid())
                if (Il2CppObject* ds = getComp[go]((Il2CppObject*)dsC.GetMonoType())) setEnabled[ds](false);
            Il2CppObject* vc = getTr.IsValid() && trFind.IsValid() ? trFind[getTr[go]()](CreateMonoString("VisualContainer")) : nullptr;
            Il2CppObject* an = vc && getGo.IsValid() ? getComp[getGo[vc]()]((Il2CppObject*)animC.GetMonoType()) : nullptr;
            if (an) {
                if (setWrap.IsValid()) setWrap[an](2);   // WrapMode.Loop
                bool ok = play.IsValid() && play[an](CreateMonoString("dancingSkeletonArmature|Anim_dancingSkeleton_salsa"));
                // styles: fast / slow change the clip speed (AnimationState via Animation[name])
                static Method<Il2CppObject*> getState(FindOverload(animC, "get_Item", {"System.String"}));
                static Method<void> setSpeed(Class("UnityEngine", "AnimationState").GetMethod("set_speed", 1));
                float spd = s.style == 1 ? 2.0f : s.style == 2 ? 0.45f : 1.0f;
                // live64: the salsa played once and froze - Animation.wrapMode doesn't reach the
                // clip's existing state (clip wrap = Default), so loop the state itself
                static Method<void> stWrap(Class("UnityEngine", "AnimationState").GetMethod("set_wrapMode", 1));
                if (getState.IsValid())
                    if (Il2CppObject* st = getState[an](CreateMonoString("dancingSkeletonArmature|Anim_dancingSkeleton_salsa"))) {
                        if (stWrap.IsValid()) stWrap[st](2);   // WrapMode.Loop
                        if (spd != 1.0f && setSpeed.IsValid()) setSpeed[st](spd);
                    }
                if (s.style >= 3 && s.style <= 7) {
                    SkLive L{Pin(go), s.style, nowSeconds(), nowSeconds() + (s.ttl > 0 ? s.ttl : 20.f), s.x, s.y, s.z, s.yaw, {}, {}, nullptr, {}, 1.f};
                    if (s.style == 6) L.dance = SkMakeDance(s.seed);
                    if (s.style == 7) {
                        L.clip = SkClipGet(s.slug);
                        static Method<Vector3> trPos(trC.GetMethod("get_position", 0));
                        int got = 0;
                        for (int b = 0; b < kSkClipN; b++) {
                            Il2CppObject* bt = trFind[vc](CreateMonoString((std::string("dancingSkeletonArmature/Hips/") + kSkClipBones[b]).c_str()));
                            L.cbones.push_back(bt ? Pin(bt) : nullptr);
                            got += bt != nullptr;
                        }
                        // the clip's hip offsets are in hip heights: scale by this skeleton's hip height
                        Il2CppObject* hips = trFind[vc](CreateMonoString("dancingSkeletonArmature/Hips"));
                        if (hips && trPos.IsValid()) { float hh = trPos[hips]().y - s.y; if (hh > 0.2f && hh < 5.f) L.hipScale = hh; }
                        if (logged < 20) DevlogPush(std::string("[skel] dance ") + s.slug + ": " + std::to_string(got) + "/" + std::to_string(kSkClipN) + " bones");
                    }
                    if (s.style >= 5) {   // own dance: salsa off, grab the bones
                        static Method<void> stopAnim(animC.GetMethod("Stop", 0));
                        if (stopAnim.IsValid()) stopAnim[an]();
                        if (setEnabled.IsValid()) setEnabled[an](false);
                        int got = 0;
                        for (int b = 0; b < SKB_N; b++) {
                            Il2CppObject* bt = trFind[vc](CreateMonoString(kSkBones[b]));
                            L.bones[b] = bt ? Pin(bt) : nullptr;
                            got += bt != nullptr;
                        }
                        if (logged < 20) DevlogPush("[skel] gangnam style: " + std::to_string(got) + "/" + std::to_string(SKB_N) + " bones");
                    }
                    g_skLive.push_back(L);
                }
                if (logged < 20) DevlogPush(std::string("[skel] salsa ") + (ok ? "playing" : "Play() returned false"));
            } else if (logged < 20) DevlogPush("[skel] no Animation on VisualContainer");
        } catch (...) { DevlogPush("[skel] animate EXCEPTION"); }
        if (destroyT.IsValid()) destroyT(go, s.ttl > 0 ? s.ttl : 20.f);
        if (logged++ < 20) DevlogPush("[skel] spawned #" + std::to_string(s.id) + " at " + std::to_string((int)s.x) + "," +
                                      std::to_string((int)s.y) + "," + std::to_string((int)s.z));
    }
}

// ---------------------------------------------------------------------
//  live59 DIAG: reskin call tracer (owner 2026-10-18: "add more stuff for debugging so
//  we can see how to save reskins"). Every instance method of the imagination / item
//  customization / reskin classes (class dump live55) whose arguments and return are
//  pointer-sized (no floats / big structs) is hooked; each call logs (first 8 per
//  method) "[tr] Class#index(args)" with string / list / object-type arguments. Methods
//  shorter than 32 bytes and code shared by several methods are skipped (an inline
//  hook would overwrite the next function).
// ---------------------------------------------------------------------
static std::string CiStr(void* o);      // defined with the live52 reskin diag below
static std::string CiList(void* lst);
typedef void* (*TrFn)(void*, void*, void*, void*, void*, void*, void*, void*);
static const int kTrMax = 600;
static TrFn g_trOrig[kTrMax];
static std::atomic<int> g_trN[kTrMax];
static std::string g_trName[kTrMax];
static int g_trArgc[kTrMax];
static uint8_t g_trRef[kTrMax][8];   // 1 = the parameter is a reference type (safe to read as an object)
// live68: every pointer the tracers read is checked first (process_vm_readv on ourselves
// fails cleanly on unmapped memory) - live59/60/67 crashed reading a non-pointer field
#include <sys/uio.h>
static bool TrSafeRead(const void* addr, void* out, size_t n) {
    uintptr_t u = (uintptr_t)addr;
    if (u < 0x10000 || (u >> 48) != 0) return false;
    struct iovec local{out, n}, remote{(void*)addr, n};
    return process_vm_readv(getpid(), &local, 1, &remote, 1, 0) == (ssize_t)n;
}
static std::string TrSafeStr(void* o) {      // a managed string, or "" if it isn't one
    void* klass = nullptr;
    if (!TrSafeRead(o, &klass, 8) || !klass) return "";
    void* namePtr = nullptr;
    if (!TrSafeRead((uint8_t*)klass + 0x10, &namePtr, 8) || !namePtr) return "";
    char nm[8] = {0};
    if (!TrSafeRead(namePtr, nm, 7) || strcmp(nm, "String") != 0) return "";
    int32_t len = 0;
    if (!TrSafeRead((uint8_t*)o + 0x10, &len, 4) || len < 0 || len > 200) return "";
    std::u16string w(len, u'\0');
    if (len && !TrSafeRead((uint8_t*)o + 0x14, &w[0], len * 2)) return "";
    std::string out;
    for (char16_t c : w) out += (c < 128 ? (char)c : '?');
    return out;
}
static std::string TrSafeClassName(void* o) {
    void* klass = nullptr; void* namePtr = nullptr; char nm[40] = {0};
    if (!TrSafeRead(o, &klass, 8) || !TrSafeRead((uint8_t*)klass + 0x10, &namePtr, 8)) return "?";
    for (int i = 0; i < 39; i++) { if (!TrSafeRead((uint8_t*)namePtr + i, &nm[i], 1) || !nm[i]) break; }
    return nm;
}

static void TrLog(int n, void** a) {
    std::string line = (g_trName[n].rfind("[role]", 0) == 0 ? g_trName[n] : "[tr] " + g_trName[n]) + "(";
    for (int i = 0; i < g_trArgc[n] && i < 6; i++) {
        void* v = a[i + 1];   // a[0] = this
        std::string s;
        if (!g_trRef[n][i]) s = std::to_string((long)(int32_t)(uintptr_t)v);
        else if (!v) s = "null";
        else {
            std::string str = TrSafeStr(v);
            if (!str.empty() || TrSafeClassName(v) == "String") s = "\"" + str + "\"";
            else {
                s = "<" + TrSafeClassName(v);
                for (int off = 0x10; off <= 0x28; off += 8) {   // first string field (RoleData -> key)
                    void* f = nullptr;
                    if (TrSafeRead((uint8_t*)v + off, &f, 8) && f) { std::string fs = TrSafeStr(f); if (!fs.empty()) { s += ":" + fs; break; } }
                }
                s += ">";
            }
        }
        line += (i ? ", " : "") + s;
    }
    DevlogPush(line + ")");
}
template <int N> static void* TrHook(void* a0, void* a1, void* a2, void* a3, void* a4, void* a5, void* a6, void* a7) {
    if (g_trN[N].fetch_add(1) < 8) { void* a[8] = {a0, a1, a2, a3, a4, a5, a6, a7}; try { TrLog(N, a); } catch (...) {} }
    return g_trOrig[N](a0, a1, a2, a3, a4, a5, a6, a7);
}
template <size_t... I> static std::array<TrFn, sizeof...(I)> TrMake(std::index_sequence<I...>) { return {{ &TrHook<(int)I>... }}; }

static bool TrSafeType(const Il2CppType* t) {
    if (!t) return false;
    switch (t->type) {
        case 0x01: /* void */ case 0x02: /* bool */ case 0x03: case 0x04: case 0x05: case 0x06: case 0x07:
        case 0x08: case 0x09: case 0x0a: case 0x0b: case 0x18: case 0x19:
        case 0x0e: /* string */ case 0x12: /* class */ case 0x1c: /* object */ case 0x1d: /* szarray */
        case 0x14: /* array */
            return true;
        case 0x15: {   // generic instance: reference types only
            auto* gc = t->data.generic_class;
            return gc && gc->type && gc->type->type == 0x12;
        }
        case 0x11: {   // valuetype: enums only
            Class c(t);
            return c.IsValid() && c.GetClass() && c.GetClass()->enumtype;
        }
        default: return false;   // floats (0x0c/0x0d), structs, pointers, byref
    }
}

// live67 DIAG: which RoleManager check hides other players' developer badge? Every
// RoleManager method that returns bool (pointer/int args only) is hooked and logs its
// inputs and answer ("[role] RoleManager#idx(args) -> 0/1"), first 40 calls each.
static bool g_trStatic[kTrMax];
static bool g_trRet[kTrMax];
template <int N> static void* TrHookR(void* a0, void* a1, void* a2, void* a3, void* a4, void* a5, void* a6, void* a7) {
    void* r = g_trOrig[N](a0, a1, a2, a3, a4, a5, a6, a7);
    if (g_trN[N].fetch_add(1) < 40) {
        try {
            void* a[9] = {nullptr, a0, a1, a2, a3, a4, a5, a6, a7};   // static: args start at a0
            void** base = g_trStatic[N] ? a : a + 1;                     // TrLog reads base[i + 1]
            std::string saved = g_trName[N];
            g_trName[N] = saved + "";
            TrLog(N, base);
            DevlogPush("[role]   -> " + std::to_string((int)((uintptr_t)r & 0xff)));
        } catch (...) {}
    }
    return r;
}
template <size_t... I> static std::array<TrFn, sizeof...(I)> TrMakeR(std::index_sequence<I...>) { return {{ &TrHookR<(int)I>... }}; }

static void InstallRoleTracer() {
    try {
        static auto hooks = TrMakeR(std::make_index_sequence<64>{});
        Class k("", "RoleManager", Image("Assembly-CSharp.dll"));
        if (!k.IsValid()) { DevlogPush("[role] RoleManager missing"); return; }
        int n = 0, idx = 0;
        for (auto& mb : k.GetMethods(false)) {
            int myIdx = idx++;
            auto* mi = mb.GetInfo();
            if (!mi || !mi->methodPointer || n >= 64 || !mi->return_type || mi->return_type->type != 0x02) continue;   // bool only
            {   // live68: skip tiny / shared code (an inline hook would overwrite the next function)
                static std::vector<uintptr_t> starts;
                static std::map<uintptr_t, int> uses;
                if (starts.empty()) {
                    for (auto& cls : Image("Assembly-CSharp.dll").GetClasses(false))
                        for (auto& m2 : cls.GetMethods(false)) if (auto* m2i = m2.GetInfo()) if (m2i->methodPointer) {
                            starts.push_back((uintptr_t)m2i->methodPointer); uses[(uintptr_t)m2i->methodPointer]++; }
                    std::sort(starts.begin(), starts.end());
                }
                uintptr_t pp = (uintptr_t)mi->methodPointer;
                auto it = std::upper_bound(starts.begin(), starts.end(), pp);
                long sz = it == starts.end() ? 1 << 20 : (long)(*it - pp);
                if (sz < 48 || uses[pp] != 1) continue;
            }
            bool ok = mi->parameters_count >= 1 && mi->parameters_count <= 5;
            for (int i = 0; ok && i < mi->parameters_count; i++) ok = mi->parameters && TrSafeType(mi->parameters[i]);
            if (!ok) continue;
            g_trName[n] = "[role] RoleManager#" + std::to_string(myIdx) + (mi->name && isalpha((unsigned char)mi->name[0]) ? std::string(".") + mi->name : "");
            g_trArgc[n] = mi->parameters_count;
            g_trStatic[n] = (mi->flags & 0x10) != 0;
            for (int i = 0; i < mi->parameters_count && i < 8; i++) {
                int tt = mi->parameters[i]->type;
                g_trRef[n][i] = (tt == 0x0e || tt == 0x12 || tt == 0x1c || tt == 0x1d || tt == 0x14 || tt == 0x15) ? 1 : 0;
            }
            ::BasicHook((void*)mi->methodPointer, (void*)hooks[n], g_trOrig[n]);
            n++;
        }
        DevlogPush("[role] hooked " + std::to_string(n) + " RoleManager bool methods");
    } catch (...) { DevlogPush("[role] EXCEPTION"); }
}

static void InstallReskinTracer() {
    // live60: trimmed (live59 hooked 587 methods and read ints as objects -> SIGSEGV)
    static const char* kClasses[] = {"CustomizedItemDatabase", "PlayerImagination", "ImaginationPageSelector",
        "ClosetImaginationDisplay", "ItemCustomizationBenchMapObject", "PurchaseCustomizedItemConsole", nullptr};
    try {
        static auto hooks = TrMake(std::make_index_sequence<kTrMax>{});
        // every method entry point in Assembly-CSharp, sorted, to measure function sizes
        std::vector<uintptr_t> starts;
        Image img("Assembly-CSharp.dll");
        for (auto& cls : img.GetClasses(false))
            for (auto& m : cls.GetMethods(false)) if (auto* mi = m.GetInfo()) if (mi->methodPointer) starts.push_back((uintptr_t)mi->methodPointer);
        std::sort(starts.begin(), starts.end());
        auto sizeOf = [&](uintptr_t p) -> long {
            auto it = std::upper_bound(starts.begin(), starts.end(), p);
            return it == starts.end() ? 1 << 20 : (long)(*it - p);
        };
        std::map<uintptr_t, int> uses;
        for (auto p : starts) uses[p]++;
        std::set<uintptr_t> done;
        int n = 0, skipped = 0;
        for (int c = 0; kClasses[c]; c++) {
            Class k("", kClasses[c], img);
            if (!k.IsValid()) { DevlogPush(std::string("[tr] class missing: ") + kClasses[c]); continue; }
            int idx = 0;
            for (auto& mb : k.GetMethods(false)) {
                int myIdx = idx++;
                auto* mi = mb.GetInfo();
                if (!mi || !mi->methodPointer || (mi->flags & 0x10) || n >= kTrMax) continue;   // static / no code
                std::string nm = mi->name ? mi->name : "";
                if (nm == "Update" || nm == "LateUpdate" || nm == "FixedUpdate" || nm == ".ctor" || nm == ".cctor") continue;
                uintptr_t p = (uintptr_t)mi->methodPointer;
                bool ok = mi->parameters_count >= 1 && mi->parameters_count <= 6 && TrSafeType(mi->return_type)
                          && uses[p] == 1 && !done.count(p) && sizeOf(p) >= 48 && n < 160;
                for (int i = 0; ok && i < mi->parameters_count; i++) ok = mi->parameters && TrSafeType(mi->parameters[i]);
                if (!ok) { skipped++; continue; }
                done.insert(p);
                g_trName[n] = std::string(kClasses[c]) + "#" + std::to_string(myIdx) +
                              (nm.size() && nm.size() < 40 && isalpha((unsigned char)nm[0]) ? "." + nm : "");
                g_trArgc[n] = mi->parameters_count;
                for (int i = 0; i < mi->parameters_count && i < 8; i++) {
                    int tt = mi->parameters[i]->type;
                    g_trRef[n][i] = (tt == 0x0e || tt == 0x12 || tt == 0x1c || tt == 0x1d || tt == 0x14 || tt == 0x15) ? 1 : 0;
                }
                ::BasicHook((void*)p, (void*)hooks[n], g_trOrig[n]);
                n++;
            }
        }
        DevlogPush("[tr] reskin tracer: hooked " + std::to_string(n) + " methods, skipped " + std::to_string(skipped));
    } catch (...) { DevlogPush("[tr] tracer EXCEPTION"); }
}

// ---------------------------------------------------------------------
//  Trass/developer badge on OTHER players (live69, owner 2026-10-18). Real Yeeps never
//  builds another player's roleBadge_developer zipper badge (reviewer / tester / BAM
//  badges do get built; every RoleManager check says yes - an anti-impersonation rule in
//  obfuscated code). So the mod builds it: once a second, for every other player whose
//  Photon "activeCosmeticDatas" has roleBadge_developer, find their avatar (playerModel
//  whose cosmetic children match their list), and if their ZipperAnchor is empty, clone
//  any zipper item in the scene into it with the trassDev badge meshes. Removed again when
//  they take the badge off.
// ---------------------------------------------------------------------
struct DevBadge { void* go; void* anchor; };
static std::vector<DevBadge> g_devBadges;

static void DevBadgeTick(double now) {
    static double next = 0;
    if (now < next) return;
    next = now + 1.0;
    static BNM::Class pn("Photon.Pun", "PhotonNetwork", Image("PhotonUnityNetworking.dll"));
    static BNM::Class playerC("Photon.Realtime", "Player", Image("PhotonRealtime.dll"));
    static BNM::Class htC("ExitGames.Client.Photon", "Hashtable", Image("Photon3Unity3D.dll"));
    static Method<Array<Il2CppObject*>*> others(pn.GetMethod("get_PlayerListOthers", 0));
    static Method<Il2CppObject*> props(playerC.GetMethod("get_CustomProperties", 0));
    static Method<Il2CppObject*> htGet(htC.GetMethod("get_Item", 1));
    static Class objC("UnityEngine", "Object"), goC("UnityEngine", "GameObject"), trC("UnityEngine", "Transform"), compC("UnityEngine", "Component");
    static Class smrC("UnityEngine", "SkinnedMeshRenderer"), mfC("UnityEngine", "MeshFilter"), mrC("UnityEngine", "MeshRenderer");
    static Method<BNM::Structures::Mono::String*> gn(objC.GetMethod("get_name", 0));
    static Method<void> setName(objC.GetMethod("set_name", 1));
    static Method<Il2CppObject*> getTr(compC.GetMethod("get_transform", 0));
    static Method<Il2CppObject*> getGo(compC.GetMethod("get_gameObject", 0));
    static Method<Il2CppObject*> parentOf(trC.GetMethod("get_parent", 0));
    static Method<int> childCount(trC.GetMethod("get_childCount", 0));
    static Method<Il2CppObject*> getChild(trC.GetMethod("GetChild", 1));
    static Method<Il2CppObject*> trFind(FindOverload(trC, "Find", {"System.String"}));
    static Method<Il2CppObject*> inst(FindOverload(objC, "Instantiate", {"UnityEngine.Object", "UnityEngine.Transform"}));
    static Method<void> destroy(objC.GetMethod("Destroy", 1));
    static Method<Il2CppObject*> getComp(FindOverload(goC, "GetComponent", {"System.Type"}));
    static Method<void> mfSetMesh(mfC.GetMethod("set_sharedMesh", 1));
    static Method<void> forceOff(Class("UnityEngine", "Renderer").GetMethod("set_forceRenderingOff", 1));
    static Method<Il2CppObject*> findAll(FindOverload(Class("UnityEngine", "Resources"), "FindObjectsOfTypeAll", {"System.Type"}));
    if (!others.IsValid() || !props.IsValid() || !htGet.IsValid() || !gn.IsValid() || !trFind.IsValid() || !inst.IsValid()) return;
    auto nameOf = [&](Il2CppObject* o) { auto* s = o ? gn[o]() : nullptr; return s ? s->str() : std::string(); };
    // 1) who wears it (their other cosmetics, sorted, identify their avatar)
    std::vector<std::vector<std::string>> wearers;
    std::set<std::string> wearerKeys;
    if (auto* pl = others()) for (int i = 0; i < (int)pl->capacity; i++) {
        Il2CppObject* p = pl->m_Items[i];
        Il2CppObject* h = p ? props[p]() : nullptr;
        auto* arr = h ? (Array<Il2CppObject*>*)htGet[h]((Il2CppObject*)CreateMonoString("activeCosmeticDatas")) : nullptr;
        if (!arr || !((Il2CppObject*)arr)->klass || !((Il2CppObject*)arr)->klass->name || strstr(((Il2CppObject*)arr)->klass->name, "[]") == nullptr) continue;
        bool dev = false; std::vector<std::string> rest;
        for (int j = 0; j < (int)arr->capacity; j++) {
            std::string c = MonoStr(arr->m_Items[j]);
            if (c == "roleBadge_developer") dev = true;
            else if (c.rfind("roleBadge_", 0) != 0 && c.rfind("zipper", 0) != 0) rest.push_back(c);
        }
        std::sort(rest.begin(), rest.end());
        std::string key; for (auto& c : rest) key += c + "|";
        if (dev && !rest.empty()) { wearers.push_back(rest); wearerKeys.insert(key); }   // an empty outfit would match every bare model
    }
    // drop our badges whose wearer took it off / left
    for (size_t i = 0; i < g_devBadges.size();) {
        Il2CppObject* go = CosTarget(g_devBadges[i].go);
        Il2CppObject* an = CosTarget(g_devBadges[i].anchor);
        bool keep = go && Alive(go) && an && Alive(an);
        if (keep) {   // still a wearer? (playerModel = anchor -> Torso -> GorillaArmature2 -> playerModel)
            Il2CppObject* pm = parentOf[parentOf[parentOf[an]()]()]();
            std::vector<std::string> kids;
            for (int c = 0, n = pm ? childCount[pm]() : 0; c < n; c++) {
                std::string nm = nameOf(getChild[pm](c));
                if (nm != "SimpleAvatar2" && nm != "GorillaArmature2" && nm.rfind("zipper", 0) != 0) kids.push_back(nm);
            }
            std::sort(kids.begin(), kids.end());
            std::string key; for (auto& c : kids) key += c + "|";
            keep = wearerKeys.count(key) > 0;
        }
        if (!keep) {
            if (go && Alive(go) && destroy.IsValid()) destroy(go);
            Unpin(g_devBadges[i].go); Unpin(g_devBadges[i].anchor);
            g_devBadges.erase(g_devBadges.begin() + i);
        } else i++;
    }
    if (wearers.empty()) return;
    // 2) the trassDev meshes + a zipper item to clone
    static void* meshL0 = nullptr; static void* meshL1 = nullptr;
    if (!CosTarget(meshL0) && findAll.IsValid()) {
        if (auto* ms = (Array<Il2CppObject*>*)findAll((Il2CppObject*)Class("UnityEngine", "Mesh").GetMonoType()))
            for (int i = 0; i < (int)ms->capacity; i++) {
                std::string nm = nameOf(ms->m_Items[i]);
                if (nm == "cosmetic_zipper_badge_trassDev") meshL0 = Pin(ms->m_Items[i]);
                else if (nm == "cosmetic_zipper_badge_trassDev_LOD1") meshL1 = Pin(ms->m_Items[i]);
            }
        static int warn = 0;
        if (!CosTarget(meshL0) && warn++ < 3) DevlogPush("[devbadge] trassDev mesh not in memory");
    }
    if (!CosTarget(meshL0)) return;
    // 3) every avatar: playerModel parents of skinned renderers
    auto* smrs = Method<Array<Il2CppObject*>*>(findObjsMethod())((Il2CppObject*)smrC.GetMonoType());
    if (!smrs) return;
    std::set<void*> seen;
    Il2CppObject* templ = nullptr;
    std::vector<Il2CppObject*> models;
    for (int i = 0; i < (int)smrs->capacity; i++) {
        Il2CppObject* t = smrs->m_Items[i] ? getTr[smrs->m_Items[i]]() : nullptr;
        Il2CppObject* pm = t ? parentOf[t]() : nullptr;
        if (!pm || seen.count(pm) || nameOf(pm) != "playerModel") continue;
        seen.insert(pm);
        static Method<Il2CppObject*> trRoot(trC.GetMethod("get_root", 0));
        Il2CppObject* root = trRoot.IsValid() ? trRoot[pm]() : nullptr;
        bool puppet = root && nameOf(root).rfind("PuppetPlayer", 0) == 0;   // other players only (not mannequins / me)
        if (puppet) models.push_back(pm);
        if (!templ) {   // any zipper item already built (mine or another player's) to clone
            Il2CppObject* an = trFind[pm](CreateMonoString("GorillaArmature2/Torso/ZipperAnchor"));
            if (an && childCount[an]() > 0) templ = getGo[getChild[an](0)]();
        }
    }
    if (!templ) return;
    for (Il2CppObject* pm : models) {
        std::vector<std::string> kids;
        for (int c = 0, n = childCount[pm](); c < n; c++) {
            std::string nm = nameOf(getChild[pm](c));
            if (nm != "SimpleAvatar2" && nm != "GorillaArmature2" && nm.rfind("zipper", 0) != 0) kids.push_back(nm);
        }
        std::sort(kids.begin(), kids.end());
        std::string key; for (auto& c : kids) key += c + "|";
        if (!wearerKeys.count(key)) continue;
        Il2CppObject* an = trFind[pm](CreateMonoString("GorillaArmature2/Torso/ZipperAnchor"));
        if (!an || childCount[an]() > 0) continue;   // the game built something there itself
        Il2CppObject* b = inst(templ, an);
        if (!b) continue;
        setName[b](CreateMonoString("roleBadge_developer"));
        static Method<Il2CppObject*> goTr(goC.GetMethod("get_transform", 0));   // live71: GameObject's, not Component's
        Il2CppObject* bt = goTr[b]();
        static Method<Il2CppObject*> mfGetMesh(mfC.GetMethod("get_sharedMesh", 0));
        std::string dbg = "template " + nameOf(templ);
        for (const char* lod : {"LOD0", "LOD1"}) {
            Il2CppObject* lt = trFind[bt](CreateMonoString(lod));
            Il2CppObject* lg = lt ? getGo[lt]() : nullptr;
            if (!lg) { dbg += std::string(" ") + lod + ":missing"; continue; }
            if (Il2CppObject* f = getComp[lg]((Il2CppObject*)mfC.GetMonoType())) {
                std::string before = mfGetMesh.IsValid() ? nameOf(mfGetMesh[f]()) : "?";
                Il2CppObject* want = CosTarget((strcmp(lod, "LOD1") == 0 && CosTarget(meshL1)) ? meshL1 : meshL0);
                mfSetMesh[f](want);
                std::string after = mfGetMesh.IsValid() ? nameOf(mfGetMesh[f]()) : "?";
                dbg += std::string(" ") + lod + ":" + before + "->" + after;
            } else dbg += std::string(" ") + lod + ":nofilter";
            if (Il2CppObject* r = getComp[lg]((Il2CppObject*)mrC.GetMonoType())) if (forceOff.IsValid()) forceOff[r](false);
        }
        static int dlog = 0;
        if (dlog++ < 10) DevlogPush("[devbadge] " + dbg);
        g_devBadges.push_back({Pin(b), Pin(an)});
        static int logged = 0;
        if (logged++ < 20) DevlogPush("[devbadge] built a trass badge on another player (" + std::to_string(g_devBadges.size()) + " total)");
    }
}

// ---------------------------------------------------------------------
//  live54 one-off class dump (once per install of this build): fields with
//  offsets and methods with RVAs / parameter types for the classes the next
//  features need (card art, name-tag badge, magnifying-glass menu, ice pillow
//  breaking). Names are obfuscated in 2.32; types, offsets and RVAs still let us
//  pick the right members. "[cdump]" devlog lines.
// ---------------------------------------------------------------------
static void LiveClassDump() {
    static const char* exact[] = {"TradingCardManager", "TradingCardItem", "TradingCardFrontImageStrategy",
        "TradingCardCachedFrontImageStrategy", "TradingCardVisualOnlyMaterialOwner", "TradingCardGeneratorUtility",
        "TradingCardRarityDisplay", "Nametag", "NametagManager", "PlayerNameTagSpawner", "RoleManager", "CosmeticsManager",
        "MagnifyingGlassItem", "Item", "MapObject", "MultiRenderMapObject", "TimeThresholdBreakableMapObject",
        "BombBreakableMapObject", "LaserBreakableMapObject", "ItemHealthHelper", "LocomotionSurface", "FireManagerZone",
        "ExplosionEffect", "BreakableBlockShatterEffect", "WorldAndRoomBiomeSettingLine", "XRItemInteractable",
        "AssignCosmeticUtility", "AssignCosmeticDirectUtility", "PresetRoleUtility", "AddRoleUtility", nullptr};
    static const char* subs[] = {"Moderator", "Biome", "TradingCard", "Nametag", "Breakable", "MapManager",
        "MapObjectManager", "Teleport", "PlayerManager", "AvatarCosmetic", "Customiz", "Imagination", "Reskin",
        "Inventory", "Badge", "Party", "Skelet", "Dance", nullptr};
    try {
        if (!g_il2cppBase) {
            Dl_info di;
            void* sym = BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_init");
            if (sym && dladdr(sym, &di)) g_il2cppBase = (uintptr_t)di.dli_fbase;
        }
        Image img("Assembly-CSharp.dll");
        if (!img.IsValid()) { DevlogPush("[cdump] no Assembly-CSharp"); return; }
        int n = 0;
        auto fmtType = [](const Il2CppType* t) { return t ? typeName(t) : std::string("?"); };
        for (auto& cls : img.GetClasses(false)) {
            auto* k = cls.GetClass();
            if (!k || !k->name) continue;
            std::string nm = k->name;
            bool want = false;
            for (int i = 0; exact[i] && !want; i++) want = nm == exact[i];
            for (int i = 0; subs[i] && !want; i++) want = nm.find(subs[i]) != std::string::npos;
            if (!want) continue;
            n++;
            std::string line = "[cdump] C " + nm + " : " + (k->parent && k->parent->name ? k->parent->name : "-") +
                               " size=" + std::to_string(k->instance_size) + " |";
            auto flush = [&](bool force) { if (force || line.size() > 1600) { DevlogPush(line); line = "[cdump] + " + nm + " |"; } };
            for (auto& f : cls.GetFields(false)) {
                auto* fi = f.GetInfo();
                if (!fi || !fi->type) continue;
                line += std::string(" F ") + (fi->type->attrs & 0x10 ? "s:" : "") + (fi->name ? fi->name : "?") + "@" +
                        std::to_string(fi->offset) + ":" + fmtType(fi->type) + ";";
                flush(false);
            }
            for (auto& m : cls.GetMethods(false)) {
                auto* mi = m.GetInfo();
                if (!mi) continue;
                uintptr_t rva = mi->methodPointer && g_il2cppBase ? (uintptr_t)mi->methodPointer - g_il2cppBase : 0;
                line += std::string(" M ") + (mi->name ? mi->name : "?") + "(";
                for (int p = 0; p < mi->parameters_count; p++)
                    line += (p ? "," : "") + (mi->parameters && mi->parameters[p] ? fmtType(mi->parameters[p]) : std::string("?"));
                line += ")" + fmtType(mi->return_type) + "@" + std::to_string(rva) + ";";
                flush(false);
            }
            flush(true);
        }
        DevlogPush("[cdump] done classes=" + std::to_string(n));
    } catch (...) { DevlogPush("[cdump] EXCEPTION"); }
}

static void RemoteTick() {
    double now = nowSeconds();
    static bool firstTick = true;
    if (firstTick) { firstTick = false; RLOG("Remote: first tick"); BigFixLastRoom(); }
    // feed poll (big: fetched by BgPollThread)
    {
        static bool bgStarted = false;
        if (!bgStarted) { bgStarted = true; std::thread(BgPollThread).detach(); }
        std::string body; bool fresh = false;
        { std::lock_guard<std::mutex> lk(g_bgPollMu); if (g_bgFeedNew) { body.swap(g_bgFeedBody); g_bgFeedNew = false; fresh = true; } }
        if (fresh) {
            try { RLOG("Remote: feed ok (%d bytes)", (int)body.size()); ParseFeed(body); }
            catch (...) { RLOG("Remote: feed parse EXCEPTION"); }
        }
        std::string bbody; bool bfresh = false;
        { std::lock_guard<std::mutex> lk(g_bgPollMu); if (g_bgBuildersNew) { bbody.swap(g_bgBuildersBody); g_bgBuildersNew = false; bfresh = true; } }
        if (bfresh) {
            std::set<std::string> set{"o_OWNER0000000001"};
            size_t p = 0;
            while (p < bbody.size()) {
                size_t nl = bbody.find('\n', p);
                std::string line = bbody.substr(p, nl == std::string::npos ? std::string::npos : nl - p);
                p = nl == std::string::npos ? bbody.size() : nl + 1;
                while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
                if (line.rfind("o_", 0) == 0) set.insert(line);
            }
            std::lock_guard<std::mutex> lk(g_buildersMu); g_builders.swap(set);
        }
    }
    // disk downloads -> decode on a worker thread
    for (size_t i = 0; i < g_audioReqs.size();) {
        auto& p = g_audioReqs[i];
        if (!ReqDone(p)) { i++; continue; }
        if (ReqOk(p.req)) {
            std::string b64 = ReqText(p.req);
            std::string clip = p.clip, ver = p.ver;
            std::thread([b64, clip, ver]() {
                DecodedDisk d; d.clip = clip; d.ver = ver;
                if (DecodeToMono(B64Decode(b64), d.pcm, d.rate)) {
                    std::lock_guard<std::mutex> lk(g_decMtx); g_decoded.push_back(std::move(d));
                } else BNM_FileCheckpointF("Remote: disk %s decode failed", clip.c_str());
            }).detach();
        } else RLOG("Remote: disk %s download failed", p.clip.c_str());
        ReqDispose(p);
        g_audioReqs.erase(g_audioReqs.begin() + i);
    }
    {
        std::vector<DecodedDisk> ready;
        { std::lock_guard<std::mutex> lk(g_decMtx); ready.swap(g_decoded); }
        for (auto& d : ready) { try { BuildClipOnMain(d); } catch (...) { RLOG("Remote: build clip EXCEPTION"); } }
    }
    auto partMs = [](const timespec& a) {
        timespec b; clock_gettime(CLOCK_MONOTONIC, &b);
        return (long)((b.tv_sec - a.tv_sec) * 1000 + (b.tv_nsec - a.tv_nsec) / 1000000);
    };
    static int partLogN = 0;
    timespec t0; clock_gettime(CLOCK_MONOTONIC, &t0);
    try { ScreensTick(now); } catch (...) { RLOG("Remote: screens tick EXCEPTION"); }
    long msScr = partMs(t0); clock_gettime(CLOCK_MONOTONIC, &t0);
    try { VideoTick(now); } catch (...) { RLOG("Remote: video tick EXCEPTION"); }
    try { TvTick(now); } catch (...) { RLOG("TV: tick EXCEPTION"); }
    long msVid = partMs(t0); clock_gettime(CLOCK_MONOTONIC, &t0);
    try { SyncFeedFriendsIntoGame(); } catch (...) {}
    long msFr = partMs(t0);
    if ((msScr >= 6 || msVid >= 6 || msFr >= 6) && partLogN++ < 150)
        DevlogPush("[slowtick] parts: screens=" + std::to_string(msScr) + " video=" + std::to_string(msVid) + " friends=" + std::to_string(msFr) + " ms");
    // announcements
    Il2CppObject* go = g_annGO && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_annGO) : nullptr;
    if (go && Alive(go)) {
        if (now < g_annUntil) FollowHead(go);
        else {
            try { Method<void>(Class("UnityEngine", "Object").GetMethod("Destroy", 1))(go); } catch (...) {}
            Unpin(g_annGO); g_annGO = nullptr;
        }
    } else if (g_annGO) { Unpin(g_annGO); g_annGO = nullptr; }
    if (!g_annGO && !g_annQueue.empty()) {
        Ann a = g_annQueue.front(); g_annQueue.erase(g_annQueue.begin());
        ShowAnnouncement(a);
    }
}

static std::atomic<unsigned long long> g_frames{0};   // main-thread frames (devlog heartbeat / hang watch)
// big APK lag hunt: frame-time spikes between heartbeats (pauses > 2 s ignored)
static std::atomic<long> g_spikeMaxMs{0}, g_slow25{0}, g_slow50{0}, g_slow100{0};
static std::atomic<int> g_spikeLogN{0};
static void DevlogPush(const std::string& line);
static void FrameSpikeTick() {
    static long long lastNs = 0;
    timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    long long now = ts.tv_sec * 1000000000LL + ts.tv_nsec;
    if (lastNs) {
        long ms = (long)((now - lastNs) / 1000000);
        if (ms < 2000) {
            long m = g_spikeMaxMs.load();
            while (ms > m && !g_spikeMaxMs.compare_exchange_weak(m, ms)) {}
            if (ms > 25) g_slow25++;
            if (ms > 50) g_slow50++;
            if (ms > 100) {
                g_slow100++;
                if (g_spikeLogN.fetch_add(1) < 400) DevlogPush("[spike] frame " + std::to_string(ms) + " ms");
            }
        }
    }
    lastNs = now;
}
static void (*orig_TickUpdate)(void*, void*) = nullptr;
#ifdef BNM_LIVE_BASE
static void CreativeGuardTick(double now);   // live: runs from this tick (one hook on AccountOnlineManager.Update)
#endif
// live88 (owner 2026-10-31): voice auto-retry. Our voice app is free tier (20 CCU).
// When voice is full, Photon Voice's VoiceFollowClient sets AutoConnectAndJoin=false
// on the failed Authenticate ("authentication failed", MaxCcuReached) and NEVER
// retries, so the player stays muted until they restart. We note exactly that case
// (OnOperationResponseReceived flipping the flag true->false) and, every 30 s, turn
// it back on and call FollowLeader() so voice reconnects once a slot is free. A
// player who disconnected voice on purpose (manualDisconnect) is left alone.
static void (*orig_VFC_OnOpResp)(void*, void*, void*) = nullptr;
static Field<bool> g_vfcAuto, g_vfcErr, g_vfcManual;
static MethodBase g_vfcFollow;
static std::vector<void*> g_voiceRetry;      // il2cpp strong gchandles
static std::mutex g_voiceMx;
static std::atomic<int> g_voiceRetryLog{0};
static bool VfcGet(Field<bool>& f, void* o) { f.SetInstance((Il2CppObject*)o); return f(); }
static void hook_VFC_OnOpResp(void* self, void* resp, void* mi) {
    if (orig_VFC_OnOpResp) orig_VFC_OnOpResp(self, resp, mi);
    // live90: Photon flips AutoConnectAndJoin off in OnVoiceStateChanged (after the
    // failed auth disconnects), NOT here -- so just REMEMBER every voice client the
    // first time it gets an op response; VoiceRetryTick checks the flag itself.
    try {
        if (!self || !p_gchandle_new || !p_gchandle_target) return;
        std::lock_guard<std::mutex> lk(g_voiceMx);
        for (void* h : g_voiceRetry) if (p_gchandle_target(h) == self) return;
        if (g_voiceRetry.size() < 8) {
            g_voiceRetry.push_back(p_gchandle_new(self, false));
            if (g_voiceRetryLog.fetch_add(1) < 10) DevlogPush("[voice] watching voice client for auto-retry");
        }
    } catch (...) {}
}
static void VoiceRetryTick(double now) {
    static double last = 0;
    if (now - last < 30) return;
    last = now;
    std::lock_guard<std::mutex> lk(g_voiceMx);
    for (size_t i = 0; i < g_voiceRetry.size();) {
        void* h = g_voiceRetry[i];
        void* o = (p_gchandle_target && h) ? p_gchandle_target(h) : nullptr;
        if (!o || !*(void**)((uint8_t*)o + 0x10)) {           // destroyed -> forget it
            if (h && p_gchandle_free) p_gchandle_free(h);
            g_voiceRetry.erase(g_voiceRetry.begin() + i);
            continue;
        }
        try {
            // switched off by Photon (voice full / auth failed), not by the player
            if (g_vfcAuto.IsValid() && !VfcGet(g_vfcAuto, o) &&
                !(g_vfcManual.IsValid() && VfcGet(g_vfcManual, o))) {
                g_vfcAuto.SetInstance((Il2CppObject*)o); g_vfcAuto = true;
                if (g_vfcErr.IsValid()) { g_vfcErr.SetInstance((Il2CppObject*)o); g_vfcErr = false; }
                if (g_vfcFollow.IsValid()) { Method<void> fl(g_vfcFollow); fl[(Il2CppObject*)o](); }
                if (g_voiceRetryLog.fetch_add(1) < 60) DevlogPush("[voice] voice was off after a failed connect -> retrying");
            }
        } catch (...) {}
        i++;
    }
}
static void InstallVoiceRetry() {
    try {
        Class vfc("Photon.Voice", "VoiceFollowClient");
        if (!vfc.IsValid()) { BNM_FileCheckpoint("[voice] VoiceFollowClient NOT FOUND"); return; }
        g_vfcAuto = vfc.GetField("AutoConnectAndJoin");
        g_vfcErr = vfc.GetField("errAuthOrJoin");
        g_vfcManual = vfc.GetField("manualDisconnect");
        g_vfcFollow = vfc.GetMethod("FollowLeader", 0);
        MethodBase op = vfc.GetMethod("OnOperationResponseReceived", 1);
        if (op.IsValid()) BasicHook(op, hook_VFC_OnOpResp, orig_VFC_OnOpResp);
        BNM_FileCheckpointF("[voice] retry hook=%d auto=%d err=%d manual=%d follow=%d", op.IsValid(),
                            g_vfcAuto.IsValid(), g_vfcErr.IsValid(), g_vfcManual.IsValid(), g_vfcFollow.IsValid());
    } catch (...) { BNM_FileCheckpoint("[voice] retry install EXCEPTION"); }
}

static void hook_TickUpdate(void* self, void* mi) {
    g_frames.fetch_add(1, std::memory_order_relaxed);
    FrameSpikeTick();
    if (orig_TickUpdate) orig_TickUpdate(self, mi);
    timespec a, b; clock_gettime(CLOCK_MONOTONIC, &a);
    try { RemoteTick(); } catch (...) {}
#ifdef BNM_LIVE_BASE
    try { CreativeGuardTick(nowSeconds()); } catch (...) {}
    try { CosmeticTick(nowSeconds()); } catch (...) {}
    // CardArtTick removed: dev trading cards dropped by the owner 2026-10-18
    try { FrostBadgeTick(nowSeconds()); } catch (...) {}
    try { SkeletonTick(nowSeconds()); } catch (...) {}
    try { VoiceRetryTick(nowSeconds()); } catch (...) {}
    // DevBadgeTick (live69-71 stand-in badge) retired 2026-10-18: the real block was the
    // missing role-mask field in the Photon UserId (server now sends "$31"), so the
    // game builds the developer badge + name icon itself.
    // try { DevBadgeTick(nowSeconds()); } catch (...) {}
#endif
    clock_gettime(CLOCK_MONOTONIC, &b);
    long ms = (long)((b.tv_sec - a.tv_sec) * 1000 + (b.tv_nsec - a.tv_nsec) / 1000000);
    static int slowN = 0;
    if (ms >= 8 && slowN++ < 200) DevlogPush("[slowtick] Blaze remote tick took " + std::to_string(ms) + " ms");
}

// AudioSource.Play(): if the clip about to play is a remotely replaced disk
// track, play the downloaded clip instead.
static void (*orig_AS_Play)(void*, void*) = nullptr;
static void hook_AS_Play(void* self, void* mi) {
    try {
        bool any; { std::lock_guard<std::mutex> lk(g_diskMtx); any = !g_diskClipHandle.empty(); }
        if (any && self) {
            Class as("UnityEngine", "AudioSource");
            Il2CppObject* clip = Method<Il2CppObject*>(as.GetMethod("get_clip", 0))[(Il2CppObject*)self]();
            if (clip) {
                std::string name;
                auto it = g_clipNameCache.find(clip);
                if (it != g_clipNameCache.end()) name = it->second;
                else {
                    name = MonoStr(Method<Il2CppObject*>(Class("UnityEngine", "Object").GetMethod("get_name", 0))[clip]());
                    g_clipNameCache[clip] = name;
                }
                void* h = nullptr;
                { std::lock_guard<std::mutex> lk(g_diskMtx); auto d = g_diskClipHandle.find(name); if (d != g_diskClipHandle.end()) h = d->second; }
                Il2CppObject* repl = h && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(h) : nullptr;
                if (repl && repl != clip) Method<void>(as.GetMethod("set_clip", 1))[(Il2CppObject*)self](repl);
            }
        }
    } catch (...) {}
    if (orig_AS_Play) orig_AS_Play(self, mi);
}

// live35: lean picture-material hook for the live base. The full hook above was
// skipped on live (unverified scene-load crash suspect: per-call Class lookups,
// gameObject-name fallback, runtime-built materials). This one only reads the
// key through a cached method, only acts on the two picture blocks, and only
// uses materials shipped in the data under a Resources path (live35 data).
static const std::map<std::string, std::string> kLivePictureKeys = {
    {"1x1_stuffedCucumberCat", "blazecat/mat_cucumbercat"},
    {"3x2x1_blazeScreenRole", "blazescreen/mat_blaze"}};
static void (*orig_MO_SSM_Live)(void*, void*, void*) = nullptr;
static void hook_MO_SSM_Live(void* self, void* arr, void* mi) {
    void* pass = arr;
    try {
        static Method<Il2CppObject*> getKey(Class("", "MapObject", Image("Assembly-CSharp.dll")).GetMethod("get_key", 0));
        static void* matClass = Class("UnityEngine", "Material").GetClass();
        if (self && arr && p_array_new && matClass && getKey.IsValid()) {
            Il2CppObject* ks = getKey[(Il2CppObject*)self]();
            if (ks) {
                std::string key = MonoStr(ks);
                auto pk = kLivePictureKeys.find(key);
                if (pk != kLivePictureKeys.end()) {
                    uintptr_t n = *(uintptr_t*)((uint8_t*)arr + 0x18);     // Il2CppArray max_length
                    Method<Il2CppObject*> load(FindOverload(Class("UnityEngine", "Resources"), "Load", {"System.String"}));
                    Il2CppObject* m = nullptr;
                    auto f = g_pictureMats.find(pk->second);
                    if (f != g_pictureMats.end() && p_gchandle_target) {
                        m = (Il2CppObject*)p_gchandle_target(f->second);
                        if (!m || !Alive(m)) { m = nullptr; Unpin(f->second); g_pictureMats.erase(f); }
                    }
                    if (!m && load.IsValid()) {
                        m = load(CreateMonoString(pk->second.c_str()));
                        if (m) g_pictureMats[pk->second] = Pin(m);
                    }
                    if (n >= 2 && n < 16 && m) {
                        void* copy = p_array_new(matClass, n);
                        if (copy) {
                            memcpy((uint8_t*)copy + 0x20, (uint8_t*)arr + 0x20, n * sizeof(void*));
                            ((void**)((uint8_t*)copy + 0x20))[1] = m;
                            pass = copy;
                        }
                    }
                    static int logged = 0;
                    if (logged++ < 8) BNM_FileCheckpointF("[pic] %s n=%d mat=%d", key.c_str(), (int)n, m != nullptr);
                }
            }
        }
    } catch (...) {}
    if (orig_MO_SSM_Live) orig_MO_SSM_Live(self, pass, mi);
}

// live35: drop the game's "Suspicious event (PlayerData modified)" staff popup
// (owner 2026-10-17: "remove that popup"). Every player holds staff roles here, so
// every client got it. ImaginationPromptManager.DisplayPrompt(prompt) shows one
// queued prompt; when the prompt's text starts with "Suspicious event" we skip it
// and call OnDisplayReadyForNewPrompt() so the queue moves on. The prompt's string
// fields are found through the il2cpp field API (type STRING), never by guessing.
static void (*orig_DisplayPrompt)(void*, void*, void*) = nullptr;
static bool PromptIsSuspicious(void* prompt) {
    typedef void* (*cls_t)(void*); typedef void* (*fields_t)(void*, void**);
    typedef const void* (*ftype_t)(void*); typedef int (*ttype_t)(const void*); typedef size_t (*off_t_)(void*);
    static auto objClass = (cls_t)BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_object_get_class");
    static auto getFields = (fields_t)BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_class_get_fields");
    static auto fieldType = (ftype_t)BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_field_get_type");
    static auto typeType = (ttype_t)BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_type_get_type");
    static auto fieldOff = (off_t_)BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_field_get_offset");
    if (!prompt || !objClass || !getFields || !fieldType || !typeType || !fieldOff) return false;
    void* k = objClass(prompt); if (!k) return false;
    void* it = nullptr; int guard = 0;
    while (void* f = getFields(k, &it)) {
        if (++guard > 64) break;
        const void* t = fieldType(f);
        if (!t || typeType(t) != 0x0e) continue;                 // IL2CPP_TYPE_STRING
        size_t off = fieldOff(f);
        if (off < 0x10 || off > 0x400) continue;                 // static / odd fields
        auto* str = *(BNM::Structures::Mono::String**)((uint8_t*)prompt + off);
        if (!str) continue;
        std::string v = str->str();
        if (v.rfind("Suspicious event", 0) == 0) return true;
        if (v.rfind("You are on a LAB version", 0) == 0) return true;   // live50: 2.32 lab-build nag
    }
    return false;
}
static void hook_DisplayPrompt(void* self, void* prompt, void* mi) {
    bool skip = false;
    try { skip = PromptIsSuspicious(prompt); } catch (...) { skip = false; }
    if (skip) {
        static int logged = 0;
        if (logged++ < 5) BNM_FileCheckpoint("[prompt] suspicious-event popup dropped");
        try {
            static Method<void> ready(Class("", "ImaginationPromptManager", Image("Assembly-CSharp.dll")).GetMethod("OnDisplayReadyForNewPrompt", 0));
            if (ready.IsValid() && self) ready[(Il2CppObject*)self]();
        } catch (...) {}
        return;
    }
    if (orig_DisplayPrompt) orig_DisplayPrompt(self, prompt, mi);
}
// live42: Int32.Parse(null) guard. Some game code (2.31.4 + 2.32, nested class with a
// string argument) parses a value our server leaves empty; the ArgumentNullException
// surfaced to every player as an error popup (~450 times on 2026-10-17). A null
// string now parses as 0 instead of throwing; every real number passes straight through.
static int32_t (*orig_Int32Parse)(void*, void*) = nullptr;
static int32_t hook_Int32Parse(void* s, void* mi) {
    if (!s) {
        static std::atomic<int> n{0};
        if (n.fetch_add(1) < 3) BNM_FileCheckpoint("[parse] Int32.Parse(null) -> 0 (was an error popup)");
        return 0;
    }
    return orig_Int32Parse(s, mi);
}
static void InstallInt32ParseNullGuard() {
    try {
        Class i32("System", "Int32", Image("mscorlib.dll"));
        MethodBase p = i32.IsValid() ? FindOverload(i32, "Parse", {"System.String"}) : MethodBase();
        if (p.IsValid()) { BasicHook(p, hook_Int32Parse, orig_Int32Parse); BNM_FileCheckpoint("[parse] Int32.Parse(string) null guard hooked"); }
        else BNM_FileCheckpoint("[parse] Int32.Parse(string) NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("[parse] EXCEPTION"); }
}

// ---- live52 DIAG: what does CustomizedItemDatabase receive? (reskins never show) ----
static std::string CiStr(void* o) {
    if (!o) return "null";
    auto* k = *(IL2CPP::Il2CppClass**)o;
    if (!k || !k->name) return "?";
    if (strcmp(k->name, "String") == 0) return ((BNM::Structures::Mono::String*)o)->str().substr(0, 80);
    return std::string("<") + k->name + ">";
}
static std::string CiList(void* lst) {
    if (!lst) return "null";
    void* items = *(void**)((uint8_t*)lst + 0x10);
    int size = *(int*)((uint8_t*)lst + 0x18);
    std::string out = "n=" + std::to_string(size) + " [";
    for (int i = 0; i < size && i < 4 && items; i++) {
        void* e = ((void**)((uint8_t*)items + 0x20))[i];
        out += CiStr(e) + (i + 1 < size && i < 3 ? ", " : "");
    }
    return out + "]";
}
static std::atomic<int> g_ciN[80];
static int g_ciIdx[80];   // template slot -> real method position (for the log)
static bool CiCap(int idx) { return idx >= 0 && idx < 80 && g_ciN[idx].fetch_add(1) < 6; }
template <int N> struct CiL { static void (*orig)(void*, void*, void*);
    static void hook(void* s, void* a, void* mi) { if (CiCap(N)) DevlogPush("[ci] m" + std::to_string(g_ciIdx[N]) + "(List " + CiList(a) + ")"); orig(s, a, mi); } };
template <int N> void (*CiL<N>::orig)(void*, void*, void*) = nullptr;
template <int N> struct CiSS { static void (*orig)(void*, void*, void*, void*);
    static void hook(void* s, void* a, void* b, void* mi) { if (CiCap(N)) DevlogPush("[ci] m" + std::to_string(g_ciIdx[N]) + "(" + CiStr(a) + ", " + CiStr(b) + ")"); orig(s, a, b, mi); } };
template <int N> void (*CiSS<N>::orig)(void*, void*, void*, void*) = nullptr;
template <int N> struct CiS3 { static void (*orig)(void*, void*, void*, void*, void*);
    static void hook(void* s, void* a, void* b, void* c, void* mi) { if (CiCap(N)) DevlogPush("[ci] m" + std::to_string(g_ciIdx[N]) + "(" + CiStr(a) + ", ...)"); orig(s, a, b, c, mi); } };
template <int N> void (*CiS3<N>::orig)(void*, void*, void*, void*, void*) = nullptr;
#define CI_HOOK_L(N)  if (slot == N) { BasicHook(mb, CiL<N>::hook, CiL<N>::orig); }
#define CI_HOOK_SS(N) if (slot == N) { BasicHook(mb, CiSS<N>::hook, CiSS<N>::orig); }
#define CI_HOOK_S3(N) if (slot == N) { BasicHook(mb, CiS3<N>::hook, CiS3<N>::orig); }
static void InstallCustomizedItemDiag() {
    // live53: hook EVERY instance method of these shapes (slots 0-19 list, 20-39
    // string+string, 40-59 string-first 3 args); the log names the real position.
    try {
        Class c("", "CustomizedItemDatabase", Image("Assembly-CSharp.dll"));
        if (!c.IsValid()) { DevlogPush("[ci] class NOT FOUND"); return; }
        int idx = 0, nl = 0, nss = 0, ns3 = 0;
        for (auto& mb : c.GetMethods(false)) {
            auto* mi = mb.GetInfo();
            int n = mi ? mi->parameters_count : -1;
            std::string p0 = n > 0 ? typeName(mi->parameters[0]) : "", p1 = n > 1 ? typeName(mi->parameters[1]) : "";
            bool isStatic = mi && (mi->flags & 0x10);
            int slot = -1;
            if (!isStatic && n == 1 && p0.find("List") != std::string::npos && nl < 20) slot = nl++;
            else if (!isStatic && n == 2 && p0 == "System.String" && p1 == "System.String" && nss < 20) slot = 20 + nss++;
            else if (!isStatic && n == 3 && p0 == "System.String" && ns3 < 20) slot = 40 + ns3++;
            if (slot >= 0) {
                g_ciIdx[slot] = idx;
                CI_HOOK_L(0) CI_HOOK_L(1) CI_HOOK_L(2) CI_HOOK_L(3) CI_HOOK_L(4) CI_HOOK_L(5) CI_HOOK_L(6) CI_HOOK_L(7)
                CI_HOOK_L(8) CI_HOOK_L(9) CI_HOOK_L(10) CI_HOOK_L(11) CI_HOOK_L(12) CI_HOOK_L(13) CI_HOOK_L(14) CI_HOOK_L(15)
                CI_HOOK_L(16) CI_HOOK_L(17) CI_HOOK_L(18) CI_HOOK_L(19)
                CI_HOOK_SS(20) CI_HOOK_SS(21) CI_HOOK_SS(22) CI_HOOK_SS(23) CI_HOOK_SS(24) CI_HOOK_SS(25) CI_HOOK_SS(26) CI_HOOK_SS(27)
                CI_HOOK_SS(28) CI_HOOK_SS(29) CI_HOOK_SS(30) CI_HOOK_SS(31) CI_HOOK_SS(32) CI_HOOK_SS(33) CI_HOOK_SS(34) CI_HOOK_SS(35)
                CI_HOOK_SS(36) CI_HOOK_SS(37) CI_HOOK_SS(38) CI_HOOK_SS(39)
                CI_HOOK_S3(40) CI_HOOK_S3(41) CI_HOOK_S3(42) CI_HOOK_S3(43) CI_HOOK_S3(44) CI_HOOK_S3(45) CI_HOOK_S3(46) CI_HOOK_S3(47)
                CI_HOOK_S3(48) CI_HOOK_S3(49) CI_HOOK_S3(50) CI_HOOK_S3(51) CI_HOOK_S3(52) CI_HOOK_S3(53) CI_HOOK_S3(54) CI_HOOK_S3(55)
                CI_HOOK_S3(56) CI_HOOK_S3(57) CI_HOOK_S3(58) CI_HOOK_S3(59)
            }
            idx++;
        }
        DevlogPush("[ci] hooked list=" + std::to_string(nl) + " str2=" + std::to_string(nss) + " str3=" + std::to_string(ns3) + " of " + std::to_string(idx));
    } catch (...) { DevlogPush("[ci] EXCEPTION"); }
}

static void InstallSuspiciousPopupOff() {
    try {
        Class ipm("", "ImaginationPromptManager", Image("Assembly-CSharp.dll"));
        MethodBase dp = ipm.IsValid() ? ipm.GetMethod("DisplayPrompt", 1) : MethodBase();
        if (dp.IsValid()) { BasicHook(dp, hook_DisplayPrompt, orig_DisplayPrompt); BNM_FileCheckpoint("[prompt] DisplayPrompt hooked (suspicious popup off)"); }
        else BNM_FileCheckpoint("[prompt] DisplayPrompt NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("[prompt] EXCEPTION"); }
}

static void InstallRemoteClient() {
    // Live 2.31.4 base (tester build): remote client ENABLED. The news-feed
    // tick, video player, friend and disk-audio hooks run; the
    // MapObject.SetSharedMaterials hook (picture-screens) is the #1 scene-load
    // crash suspect on the live binary, so it is skipped on live until verified
    // (the video player does NOT need it). Crashes are logged by the crash
    // recorder for iteration.
    try {
        p_gchandle_new = (void*(*)(void*, bool))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_gchandle_new");
        p_gchandle_target = (void*(*)(void*))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_gchandle_get_target");
        p_gchandle_free = (void(*)(void*))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_gchandle_free");
        p_object_new = (void*(*)(void*))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_object_new");
        p_array_new = (void*(*)(void*, uintptr_t))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_array_new");
        p_value_box = (void*(*)(void*, void*))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_value_box");
        Class aom("", "AccountOnlineManager", Image("Assembly-CSharp.dll"));
        MethodBase up = aom.IsValid() ? aom.GetMethod("Update", 0) : MethodBase();
        if (up.IsValid()) { BasicHook(up, hook_TickUpdate, orig_TickUpdate); BNM_FileCheckpoint("Remote: tick hooked (AccountOnlineManager.Update)"); }
        else BNM_FileCheckpoint("Remote: AccountOnlineManager.Update NOT FOUND");
        Class fmgr("", "FriendManager", Image("Assembly-CSharp.dll"));
        MethodBase isf = fmgr.IsValid() ? FindOverload(fmgr, "IsFriend", {"System.String"}) : MethodBase();
        if (isf.IsValid()) { BasicHook(isf, hook_FM_IsFriend, orig_FM_IsFriend); BNM_FileCheckpoint("Friends: IsFriend hooked"); }
        else BNM_FileCheckpoint("Friends: IsFriend NOT FOUND");
#ifndef BNM_LIVE_BASE
        Class mapObj("", "MapObject", Image("Assembly-CSharp.dll"));
        MethodBase ssm = mapObj.IsValid() ? FindOverload(mapObj, "SetSharedMaterials", {"UnityEngine.Material[]"}) : MethodBase();
        if (ssm.IsValid()) { BasicHook(ssm, hook_MO_SetSharedMaterials, orig_MO_SetSharedMaterials); BNM_FileCheckpoint("Remote: MapObject.SetSharedMaterials hooked"); }
        else BNM_FileCheckpoint("Remote: MapObject.SetSharedMaterials NOT FOUND");
#else
        Class mapObjL("", "MapObject", Image("Assembly-CSharp.dll"));
        MethodBase ssmL = mapObjL.IsValid() ? FindOverload(mapObjL, "SetSharedMaterials", {"UnityEngine.Material[]"}) : MethodBase();
        if (ssmL.IsValid()) { BasicHook(ssmL, hook_MO_SSM_Live, orig_MO_SSM_Live); BNM_FileCheckpoint("Remote: lean picture hook on SetSharedMaterials (live35)"); }
        else BNM_FileCheckpoint("Remote: MapObject.SetSharedMaterials NOT FOUND");
#endif
        MethodBase play = Class("UnityEngine", "AudioSource").GetMethod("Play", 0);
        if (play.IsValid()) { BasicHook(play, hook_AS_Play, orig_AS_Play); BNM_FileCheckpoint("Remote: AudioSource.Play hooked"); }
        else BNM_FileCheckpoint("Remote: AudioSource.Play NOT FOUND");
        BNM_FileCheckpointF("Remote: gchandle api %d/%d/%d", p_gchandle_new != nullptr, p_gchandle_target != nullptr, p_gchandle_free != nullptr);
    } catch (...) { BNM_FileCheckpoint("Remote: EXCEPTION installing"); }
}

// =====================================================================
//  Item dispenser accepts EVERYTHING. The dispenser block
//  (CustomWirableDispenserMapObject, prefab "itemDispenser") takes items
//  through child ItemDespawnZones (ItemInput / PinInput / IngredientInput /
//  MobSpawnEggInput); the zone filter refuses many items before the
//  dispenser ever sees them. For those zones only, override the filter.
//  Other despawn zones (trash, cauldrons, ...) keep their normal rules.
// =====================================================================
static std::mutex g_zoneMtx;
static std::map<void*, bool> g_isDispenserZone;
static bool IsDispenserInputZone(void* zone) {
    {
        std::lock_guard<std::mutex> lk(g_zoneMtx);
        auto it = g_isDispenserZone.find(zone);
        if (it != g_isDispenserZone.end()) return it->second;
    }
    bool yes = false;
    try {
        Method<Il2CppObject*> gname(Class("UnityEngine", "Object").GetMethod("get_name", 0));
        Il2CppObject* go = Method<Il2CppObject*>(Class("UnityEngine", "Component").GetMethod("get_gameObject", 0))[(Il2CppObject*)zone]();
        std::string zn = go ? MonoStr(gname[go]()) : "";
        if (zn == "ItemInput" || zn == "PinInput" || zn == "IngredientInput" || zn == "MobSpawnEggInput") {
            Class tr("UnityEngine", "Transform");
            Il2CppObject* t = Method<Il2CppObject*>(Class("UnityEngine", "Component").GetMethod("get_transform", 0))[(Il2CppObject*)zone]();
            for (int depth = 0; t && depth < 3 && !yes; depth++) {
                t = Method<Il2CppObject*>(tr.GetMethod("get_parent", 0))[t]();
                if (!t) break;
                Il2CppObject* pgo = Method<Il2CppObject*>(Class("UnityEngine", "Component").GetMethod("get_gameObject", 0))[t]();
                if (pgo && MonoStr(gname[pgo]()).find("itemDispenser") != std::string::npos) yes = true;
            }
        }
    } catch (...) {}
    std::lock_guard<std::mutex> lk(g_zoneMtx);
    g_isDispenserZone[zone] = yes;
    return yes;
}
static std::atomic<int> g_dispLogN{0};
static uint64_t (*orig_ZoneCanAccept)(void*, void*, void*) = nullptr;
static uint64_t hook_ZoneCanAccept(void* self, void* item, void* mi) {
    uint64_t r = orig_ZoneCanAccept ? orig_ZoneCanAccept(self, item, mi) : 0;
    if ((r & 1) == 0 && self && IsDispenserInputZone(self)) {
        if (g_dispLogN.fetch_add(1) < 50) {
            std::string in;
            try { in = item ? MonoStr(Method<Il2CppObject*>(Class("UnityEngine", "Object").GetMethod("get_name", 0))[(Il2CppObject*)item]()) : "(null)"; } catch (...) {}
            BNM_FileCheckpointF("Dispenser: allowing refused item '%s'", in.c_str());
        }
        return 1;
    }
    return r;
}
static uint64_t (*orig_ZoneIgnore)(void*, void*, void*) = nullptr;
static uint64_t hook_ZoneIgnore(void* self, void* item, void* mi) {
    uint64_t r = orig_ZoneIgnore ? orig_ZoneIgnore(self, item, mi) : 0;
    if ((r & 1) && self && IsDispenserInputZone(self)) return 0;
    return r;
}
static uint64_t (*orig_ZoneAllowsLoaded)(void*, void*) = nullptr;
static uint64_t hook_ZoneAllowsLoaded(void* self, void* mi) {
    uint64_t r = orig_ZoneAllowsLoaded ? orig_ZoneAllowsLoaded(self, mi) : 0;
    if ((r & 1) == 0 && self && IsDispenserInputZone(self)) return 1;
    return r;
}
// The zone filter was not the real gate: the dispenser's item handler calls
// static CustomWirableDispenserMapObject.TryProcessInputItem(item, inputType,
// out key, out internalState[], out errorMessage, out bool) and shows
// "This item can't be used in a dispenser" when it returns false. Force it to
// accept every item, storing the item's own key (Item+0x58 in 2.4) so the dispenser
// can spawn it again.
static std::atomic<int> g_tpiLogN{0};
static bool (*orig_TryProcessInputItem)(void*, void*, void**, void**, void**, bool*, void*) = nullptr;
static bool hook_TryProcessInputItem(void* item, void* inType, void** keyOut, void** stateOut,
                                     void** errOut, bool* errFlag, void* mi) {
    bool ok = orig_TryProcessInputItem ? orig_TryProcessInputItem(item, inType, keyOut, stateOut, errOut, errFlag, mi) : false;
    if (ok || !item || !keyOut || !stateOut) return ok;
    std::string key, err;
    void* k = nullptr;
    try {
        // 2.4: the item key is the first string field (+0x58); +0x60 (1.31's
        // offset) is another object here. Only read it if it IS a string.
        k = *(void**)((uint8_t*)item + 0x58);
        static Il2CppClass* strK = Class("System", "String").GetClass();
        if (k && ((Il2CppObject*)k)->klass == strK) key = MonoStr(k);
        else k = nullptr;
        err = errOut ? MonoStr(*errOut) : "";
    } catch (...) {}
    if (g_tpiLogN.fetch_add(1) < 40)
        BNM_FileCheckpointF("Dispenser: game refused '%s' (type=%s err='%s' flag=%d) -> %s",
                            key.c_str(), MonoStr(inType).c_str(), err.c_str(), errFlag ? (int)*errFlag : -1,
                            key.empty() ? "kept refused (no key)" : "accepting");
    if (key.empty()) return false;
    *keyOut = k;
    if (!*stateOut && p_array_new) *stateOut = p_array_new(Class("System", "String").GetClass(), 0);
    if (errOut) *errOut = nullptr;
    if (errFlag) *errFlag = false;
    return true;
}
// IsItemAllowed(itemKey, itemParams[], ?, bool, inputType, out err, out key,
// out state[]) is the core check behind BOTH inserting an item
// (TryProcessInputItem) and validating a dispenser's saved contents when a map
// loads (ValidateInternalState) -- so forcing only the insert path let the
// items in, but the map load threw them away again. Accept here, keeping the
// item's own key + params (params carry e.g. which block / colour it is).
static std::atomic<int> g_iiaLogN{0};
// 2.4: arg 3 is an object (class ref), not an int as in 1.31 - an int here cut
// the pointer to 32 bits when forwarding to the original (crash on dispenser use).
static bool (*orig_IsItemAllowed)(void*, void*, void*, bool, void*, void**, void**, void**, void*) = nullptr;
static bool hook_IsItemAllowed(void* key, void* params, void* e, bool b, void* inType,
                               void** errOut, void** keyOut, void** stateOut, void* mi) {
    bool ok = orig_IsItemAllowed ? orig_IsItemAllowed(key, params, e, b, inType, errOut, keyOut, stateOut, mi) : false;
    if (ok || !key || !keyOut || !stateOut) return ok;
    if (MonoStr(key).empty()) return false;
    if (g_iiaLogN.fetch_add(1) < 30)
        BNM_FileCheckpointF("Dispenser: IsItemAllowed refused '%s' (err='%s') -> accepting",
                            MonoStr(key).c_str(), errOut ? MonoStr(*errOut).c_str() : "");
    *keyOut = key;
    *stateOut = params ? params : (p_array_new ? p_array_new(Class("System", "String").GetClass(), 0) : nullptr);
    if (errOut) *errOut = nullptr;
    return true;
}
// ---- BLAZE role blocks: MapObject.requiredRoleKey = "blaze" -------------
// MapObject.CanUseMapObject(roles) checks requiredRoleKey against a static
// NetworkManager list that this server never fills (it's empty for everyone),
// so even blaze holders were refused. For "blaze" blocks, also accept when the
// local account's own roleKeys (AccountData, from the server login) contain
// "blaze". The server only sends that role to people who have it.
static double g_blazeRoleCheckedAt = -100;
static bool g_hasBlazeRole = false;
static bool AccountHasRoleNow(const char* role) {
    bool found = false;
    try {
        Class am("", "AccountManager", Image("Assembly-CSharp.dll"));
        Il2CppObject* ad = Method<Il2CppObject*>(am.GetMethod("get_accountData", 0))();
        if (ad) {
            Il2CppClass* strK = Class("System", "String").GetClass();
            for (Class c(ad->klass); c.IsValid() && c.GetClass() && !found; c = c.GetParent()) {
                for (auto& fb : c.GetFields(false)) {
                    auto* fi = fb.GetInfo();
                    if (!fi || !fi->type || (fi->type->attrs & 0x10 /* static */)) continue;
                    if (typeName(fi->type).rfind("System.Collections.Generic.List", 0) != 0) continue;
                    auto* list = *(uint8_t**)((uint8_t*)ad + fi->offset);
                    if (!list) continue;
                    auto* items = *(uint8_t**)(list + 0x10);
                    int size = *(int*)(list + 0x18);
                    if (!items || size <= 0 || size > 4096) continue;
                    // Only List<string>: other lists (e.g. of ints/structs) hold
                    // raw values, and reading those as object pointers crashed.
                    auto* arrK = ((Il2CppObject*)items)->klass;
                    if (!arrK || arrK->element_class != strK) continue;
                    for (int i = 0; i < size && !found; i++) {
                        auto* e = ((Il2CppObject**)(items + 0x20))[i];
                        if (e && e->klass == strK && MonoStr(e) == role) found = true;
                    }
                }
                if (strcmp(c.GetClass()->name, "Object") == 0) break;
            }
        }
    } catch (...) {}
    return found;
}
static bool LocalHasRole(const char* role) {
    double now = nowSeconds();
    if (now - g_blazeRoleCheckedAt < 5.0) return g_hasBlazeRole;
    g_blazeRoleCheckedAt = now;
    g_hasBlazeRole = AccountHasRoleNow(role);
    return g_hasBlazeRole;
}
static std::atomic<int> g_cumLogN{0};
static bool (*orig_CanUseMapObject)(void*, void*, void*) = nullptr;
static bool hook_CanUseMapObject(void* self, void* roles, void* mi) {
    bool ok = orig_CanUseMapObject ? orig_CanUseMapObject(self, roles, mi) : true;
    if (ok || !self) return ok;
    std::string req;
    try { req = MonoStr(*(void**)((uint8_t*)self + 0x68)); } catch (...) {}
    bool allow = (req == "blaze") && LocalHasRole("blaze");
    if (g_cumLogN.fetch_add(1) < 20)
        BNM_FileCheckpointF("Roles: CanUseMapObject requires '%s' -> local blaze=%d -> %s",
                            req.c_str(), (int)g_hasBlazeRole, allow ? "ALLOW" : "refuse");
    return allow;
}
static void InstallBlazeRoleCheck() {
    // Live 2.31.4 base (tester build): ENABLED. Low-risk hook (reads the map
    // object's required-role string and the local blaze role).
    try {
        Class mo("", "MapObject", Image("Assembly-CSharp.dll"));
        MethodBase m = mo.IsValid() ? mo.GetMethod("CanUseMapObject", 1) : MethodBase();
        if (m.IsValid()) { BasicHook(m, hook_CanUseMapObject, orig_CanUseMapObject); BNM_FileCheckpoint("Roles: CanUseMapObject hooked"); }
        else BNM_FileCheckpoint("Roles: CanUseMapObject NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("Roles: EXCEPTION installing"); }
}

static void InstallDispenserAcceptAll() {
#ifdef BNM_LIVE_BASE
    return;   // live 2.31.4 base: the real game handles this natively; this 2.4-era hook crashes on the live binary (e.g. eating an item)
#endif
    try {
        Class z("", "ItemDespawnZone", Image("Assembly-CSharp.dll"));
        if (!z.IsValid()) { BNM_FileCheckpoint("Dispenser: ItemDespawnZone NOT FOUND"); return; }
        MethodBase a = z.GetMethod("CheckCanAcceptItem", 1);
        MethodBase i = z.GetMethod("CheckShouldIgnoreItem", 1);
        MethodBase l = z.GetMethod("get_allowsLoadedItems", 0);
        if (a.IsValid()) BasicHook(a, hook_ZoneCanAccept, orig_ZoneCanAccept);
        if (i.IsValid()) BasicHook(i, hook_ZoneIgnore, orig_ZoneIgnore);
        if (l.IsValid()) BasicHook(l, hook_ZoneAllowsLoaded, orig_ZoneAllowsLoaded);
        BNM_FileCheckpointF("Dispenser: accept-all hooks canAccept=%d ignore=%d allowsLoaded=%d",
                            a.IsValid(), i.IsValid(), l.IsValid());
        Class disp("", "CustomWirableDispenserMapObject", Image("Assembly-CSharp.dll"));
        MethodBase iia = disp.IsValid() ? disp.GetMethod("IsItemAllowed", 8) : MethodBase();
        if (iia.IsValid()) { BasicHook(iia, hook_IsItemAllowed, orig_IsItemAllowed); BNM_FileCheckpoint("Dispenser: IsItemAllowed hooked"); }
        else BNM_FileCheckpoint("Dispenser: IsItemAllowed NOT FOUND");
        MethodBase tpi = disp.IsValid() ? disp.GetMethod("TryProcessInputItem", 6) : MethodBase();
        if (tpi.IsValid()) { BasicHook(tpi, hook_TryProcessInputItem, orig_TryProcessInputItem); BNM_FileCheckpoint("Dispenser: TryProcessInputItem hooked"); }
        else BNM_FileCheckpoint("Dispenser: TryProcessInputItem NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("Dispenser: EXCEPTION installing"); }
}

// =====================================================================
//  2.4.0-blaze account / join fixes (from the earlier 2.4.0 work,
//  native-lib.cpp.240bak). RVAs in PatchCredentialGate match the stock 2.4.0
//  libil2cpp (sha256 b9953b4950b7cc07...).
// =====================================================================
static bool hook_ValidateHash(void*, void*, void*, void*, void*, void*, IL2CPP::MethodInfo*) {
    return true;
}

static void InstallValidateHashHook() {
    try {
        Class cls("", "AccountManager", Image("Assembly-CSharp.dll"));
        if (!cls.IsValid()) { BNM_FileCheckpoint("InstallValidateHashHook: class not found"); return; }
        MethodBase m = cls.GetMethod("ValidateHash", 6);
        if (!m.IsValid()) m = cls.GetMethod("ValidateHash");
        if (m.IsValid()) {
            static void* orig_ValidateHash = nullptr;
            BasicHook(m, hook_ValidateHash, orig_ValidateHash);
            BNM_FileCheckpoint("InstallValidateHashHook: hooked -> always true");
        } else BNM_FileCheckpoint("InstallValidateHashHook: ValidateHash NOT FOUND");
    } catch (...) {
        BNM_FileCheckpoint("InstallValidateHashHook: EXCEPTION");
    }
}

static void patchWord(uint8_t* il2cppBase, uint32_t off, uint32_t expect, uint32_t val, const char* tag) {
    uint32_t* addr = (uint32_t*)(il2cppBase + off);
    // Only patch the exact stock instruction; anything else means a different
    // libil2cpp and writing there would corrupt code.
    if (*addr != expect) {
        BNM_FileCheckpointF("PatchCredentialGate: %s @0x%x is %08x, expected %08x - skipped", tag, off, *addr, expect);
        return;
    }
    uintptr_t page = (uintptr_t)addr & ~((uintptr_t)0xFFF);
    if (mprotect((void*)page, 0x2000, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) {
        BNM_FileCheckpointF("PatchCredentialGate: mprotect failed @%s", tag); return;
    }
    uint32_t old = *addr;
    *addr = val;
    __builtin___clear_cache((char*)addr, (char*)addr + 4);
    mprotect((void*)page, 0x2000, PROT_READ | PROT_EXEC);
    BNM_FileCheckpointF("PatchCredentialGate: %s @0x%x %08x -> %08x", tag, off, old, val);
}

static void PatchCredentialGate() {
#ifdef BNM_LIVE_BASE
    // These are hardcoded 2.4.0 libil2cpp RVAs (base computed from the 2.4
    // offset of il2cpp_array_new_specific). They would patch unrelated code on
    // the live 2.31.4 binary, so this gate is disabled on the live base; the
    // live credential path is handled separately once diagnosed.
    BNM_FileCheckpoint("PatchCredentialGate: skipped (live base)");
    return;
#endif
    try {
        void* ans = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_array_new_specific");
        uint8_t* il2cppBase = ans ? ((uint8_t*)ans - 0x41fc160) : nullptr;
        if (!il2cppBase) { BNM_FileCheckpoint("PatchCredentialGate: no il2cppBase"); return; }
        patchWord(il2cppBase, 0x477a288, 0x34000148, 0xd503201f, "cbz-credfail->NOP");
        patchWord(il2cppBase, 0x477a29c, 0x370000a0, 0xd503201f, "tbnz-fail->NOP");
        patchWord(il2cppBase, 0x477a2ac, 0x360003c0, 0x1400001e, "tbz-success->B");
    } catch (...) { BNM_FileCheckpoint("PatchCredentialGate: EXCEPTION"); }
}

static void (*orig_GetHasPerm)(void*, void*, void*, void*, IL2CPP::MethodInfo*) = nullptr;

// fwd decls: fever-dream effects (defined below OnLoaded block)
static void FeverSetRoom(const std::string& rk);
static void InstallFeverEffects();
static void hook_GetHasPerm(void* roomKey, void* a1, void* resultCb, void* a3, IL2CPP::MethodInfo* mi) {
    try {
        std::string rk = roomKey ? ((BNM::Structures::Mono::String*)roomKey)->str() : "(null)";
        FeverSetRoom(rk);   // fever-dream effects: flip on/off by current room
        auto* del = (Il2CppObject*)resultCb;
        if (del && del->klass) {
            Class dc(del->klass);
            MethodBase inv = dc.GetMethod("Invoke");
            if (inv.IsValid()) {
                auto* imi = inv.GetInfo();
                if (imi && imi->methodPointer) {
                    bool granted = true;
                    void* args[1] = { &granted };
                    Il2CppException* e = nullptr;
                    Internal::il2cppMethods.il2cpp_runtime_invoke(imi, del, args, &e);
                    BNM_FileCheckpointF("hook_GetHasPerm: granted room=%s (exc=%p)", rk.c_str(), (void*)e);
                    return; // skip real (denying) check
                }
            }
        }
        BNM_FileCheckpointF("hook_GetHasPerm: could not invoke cb for room=%s, falling through", rk.c_str());
    } catch (...) { BNM_FileCheckpoint("hook_GetHasPerm: EXCEPTION"); }
    if (orig_GetHasPerm) orig_GetHasPerm(roomKey, a1, resultCb, a3, mi);
}

// biglive26: OWN EVERYTHING without the room-load freeze. The client processes
// each server-sent ownedPattern on the MAIN THREAD at room load (~50ms each via
// reflection), so sending all ~3974 freezes slow headsets past the Photon
// watchdog ("stuck on loading"). Instead the server sends a SMALL owned list
// (fast load) and we make PatternManager report EVERY pattern as owned/usable,
// so the build menu (a virtualised ScrollingPatternLayout -> only visible rows
// render) shows and lets you place all of them. These are O(1) per-item checks
// -> they can NOT re-introduce the load freeze.
static bool (*orig_IsPatternOwned)(void*, IL2CPP::MethodInfo*) = nullptr;
static bool hook_IsPatternOwned(void* a0, IL2CPP::MethodInfo* mi) { return true; }
static bool (*orig_IsPatternUsable)(void*, IL2CPP::MethodInfo*) = nullptr;
static bool hook_IsPatternUsable(void* a0, IL2CPP::MethodInfo* mi) { return true; }
static bool (*orig_IsPatternCollectionOwned)(void*, IL2CPP::MethodInfo*) = nullptr;
static bool hook_IsPatternCollectionOwned(void* a0, IL2CPP::MethodInfo* mi) { return true; }
static void InstallPatternUnlock() {
    try {
        Class pm("", "PatternManager", Image("Assembly-CSharp.dll"));
        if (!pm.IsValid()) { BNM_FileCheckpoint("[unlock] PatternManager not found"); return; }
        MethodBase o = pm.GetMethod("IsPatternOwned", 1);
        MethodBase u = pm.GetMethod("IsPatternUsable", 1);
        MethodBase c = pm.GetMethod("IsPatternCollectionOwned", 1);
        if (o.IsValid()) BasicHook(o, hook_IsPatternOwned, orig_IsPatternOwned);
        if (u.IsValid()) BasicHook(u, hook_IsPatternUsable, orig_IsPatternUsable);
        if (c.IsValid()) BasicHook(c, hook_IsPatternCollectionOwned, orig_IsPatternCollectionOwned);
        BNM_FileCheckpointF("[unlock] IsPatternOwned=%d Usable=%d Collection=%d", o.IsValid(), u.IsValid(), c.IsValid());
    } catch (...) { BNM_FileCheckpoint("[unlock] EXCEPTION"); }
}

static void InstallPermissionHook() {
    try {
        Class rmm("", "RoomMapManager", Image("Assembly-CSharp.dll"));
        if (!rmm.IsValid()) { BNM_FileCheckpoint("InstallPermissionHook: RoomMapManager not found"); return; }
        MethodBase m = rmm.GetMethod("GetHasPermissionToJoinRoom", 4);
        if (m.IsValid()) { BasicHook(m, hook_GetHasPerm, orig_GetHasPerm); BNM_FileCheckpoint("InstallPermissionHook: GetHasPermissionToJoinRoom hooked"); }
        else BNM_FileCheckpoint("InstallPermissionHook: GetHasPermissionToJoinRoom(4) not found");
    } catch (...) { BNM_FileCheckpoint("InstallPermissionHook: EXCEPTION"); }
}

static void TuneServicePointManager() {
    try {
        Class spm("System.Net", "ServicePointManager", Image("System.dll"));
        if (!spm.IsValid()) spm = Class("System.Net", "ServicePointManager", Image("mscorlib.dll"));
        if (!spm.IsValid()) { BNM_FileCheckpoint("SPM: class not found"); return; }
        { Method<void> m(spm.GetMethod("set_DefaultConnectionLimit", 1));
          if (m.IsValid()) { m((int)64); BNM_FileCheckpoint("SPM: DefaultConnectionLimit=64"); }
          else BNM_FileCheckpoint("SPM: set_DefaultConnectionLimit NOT FOUND"); }
        { Method<void> m(spm.GetMethod("set_MaxServicePointIdleTime", 1));
          if (m.IsValid()) { m((int)600000); BNM_FileCheckpoint("SPM: MaxServicePointIdleTime=600000"); } }
        { Method<void> m(spm.GetMethod("set_Expect100Continue", 1));
          if (m.IsValid()) { m((bool)false); BNM_FileCheckpoint("SPM: Expect100Continue=false"); } }
        { Method<void> m(spm.GetMethod("set_ReusePort", 1));
          if (m.IsValid()) { m((bool)true); BNM_FileCheckpoint("SPM: ReusePort=true"); } }
    } catch (...) { BNM_FileCheckpoint("SPM: EXCEPTION"); }
}

// AccountOnlineManager.DoRefresh bails at once when the bool at self+0x30 is 0
// (`ldrb w8,[x19,#0x30]; cbz w8,<return>`), so the account never goes online
// and GoToRoom never reaches the Photon join. Force the gate open.
static void (*orig_DoRefresh)(Il2CppObject*, void*, IL2CPP::MethodInfo*) = nullptr;
static void hook_DoRefresh(Il2CppObject* self, void* callback, IL2CPP::MethodInfo* mi) {
    if (self) *((uint8_t*)self + 0x30) = 1;
    if (orig_DoRefresh) orig_DoRefresh(self, callback, mi);
}

static void InstallAccountOnlineHooks() {
    try {
        Class cls("", "AccountOnlineManager", Image("Assembly-CSharp.dll"));
        if (!cls.IsValid()) { BNM_FileCheckpoint("InstallAccountOnlineHooks: class not found"); return; }
        MethodBase m = cls.GetMethod("DoRefresh", 1);
        if (m.IsValid()) { BasicHook(m, hook_DoRefresh, orig_DoRefresh); BNM_FileCheckpoint("InstallAccountOnlineHooks: DoRefresh hooked"); }
        else BNM_FileCheckpoint("InstallAccountOnlineHooks: DoRefresh NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("InstallAccountOnlineHooks: EXCEPTION"); }
}

// ---- 2.4.0 diagnosis: room-data stall --------------------------------
// RoomDatabase logs "Getting room data ch2_hub" but no request is ever sent.
// Log entry to the room-data methods and every exception that lands in a Task
// (async exceptions are otherwise swallowed silently).
static std::atomic<int> g_taskExN{0};
static std::string ExcText(Il2CppObject* ex) {
    std::string out;
    try {
        if (!ex || !ex->klass) return "(null)";
        out = ex->klass->name ? ex->klass->name : "?";
        Class ec(ex->klass);
        Method<BNM::Structures::Mono::String*> gm(ec.GetMethod("get_Message", 0));
        if (gm.IsValid()) { auto* m = gm[ex](); if (m) out += ": " + m->str(); }
        Method<BNM::Structures::Mono::String*> gs(ec.GetMethod("get_StackTrace", 0));
        if (gs.IsValid()) { auto* t = gs[ex](); if (t) out += " | " + t->str().substr(0, 900); }
    } catch (...) {}
    return out;
}
static bool (*orig_TaskTrySetEx)(Il2CppObject*, Il2CppObject*, IL2CPP::MethodInfo*) = nullptr;
static bool hook_TaskTrySetEx(Il2CppObject* self, Il2CppObject* exObj, IL2CPP::MethodInfo* mi) {
    if (g_taskExN.fetch_add(1) < 40) BNM_FileCheckpointF("[taskex] %s", ExcText(exObj).c_str());
    return orig_TaskTrySetEx ? orig_TaskTrySetEx(self, exObj, mi) : false;
}
static void (*orig_RDB_GetRoomData)(void*, void*, void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void hook_RDB_GetRoomData(void* a0, void* a1, void* a2, void* a3, IL2CPP::MethodInfo* mi) {
    std::string k0 = a0 ? ((BNM::Structures::Mono::String*)a0)->str() : "(null)";
    std::string k1 = a1 ? ((BNM::Structures::Mono::String*)a1)->str() : "(null)";
    BNM_FileCheckpointF("[rdb] GetRoomData(%s, %s, cb=%p, cb=%p)", k0.c_str(), k1.c_str(), a2, a3);
    if (orig_RDB_GetRoomData) orig_RDB_GetRoomData(a0, a1, a2, a3, mi);
}
static void* (*orig_RDB_Batch)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void* hook_RDB_Batch(void* a0, void* a1, IL2CPP::MethodInfo* mi) {
    BNM_FileCheckpointF("[rdb] BatchGetRoomDatasForRoomMap(%p, %p)", a0, a1);
    return orig_RDB_Batch ? orig_RDB_Batch(a0, a1, mi) : nullptr;
}
static void InstallRoomDataDiag() {
    try {
        Class t("System.Threading.Tasks", "Task", Image("mscorlib.dll"));
        MethodBase m = t.IsValid() ? t.GetMethod("TrySetException", 1) : MethodBase();
        if (m.IsValid()) { BasicHook(m, hook_TaskTrySetEx, orig_TaskTrySetEx); BNM_FileCheckpoint("[diag] Task.TrySetException hooked"); }
        else BNM_FileCheckpoint("[diag] Task.TrySetException(1) NOT FOUND");
        Class rdb("", "RoomDatabase", Image("Assembly-CSharp.dll"));
        if (rdb.IsValid()) {
            MethodBase g = rdb.GetMethod("GetRoomData", 4);
            if (g.IsValid()) { BasicHook(g, hook_RDB_GetRoomData, orig_RDB_GetRoomData); BNM_FileCheckpoint("[diag] RoomDatabase.GetRoomData hooked"); }
            MethodBase b = rdb.GetMethod("BatchGetRoomDatasForRoomMap", 2);
            if (b.IsValid()) { BasicHook(b, hook_RDB_Batch, orig_RDB_Batch); BNM_FileCheckpoint("[diag] RoomDatabase.BatchGetRoomDatasForRoomMap hooked"); }
        }
    } catch (...) { BNM_FileCheckpoint("[diag] EXCEPTION installing room diag"); }
}

// ---- 2.4.0: null strings in Lambda payloads ---------------------------
// The client's Lambda helper serializes its payload with OVRSimpleJSON; a null
// string in a JSONArray hits JSONNode.Escape(null) -> NullReferenceException,
// the request is never sent and room loading waits forever. Treat null as "".
static std::atomic<int> g_escNullN{0};
static thread_local const char* g_curLambda = nullptr;
static void* (*orig_JsonEscape)(void*, IL2CPP::MethodInfo*) = nullptr;
static void* hook_JsonEscape(void* text, IL2CPP::MethodInfo* mi) {
    if (!text) {
        if (g_escNullN.fetch_add(1) < 20)
            BNM_FileCheckpointF("[json] null string in payload (lambda=%s) -> \"\"", g_curLambda ? g_curLambda : "?");
        text = (void*)CreateMonoString("");
    }
    return orig_JsonEscape ? orig_JsonEscape(text, mi) : text;
}
static std::atomic<int> g_lambdaLogN{0};
static void* (*orig_LambdaInvoke)(void*, void*, bool, IL2CPP::MethodInfo*) = nullptr;
static void* hook_LambdaInvoke(void* fn, void* payload, bool flag, IL2CPP::MethodInfo* mi) {
    std::string f = fn ? ((BNM::Structures::Mono::String*)fn)->str() : "(null)";
    if (g_lambdaLogN.fetch_add(1) < 120) BNM_FileCheckpointF("[lambda] %s", f.c_str());
    static thread_local std::string held; held = f; g_curLambda = held.c_str();
    void* r = orig_LambdaInvoke ? orig_LambdaInvoke(fn, payload, flag, mi) : nullptr;
    g_curLambda = nullptr;
    return r;
}
static void InstallJsonNullFix() {
    try {
        Class jn("OVRSimpleJSON", "JSONNode");
        MethodBase e = jn.IsValid() ? jn.GetMethod("Escape", 1) : MethodBase();
        if (e.IsValid()) { BasicHook(e, hook_JsonEscape, orig_JsonEscape); BNM_FileCheckpoint("[json] JSONNode.Escape hooked (null -> \"\")"); }
        else BNM_FileCheckpoint("[json] JSONNode.Escape NOT FOUND");
        // Obfuscated Lambda helper: <cls>.<m>(string fn, JSONNode payload, bool) -> Task<>
        Class lc("", "][][][]]]][[][[[]][[][[[[[][[[][[][[[[]]][]]][[", Image("Assembly-CSharp.dll"));
        MethodBase li = lc.IsValid() ? lc.GetMethod("][][[]][]]]]][][]][][[]][[]]][][]][[[[[[]]]][[]", 3) : MethodBase();
        if (li.IsValid()) { BasicHook(li, hook_LambdaInvoke, orig_LambdaInvoke); BNM_FileCheckpoint("[json] Lambda helper hooked"); }
        else BNM_FileCheckpoint("[json] Lambda helper NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("[json] EXCEPTION"); }
}


// ---- 240b19 diag: JSON keys the game asks for but our replies lack --------
// OVRSimpleJSON's JSONObject[key] returns a JSONLazyCreator for a missing key
// (JSONNode[key] on arrays/strings returns null). Log each distinct missing
// key once with the node type it was asked on, so the expected reply shape of
// e.g. g2_fetchAccountDatas ("Failed to batch fetch account data:
// NO_DATA_FOUND") shows up in the devlog instead of being guessed.
static std::mutex g_jmMu;
static std::set<std::string> g_jmSeen;
static void JsonMissLog(void* self, void* key, void* r) {
    if (!key) return;
    const char* rk = r ? ((Il2CppObject*)r)->klass->name : "null";
    if (r && strcmp(rk, "JSONLazyCreator") != 0) return;
    const char* sk = self ? ((Il2CppObject*)self)->klass->name : "?";
    std::string k = ((BNM::Structures::Mono::String*)key)->str();
    std::string id = std::string(sk) + "[" + k + "]";
    {
        std::lock_guard<std::mutex> lk(g_jmMu);
        if (g_jmSeen.size() > 400 || !g_jmSeen.insert(id).second) return;
    }
    BNM_FileCheckpointF("[jsonmiss] %s (lambda=%s)", id.c_str(), g_curLambda ? g_curLambda : "-");
}
static void* (*orig_JO_Get)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void* hook_JO_Get(void* self, void* key, IL2CPP::MethodInfo* mi) {
    void* r = orig_JO_Get ? orig_JO_Get(self, key, mi) : nullptr;
    try { JsonMissLog(self, key, r); } catch (...) {}
    return r;
}
static void InstallJsonMissDiag() {
    try {
        MethodBase o = FindOverload(Class("OVRSimpleJSON", "JSONObject"), "get_Item", {"System.String"});
        if (o.IsValid()) BasicHook(o, hook_JO_Get, orig_JO_Get);
        // (JSONNode's base get_Item is a tiny "return null": too short for an
        // inline hook, so arrays/strings indexed by key are not logged.)
        BNM_FileCheckpointF("[jsonmiss] JSONObject.get_Item(string) hooked=%d", o.IsValid());
    } catch (...) { BNM_FileCheckpoint("[jsonmiss] EXCEPTION"); }
}



// ---- 240b25: custom blocks built at runtime ------------------------------
// Objects ADDED to 2.4's resources.assets are never loadable (Unity 6 ignores
// them: Resources.Load of a new path -> null, LoadAll returns only the stock
// set), so "Could not find map object data with key: 1x5x2_stuffedTextBlaze".
// Instead, when the game loads MapObjects/complexDecorationStuffed, we append
// runtime copies of a stock pillow (kept under an inactive, persistent parent
// so they behave like prefabs), renamed to our keys, with our own meshes
// (rtblocks_data.h, from the 1.31 builders), grid size, role and materials.
// The pattern entries (names in the build menu) are still in the game data.
#include "rtblocks_data.h"
struct RtV3 { float x, y, z; };
static void* g_rtHolder = nullptr;
static std::vector<void*> g_rtClones;                // pinned, same element type as LoadAll's array
static std::map<std::string, void*> g_rtMats;        // "blaze" (screen) / "cat" -> pinned material
static struct RtMatsPtrInit { RtMatsPtrInit() { g_rtMatsPtr = &g_rtMats; } } g_rtMatsPtrInit;
static std::atomic<int> g_rtLogN{0};
static const char* g_rtStage = "-";
#define RTLOG(...) do { if (g_rtLogN.fetch_add(1) < 300) BNM_FileCheckpointF(__VA_ARGS__); } while (0)   // big: was 60
static void RtInitApi() {
    if (!p_object_new) p_object_new = (void*(*)(void*))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_object_new");
    if (!p_array_new) p_array_new = (void*(*)(void*, uintptr_t))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_array_new");
}
static void RtSetRef(void* obj, size_t off, void* val) {
    static auto wb = (void (*)(void*, void**, void*))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_gc_wbarrier_set_field");
    void** slot = (void**)((uint8_t*)obj + off);
    if (wb) wb(obj, slot, val); else *slot = val;
}
static Il2CppObject* RtResLoad(const char* path) {
    Method<Il2CppObject*> load(FindOverload(Class("UnityEngine", "Resources"), "Load", {"System.String"}));
    return load.IsValid() ? load(CreateMonoString(path)) : nullptr;
}
static void* RtArr(const char* ns, const char* n, size_t count, const void* src, size_t elem) {
    void* a = p_array_new(Class(ns, n).GetClass(), count);
    if (a && src) memcpy((uint8_t*)a + 0x20, src, count * elem);
    return a;
}
static Il2CppObject* RtMesh(const char* name, const uint8_t* verts, int vc, const uint16_t* i0, int n0, const uint16_t* i1, int n1) {
    g_rtStage = "mesh";
    Class meshC("UnityEngine", "Mesh");
    auto* m = (Il2CppObject*)p_object_new(meshC.GetClass());
    Method<void>(meshC.GetMethod(".ctor", 0))[m]();
    Method<void>(Class("UnityEngine", "Object").GetMethod("set_name", 1))[m](CreateMonoString(name));
    std::vector<float> pos(vc * 3), nrm(vc * 3), col(vc * 4), uv(vc * 2);
    for (int i = 0; i < vc; i++) {
        const float* f = (const float*)(verts + 48 * i);
        memcpy(&pos[i * 3], f, 12); memcpy(&nrm[i * 3], f + 3, 12);
        memcpy(&col[i * 4], f + 6, 16); memcpy(&uv[i * 2], f + 10, 8);
    }
    Method<void>(FindOverload(meshC, "set_vertices", {"UnityEngine.Vector3[]"}))[m](RtArr("UnityEngine", "Vector3", vc, pos.data(), 12));
    Method<void>(FindOverload(meshC, "set_normals", {"UnityEngine.Vector3[]"}))[m](RtArr("UnityEngine", "Vector3", vc, nrm.data(), 12));
    Method<void>(FindOverload(meshC, "set_colors", {"UnityEngine.Color[]"}))[m](RtArr("UnityEngine", "Color", vc, col.data(), 16));
    Method<void>(FindOverload(meshC, "set_uv", {"UnityEngine.Vector2[]"}))[m](RtArr("UnityEngine", "Vector2", vc, uv.data(), 8));
    Method<void>(FindOverload(meshC, "set_subMeshCount", {"System.Int32"}))[m](n1 > 0 ? 2 : 1);
    Method<void> setTri(FindOverload(meshC, "SetTriangles", {"System.Int32[]", "System.Int32"}));
    std::vector<int> t0(i0, i0 + n0), t1(i1, i1 + n1);
    setTri[m](RtArr("System", "Int32", n0, t0.data(), 4), 0);
    if (n1 > 0) setTri[m](RtArr("System", "Int32", n1, t1.data(), 4), 1);
    Method<void>(meshC.GetMethod("RecalculateBounds", 0))[m]();
    return m;
}
static Il2CppObject* RtPictureMaterial(const char* name, Il2CppObject* tex) {
    g_rtStage = "pictureMaterial";
    // biglive26 FIX: build a GUARANTEED real Material with an unlit textured
    // shader (was: Instantiate solid_decoration_1 and call Material.set_mainTexture
    // on the result, whose type was unreliable -> the "material" wasn't a valid
    // textured Material -> the cat block rendered WHITE). Unlit/Texture samples
    // _MainTex with the mesh UVs, so the picture shows upright on every face.
    try {
        Class shaderC("UnityEngine", "Shader");
        Method<Il2CppObject*> findShader(shaderC.GetMethod("Find", 1));
        Il2CppObject* sh = nullptr;
        for (const char* sn : {"Unlit/Texture", "Sprites/Default", "Unlit/Transparent", "Standard"}) {
            if (!findShader.IsValid()) break;
            sh = findShader(CreateMonoString(sn));
            if (sh) { RTLOG("[rt] picture material shader=%s", sn); break; }
        }
        if (!sh) { RTLOG("[rt] picture material: no shader found"); return nullptr; }
        Class matC("UnityEngine", "Material");
        Il2CppObject* mat = (Il2CppObject*)p_object_new(matC.GetClass());
        if (!mat) return nullptr;
        Method<void> ctor(FindOverload(matC, ".ctor", {"UnityEngine.Shader"}));
        if (ctor.IsValid()) ctor[mat](sh);
        Method<void>(Class("UnityEngine", "Object").GetMethod("set_name", 1))[mat](CreateMonoString(name));
        if (tex) Method<void>(matC.GetMethod("set_mainTexture", 1))[mat](tex);
        Method<void>(Class("UnityEngine", "Object").GetMethod("DontDestroyOnLoad", 1))(mat);
        RTLOG("[rt] picture material built (real Material) name=%s tex=%d", name, tex != nullptr);
        return mat;
    } catch (...) { RTLOG("[rt] picture material EXCEPTION"); return nullptr; }
}
static Il2CppObject* RtCatTexture() {
    g_rtStage = "catTexture";
    // Same path as the picture screens: RGB24 texture + LoadRawTextureData.
    // Any failure here only costs the cat its picture, not the blocks.
    try {
        Class t2("UnityEngine", "Texture2D");
        Method<void> ctor(FindOverload(t2, ".ctor", {"System.Int32", "System.Int32", "UnityEngine.TextureFormat", "System.Boolean"}));
        Method<void> load(FindOverload(t2, "LoadRawTextureData", {"System.Byte[]"}));
        Method<void> apply(FindOverload(t2, "Apply", {"System.Boolean", "System.Boolean"}));
        if (!ctor.IsValid() || !load.IsValid() || !apply.IsValid()) { RTLOG("[rt] cat texture api missing"); return nullptr; }
        auto* t = (Il2CppObject*)p_object_new(t2.GetClass());
        ctor[t](kRt_catW, kRt_catH, 3 /* RGB24 */, false);
        load[t]((Il2CppObject*)RtArr("System", "Byte", (size_t)kRt_catW * kRt_catH * 3, kRt_catRgb, 1));
        apply[t](false, false);
        Method<void>(Class("UnityEngine", "Object").GetMethod("DontDestroyOnLoad", 1))(t);
        RTLOG("[rt] cat texture ok");
        return t;
    } catch (...) { RTLOG("[rt] cat texture EXCEPTION"); return nullptr; }
}
struct RtSpec { const char* key; int sx, sy, sz; const char* role; int mesh; const char* pictureMat; };
static const RtSpec kRtSpecs[] = {
    {"1x5x2_stuffedTextBlaze", 5, 2, 1, "blaze", 0, nullptr},
    {"3x2x1_blazeScreenRole", 3, 2, 1, "blaze", 1, "blaze"},
    {"1x1_stuffedCucumberCat", 1, 1, 1, "", 2, "cat"},
};
static bool RtBuild(void* srcElem, Il2CppClass* elemK) {
    RtInitApi();
    if (!p_object_new || !p_array_new) { RTLOG("[rt] il2cpp api missing"); return false; }
    Class goC("UnityEngine", "GameObject"), objC("UnityEngine", "Object"), compC("UnityEngine", "Component");
    // LoadAll returns Object[]: look at what the elements really are
    elemK = ((Il2CppObject*)srcElem)->klass;
    bool elemIsGo = elemK == goC.GetClass();
    RTLOG("[rt] source element class %s", elemK && elemK->name ? elemK->name : "?");
    g_rtStage = "b1";
    Il2CppObject* srcGo = elemIsGo ? (Il2CppObject*)srcElem
                                   : Method<Il2CppObject*>(compC.GetMethod("get_gameObject", 0))[(Il2CppObject*)srcElem]();
    if (!srcGo) { RTLOG("[rt] no source GameObject"); return false; }
    // inactive persistent parent: clones keep activeSelf=true but stay dormant
    g_rtStage = "b2";
    auto* holder = (Il2CppObject*)p_object_new(goC.GetClass());
    g_rtStage = "b3";
    Method<void>(FindOverload(goC, ".ctor", {"System.String"}))[holder](CreateMonoString("blaze_runtime_blocks"));
    g_rtStage = "b4";
    Method<void>(goC.GetMethod("SetActive", 1))[holder](false);
    g_rtStage = "b5";
    Method<void>(objC.GetMethod("DontDestroyOnLoad", 1))(holder);
    g_rtStage = "b6";
    g_rtHolder = Pin(holder);
    g_rtStage = "b7";
    Il2CppObject* holderTr = Method<Il2CppObject*>(goC.GetMethod("get_transform", 0))[holder]();
    Method<Il2CppObject*> inst(FindOverload(objC, "Instantiate", {"UnityEngine.Object", "UnityEngine.Transform"}));
    Method<Il2CppObject*> getComp(FindOverload(goC, "GetComponent", {"System.Type"}));
    Class moC("", "MapObject", Image("Assembly-CSharp.dll"));
    Class mfC("UnityEngine", "MeshFilter"), mrC("UnityEngine", "MeshRenderer"), bcC("UnityEngine", "BoxCollider");
    g_rtStage = "b8";
    Il2CppObject* stuffed = RtResLoad("MapObjects/stuffed/solid_stuffed");
    g_rtStage = "b9";
    Il2CppObject* frame = RtResLoad("MapObjects/complexDecoration/solid/solid_decoration_0");
    g_rtStage = "b10";
    if (!g_rtMats.count("blaze")) { auto* m = RtPictureMaterial("blazeScreen_blaze", nullptr); if (m) g_rtMats["blaze"] = Pin(m); }
    g_rtStage = "b11";
    if (!g_rtMats.count("cat")) { auto* m = RtPictureMaterial("cucumberCat", RtCatTexture()); if (m) g_rtMats["cat"] = Pin(m); }
    for (const RtSpec& sp : kRtSpecs) try {
        g_rtStage = "b12";
        Il2CppObject* go = inst(srcGo, holderTr);
        if (!go) { RTLOG("[rt] Instantiate failed for %s", sp.key); continue; }
        g_rtStage = "b13";
        Method<void>(objC.GetMethod("set_name", 1))[go](CreateMonoString(sp.key));
        g_rtStage = "b14";
        Il2CppObject* mo = getComp[go]((Il2CppObject*)moC.GetMonoType());
        if (mo) {
            int* sz = (int*)((uint8_t*)mo + 0x20); sz[0] = sp.sx; sz[1] = sp.sy; sz[2] = sp.sz;
            RtSetRef(mo, 0x70, (void*)CreateMonoString(sp.role));
        }
        g_rtStage = "b15";
        Il2CppObject* mesh = sp.mesh == 0 ? RtMesh(sp.key, kRt_blaze_verts, kRt_blaze_vcount, kRt_blaze_idx0, kRt_blaze_n0, kRt_blaze_idx1, kRt_blaze_n1)
                           : sp.mesh == 1 ? RtMesh(sp.key, kRt_screenRole_verts, kRt_screenRole_vcount, kRt_screenRole_idx0, kRt_screenRole_n0, kRt_screenRole_idx1, kRt_screenRole_n1)
                                          : RtMesh(sp.key, kRt_cat_verts, kRt_cat_vcount, kRt_cat_idx0, kRt_cat_n0, kRt_cat_idx1, kRt_cat_n1);
        const float* ext = sp.mesh == 0 ? kRt_blaze_ext : sp.mesh == 1 ? kRt_screenRole_ext : kRt_cat_ext;
        // Stuffed blocks keep their mesh on LOD children (LOD0..2) + a LODGroup.
        // big: the game wants the MeshRenderer on the block itself ("MapObject X
        // has no MeshRenderer", and the map batcher then fails), so remove the
        // LOD children + LODGroup and put a MeshFilter/MeshRenderer on the root.
        g_rtStage = "lod";
        Il2CppObject* vis = go;
        {
            Class trC("UnityEngine", "Transform");
            Method<void> destroyNow(FindOverload(objC, "DestroyImmediate", {"UnityEngine.Object"}));
            Il2CppObject* tr = Method<Il2CppObject*>(goC.GetMethod("get_transform", 0))[go]();
            int cc = Method<int>(trC.GetMethod("get_childCount", 0))[tr]();
            std::vector<Il2CppObject*> kids;
            for (int ci = 0; ci < cc; ci++) {
                Il2CppObject* ct = Method<Il2CppObject*>(trC.GetMethod("GetChild", 1))[tr](ci);
                if (ct) kids.push_back(Method<Il2CppObject*>(compC.GetMethod("get_gameObject", 0))[ct]());
            }
            Il2CppObject* lod = getComp[go]((Il2CppObject*)Class("UnityEngine", "LODGroup").GetMonoType());
            if (lod) destroyNow(lod);
            for (Il2CppObject* k : kids) if (k) destroyNow(k);
            Method<Il2CppObject*> addComp(FindOverload(goC, "AddComponent", {"System.Type"}));
            if (!getComp[go]((Il2CppObject*)mfC.GetMonoType())) addComp[go]((Il2CppObject*)mfC.GetMonoType());
            if (!getComp[go]((Il2CppObject*)mrC.GetMonoType())) addComp[go]((Il2CppObject*)mrC.GetMonoType());
            // keep one BoxCollider on the root
            Method<Il2CppObject*> getComps(FindOverload(goC, "GetComponents", {"System.Type"}));
            auto* bcs = (uint8_t*)getComps[go]((Il2CppObject*)bcC.GetMonoType());
            if (bcs) {
                uintptr_t nb = *(uintptr_t*)(bcs + 0x18);
                for (uintptr_t bi = 1; bi < nb; bi++) { void* x = ((void**)(bcs + 0x20))[bi]; if (x) destroyNow((Il2CppObject*)x); }
            }
        }
        g_rtStage = "b16";
        Il2CppObject* mf = getComp[vis]((Il2CppObject*)mfC.GetMonoType());
        if (mf && mesh) Method<void>(mfC.GetMethod("set_sharedMesh", 1))[mf](mesh);
        g_rtStage = "b17";
        Il2CppObject* mr = getComp[vis]((Il2CppObject*)mrC.GetMonoType());
        if (mr) {
            g_rtStage = "b18";
            Il2CppObject* slot0 = sp.mesh == 1 ? frame : stuffed;
            void* pic = sp.pictureMat && g_rtMats.count(sp.pictureMat) ? p_gchandle_target(g_rtMats[sp.pictureMat]) : nullptr;
            void* mats[2] = {slot0, pic};
            void* arr = RtArr("UnityEngine", "Material", pic ? 2 : 1, mats, sizeof(void*));
            g_rtStage = "b19";
            Method<void>(FindOverload(Class("UnityEngine", "Renderer"), "set_sharedMaterials", {"UnityEngine.Material[]"}))[mr](arr);
        }
        g_rtStage = "b20";
        Il2CppObject* bc = getComp[go]((Il2CppObject*)bcC.GetMonoType());
        if (bc) {
            RtV3 size{ext[0] * 2, ext[1] * 2, ext[2] * 2}, ctr{0, 0, 0};
            g_rtStage = "b21";
            Method<void>(bcC.GetMethod("set_size", 1))[bc](size);
            g_rtStage = "b22";
            Method<void>(bcC.GetMethod("set_center", 1))[bc](ctr);
        }
        g_rtStage = "b23";
        void* elem = elemIsGo ? (void*)go : (void*)getComp[go]((Il2CppObject*)Class(elemK).GetMonoType());
        if (elem) g_rtClones.push_back(Pin(elem));
        RTLOG("[rt] built %s (mo=%d mesh=%d mf=%d mr=%d bc=%d visChild=%d)", sp.key, mo != nullptr, mesh != nullptr, mf != nullptr, mr != nullptr, bc != nullptr, vis != go);
    } catch (...) { RTLOG("[rt] %s FAILED at %s", sp.key, g_rtStage); }
    return !g_rtClones.empty();
}
// Called from hook_ResLoadAll: returns a new array with our clones appended.
static void* RtInject(void* path, void* arr) {
    if (!path || !arr) return arr;
    std::string p = ((BNM::Structures::Mono::String*)path)->str();
    if (!g_tvPrefabDone) { try { TvPatchPrefab(arr); } catch (...) { RTLOG("[rt] TV patch EXCEPTION"); } }
    if (strcasecmp(p.c_str(), "MapObjects/complexDecorationStuffed") != 0) return arr;
    auto* a = (Il2CppObject*)arr;
    Il2CppClass* elemK = a->klass ? a->klass->element_class : nullptr;
    uintptr_t n = *(uintptr_t*)((uint8_t*)arr + 0x18);
    if (!elemK || n == 0) return arr;
    if (g_rtClones.empty()) {
        void* src = nullptr;
        Method<Il2CppObject*> getName(Class("UnityEngine", "Object").GetMethod("get_name", 0));
        for (uintptr_t i = 0; i < n && !src; i++) {
            void* e = ((void**)((uint8_t*)arr + 0x20))[i];
            if (e && MonoStr(getName[(Il2CppObject*)e]()) == "2x2x2_stuffedTrophie_BAM2") src = e;
        }
        if (!src) src = ((void**)((uint8_t*)arr + 0x20))[0];
        try { RtBuild(src, elemK); }
        catch (std::exception& e) { RTLOG("[rt] build EXCEPTION at %s: %s", g_rtStage, e.what()); }
        catch (...) { RTLOG("[rt] build EXCEPTION at %s", g_rtStage); }
        RTLOG("[rt] element type %s, built %d clones", elemK->name, (int)g_rtClones.size());
    }
    if (g_rtClones.empty()) return arr;
    // big APK: the game data may now carry blocks with the same keys (sorted
    // object table makes data-side prefabs loadable) - never add a duplicate.
    std::set<std::string> have;
    {
        Method<Il2CppObject*> getName(Class("UnityEngine", "Object").GetMethod("get_name", 0));
        for (uintptr_t i = 0; i < n; i++) {
            void* e = ((void**)((uint8_t*)arr + 0x20))[i];
            if (e) have.insert(MonoStr(getName[(Il2CppObject*)e]()));
        }
    }
    std::vector<void*> add;
    {
        Method<Il2CppObject*> getName(Class("UnityEngine", "Object").GetMethod("get_name", 0));
        for (size_t i = 0; i < g_rtClones.size(); i++) {
            void* c = p_gchandle_target(g_rtClones[i]);
            if (c && !have.count(MonoStr(getName[(Il2CppObject*)c]()))) add.push_back(c);
        }
    }
    if (add.empty()) return arr;
    void* out = p_array_new(elemK, n + add.size());
    if (!out) return arr;
    memcpy((uint8_t*)out + 0x20, (uint8_t*)arr + 0x20, n * sizeof(void*));
    for (size_t i = 0; i < add.size(); i++)
        ((void**)((uint8_t*)out + 0x20))[n + i] = add[i];
    return out;
}

// ---- 240b23 diag: what the game loads through Resources -----------------
// Custom blocks still give "Could not find map object data with key" although
// they're in the ResourceManager. Log each distinct Load/LoadAll path + type
// (LoadAll with the result count), so the devlog shows how 2.4 finds blocks.
static std::mutex g_resMu;
static std::set<std::string> g_resSeen;
static std::string TypeNameOf(void* sysType) {
    try {
        if (!sysType) return "?";
        Method<Il2CppObject*> gn(Class("System", "Type").GetMethod("get_FullName", 0));
        return MonoStr(gn[(Il2CppObject*)sysType]());
    } catch (...) { return "?"; }
}
static void ResLog(const char* what, void* path, void* type, long n) {
    std::string p = path ? ((BNM::Structures::Mono::String*)path)->str() : "(null)";
    std::string id = std::string(what) + " " + p;
    {
        std::lock_guard<std::mutex> lk(g_resMu);
        if (g_resSeen.size() > 5000 || !g_resSeen.insert(id).second) return;   // big: was 600
    }
    BNM_FileCheckpointF("[res] %s '%s' type=%s n=%ld", what, p.c_str(), TypeNameOf(type).c_str(), n);
}
static void* (*orig_ResLoad)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void* hook_ResLoad(void* path, void* type, IL2CPP::MethodInfo* mi) {
    void* r = orig_ResLoad ? orig_ResLoad(path, type, mi) : nullptr;
    try { ResLog("Load", path, type, r ? 1 : 0); } catch (...) {}
    return r;
}
static void* (*orig_ResLoadAll)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void* hook_ResLoadAll(void* path, void* type, IL2CPP::MethodInfo* mi) {
    void* r = orig_ResLoadAll ? orig_ResLoadAll(path, type, mi) : nullptr;
    try { r = RtInject(path, r); } catch (...) { RTLOG("[rt] inject EXCEPTION"); }
    try { ResLog("LoadAll", path, type, r ? (long)*(uintptr_t*)((uint8_t*)r + 0x18) : -1); } catch (...) {}
    return r;
}
// live92 Yeeps TV: live builds skip InstallResourcesDiag (its Resources.Load hook
// crashed scene load), so hook ONLY LoadAll (a few calls per map load) and only
// to patch the 1x1 computer prefab once; after that it is a single bool check.
static void* (*orig_TvLoadAll)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void* hook_TvLoadAll(void* path, void* type, IL2CPP::MethodInfo* mi) {
    void* r = orig_TvLoadAll ? orig_TvLoadAll(path, type, mi) : nullptr;
    if (!g_tvPrefabDone && r) { try { TvPatchPrefab(r); } catch (...) { BNM_FileCheckpoint("TV: patch EXCEPTION"); } }
    return r;
}
static void InstallTvHook() {
    try {
        MethodBase a = FindOverload(Class("UnityEngine", "Resources"), "LoadAll", {"System.String", "System.Type"});
        if (a.IsValid()) BasicHook(a, hook_TvLoadAll, orig_TvLoadAll);
        BNM_FileCheckpointF("TV: LoadAll hook=%d", a.IsValid());
    } catch (...) { BNM_FileCheckpoint("TV: hook EXCEPTION"); }
}
static void InstallResourcesDiag() {
    try {
        Class rc("UnityEngine", "Resources");
        MethodBase l = FindOverload(rc, "Load", {"System.String", "System.Type"});
        MethodBase a = FindOverload(rc, "LoadAll", {"System.String", "System.Type"});
        if (l.IsValid()) BasicHook(l, hook_ResLoad, orig_ResLoad);
        if (a.IsValid()) BasicHook(a, hook_ResLoadAll, orig_ResLoadAll);
        BNM_FileCheckpointF("[res] hooks Load=%d LoadAll=%d", l.IsValid(), a.IsValid());
    } catch (...) { BNM_FileCheckpoint("[res] EXCEPTION"); }
}

// ---- 2.4.0 RE helper: dump string literals a code range loads ------------
// Push /sdcard/Android/data/com.TrassGames.Yeeps/files/strdump.txt with lines
// "<rva hex> <len hex>" (libil2cpp vaddr). Every adrp+ldr pair in the range is
// decoded to its metadata slot; slots holding a System.String are written to
// the checkpoint as "[str] <insn rva> '<text>'". The file is deleted after use.
static void StrDumpOnce() {
    const char* path = "/sdcard/Android/data/com.TrassGames.Yeeps/files/strdump.txt";
    FILE* f = fopen(path, "r");
    if (!f) return;
    void* ans = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_array_new_specific");
    uint8_t* base = ans ? (uint8_t*)ans - 0x41fc160 : nullptr;
    Il2CppClass* strCls = nullptr;
    try { strCls = Class("System", "String").GetClass(); } catch (...) {}
    char line[128];
    while (base && strCls && fgets(line, sizeof line, f)) {
        unsigned long long rva = 0, len = 0;
        if (sscanf(line, "%llx %llx", &rva, &len) != 2 || len > 0x8000) continue;
        BNM_FileCheckpointF("[str] == range %llx +%llx", rva, len);
        uint32_t* code = (uint32_t*)(base + rva);
        uintptr_t reg[32] = {};
        for (unsigned long long i = 0; i < len / 4; i++) {
            uint32_t w = code[i];
            if ((w & 0x9F000000u) == 0x90000000u) {               // adrp
                int64_t imm = (int64_t)((((w >> 5) & 0x7FFFF) << 2) | ((w >> 29) & 3));
                if (imm & (1LL << 20)) imm -= (1LL << 21);
                reg[w & 31] = (((uintptr_t)&code[i]) & ~(uintptr_t)0xFFF) + (uintptr_t)(imm << 12);
            } else if ((w & 0xFFC00000u) == 0xF9400000u) {         // ldr xT, [xN, #imm]
                uint32_t rn = (w >> 5) & 31;
                if (!reg[rn]) continue;
                void** slot = (void**)(reg[rn] + ((w >> 10) & 0xFFF) * 8);
                if ((uintptr_t)slot < (uintptr_t)base || (uintptr_t)slot + 8 > (uintptr_t)base + 0xadb30e0) continue;
                void* v = *slot;
                if (!v || ((uintptr_t)v & 7)) continue;
                auto* o = (Il2CppObject*)v;
                if (o->klass != strCls) {
                    // GOT entry -> metadata variable -> string (2 levels)
                    if ((uintptr_t)v < (uintptr_t)base || (uintptr_t)v + 8 > (uintptr_t)base + 0xadb30e0) continue;   // libil2cpp mapped extent
                    void* v2 = *(void**)v;
                    if (!v2 || ((uintptr_t)v2 & 7)) continue;
                    o = (Il2CppObject*)v2;
                    if (o->klass != strCls) continue;
                }
                std::string t = ((BNM::Structures::Mono::String*)o)->str();
                BNM_FileCheckpointF("[str] %llx '%s'", (unsigned long long)(rva + i * 4), t.substr(0, 120).c_str());
            }
        }
    }
    fclose(f);
    remove(path);
    BNM_FileCheckpoint("[str] done");
}
static void StrDumpPollThread() {
    for (;;) {
        std::this_thread::sleep_for(std::chrono::seconds(2));
        AttachThread();
        try { StrDumpOnce(); } catch (...) {}
    }
}

// ---- 2.4.0 diag: room map node parse (RVA hooks, this libil2cpp only) ----
static uint8_t* g_il2Base240 = nullptr;
typedef void* (*NodeGetStr_t)(void* node, void* key, void* a2, void* a3);
static std::atomic<int> g_parseLogN{0};
static bool (*orig_ParseNode)(void*, void*, void*, void*, void*) = nullptr;
static bool hook_ParseNode(void* mapKey, void* node, void* dict, void* outNode, void* mi) {
    bool r = orig_ParseNode ? orig_ParseNode(mapKey, node, dict, outNode, mi) : false;
    if (g_parseLogN.fetch_add(1) < 80) {
        std::string rk = "?";
        try {
            auto get = (NodeGetStr_t)(g_il2Base240 + 0x5181664);
            void* s = get(node, CreateMonoString("roomKey"), nullptr, nullptr);
            rk = s ? ((BNM::Structures::Mono::String*)s)->str() : "(null)";
        } catch (...) {}
        BNM_FileCheckpointF("[node] parse %s -> %d (dict=%p)", rk.c_str(), (int)r, dict);
    }
    return r;
}
static void (*orig_RoomMapCtor)(void*, void*, void*, int, void*) = nullptr;
static void hook_RoomMapCtor(void* self, void* key, void* nodes, int ver, void* mi) {
    std::string k = key ? ((BNM::Structures::Mono::String*)key)->str() : "(null)";
    int n = nodes ? *(int*)((uint8_t*)nodes + 0x18) : -1;
    BNM_FileCheckpointF("[node] RoomMap ctor key=%s nodes=%d version=%d", k.c_str(), n, ver);
    if (orig_RoomMapCtor) orig_RoomMapCtor(self, key, nodes, ver, mi);
}
static void (*orig_GetNode)(void*, void*, void*, void*) = nullptr;
static void hook_GetNode(void* mapKey, void* roomKey, void* cb, void* mi) {
    std::string a = mapKey ? ((BNM::Structures::Mono::String*)mapKey)->str() : "(null)";
    std::string b = roomKey ? ((BNM::Structures::Mono::String*)roomKey)->str() : "(null)";
    BNM_FileCheckpointF("[node] GetNode(%s, %s)", a.c_str(), b.c_str());
    if (orig_GetNode) orig_GetNode(mapKey, roomKey, cb, mi);
}
static void InstallNodeDiag() {
    void* ans = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_array_new_specific");
    if (!ans) return;
    g_il2Base240 = (uint8_t*)ans - 0x41fc160;
    struct { uint32_t rva; void* hook; void** orig; const char* n; } hs[] = {
        {0x5217644, (void*)hook_ParseNode,   (void**)&orig_ParseNode,   "ParseNode"},
        {0x5917c04, (void*)hook_RoomMapCtor, (void**)&orig_RoomMapCtor, "RoomMap.ctor"},
        {0x521970c, (void*)hook_GetNode,     (void**)&orig_GetNode,     "GetNode"},
    };
    for (auto& h : hs) {
        void* tr = nullptr;
        if (DobbyHook(g_il2Base240 + h.rva, (dobby_dummy_func_t)h.hook, (dobby_dummy_func_t*)&tr) == 0) { *h.orig = tr; BNM_FileCheckpointF("[node] hooked %s", h.n); }
        else BNM_FileCheckpointF("[node] hook FAILED %s", h.n);
    }
}

// Remote string dump: "strdump <rva> <len>" lines in a devlog upload reply.
// Same scan as StrDumpOnce, bounded to the libil2cpp mapping, results -> devlog.
static void DevlogPush(const std::string& line);
static void StrDumpRangeRemote(unsigned long long rva, unsigned long long len) {
    void* ans = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_array_new_specific");
    uint8_t* base = ans ? (uint8_t*)ans - 0x41fc160 : nullptr;
    if (!base || len > 0x10000 || rva + len > 0xadb30e0) { DevlogPush("[str] bad range"); return; }
    Il2CppClass* strCls = nullptr;
    try { strCls = Class("System", "String").GetClass(); } catch (...) {}
    if (!strCls) return;
    const uintptr_t lo = (uintptr_t)base, hi = (uintptr_t)base + 0xadb30e0;
    char hdr[64]; snprintf(hdr, sizeof hdr, "[str] == range %llx +%llx", rva, len); DevlogPush(hdr);
    uint32_t* code = (uint32_t*)(base + rva);
    uintptr_t reg[32] = {};
    for (unsigned long long i = 0; i < len / 4; i++) {
        uint32_t w = code[i];
        if ((w & 0x9F000000u) == 0x90000000u) {
            int64_t imm = (int64_t)((((w >> 5) & 0x7FFFF) << 2) | ((w >> 29) & 3));
            if (imm & (1LL << 20)) imm -= (1LL << 21);
            reg[w & 31] = (((uintptr_t)&code[i]) & ~(uintptr_t)0xFFF) + (uintptr_t)(imm << 12);
        } else if ((w & 0xFFC00000u) == 0xF9400000u) {
            uint32_t rn = (w >> 5) & 31;
            if (!reg[rn]) continue;
            uintptr_t slot = reg[rn] + ((w >> 10) & 0xFFF) * 8;
            if (slot < lo || slot + 8 > hi) continue;
            uintptr_t v = *(uintptr_t*)slot;
            if (!v || (v & 7) || v < lo || v + 8 > hi) continue;           // must point into the lib
            uintptr_t v2 = *(uintptr_t*)v;                                   // metadata var -> object
            if (!v2 || (v2 & 7)) continue;
            auto* o = (Il2CppObject*)v2;
            // the object lives on the GC heap: check its class pointer without trusting it
            if ((uintptr_t)o->klass != (uintptr_t)strCls) continue;
            std::string t = ((BNM::Structures::Mono::String*)o)->str();
            char ln[200]; snprintf(ln, sizeof ln, "[str] %llx '", rva + i * 4);
            DevlogPush(std::string(ln) + t.substr(0, 120) + "'");
        }
    }
    DevlogPush("[str] done");
}

// ---- 2.4.0 remote logs (test builds) ---------------------------------------
// Every Unity log line + exception + crash record is buffered and POSTed to
// http://live.your-domain.example/client/devlog every few seconds, so the game can
// be debugged without the headset on USB. A native crash handler writes the
// signal + pc/lr (module + offset) to crash_last.txt, uploaded on next start.
static std::mutex g_devlogMu;
static std::deque<std::string> g_devlog;
static std::atomic<bool> g_devlogOn{true};
static const char* kDevlogHost = "live.your-domain.example";
static const char* kCrashFile = "/sdcard/Android/data/com.TrassGames.Yeeps/files/crash_last.txt";
static void DevlogPush(const std::string& line) {
    if (!g_devlogOn.load()) return;
    char ts[32]; time_t t = time(nullptr); struct tm tmv; localtime_r(&t, &tmv);
    strftime(ts, sizeof ts, "%H:%M:%S ", &tmv);
    std::lock_guard<std::mutex> lk(g_devlogMu);
#ifdef BNM_LIVE_BASE
    if (g_devlog.size() > 3000) g_devlog.pop_front();    // live base: keep memory low (OOM-tight)
    g_devlog.push_back(std::string(ts) + line.substr(0, 500));
#else
    if (g_devlog.size() > 30000) g_devlog.pop_front();   // big: was 6000
    g_devlog.push_back(std::string(ts) + line.substr(0, 2000));
#endif
}
static const char* kLogTypeName[] = {"Error", "Assert", "Warning", "Log", "Exception"};
static void (*orig_InternalLog)(int, int, void*, void*, void*) = nullptr;
// big: error counters for the heartbeat, and a script stack trace for the first
// few occurrences of every distinct error / assert (Unity passes only the text here)
static std::atomic<long> g_logErrN{0}, g_logWarnN{0}, g_logExcN{0};
static std::mutex g_errSeenMu;
static std::map<std::string, int> g_errSeen;
static int g_errStacks = 0;
static std::atomic<long> g_colorKeyNull{0};
static void hook_InternalLog(int type, int opt, void* msg, void* obj, void* mi) {
    try {
        std::string m = msg ? ((BNM::Structures::Mono::String*)msg)->str() : "";
        // big: live pets' colorings have no key; 2.4 logs this every frame and shows
        // it as an error popup. Count it, report it once, don't pass it on.
        if (type == 0 && m == "Colors: Key is null") {
            if (g_colorKeyNull.fetch_add(1) == 0) DevlogPush("[petcolor] 'Colors: Key is null' silenced (live pet colorings)");
            return;
        }
        // biglive26 LOADING-FREEZE FIX: the dressing-room/permitted-patterns
        // cosmetic filter logs "Unexpected pattern type for filter: cosmetic -
        // <outfit/category>" ~1500x per rebuild, ~18k per room load. Each warning
        // pays Unity's stack-trace capture + our devlog upload -> ~160s frozen
        // (frames=0) at room load. Harmless porting warning -> count once, drop
        // it (skip devlog + Unity handler) so load is instant.
        static std::atomic<long> g_filterWarnN{0};
        if (m.rfind("Unexpected pattern type for filter", 0) == 0) {
            if (g_filterWarnN.fetch_add(1) == 0) DevlogPush("[filter] 'Unexpected pattern type for filter' silenced (cosmetic filter spam, was freezing load)");
            return;
        }
        // live89: GameAnalytics is turned off on purpose (no telemetry), so every GA call
        // logs "REMEMBER THE SDK NEEDS TO BE MANUALLY INITIALIZED NOW" (~500x/session).
        static std::atomic<long> g_gaNagN{0};
        if (m.find("GameAnalytics: REMEMBER THE SDK NEEDS TO BE MANUALLY INITIALIZED") != std::string::npos) {
            if (g_gaNagN.fetch_add(1) == 0) DevlogPush("[ga] GameAnalytics 'not initialized' nag silenced (GA off on purpose)");
            return;
        }
        if (m.find("Connect failed (") != std::string::npos)      // live83: is the CCU cap the cause?
            g_maxCcu = m.find("MaxCcuReached") != std::string::npos;
        std::string line = std::string("[") + ((type >= 0 && type <= 4) ? kLogTypeName[type] : "?") + "] " + m;
        if (type == 0 || type == 1) {
            g_logErrN++;
            bool stack = false;
            {
                std::lock_guard<std::mutex> lk(g_errSeenMu);
                int& n = g_errSeen[m.substr(0, 160)];
                if (++n <= 3 && g_errStacks < 600 && g_errSeen.size() < 5000) { stack = true; g_errStacks++; }
            }
            if (stack) {
                static Method<BNM::Structures::Mono::String*> ext(Class("UnityEngine", "StackTraceUtility", Image("UnityEngine.CoreModule.dll")).GetMethod("ExtractStackTrace", 0));
                if (ext.IsValid()) {
                    auto* st = ext();
                    if (st) {
                        std::string t = st->str().substr(0, 1800);
                        for (auto& c : t) if (c == '\n' || c == '\r') c = '|';
                        line += "\n[stack] " + t;     // one line, frames separated by |
                    }
                }
            }
        } else if (type == 2) g_logWarnN++;
        DevlogPush(line);
        // big51: the game routes every Error/Assert/Exception log into its
        // on-screen "Imagination Prompt" debug overlay (and re-logs it as
        // "LOGGING - [Error] ..."), which spams testers with popups for the
        // harmless porting warnings. We keep our own devlog record above, but
        // do NOT forward error-level logs to Unity's handler, so no popup
        // fires. Warnings (2) and plain logs (3) still flow through normally.
        if (type == 0 || type == 1 || type == 4) return;
    } catch (...) {}
    if (orig_InternalLog) orig_InternalLog(type, opt, msg, obj, mi);
}
static void (*orig_InternalLogEx)(void*, void*, void*) = nullptr;
static void hook_InternalLogEx(void* ex, void* obj, void* mi) {
    g_logExcN++;
    try { DevlogPush("[Exception] " + ExcText((Il2CppObject*)ex)); } catch (...) {}
    // big51: recorded above; don't forward to Unity so no on-screen popup.
    return;
}
// Player mods (see LoadPlayerMods): the mod running when the game crashed, or the
// mod whose code the crash is in, is written to kModsBadFile and skipped next start.
static const char* kModsBadFile = "/data/data/com.TrassGames.Yeeps/files/ymods/bad.txt";
static char g_modActive[160];          // "<file> <hash>" of the mod running right now, "" = none
static char g_modFileHash[16][160];    // "<file> <hash>" per loaded mod, matched by path below
static char g_modFilePath[16][256];
static int g_modFileN = 0;
static void ModCrashMark(uintptr_t pc, uintptr_t lr) {
    const char* who = g_modActive[0] ? g_modActive : nullptr;
    for (int i = 0; !who && i < g_modFileN; i++) {
        Dl_info d = {};
        for (uintptr_t a : {pc, lr})
            if (a && dladdr((void*)a, &d) && d.dli_fname && strcmp(d.dli_fname, g_modFilePath[i]) == 0) who = g_modFileHash[i];
    }
    if (!who) return;
    int fd = open(kModsBadFile, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd < 0) return;
    write(fd, who, strlen(who)); write(fd, "\n", 1);
    close(fd);
}
static struct sigaction g_oldSig[32];
static void CrashHandler(int sig, siginfo_t* info, void* uctx) {
    int fd = open(kCrashFile, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) {
        char buf[512];
        auto* uc = (ucontext_t*)uctx;
        uintptr_t pc = uc ? uc->uc_mcontext.pc : 0, lr = uc ? uc->uc_mcontext.regs[30] : 0;
        Dl_info a = {}, b = {};
        dladdr((void*)pc, &a); dladdr((void*)lr, &b);
        int n = snprintf(buf, sizeof buf, "signal %d addr %p build %s\npc %p %s+0x%lx\nlr %p %s+0x%lx\n",
            sig, info ? info->si_addr : nullptr, g_buildTag.c_str(),
            (void*)pc, a.dli_fname ? a.dli_fname : "?", (unsigned long)(pc - (uintptr_t)a.dli_fbase),
            (void*)lr, b.dli_fname ? b.dli_fname : "?", (unsigned long)(lr - (uintptr_t)b.dli_fbase));
        if (n > 0) write(fd, buf, n);
        // frame-pointer walk (async-signal-safe, bounded): caller chain as module+offset
        if (uc) {
            uintptr_t fp = uc->uc_mcontext.regs[29];
            for (int i = 0; i < 24 && fp && !(fp & 7); i++) {
                uintptr_t nfp = ((uintptr_t*)fp)[0], ret = ((uintptr_t*)fp)[1];
                Dl_info d = {};
                if (ret && dladdr((void*)ret, &d) && d.dli_fbase) {
                    const char* nm = d.dli_fname ? strrchr(d.dli_fname, '/') : nullptr;
                    n = snprintf(buf, sizeof buf, "#%02d %s+0x%lx\n", i, nm ? nm + 1 : "?",
                                 (unsigned long)(ret - (uintptr_t)d.dli_fbase));
                    if (n > 0) write(fd, buf, n);
                }
                if (nfp <= fp || nfp - fp > (1u << 20)) break;
                fp = nfp;
            }
        }
        close(fd);
    }
    { auto* uc = (ucontext_t*)uctx;
      ModCrashMark(uc ? uc->uc_mcontext.pc : 0, uc ? uc->uc_mcontext.regs[30] : 0); }
    sigaction(sig, &g_oldSig[sig], nullptr);   // chain to the previous handler
    raise(sig);
}
static void InstallCrashHandler() {
    {   // own signal stack: a stack overflow (deep recursion) can still be recorded
        static std::vector<char> alt(256 * 1024);
        stack_t ss = {};
        ss.ss_sp = alt.data(); ss.ss_size = alt.size(); ss.ss_flags = 0;
        sigaltstack(&ss, nullptr);
    }
    struct sigaction sa = {};
    sa.sa_sigaction = CrashHandler;
    sa.sa_flags = SA_SIGINFO | SA_ONSTACK;
    for (int sig : {SIGSEGV, SIGABRT, SIGILL, SIGBUS, SIGFPE, SIGTRAP}) sigaction(sig, &sa, &g_oldSig[sig]);   // SIGTRAP: Unity's fatal errors
}
// --- 240b16 debug extras: device info, heartbeat (fps/memory), hang watch,
// remote commands (ping, meminfo, logcat N, ls) -----------------------------
#include <sys/system_properties.h>
#include <sys/statvfs.h>
static std::string ReadSmall(const char* path, size_t cap = 4096) {
    std::string o; if (FILE* f = fopen(path, "r")) { char b[1024]; size_t n;
        while (o.size() < cap && (n = fread(b, 1, sizeof b, f)) > 0) o.append(b, n); fclose(f); }
    return o;
}
static std::string ProcStatus(const char* key) {   // e.g. "VmRSS" -> "123456 kB"
    std::istringstream in(ReadSmall("/proc/self/status", 8192)); std::string ln;
    while (std::getline(in, ln)) if (ln.compare(0, strlen(key), key) == 0) {
        size_t p = ln.find_first_not_of(" \t", strlen(key) + 1); return p == std::string::npos ? "" : ln.substr(p); }
    return "?";
}
static void DevlogDeviceInfo() {
    char model[PROP_VALUE_MAX] = {}, rel[PROP_VALUE_MAX] = {}, sdk[PROP_VALUE_MAX] = {}, fp[PROP_VALUE_MAX] = {};
    __system_property_get("ro.product.model", model);
    __system_property_get("ro.build.version.release", rel);
    __system_property_get("ro.build.version.sdk", sdk);
    __system_property_get("ro.build.version.incremental", fp);
    struct statvfs sv = {}; unsigned long long freeMB = 0;
    if (statvfs("/sdcard", &sv) == 0) freeMB = (unsigned long long)sv.f_bavail * sv.f_frsize / (1024 * 1024);
    char b[400];
    snprintf(b, sizeof b, "[dev] model=%s android=%s sdk=%s os=%s freeMB=%llu memTotal=%s",
             model, rel, sdk, fp, freeMB, ProcStatus("VmHWM").c_str());
    DevlogPush(b);
    std::string mi = ReadSmall("/proc/meminfo", 300);
    size_t e = mi.find('\n', mi.find("MemAvailable")); 
    DevlogPush("[dev] " + mi.substr(0, e == std::string::npos ? 200 : e));
}
static void DevlogRunCommand(const std::string& ln) {
    unsigned long long a = 0, b = 0; int n = 0; char arg[256] = {};
    if (sscanf(ln.c_str(), "strdump %llx %llx", &a, &b) == 2) { try { StrDumpRangeRemote(a, b); } catch (...) {} return; }
    if (ln == "ping") { DevlogPush("[cmd] pong frames=" + std::to_string(g_frames.load())); return; }
    if (ln == "meminfo") {
        DevlogPush("[cmd] rss=" + ProcStatus("VmRSS") + " hwm=" + ProcStatus("VmHWM") +
                   " threads=" + ProcStatus("Threads") + " fdsize=" + ProcStatus("FDSize"));
        return;
    }
    if (sscanf(ln.c_str(), "logcatall %d", &n) == 1) {   // all of this app's processes (earlier runs too)
        if (n < 1) n = 300; if (n > 3000) n = 3000;
        char cmd[96]; snprintf(cmd, sizeof cmd, "logcat -d -t %d -v brief 2>&1", n);
        if (FILE* p = popen(cmd, "r")) { char l[1024];
            while (fgets(l, sizeof l, p)) { size_t L = strlen(l); if (L && l[L-1] == '\n') l[L-1] = 0; DevlogPush(std::string("[logcat] ") + l); }
            pclose(p); }
        DevlogPush("[cmd] logcatall done");
        return;
    }
    if (sscanf(ln.c_str(), "logcat %d", &n) == 1) {   // this process's own logcat lines
        if (n < 1) n = 200; if (n > 3000) n = 3000;
        char cmd[96]; snprintf(cmd, sizeof cmd, "logcat -d -t %d --pid=%d 2>&1", n, (int)getpid());
        if (FILE* p = popen(cmd, "r")) { char l[1024];
            while (fgets(l, sizeof l, p)) { size_t L = strlen(l); if (L && l[L-1] == '\n') l[L-1] = 0; DevlogPush(std::string("[logcat] ") + l); }
            pclose(p); }
        DevlogPush("[cmd] logcat done");
        return;
    }
    if (sscanf(ln.c_str(), "ls %255s", arg) == 1 && strstr(arg, "..") == nullptr) {
        std::string dir = std::string("/sdcard/Android/data/com.TrassGames.Yeeps/") + arg;
        std::string cmd = "ls -la '" + dir + "' 2>&1 | head -100";
        if (FILE* p = popen(cmd.c_str(), "r")) { char l[512];
            while (fgets(l, sizeof l, p)) { size_t L = strlen(l); if (L && l[L-1] == '\n') l[L-1] = 0; DevlogPush(std::string("[ls] ") + l); }
            pclose(p); }
        return;
    }
    if (!ln.empty() && ln != "ok") DevlogPush("[cmd] unknown: " + ln.substr(0, 80));
}
static void DevlogFlush();
static void BigClassProbe();
static void DevlogUploadThread() {
    DevlogDeviceInfo();
    // big: the previous run's last logcat lines (same app uid = readable): why it died
    {
        char cmd[160];
        // crash reports only, from the whole buffer (Unity's own crash handler prints
        // "E/CRASH" + backtrace lines; the system adds F/DEBUG tombstone lines)
        snprintf(cmd, sizeof cmd, "logcat -d -v brief 2>&1 | grep -E 'CRASH|F/DEBUG|F/libc|Fatal signal|backtrace|#[0-9]+ pc|Abort message|Cause:' | tail -300");
        if (FILE* p = popen(cmd, "r")) {
            char l[700]; int n = 0;
            while (fgets(l, sizeof l, p)) {
                size_t L = strlen(l); if (L && l[L-1] == '\n') l[L-1] = 0;
                DevlogPush(std::string("[prevlog] ") + l); n++;
            }
            pclose(p);
            DevlogPush("[prevlog] end (" + std::to_string(n) + " lines)");
        }
    }
    // a crash record from the previous run goes first
    if (FILE* f = fopen(kCrashFile, "r")) {
        char b[4096]; size_t n = fread(b, 1, sizeof b - 1, f); b[n] = 0; fclose(f);
        DevlogPush(std::string("[PrevCrash] ") + b);
        remove(kCrashFile);
    }
    long ckOff = 0;   // also forward new checkpoint-file lines ([ovr], [lambda], ...)
    const char* ckPath = "/sdcard/Android/data/com.TrassGames.Yeeps/files/bnm_checkpoint.txt";
    unsigned long long hbFrames = g_frames.load(), lastFrames = hbFrames;
    auto hbAt = std::chrono::steady_clock::now();
    int stalled = 0;   // 5 s rounds with no new frame
    for (;;) {
        std::this_thread::sleep_for(std::chrono::seconds(5));
        {   // hang watch: frames stop when the game freezes (or is paused / headset off)
            unsigned long long f = g_frames.load();
            if (f == lastFrames && f > 0) {
                if (++stalled == 2) DevlogPush("[hang] no frames for 10s (freeze, or game paused)");
            } else {
                if (stalled >= 2) DevlogPush("[hang] frames resumed after ~" + std::to_string(stalled * 5) + "s");
                stalled = 0;
            }
            lastFrames = f;
            auto now = std::chrono::steady_clock::now();
            double secs = std::chrono::duration<double>(now - hbAt).count();
            if (secs >= 30 && stalled == 0) {   // heartbeat: fps + memory
                char b[256];
                snprintf(b, sizeof b, "[hb] fps=%.0f rss=%s threads=%s maxFrame=%ldms over25ms=%ld over50ms=%ld over100ms=%ld errors=%ld exceptions=%ld warnings=%ld",
                         (f - hbFrames) / secs, ProcStatus("VmRSS").c_str(), ProcStatus("Threads").c_str(),
                         g_spikeMaxMs.exchange(0), g_slow25.exchange(0), g_slow50.exchange(0), g_slow100.exchange(0),
                         g_logErrN.exchange(0), g_logExcN.exchange(0), g_logWarnN.exchange(0));
                DevlogPush(b);
                hbFrames = f; hbAt = now;
            }
        }
        if (FILE* cf = fopen(ckPath, "r")) {
            fseek(cf, 0, SEEK_END); long end = ftell(cf);
            if (end < ckOff) ckOff = 0;
            if (end > ckOff) {
                fseek(cf, ckOff, SEEK_SET);
                char ln[1024];
                while (fgets(ln, sizeof ln, cf)) {
                    size_t L = strlen(ln); if (L && ln[L-1] == '\n') ln[L-1] = 0;
                    if (strncmp(ln, "CHECKPOINT", 10) && strncmp(ln, "resolved via", 12)) DevlogPush(std::string("[ck] ") + ln);
                }
                ckOff = ftell(cf);
            }
            fclose(cf);
        }
        DevlogFlush();
    }
}
// POST everything buffered (upload thread every 5 s; the boot watch more often)
static void DevlogFlush() {
    static std::mutex flushMu;
    std::lock_guard<std::mutex> fl(flushMu);
    {
        std::string body;
        {
            std::lock_guard<std::mutex> lk(g_devlogMu);
            while (!g_devlog.empty() && body.size() < 200000) { body += g_devlog.front(); body += '\n'; g_devlog.pop_front(); }
        }
        if (body.empty()) return;
        try {
            httplib::Client cli(kDevlogHost, 80);
            cli.set_connection_timeout(10); cli.set_read_timeout(10);
            httplib::Headers h = {{"X-Build-Tag", g_buildTag}};
            auto r = cli.Post("/client/devlog", h, body, "text/plain");
            if (r && r->status == 200 && r->body.size() > 2) {   // queued remote commands
                AttachThread();
                std::istringstream cmds(r->body);
                std::string ln;
                while (std::getline(cmds, ln)) {
                    if (!ln.empty() && ln.back() == '\r') ln.pop_back();
                    try { DevlogRunCommand(ln); } catch (...) {}
                }
            }
            if (!r || r->status != 200) {   // big: put the whole batch back and retry next round
                std::lock_guard<std::mutex> lk(g_devlogMu);
                if (g_devlog.size() < 25000) {
                    std::vector<std::string> lines;
                    std::istringstream bs(body);
                    for (std::string l; std::getline(bs, l);) lines.push_back(l);
                    for (auto it = lines.rbegin(); it != lines.rend(); ++it) g_devlog.push_front(*it);
                    g_devlog.push_front("[devlog] upload failed (" + std::to_string(r ? r->status : -1) + "), retrying");
                } else g_devlog.push_front("[devlog] upload failed; batch dropped (buffer full)");
            }
        } catch (...) {}
    }
}
// big: the first minutes of a run (boot + game scene load) are where testers' games
// died without a trace - log memory every 2 s and push it out right away
static void BootWatchThread() {
    auto t0 = std::chrono::steady_clock::now();
    long lastKb = 0;
    for (int i = 0; i < 120; i++) {               // 4 minutes
        std::this_thread::sleep_for(std::chrono::seconds(2));
        long kb = atol(ProcStatus("VmRSS").c_str());
        double t = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
        if (i % 5 == 0 || labs(kb - lastKb) > 40000) {
            char b[200];
            snprintf(b, sizeof b, "[boot] t=%.0fs rss=%ldMB frames=%llu avail=%s", t, kb / 1024,
                     (unsigned long long)g_frames.load(), ProcStatus("VmSwap").c_str());
            DevlogPush(b);
            lastKb = kb;
        }
        DevlogFlush();
        if (g_frames.load() > 600) break;          // the game is running: normal logging takes over
    }
}
// big: diagnose "Colors: Key is null" / ArgumentNullException from
// PetController.SetColoring(PetManager+PetColoring) on visual-only pets: log
// which pet and what the coloring holds (first call per pet name). The argument
// may be an object or an inline struct, so all argument registers are forwarded.
static std::string BigDescribe(void* p) {
    uintptr_t v = (uintptr_t)p;
    if (!v) return "null";
    if ((v & 7) || v < 0x10000 || v > 0x8000000000ULL) { char b[32]; snprintf(b, sizeof b, "0x%llx", (unsigned long long)v); return b; }
    try {
        auto* o = (IL2CPP::Il2CppObject*)p;
        if (!o->klass || !o->klass->name) return "?";
        std::string cn = o->klass->name;
        if (cn == "String") return "\"" + ((BNM::Structures::Mono::String*)p)->str().substr(0, 60) + "\"";
        std::string r = "<" + cn;
        // object: show its first string fields
        for (int off = 0x10; off < 0x40; off += 8) {
            void* f = *(void**)((uint8_t*)p + off);
            uintptr_t fv = (uintptr_t)f;
            if (!fv) { r += " null@" + std::to_string(off); continue; }
            if ((fv & 7) || fv < 0x10000 || fv > 0x8000000000ULL) continue;
            auto* fo = (IL2CPP::Il2CppObject*)f;
            if (fo->klass && fo->klass->name && std::string(fo->klass->name) == "String")
                r += " \"" + ((BNM::Structures::Mono::String*)f)->str().substr(0, 40) + "\"@" + std::to_string(off);
        }
        return r + ">";
    } catch (...) { return "!"; }
}
static void (*orig_PetSetColoring)(void*, void*, void*, void*, void*, void*) = nullptr;
static void hook_PetSetColoring(void* self, void* a1, void* a2, void* a3, void* a4, void* a5) {
    static std::mutex mu; static std::set<std::string> seen; static int n = 0;
    try {
        std::string name = "?";
        if (self) {
            Method<IL2CPP::Il2CppObject*> go(Class("UnityEngine", "Component").GetMethod("get_gameObject", 0));
            Method<BNM::Structures::Mono::String*> nm(Class("UnityEngine", "Object").GetMethod("get_name", 0));
            auto* g = go.IsValid() ? go[(IL2CPP::Il2CppObject*)self]() : nullptr;
            auto* s = (g && nm.IsValid()) ? nm[g]() : nullptr;
            if (s) name = s->str();
        }
        bool log = false;
        { std::lock_guard<std::mutex> lk(mu); if (n < 30 && seen.insert(name).second) { n++; log = true; } }
        if (log) DevlogPush("[petcolor] " + name + " a1=" + BigDescribe(a1) + " a2=" + BigDescribe(a2) + " a3=" + BigDescribe(a3));
    } catch (...) {}
    // The live pets (puma, spider, deer, dino, turtle, alien) come with a coloring
    // whose third field (its color key) is null: 2.4 then logs "Colors: Key is null",
    // throws ArgumentNullException and shows it as a popup every frame. Skip those
    // calls (the pet keeps its default colors).
    if (!a3) {
        static std::atomic<int> skipped{0};
        if (skipped.fetch_add(1) == 0) DevlogPush("[petcolor] skipping colorings without a key (live pets)");
        return;
    }
    try {
        orig_PetSetColoring(self, a1, a2, a3, a4, a5);
    } catch (...) {
        static std::atomic<int> caught{0};
        if (caught.fetch_add(1) == 0) DevlogPush("[petcolor] exception in SetColoring caught (pet keeps default colors)");
    }
}
// big: Item.Update threw NullReferenceException / ArgumentNullException every
// frame for some ported item (error popups). Catch it here, report the item
// once by name, and keep the frame going (the rest of Update was skipped anyway).
static std::string BigGoName(void* comp) {
    try {
        Method<IL2CPP::Il2CppObject*> go(Class("UnityEngine", "Component").GetMethod("get_gameObject", 0));
        Method<BNM::Structures::Mono::String*> nm(Class("UnityEngine", "Object").GetMethod("get_name", 0));
        auto* g = (comp && go.IsValid()) ? go[(IL2CPP::Il2CppObject*)comp]() : nullptr;
        auto* s = (g && nm.IsValid()) ? nm[g]() : nullptr;
        return s ? s->str() : "?";
    } catch (...) { return "?"; }
}
static void (*orig_ItemUpdate)(void*, void*) = nullptr;
static void hook_ItemUpdate(void* self, void* mi) {
    try {
        orig_ItemUpdate(self, mi);
    } catch (...) {
        static std::mutex mu; static std::set<std::string> seen;
        std::string n = BigGoName(self);
        bool first;
        { std::lock_guard<std::mutex> lk(mu); first = seen.insert(n).second && seen.size() < 50; }
        if (first) DevlogPush("[itemupd] exception in Item.Update of " + n + " (silenced)");
    }
}
static void InstallBigItemUpdateGuard() {
#ifdef BNM_LIVE_BASE
    return;   // live 2.31.4 base: the real game handles this natively; this 2.4-era hook crashes on the live binary (e.g. eating an item)
#endif
    try {
        MethodBase m = Class("", "Item", Image("Assembly-CSharp.dll")).GetMethod("Update", 0);
        if (m.IsValid()) { BasicHook(m, hook_ItemUpdate, orig_ItemUpdate); DevlogPush("[itemupd] Item.Update guarded"); }
        else DevlogPush("[itemupd] Item.Update NOT FOUND");
    } catch (...) { DevlogPush("[itemupd] EXCEPTION"); }
}

// big: ColorManager logs "Colors: Key is null" and then looks the null key up in
// its Dictionary -> ArgumentNullException (hundreds of popups from live pets).
// Guard its string-keyed lookups: a null key returns "not found" right away.
struct BigColor { float r, g, b, a; };
static bool (*orig_ColTryI0)(void*, void*, void*, void*) = nullptr;
static bool hook_ColTryI0(void* self, void* key, void* out, void* mi) { return key ? orig_ColTryI0(self, key, out, mi) : false; }
static bool (*orig_ColTryI1)(void*, void*, void*, void*) = nullptr;
static bool hook_ColTryI1(void* self, void* key, void* out, void* mi) { return key ? orig_ColTryI1(self, key, out, mi) : false; }
static bool (*orig_ColTryS0)(void*, void*, void*) = nullptr;
static bool hook_ColTryS0(void* key, void* out, void* mi) { return key ? orig_ColTryS0(key, out, mi) : false; }
static bool (*orig_ColTryS1)(void*, void*, void*) = nullptr;
static bool hook_ColTryS1(void* key, void* out, void* mi) { return key ? orig_ColTryS1(key, out, mi) : false; }
static BigColor (*orig_ColGetI0)(void*, void*, void*) = nullptr;
static BigColor hook_ColGetI0(void* self, void* key, void* mi) { return key ? orig_ColGetI0(self, key, mi) : BigColor{1, 1, 1, 1}; }
static BigColor (*orig_ColGetI1)(void*, void*, void*) = nullptr;
static BigColor hook_ColGetI1(void* self, void* key, void* mi) { return key ? orig_ColGetI1(self, key, mi) : BigColor{1, 1, 1, 1}; }
static BigColor (*orig_ColGetS0)(void*, void*) = nullptr;
static BigColor hook_ColGetS0(void* key, void* mi) { return key ? orig_ColGetS0(key, mi) : BigColor{1, 1, 1, 1}; }
static BigColor (*orig_ColGetS1)(void*, void*) = nullptr;
static BigColor hook_ColGetS1(void* key, void* mi) { return key ? orig_ColGetS1(key, mi) : BigColor{1, 1, 1, 1}; }
static void InstallBigColorGuard() {
#ifdef BNM_LIVE_BASE
    return;   // live 2.31.4 base: the real game handles this natively; this 2.4-era hook crashes on the live binary (e.g. eating an item)
#endif
    try {
        Class cm("", "ColorManager", Image("Assembly-CSharp.dll"));
        if (!cm.IsValid()) { DevlogPush("[colorguard] ColorManager NOT FOUND"); return; }
        int ti = 0, ts = 0, gi = 0, gs = 0;
        std::string done;
        for (auto& m : cm.GetMethods(false)) {
            auto* mi = m.GetInfo();
            if (!m.IsValid() || !mi || !mi->parameters_count || !mi->parameters || !mi->return_type) continue;
            if (typeName(mi->parameters[0]) != "System.String") continue;
            std::string rt = typeName(mi->return_type);
            bool isStatic = (mi->flags & 0x10) != 0;
            int np = (int)mi->parameters_count;
            std::string nm = mi->name ? mi->name : "?";
            if (rt == "System.Boolean" && np == 2) {
                if (!isStatic && ti == 0) { BasicHook(m, hook_ColTryI0, orig_ColTryI0); ti++; done += nm + "(bool,i) "; }
                else if (!isStatic && ti == 1) { BasicHook(m, hook_ColTryI1, orig_ColTryI1); ti++; done += nm + "(bool,i) "; }
                else if (isStatic && ts == 0) { BasicHook(m, hook_ColTryS0, orig_ColTryS0); ts++; done += nm + "(bool,s) "; }
                else if (isStatic && ts == 1) { BasicHook(m, hook_ColTryS1, orig_ColTryS1); ts++; done += nm + "(bool,s) "; }
            } else if (rt == "UnityEngine.Color" && np == 1) {
                if (!isStatic && gi == 0) { BasicHook(m, hook_ColGetI0, orig_ColGetI0); gi++; done += nm + "(color,i) "; }
                else if (!isStatic && gi == 1) { BasicHook(m, hook_ColGetI1, orig_ColGetI1); gi++; done += nm + "(color,i) "; }
                else if (isStatic && gs == 0) { BasicHook(m, hook_ColGetS0, orig_ColGetS0); gs++; done += nm + "(color,s) "; }
                else if (isStatic && gs == 1) { BasicHook(m, hook_ColGetS1, orig_ColGetS1); gs++; done += nm + "(color,s) "; }
            }
        }
        DevlogPush("[colorguard] hooked: " + done);
    } catch (...) { DevlogPush("[colorguard] EXCEPTION"); }
}

static void InstallBigPetDiag() {
#ifdef BNM_LIVE_BASE
    return;   // live 2.31.4 base: the real game handles this natively; this 2.4-era hook crashes on the live binary (e.g. eating an item)
#endif
    // the class name in the stack was cut ("...ontroller.SetColoring"): find it
    try {
        Image img("Assembly-CSharp.dll");
        if (!img.IsValid()) { DevlogPush("[petcolor] no Assembly-CSharp image"); return; }
        std::string all;
        int hooked = 0;
        for (auto& cls : img.GetClasses(false)) {
            auto* k = cls.GetClass();
            if (!cls.IsValid() || !k || !k->name) continue;
            MethodBase m = cls.GetMethod("SetColoring", 1);
            if (!m.IsValid()) continue;
            std::string cn = k->name;
            all += cn + " ";
            if (!hooked && cn.size() >= 10 && cn.compare(cn.size() - 10, 10, "Controller") == 0) {
                BasicHook(m, hook_PetSetColoring, orig_PetSetColoring);
                DevlogPush("[petcolor] hooked " + cn + ".SetColoring");
                hooked++;
            }
        }
        DevlogPush("[petcolor] classes with SetColoring(1): " + all);
    } catch (...) { DevlogPush("[petcolor] hook EXCEPTION"); }
}

// live89 (owner 2026-10-31: "fix the error messages that pop up"): the game's logger
// subscribes to Application.logMessageReceived and shows every Error / Assert /
// Exception as an on-screen prompt. Managed Debug.LogError is already dropped in
// hook_InternalLog, but engine-native errors (e.g. SoundChannel.cpp) and unhandled
// exceptions from the player loop go straight to Application.CallLogCallback. Record
// them in our devlog (first 3 of each) and don't hand them to subscribers -> no popup.
// Warnings and normal logs still flow through.
static void (*orig_CallLogCb)(void*, void*, int, bool, void*) = nullptr;
static std::atomic<long> g_cbDropN{0};
static void hook_CallLogCb(void* msg, void* st, int type, bool mainThread, void* mi) {
    if (type == 0 || type == 1 || type == 4) {
        g_cbDropN++;
        try {
            std::string m = msg ? ((BNM::Structures::Mono::String*)msg)->str() : "";
            static std::mutex mu; static std::map<std::string, int> seen;
            bool push = false;
            { std::lock_guard<std::mutex> lk(mu); if (seen.size() < 2000 && ++seen[m.substr(0, 160)] <= 3) push = true; }
            if (push) {
                std::string t = st ? ((BNM::Structures::Mono::String*)st)->str().substr(0, 600) : "";
                for (auto& c : t) if (c == '\n' || c == '\r') c = '|';
                DevlogPush(std::string("[popup-blocked ") + (type == 4 ? "Exception" : type == 1 ? "Assert" : "Error") + "] " + m.substr(0, 400)
                           + (t.empty() ? "" : "\n[stack] " + t));
            }
        } catch (...) {}
        return;
    }
    if (orig_CallLogCb) orig_CallLogCb(msg, st, type, mainThread, mi);
}

static void InstallRemoteLogs() {
    try {
        Class dl("UnityEngine", "DebugLogHandler", Image("UnityEngine.CoreModule.dll"));
        MethodBase m = dl.IsValid() ? dl.GetMethod("Internal_Log", 4) : MethodBase();
        if (m.IsValid()) { BasicHook(m, hook_InternalLog, orig_InternalLog); BNM_FileCheckpoint("[devlog] Internal_Log hooked"); }
        else BNM_FileCheckpoint("[devlog] DebugLogHandler.Internal_Log NOT FOUND");
        MethodBase e = dl.IsValid() ? dl.GetMethod("Internal_LogException", 2) : MethodBase();
        if (e.IsValid()) { BasicHook(e, hook_InternalLogEx, orig_InternalLogEx); BNM_FileCheckpoint("[devlog] Internal_LogException hooked"); }
        Class app("UnityEngine", "Application", Image("UnityEngine.CoreModule.dll"));
        MethodBase clc = app.IsValid() ? app.GetMethod("CallLogCallback", 4) : MethodBase();
        if (clc.IsValid()) { BasicHook(clc, hook_CallLogCb, orig_CallLogCb); BNM_FileCheckpoint("[devlog] Application.CallLogCallback hooked (error popups off)"); }
        else BNM_FileCheckpoint("[devlog] Application.CallLogCallback NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("[devlog] EXCEPTION"); }
    InstallCrashHandler();
    DevlogPush("[devlog] started build " + g_buildTag);
    std::thread(DevlogUploadThread).detach();
    std::thread(BootWatchThread).detach();
    InstallBigPetDiag();
    InstallBigItemUpdateGuard();
    InstallBigColorGuard();
#ifndef BNM_LIVE_BASE
    // The class-layout probe iterates every il2cpp class and builds large
    // strings; on the memory-tight live base (the full 2.31.4 scene already
    // uses ~1.6GB at load) that extra pressure contributes to OOM crashes at
    // scene load, so it's off for the live build.
    std::thread([]() {                             // class probe at boot (not tied to the game scene)
        std::this_thread::sleep_for(std::chrono::seconds(3));
        try {
            AttachThread();
            std::string mark = std::string("/data/data/com.TrassGames.Yeeps/files/big_probe_") + kBuildId;
            if (access(mark.c_str(), F_OK) != 0) {
                BigClassProbe();
                if (FILE* f = fopen(mark.c_str(), "w")) fclose(f);
            } else DevlogPush("[cls] already probed on this install");
        } catch (...) { DevlogPush("[cls] probe thread EXCEPTION"); }
    }).detach();
#endif
}


// ---- News board buttons (ported from 131b11) -----------------------------
// ActiveNewsDisplay's UnityEvent targets keep their real names in 2.4 (UnityEvents
// call by name). Discord -> our server's invite. 1.31 sends "Leave a review" to
// the BNM Explorer mod menu on :8080; 2.4 does not run that server (it lets
// anyone on the same network invoke game methods), so the button opens the
// 2.4 download page instead.
static const char* kDiscordInvite = "https://discord.gg/SvaTcWcff9";
static const char* kReviewUrl = "http://localhost:8080";   // "leave a review" button -> the built mod menu (BNM Explorer on 127.0.0.1:8080)
static void OpenUrl(const char* url) {
    try {
        Method<void> o(FindOverload(Class("UnityEngine", "Application"), "OpenURL", {"System.String"}));
        if (o.IsValid()) o(CreateMonoString(url));
        BNM_FileCheckpointF("Buttons: OpenURL %s (%d)", url, (int)o.IsValid());
    } catch (...) { BNM_FileCheckpoint("Buttons: OpenURL EXCEPTION"); }
}
static void (*orig_OpenDiscord)(void*, void*) = nullptr;
static void hook_OpenDiscord(void*, void*) { OpenUrl(kDiscordInvite); }
static void (*orig_OpenReview)(void*, void*) = nullptr;
static void hook_OpenReview(void*, void*) { OpenUrl(kReviewUrl); }
// Live (owner 2026-10-28): hook the "open the built mod menu" (leave a review) button's
// own handler too, so it always opens the mod menu even if the game doesn't go through
// Application.OpenURL with a store link (no press was ever logged via the redirect).
static void InstallReviewButton() {
    try {
        Class nd("", "ActiveNewsDisplay", Image("Assembly-CSharp.dll"));
        MethodBase r = nd.IsValid() ? nd.GetMethod("OnLeaveReviewLinkButtonPressed", 0) : MethodBase();
        if (r.IsValid()) BasicHook(r, hook_OpenReview, orig_OpenReview);
        BNM_FileCheckpointF("[btn] review button hook=%d (class %d)", r.IsValid(), nd.IsValid());
    } catch (...) { BNM_FileCheckpoint("[btn] review button hook EXCEPTION"); }
}
static void InstallNewsButtons() {
    try {
        Class nd("", "ActiveNewsDisplay", Image("Assembly-CSharp.dll"));
        MethodBase d = nd.IsValid() ? nd.GetMethod("OnOpenDiscordLinkButtonPressed", 0) : MethodBase();
        MethodBase r = nd.IsValid() ? nd.GetMethod("OnLeaveReviewLinkButtonPressed", 0) : MethodBase();
        if (d.IsValid()) BasicHook(d, hook_OpenDiscord, orig_OpenDiscord);
        if (r.IsValid()) BasicHook(r, hook_OpenReview, orig_OpenReview);
        BNM_FileCheckpointF("Buttons: discord hook=%d review hook=%d", d.IsValid(), r.IsValid());
    } catch (...) { BNM_FileCheckpoint("Buttons: EXCEPTION installing"); }
}

// ---- Catch-all button redirect (biglive27) -------------------------------
// Instead of hooking the news-display press methods (which miss any other place
// the game links out), hook the two primitives every such button ends up using:
//   1) UnityEngine.Application.OpenURL(string): any Discord link -> OUR invite;
//      the Meta store / "leave a review" link -> the built mod menu on :8080.
//   2) TMPro.TMP_Text.set_text(string): relabel the "Leave a review to help us
//      grow!" button to "open the built mod menu".
static const char* kModMenuLabel = "open the built mod menu";
static void (*orig_AppOpenURL)(void*, IL2CPP::MethodInfo*) = nullptr;   // static method: (string, mi)
static void hook_AppOpenURL(void* urlObj, IL2CPP::MethodInfo* mi) {
    void* pass = urlObj;
    try {
        std::string u = urlObj ? ((BNM::Structures::Mono::String*)urlObj)->str() : "";
        std::string lo = u; for (auto& c : lo) c = (char)tolower((unsigned char)c);
        if (lo.find("discord") != std::string::npos) {
            pass = CreateMonoString(kDiscordInvite);
            BNM_FileCheckpointF("[btn] OpenURL discord '%s' -> %s", u.substr(0,60).c_str(), kDiscordInvite);
        } else if (lo.find("meta.com") != std::string::npos || lo.find("oculus.com") != std::string::npos ||
                   lo.find("/experiences/") != std::string::npos || u.find("7276525889052788") != std::string::npos) {
            pass = CreateMonoString(kReviewUrl);
            BNM_FileCheckpointF("[btn] OpenURL review '%s' -> %s", u.substr(0,60).c_str(), kReviewUrl);
        }
    } catch (...) {}
    if (orig_AppOpenURL) orig_AppOpenURL(pass, mi);
}
static void (*orig_TMPSetText)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void hook_TMPSetText(void* self, void* textObj, IL2CPP::MethodInfo* mi) {
    void* pass = textObj;
    try {
        if (textObj) {
            std::string t = ((BNM::Structures::Mono::String*)textObj)->str();
            if (t.rfind("Leave a review to help us grow", 0) == 0) {
                pass = CreateMonoString(kModMenuLabel);
                BNM_FileCheckpoint("[btn] relabel 'Leave a review...' -> 'open the built mod menu'");
            }
        }
    } catch (...) {}
    if (orig_TMPSetText) orig_TMPSetText(self, pass, mi);
}
static void InstallButtonRedirect() {
    try {
        MethodBase o = FindOverload(Class("UnityEngine", "Application"), "OpenURL", {"System.String"});
        if (o.IsValid()) { BasicHook(o, hook_AppOpenURL, orig_AppOpenURL); BNM_FileCheckpoint("[btn] Application.OpenURL hooked"); }
        else BNM_FileCheckpoint("[btn] Application.OpenURL NOT FOUND");
        Class tmp("TMPro", "TMP_Text");
        MethodBase st = tmp.IsValid() ? tmp.GetMethod("set_text", 1) : MethodBase();
        if (st.IsValid()) { BasicHook(st, hook_TMPSetText, orig_TMPSetText); BNM_FileCheckpoint("[btn] TMP_Text.set_text hooked (relabel)"); }
        else BNM_FileCheckpoint("[btn] TMP_Text.set_text NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("[btn] InstallButtonRedirect EXCEPTION"); }
}

// =====================================================================
//  FEVER DREAM effects (room-gated, LIVE-BASE safe).
//
//  Drop this block into apk-work/big/bnm-src-big/.../native-lib.cpp,
//  ABOVE OnLoaded(). It only uses stable by-name UnityEngine calls
//  (RenderSettings fog + ambient light), all on the main thread, all in
//  try/catch. It installs its OWN per-frame tick on AccountOnlineManager
//  .Update (the feed tick is disabled on live; this one does NO material
//  or audio hooks, so it avoids the crash that disabled the feed tick).
//
//  Integration (3 edits):
//   1) paste this whole block above OnLoaded().
//   2) in hook_GetHasPerm(...), right after `std::string rk = ...;`, add:
//          FeverSetRoom(rk);
//      (so entering/leaving the fever room flips the effect).
//   3) in OnLoaded(), under the BNM_LIVE_BASE path, add a call:
//          InstallFeverEffects();
//
//  Gate: effects run only while the local player's current room is
//  kFeverRoom. Nothing happens in any other room, so this is safe to ship.
// =====================================================================
static const char* kFeverRoom = "fever_dream_1";

static std::atomic<bool> g_inFever{false};
static double g_feverSince = 0.0;
static std::atomic<bool> g_feverCaptured{false};
// captured room defaults, restored on leave
static bool  g_sv_fog = false;
static int   g_sv_fogMode = 1;
static float g_sv_fogDensity = 0.0f;
static Color g_sv_fogColor = {0, 0, 0, 1};
static int   g_sv_ambMode = 0;
static Color g_sv_ambLight = {0, 0, 0, 1};

static void FeverSetRoom(const std::string& rk) {
    bool now = (rk == kFeverRoom);
    bool was = g_inFever.load();
    if (now && !was) { g_feverSince = nowSeconds(); }
    g_inFever.store(now);
}

// --- tiny HSV->RGB (h,s,v in 0..1) -----------------------------------
static Color HsvColor(float h, float s, float v, float a = 1.f) {
    h = h - floorf(h);
    float r = 0, g = 0, b = 0;
    int i = (int)(h * 6.f);
    float f = h * 6.f - i;
    float p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s);
    switch (i % 6) {
        case 0: r = v; g = t; b = p; break;
        case 1: r = q; g = v; b = p; break;
        case 2: r = p; g = v; b = t; break;
        case 3: r = p; g = q; b = v; break;
        case 4: r = t; g = p; b = v; break;
        default: r = v; g = p; b = q; break;
    }
    return Color(r, g, b, a);
}

// Cache the RenderSettings setters (static class).
struct FeverRS {
    MethodBase setFog, getFog, setFogMode, getFogMode, setFogCol, getFogCol,
               setFogDen, getFogDen, setAmbMode, getAmbMode, setAmbLight, getAmbLight;
    bool ready = false;
    void init() {
        if (ready) return;
        Class rs("UnityEngine", "RenderSettings");
        if (!rs.IsValid()) return;
        setFog = rs.GetMethod("set_fog", 1);      getFog = rs.GetMethod("get_fog", 0);
        setFogMode = rs.GetMethod("set_fogMode", 1); getFogMode = rs.GetMethod("get_fogMode", 0);
        setFogCol = rs.GetMethod("set_fogColor", 1); getFogCol = rs.GetMethod("get_fogColor", 0);
        setFogDen = rs.GetMethod("set_fogDensity", 1); getFogDen = rs.GetMethod("get_fogDensity", 0);
        setAmbMode = rs.GetMethod("set_ambientMode", 1); getAmbMode = rs.GetMethod("get_ambientMode", 0);
        setAmbLight = rs.GetMethod("set_ambientLight", 1); getAmbLight = rs.GetMethod("get_ambientLight", 0);
        ready = setFog.IsValid() && setFogCol.IsValid();
    }
};
static FeverRS g_rs;

static void FeverCapture() {
    if (g_feverCaptured.load()) return;
    try {
        if (g_rs.getFog.IsValid())      g_sv_fog = Method<bool>(g_rs.getFog)();
        if (g_rs.getFogMode.IsValid())  g_sv_fogMode = Method<int>(g_rs.getFogMode)();
        if (g_rs.getFogDen.IsValid())   g_sv_fogDensity = Method<float>(g_rs.getFogDen)();
        if (g_rs.getFogCol.IsValid())   g_sv_fogColor = Method<Color>(g_rs.getFogCol)();
        if (g_rs.getAmbMode.IsValid())  g_sv_ambMode = Method<int>(g_rs.getAmbMode)();
        if (g_rs.getAmbLight.IsValid()) g_sv_ambLight = Method<Color>(g_rs.getAmbLight)();
        g_feverCaptured.store(true);
        BNM_FileCheckpoint("Fever: captured room render defaults");
    } catch (...) { BNM_FileCheckpoint("Fever: capture EXCEPTION"); }
}

static void FeverRestore() {
    if (!g_feverCaptured.load()) return;
    try {
        if (g_rs.setFog.IsValid())      Method<void>(g_rs.setFog)((bool)g_sv_fog);
        if (g_rs.setFogMode.IsValid())  Method<void>(g_rs.setFogMode)((int)g_sv_fogMode);
        if (g_rs.setFogDen.IsValid())   Method<void>(g_rs.setFogDen)((float)g_sv_fogDensity);
        if (g_rs.setFogCol.IsValid())   Method<void>(g_rs.setFogCol)(g_sv_fogColor);
        if (g_rs.setAmbMode.IsValid())  Method<void>(g_rs.setAmbMode)((int)g_sv_ambMode);
        if (g_rs.setAmbLight.IsValid()) Method<void>(g_rs.setAmbLight)(g_sv_ambLight);
    } catch (...) {}
    g_feverCaptured.store(false);
}

// Is the local head high up in the fall shaft? (shaft y 10..184 -> world y
// ~= gy*0.5. Above ~y 55 m we're falling.) Returns 0..1 "fall intensity".
static float FeverFallAmount() {
    try {
        Il2CppObject* ct = HeadTransform();
        if (!ct) return 0.f;
        Vector3 p = Method<Vector3>(Class("UnityEngine", "Transform").GetMethod("get_position", 0))[ct]();
        float worldY = p.y;                 // metres
        // shaft spans ~y 5..92 m; treat >30 m as "in the fall"
        if (worldY < 28.f) return 0.f;
        float a = (worldY - 28.f) / 60.f;
        return a < 0 ? 0.f : (a > 1 ? 1.f : a);
    } catch (...) { return 0.f; }
}

static void FeverTick() {
    g_rs.init();
    if (!g_rs.ready) return;
    if (!g_inFever.load()) { if (g_feverCaptured.load()) FeverRestore(); return; }
    FeverCapture();
    double t = nowSeconds() - g_feverSince;
    float fall = FeverFallAmount();

    // Fog: exponential, slowly breathing. Denser in the fall.
    float breathe = 0.5f + 0.5f * sinf((float)t * 0.45f);        // 0..1, ~14 s cycle
    float density = 0.030f + 0.022f * breathe + 0.055f * fall;   // maze thick-ish; shaft thicker
    // Hue drift yellow(0.14)->pink(0.92)->teal(0.5), slow in maze, fast in fall.
    float hz = 0.012f + 0.10f * fall;
    float hue = 0.14f + 0.5f + 0.5f * sinf((float)t * hz * 6.2832f);  // wanders
    float flick = fall > 0.01f ? (0.85f + 0.15f * sinf((float)t * 23.f)) : 1.0f;  // shaft strobe
    Color fogC = HsvColor(hue, fall > 0.01f ? 0.85f : 0.45f, (0.62f + 0.12f * breathe) * flick);
    Color ambC = HsvColor(hue + 0.08f, fall > 0.01f ? 0.70f : 0.35f, 0.55f * flick);
    try {
        Method<void>(g_rs.setFog)((bool)true);
        if (g_rs.setFogMode.IsValid()) Method<void>(g_rs.setFogMode)((int)2);   // Exponential
        Method<void>(g_rs.setFogDen)((float)density);
        Method<void>(g_rs.setFogCol)(fogC);
        if (g_rs.setAmbMode.IsValid()) Method<void>(g_rs.setAmbMode)((int)3);    // Flat/colour
        if (g_rs.setAmbLight.IsValid()) Method<void>(g_rs.setAmbLight)(ambC);
    } catch (...) {}
}

static void (*orig_FeverTickUpdate)(void*, void*) = nullptr;
static void hook_FeverTickUpdate(void* self, void* mi) {
    if (orig_FeverTickUpdate) orig_FeverTickUpdate(self, mi);
    try { FeverTick(); } catch (...) {}
}

static void InstallFeverEffects() {
    try {
        Class aom("", "AccountOnlineManager", Image("Assembly-CSharp.dll"));
        MethodBase up = aom.IsValid() ? aom.GetMethod("Update", 0) : MethodBase();
        if (up.IsValid()) {
            BasicHook(up, hook_FeverTickUpdate, orig_FeverTickUpdate);
            BNM_FileCheckpoint("Fever: tick hooked (AccountOnlineManager.Update)");
        } else {
            BNM_FileCheckpoint("Fever: AccountOnlineManager.Update NOT FOUND");
        }
    } catch (...) { BNM_FileCheckpoint("Fever: install EXCEPTION"); }
}


// --- Creative-mode grief lock (biglive24, owner 2026-10-13: "make sure all maps are
// mode locked ... people are setting maps to creative mode and griefing"). Creative is
// the Photon room property "creativeMode"; ANY client can set it (game UI, a modded
// client, or the mod menu's Photon call), and it breaks the map live for everyone in the
// room. Server isModeLocked alone doesn't stop the runtime prop, so every client flips
// creativeMode straight back OFF in a room where creative isn't allowed. Allowed: private
// worlds (p_), the Wilds (wild*), the Creative Mode Classroom (official_4b). ~4x/sec on
// the AccountOnlineManager.Update tick. Uses the same prop the game reads, so a flip is
// authoritative for the whole room.
// Owner 2026-10-17: "make it so my account only can set maps to creative".
// Creative stays on only when the owner turned it on: the owner's own client tags
// the room with creativeBy=<owner account> as soon as it sees creative on; every
// other client reverts creative (after a 2 s grace for that tag to arrive) unless
// the tag is there. When creative goes off the tag is cleared.
static const char* kCreativeOwner = "o_OWNER0000000001";
static void CreativeGuardTick(double now) {
    static double nextCheck = 0, lastRevert = 0, firstOn = 0; static int logged = 0;
    if (now < nextCheck) return; nextCheck = now + 0.25;
    static BNM::Class pn("Photon.Pun", "PhotonNetwork", Image("PhotonUnityNetworking.dll"));
    static BNM::Class roomInfo("Photon.Realtime", "RoomInfo", Image("PhotonRealtime.dll"));
    static BNM::Class roomC("Photon.Realtime", "Room", Image("PhotonRealtime.dll"));
    static BNM::Class playerC("Photon.Realtime", "Player", Image("PhotonRealtime.dll"));
    static BNM::Class htC("ExitGames.Client.Photon", "Hashtable", Image("Photon3Unity3D.dll"));
    if (!pn.IsValid() || !roomInfo.IsValid() || !roomC.IsValid() || !htC.IsValid() || !playerC.IsValid()) return;
    static Method<Il2CppObject*> curRoom(pn.GetMethod("get_CurrentRoom", 0));
    static Method<Il2CppObject*> localPlayer(pn.GetMethod("get_LocalPlayer", 0));
    static Method<Il2CppObject*> userId(playerC.GetMethod("get_UserId", 0));
    static Method<Il2CppObject*> getProps(roomInfo.GetMethod("get_CustomProperties", 0));
    static Method<Il2CppObject*> htGet(htC.GetMethod("get_Item", 1));
    if (!curRoom.IsValid() || !getProps.IsValid() || !htGet.IsValid()) return;
    Il2CppObject* room = curRoom(); if (!room) { firstOn = 0; return; }
    Il2CppObject* props = getProps[room](); if (!props) return;
    auto* v = htGet[props]((Il2CppObject*)CreateMonoString("creativeMode"));
    bool on = false;
    if (v && v->klass && v->klass->name) {
        std::string tn = v->klass->name;
        const char* data = (const char*)v + sizeof(Il2CppObject);
        on = tn == "Boolean" ? *(const uint8_t*)data != 0 : tn == "Int32" ? *(const int32_t*)data != 0 : false;
    }
    std::string by = MonoStr(htGet[props]((Il2CppObject*)CreateMonoString("creativeBy")));
    static Method<void> htCtor(htC.GetMethod(".ctor", 0));
    static Method<void> htSet(htC.GetMethod("set_Item", 2));
    static Method<bool> setProps(roomC.GetMethod("SetCustomProperties", 3));
    if (!p_object_new || !htCtor.IsValid() || !htSet.IsValid() || !setProps.IsValid()) return;
    auto setProp = [&](const char* k, Il2CppObject* val) {
        auto* ht = (Il2CppObject*)p_object_new(htC.GetClass()); if (!ht) return;
        htCtor[ht]();
        htSet[ht]((Il2CppObject*)CreateMonoString(k), val);
        setProps[room](ht, (Il2CppObject*)nullptr, (Il2CppObject*)nullptr);
    };
    if (!on) {
        firstOn = 0;
        if (!by.empty() && now - lastRevert > 1.0) { lastRevert = now; setProp("creativeBy", (Il2CppObject*)CreateMonoString("")); }
        return;
    }
    if (IsCreativeBuilder(by)) return;                      // a map builder turned it on
    {   // owner 2026-10-17: creative stays free in private worlds, the Wilds and the
        // Creative Classroom - the owner-only lock is for every other map
        std::string rk0 = MonoStr(htGet[props]((Il2CppObject*)CreateMonoString("roomKey")));
        if (rk0.rfind("p_", 0) == 0 || rk0.rfind("wild", 0) == 0 || rk0 == "official_4b") return;
    }
    std::string me;
    if (localPlayer.IsValid() && userId.IsValid()) {
        Il2CppObject* lp = localPlayer();
        if (lp) me = MonoStr(userId[lp]());
    }
    if (IsCreativeBuilder(me)) {                            // I am a builder: tag it as mine
        if (now - lastRevert > 1.0) { lastRevert = now; setProp("creativeBy", (Il2CppObject*)CreateMonoString(me.c_str())); }
        return;
    }
    if (firstOn == 0) { firstOn = now; return; }
    if (now - firstOn < 2.0 || now - lastRevert < 1.0) return;   // grace for the owner's tag
    lastRevert = now;
    if (!p_value_box || !v) return;
    int64_t zero = 0;
    setProp("creativeMode", (Il2CppObject*)p_value_box(v->klass, &zero));
    std::string rk = MonoStr(htGet[props]((Il2CppObject*)CreateMonoString("roomKey")));
    if (logged++ < 20) BNM_FileCheckpointF("[creative] flipped creative OFF in %s (owner-only creative)", rk.c_str());
}
static void (*orig_CreativeUpdate)(void*, void*) = nullptr;
static void hook_CreativeUpdate(void* self, void* mi) {
    if (orig_CreativeUpdate) orig_CreativeUpdate(self, mi);
    try { CreativeGuardTick(nowSeconds()); } catch (...) {}
}
static void InstallCreativeGuard() {
    p_object_new = (void*(*)(void*))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_object_new");
    p_value_box  = (void*(*)(void*, void*))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_value_box");
    Class aom("", "AccountOnlineManager", Image("Assembly-CSharp.dll"));
    MethodBase up = aom.IsValid() ? aom.GetMethod("Update", 0) : MethodBase();
    if (up.IsValid()) { BasicHook(up, hook_CreativeUpdate, orig_CreativeUpdate);
                        BNM_FileCheckpoint("[creative] grief-lock tick hooked (AccountOnlineManager.Update)"); }
    else BNM_FileCheckpoint("[creative] AccountOnlineManager.Update NOT FOUND");
}

// ---- Player mods (owner 2026-10-29) ------------------------------------------
// The game ships only BNM; players add mods themselves. A mod is a .so built with
// the yeeps-mods SDK (github.com/Blazeisodd/Blazes-Apk-mod-menu-addon) and dropped in
// Android/data/com.TrassGames.Yeeps/files/mods/ (SideQuest). At startup each one is
// copied to private storage (Android won't run code from shared storage), dlopen'd
// and handed the YeepsModApi (Include/yeeps_mod_api.h). Safety, no matter what a
// mod does:
//  - a mod that crashes the game is listed in ymods/bad.txt and skipped on the next
//    start (until its file changes);
//  - a mod whose per-frame update keeps taking > 4 ms, or keeps throwing, gets its
//    updates turned off;
//  - a file named DISABLE in the mods folder turns every mod off;
//  - every loaded mod (file + hash) goes to the devlog.
#include "Include/yeeps_mod_api.h"
static const char* kModsDir  = "/sdcard/Android/data/com.TrassGames.Yeeps/files/mods";
static const char* kModsPriv = "/data/data/com.TrassGames.Yeeps/files/ymods";
static const int kMaxMods = 16;
struct PlayerMod {
    std::string file, tag;            // tag = "<file> <hash>"
    std::vector<YeepsUpdateFn> updates;
    std::vector<YeepsEventFn> roomFns;
    int slowFrames = 0, errors = 0;
    bool updatesOff = false;
};
static std::vector<PlayerMod> g_playerMods;
static int g_modInitIdx = -1;         // mod inside YeepsMod_Init right now
static YeepsModApi g_modApi = {};
static std::string g_modGameVersion;

static int ModOwnerOf(void* fn) {      // which mod's code a callback lives in
    if (g_modInitIdx >= 0) return g_modInitIdx;
    Dl_info d = {};
    if (!fn || !dladdr(fn, &d) || !d.dli_fname) return -1;
    for (int i = 0; i < g_modFileN && i < (int)g_playerMods.size(); i++)
        if (strcmp(d.dli_fname, g_modFilePath[i]) == 0) return i;
    return -1;
}
static void* ModApi_resolve(const char* name) {
    return name ? BNM_dlsym(BNM::Internal::il2cppLibraryHandle, name) : nullptr;
}
static int ModApi_hook(void* target, void* repl, void** orig) {
    if (!target || !repl) return -1;
    return DobbyHook(target, (dobby_dummy_func_t)repl, (dobby_dummy_func_t*)orig) == 0 ? 0 : -1;
}
static int ModApi_unhook(void* target) { return target && DobbyDestroy(target) == 0 ? 0 : -1; }
static void ModApi_onUpdate(YeepsUpdateFn fn) {
    int i = ModOwnerOf((void*)fn);
    if (i >= 0 && fn) g_playerMods[i].updates.push_back(fn);
}
static void ModApi_onRoomChange(YeepsEventFn fn) {
    int i = ModOwnerOf((void*)fn);
    if (i >= 0 && fn) g_playerMods[i].roomFns.push_back(fn);
}
static void ModApi_log(const char* msg) {
    static double windowStart = 0; static int n = 0;
    double now = nowSeconds();
    if (now - windowStart > 1.0) { windowStart = now; n = 0; }
    if (!msg || ++n > 20) return;               // max 20 lines a second for all mods
    BNM_FileCheckpointF("[mod] %.300s", msg);
}
static void* g_modLastPlayer = nullptr;
static void* ModApi_localPlayer() { return g_modLastPlayer; }
static int ModApi_hasRole(const char* role) {
    static std::map<std::string, std::pair<double, bool>> cache;
    if (!role) return 0;
    double now = nowSeconds();
    auto it = cache.find(role);
    if (it != cache.end() && now - it->second.first < 5.0) return it->second.second;
    bool has = AccountHasRoleNow(role);
    cache[role] = {now, has};
    return has;
}

static void ModsMarkActive(int i) {
    if (i < 0) { g_modActive[0] = 0; return; }
    snprintf(g_modActive, sizeof g_modActive, "%s", g_playerMods[i].tag.c_str());
}
static bool CopyFile(const std::string& from, const std::string& to) {
    int in = open(from.c_str(), O_RDONLY);
    if (in < 0) return false;
    int out = open(to.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0700);
    if (out < 0) { close(in); return false; }
    char buf[1 << 16]; ssize_t n; bool ok = true;
    while ((n = read(in, buf, sizeof buf)) > 0) if (write(out, buf, n) != n) { ok = false; break; }
    close(in); close(out);
    return ok && n == 0;
}

static void (*orig_ModPlayerLateUpdate)(void*, void*) = nullptr;
static int g_modLastFrame = -1;
static void ModsTick(void* player) {
    static Method<int> frameCount(Class("UnityEngine", "Time", Image("UnityEngine.CoreModule.dll")).GetMethod("get_frameCount", 0));
    static Method<float> deltaTime(Class("UnityEngine", "Time", Image("UnityEngine.CoreModule.dll")).GetMethod("get_deltaTime", 0));
    int frame = frameCount.IsValid() ? frameCount() : -2;
    if (frame == g_modLastFrame) return;        // once per frame
    g_modLastFrame = frame;
    if (player != g_modLastPlayer) {            // new room / scene
        g_modLastPlayer = player;
        for (size_t i = 0; i < g_playerMods.size(); i++)
            for (auto fn : g_playerMods[i].roomFns) {
                ModsMarkActive((int)i);
                try { fn(); } catch (...) { g_playerMods[i].errors++; }
            }
        ModsMarkActive(-1);
    }
    float dt = deltaTime.IsValid() ? deltaTime() : 0.016f;
    for (size_t i = 0; i < g_playerMods.size(); i++) {
        auto& m = g_playerMods[i];
        if (m.updatesOff || m.updates.empty()) continue;
        double t0 = nowSeconds();
        ModsMarkActive((int)i);
        for (auto fn : m.updates) {
            try { fn(dt); } catch (...) { m.errors++; }
        }
        double ms = (nowSeconds() - t0) * 1000.0;
        if (ms > 4.0) m.slowFrames++;
        if (frame % 600 == 0) m.slowFrames = 0;
        if (m.slowFrames > 90 || m.errors > 30) {
            m.updatesOff = true;
            BNM_FileCheckpointF("[mods] %s updates turned OFF (%s)", m.file.c_str(),
                                m.errors > 30 ? "keeps throwing errors" : "too slow, lagging the game");
        }
    }
    ModsMarkActive(-1);
}
// LateUpdate is declared on the Player base class, so this hook also runs for
// every remote PuppetPlayer. Only the local MasterPlayer drives the mods.
static BNM::IL2CPP::Il2CppClass* g_modMasterPlayerClass = nullptr;
static void hook_ModPlayerLateUpdate(void* self, void* mi) {
    if (orig_ModPlayerLateUpdate) orig_ModPlayerLateUpdate(self, mi);
    if (!self || !g_modMasterPlayerClass || ((BNM::IL2CPP::Il2CppObject*)self)->klass != g_modMasterPlayerClass) return;
    try { ModsTick(self); } catch (...) { ModsMarkActive(-1); }
}

static void LoadPlayerMods() {
    mkdir(kModsDir, 0770);
    mkdir(kModsPriv, 0700);
    std::string readme = std::string(kModsDir) + "/README.txt";
    if (access(readme.c_str(), F_OK) != 0)
        if (FILE* f = fopen(readme.c_str(), "w")) {
            fputs("Put Yeeps mods (.so files) in this folder, then restart the game.\n"
                  "Only install mods from https://github.com/Blazeisodd/Blazes-Apk-mod-menu-addon\n"
                  "Help and the official download: https://discord.gg/UfYxE9W9dX\n"
                  "Game crashing? Make an empty file named DISABLE here to turn every mod off.\n", f);
            fclose(f);
        }
    if (access((std::string(kModsDir) + "/DISABLE").c_str(), F_OK) == 0) {
        BNM_FileCheckpoint("[mods] DISABLE file present: no mods loaded");
        return;
    }
    std::set<std::string> bad;
    if (FILE* f = fopen(kModsBadFile, "r")) {
        char line[200];
        while (fgets(line, sizeof line, f)) { line[strcspn(line, "\r\n")] = 0; if (line[0]) bad.insert(line); }
        fclose(f);
    }
    std::vector<std::string> files;
    if (DIR* d = opendir(kModsDir)) {
        while (dirent* e = readdir(d)) {
            std::string n = e->d_name;
            if (n.size() > 3 && n.compare(n.size() - 3, 3, ".so") == 0 && n.find('/') == std::string::npos) files.push_back(n);
        }
        closedir(d);
    }
    std::sort(files.begin(), files.end());
    if (files.size() > (size_t)kMaxMods) files.resize(kMaxMods);
    if (files.empty()) { BNM_FileCheckpoint("[mods] no mods installed"); return; }

    g_modGameVersion = LiveVersionLabel();
    size_t dash = g_modGameVersion.find("-blaze");
    if (dash != std::string::npos) g_modGameVersion.resize(dash);
    g_modApi.version = YEEPS_MOD_API_VERSION;
    g_modApi.size = sizeof(YeepsModApi);
    g_modApi.resolve = ModApi_resolve;
    g_modApi.il2cppHandle = BNM::Internal::il2cppLibraryHandle;
    g_modApi.hook = ModApi_hook;
    g_modApi.unhook = ModApi_unhook;
    g_modApi.onUpdate = ModApi_onUpdate;
    g_modApi.onRoomChange = ModApi_onRoomChange;
    g_modApi.log = ModApi_log;
    g_modApi.hasRole = ModApi_hasRole;
    g_modApi.gameVersion = g_modGameVersion.c_str();
    g_modApi.buildId = kBuildId;
    g_modApi.modsDir = kModsDir;
    g_modApi.localPlayer = ModApi_localPlayer;

    g_playerMods.reserve(kMaxMods);
    for (const auto& n : files) {
        std::string src = std::string(kModsDir) + "/" + n, dst = std::string(kModsPriv) + "/" + n;
        char tag[160];
        snprintf(tag, sizeof tag, "%s %016llx", n.c_str(), (unsigned long long)Fnv1aFile(src.c_str()));
        if (bad.count(tag)) {
            BNM_FileCheckpointF("[mods] SKIPPED %s: it crashed the game last time (replace the file to try again)", tag);
            continue;
        }
        if (!CopyFile(src, dst)) { BNM_FileCheckpointF("[mods] %s: copy failed", n.c_str()); continue; }
        int idx = (int)g_playerMods.size();
        g_playerMods.push_back({n, tag});
        snprintf(g_modFileHash[idx], sizeof g_modFileHash[idx], "%s", tag);
        snprintf(g_modFilePath[idx], sizeof g_modFilePath[idx], "%s", dst.c_str());
        g_modFileN = idx + 1;
        ModsMarkActive(idx);                    // a crash from here on blames this mod
        void* h = dlopen(dst.c_str(), RTLD_NOW | RTLD_LOCAL);
        auto init = h ? (int (*)(const YeepsModApi*))dlsym(h, "YeepsMod_Init") : nullptr;
        auto name = h ? (const char* (*)())dlsym(h, "YeepsMod_Name") : nullptr;
        int rc = -1;
        if (init) {
            g_modInitIdx = idx;
            try { rc = init(&g_modApi); } catch (...) { rc = -2; }
            g_modInitIdx = -1;
        }
        ModsMarkActive(-1);
        if (rc != 0) {
            BNM_FileCheckpointF("[mods] %s NOT loaded: %s", tag,
                                !h ? dlerror() : !init ? "no YeepsMod_Init" : rc == -2 ? "init threw" : "init failed");
            g_playerMods[idx].updatesOff = true;
            g_playerMods[idx].updates.clear(); g_playerMods[idx].roomFns.clear();
            continue;
        }
        BNM_FileCheckpointF("[mods] loaded %s (%s)", tag, name ? name() : "?");
    }
    bool any = false;
    for (auto& m : g_playerMods) any |= !m.updates.empty() || !m.roomFns.empty();
    if (!any) return;
    Class mp("", "MasterPlayer", Image("Assembly-CSharp.dll"));
    g_modMasterPlayerClass = mp.IsValid() ? mp.GetClass() : nullptr;
    MethodBase lu = mp.IsValid() ? mp.GetMethod("LateUpdate", 0) : MethodBase();
    if (lu.IsValid()) BasicHook(lu, hook_ModPlayerLateUpdate, orig_ModPlayerLateUpdate);
    BNM_FileCheckpointF("[mods] update hook (MasterPlayer.LateUpdate) = %d", lu.IsValid());
}

static void OnLoaded() {
    // PUBLIC BUILD: only the BNM Explorer + its Debug.Log feed. The Meta
    // entitlement/user-proof bypass is installed from JNI_OnLoad
    // (OvrHookPollThread) and does not depend on il2cpp attach. All the
    // credential/crypto/backtrace diagnostics are removed for distribution.
    __android_log_print(ANDROID_LOG_ERROR, "MYDBG", "OnLoaded fired - il2cpp attach succeeded");
    BNM_FileCheckpoint("OnLoaded fired - il2cpp attach succeeded");
#ifdef PLAY_BUILD
    // playyeeps = REAL Yeeps, only what's necessary changed. We install ONLY:
    //  - the backend redirect to our proxy (so login/account skip the Meta nonce
    //    and our proxy can serve an editable account; the proxy forwards the
    //    world to real Yeeps), with the build tag + device key the proxy needs;
    //  - the gates that let OUR (local) account pass the client's checks and go
    //    online / join rooms (hash validate, DoRefresh, join permission);
    //  - the JSON null-fix (prevents a client NRE on our responses).
    // DELIBERATELY NOT installed (so the game is otherwise stock real Yeeps):
    //  - Photon app override  -> the client uses REAL Yeeps' Photon lobbies
    //  - version label / big texts, telemetry kill, voice-patrol kill
    //  - every world/beta4/feature hook, every diagnostic/remote-log hook
    // Tomahawk is neutralized by the stub libTomahawkCore.so, not a hook.
#ifdef DUMP_METADATA
    // One-off: dump the decrypted il2cpp metadata from memory (for reading).
    DumpDecryptedMetadata();
    DumpMetaV2();
#endif
    ComputeBuildTag();
    ComputeDeviceKey();
    InstallBackendRedirect();
    InstallJsonNullFix();
    InstallAccountOnlineHooks();
    InstallValidateHashHook();
    InstallPermissionHook();
    // InstallPatternUnlock();  REMOVED: IsPatternOwned=true hook broke the build menu ("own nothing"). own-everything is server-side (ownedPatterns).
    InstallFeverEffects();                  // fever-dream room: fog/light effects (room-gated, live-safe)
    // Multiplayer MUST use our Photon app. Real Yeeps' Photon custom-auth
    // validates against a real account session (tied to the Meta nonce), so our
    // local account is rejected ("Failed to authenticate", -2114281333) and the
    // game dead-ends -- it needs a Photon connection even to load. This points
    // the client at our Photon app (whose custom auth accepts our account) and
    // adds the device key. Same necessary identity replacement as the account.
    InstallPhotonAuthKey();
    BNM_FileCheckpoint("[play] minimal hooks installed (real-yeeps + our photon)");
    return;
#endif
#ifdef BNM_LIVE_BASE
    // biglive20 = the stock 2.31.4 APK with ONLY what our server needs (owner 2026-10-13:
    // "only apply the needed stuff"). Everything else stays the real game. Off on
    // purpose: every *Diag hook and MapObject.SetSharedMaterials (scene-load crashes on
    // the live binary), the 1.31/2.4 feature hooks (remote feed, dispenser, BLAZE role
    // check, news buttons), the explorer. Meta proof/entitlement (OvrHookPollThread)
    // and VoicePatrol-off run from JNI_OnLoad; Tomahawk is a stub library; Talsec.start
    // is a no-op in smali.
    InstallGameAnalyticsBlock();           // GameAnalytics never starts (no data to GA)
    InstallVersionLabelClean();            // version label = "2.31.4-blaze"
    InstallBigVersionTexts();              //   (loading / error screens too)
    ComputeBuildTag();                     // build id + native-lib hashes for the server
    ComputeDeviceKey();                    // headset key sent with every API call
    InstallPhotonAuthKey();                // ...and with every Photon room join
    InstallBackendRedirect();              // every Yeeps/AWS API host -> our server
    InstallJsonNullFix();                  // null strings in our JSON replies -> ""
    InstallRemoteLogs();                   // logs + crashes -> OUR server (tester debugging)
    TuneServicePointManager();             // AWS SDK connection pool
    InstallAccountOnlineHooks();           // DoRefresh gate -> account goes online
    InstallValidateHashHook();             // account hash check -> pass
    InstallPermissionHook();               // GetHasPermissionToJoinRoom -> granted
    InstallRemoteClient();                 // announcements, video, screens, friends, disk audio + creative lock (one tick hook)
    // Fever-dream fog/light REMOVED (owner 2026-10-13: "remove the fever dream addons"); the
    // ??? room is now just a big map mess, no special atmosphere.
    InstallSuspiciousPopupOff();           // no "Suspicious event (PlayerData modified)" popup
    // InstallReskinTracer();  live61: OFF - live59/60 crashed (SIGSEGV ~2 min in) with it on
    // InstallRoleTracer();  live69: off again (diag done - only roleBadge_developer is blocked, roles all pass)
    std::thread([]() {                     // live54: one-off class dump (card art, badge, glass menu, ice breaking)
        std::this_thread::sleep_for(std::chrono::seconds(25));
        try {
            AttachThread();
            std::string mark = std::string("/data/data/com.TrassGames.Yeeps/files/cdump_") + kBuildId;
            if (access(mark.c_str(), F_OK) != 0) {
                LiveClassDump();
                if (FILE* f = fopen(mark.c_str(), "w")) fclose(f);
            } else DevlogPush("[cdump] already dumped on this install");
        } catch (...) { DevlogPush("[cdump] thread EXCEPTION"); }
    }).detach();
    InstallInt32ParseNullGuard();          // Int32.Parse(null) -> 0 (no ArgumentNullException popup)
    InstallButtonRedirect();               // Discord link -> our invite; "leave a review" -> mod menu (:8080) + relabel
    InstallReviewButton();                 // the button's own handler -> http://localhost:8080 (mod menu)
#ifdef BNM_LIVE_BASE
    InstallTvHook();                       // live92: Yeeps TV computer screens
#endif
    InstallVoiceRetry();                   // live88: voice reconnects after a "voice full" failure
    hookDebugLog();                        // feed the explorer /logs + our devlog
    std::thread(startServer).detach();     // BNM Explorer on 127.0.0.1:8080 (GameManager edits crash)
    try { LoadPlayerMods(); } catch (...) { BNM_FileCheckpoint("[mods] loader EXCEPTION"); }   // players' own .so mods
    BNM_FileCheckpoint("[live27] required hooks + explorer + creative grief-lock + button redirect");
#else
    InstallGameAnalyticsBlock();           // kill third-party telemetry
    InstallVersionLabelClean();            // show just the version name
    ComputeBuildTag();                     // build id + native-lib hashes for the server
    ComputeDeviceKey();                    // headset key sent with every API call
    InstallPhotonAuthKey();                // ...and with every Photon room join
    InstallBackendRedirect();              // execute-api + lambda + dynamodb -> self-host
#ifndef BNM_LIVE_BASE
    InstallRoomDataDiag();                 // 2.4.0 diagnosis (room-data stall)
#endif
    InstallJsonNullFix();                  // 2.4.0: null strings in Lambda payloads -> ""
#ifndef BNM_LIVE_BASE
    InstallJsonMissDiag();                 // 240b19: log JSON keys the game wants but replies lack
    // ResourcesDiag hooks Resources.Load, which the game calls hundreds of
    // times DURING scene load -- a high-frequency hook right where the live
    // build was crashing. Off for live.
    InstallResourcesDiag();                // 240b23: log Resources.Load/LoadAll paths
#endif
    InstallBigVersionTexts();              // big: version texts -> "Blazes Yeeps Apk"
    InstallRemoteLogs();                   // 2.4.0 test builds: logs + crashes -> 240 server
#ifndef BNM_LIVE_BASE
    InstallNodeDiag();                     // 2.4.0 diag: room map nodes
#endif
    TuneServicePointManager();             // 2.4.0: AWS SDK connection pool tuning
    InstallAccountOnlineHooks();           // 2.4.0: DoRefresh gate -> account goes online
    InstallValidateHashHook();             // 2.4.0: account hash check -> pass
    InstallPermissionHook();               // 2.4.0: GetHasPermissionToJoinRoom -> granted
    PatchCredentialGate();                 // 2.4.0: credential-fail branch -> continue
    // InstallAccountValidateBypass();     // 1.31-only (hardcoded 1.31 RVAs) - 2.4.0 needs its own gate (TBD)
    InstallRemoteClient();                 // server announcements + remote disk audio
    InstallDispenserAcceptAll();           // item dispenser takes every item
    InstallBlazeRoleCheck();               // BLAZE blocks: placeable with the blaze role
    InstallNewsButtons();                  // news board: Discord invite + download page
    hookDebugLog();                        // feed Explorer /logs
    // big48 crash: the BNM Explorer server (0.0.0.0:8080) ran game methods for
    // whoever on the network sent a request, and one of those calls made Unity
    // abort (SIGTRAP, stack ends in startServer). Not started in big builds.
    // std::thread(startServer).detach();
#endif
}


// ---- Voice Patrol fully OFF (240b16) --------------------------------------
// libVoicePatrol_SDK.so is a third-party voice-moderation SDK: it streams mic
// audio (push_game_audio_data) to wsapi.voicepatrol.dev over gRPC, can mute the
// player (set_muted) and files reports. The C# wrapper P/Invokes these exports
// and reads string results as IntPtr (the SDK returns pointers to its own
// static strings, never freed by the caller), so every string-returning export
// is replaced by one returning a static "", and is_* by false. The library is
// loaded here first so it is hooked before the game ever calls it.
static const char kVPEmpty[] = "";
static const char* vp_noop_str() { return kVPEmpty; }
static int vp_noop_false() { return 0; }
static void InstallVoicePatrolOff() {
    void* h = dlopen("libVoicePatrol_SDK.so", RTLD_NOW);
    if (!h) { BNM_FileCheckpointF("VoicePatrol: dlopen failed (%s)", dlerror()); return; }
    static const char* kStr[] = {"start_sdk", "stop_sdk", "on_joined_room", "on_left_room",
        "process_audio_file_data", "push_game_audio_data", "set_metadata", "set_muted",
        "submit_player_report"};
    static const char* kBool[] = {"is_muted", "is_processing_file", "is_sdk_running"};
    int ok = 0, total = 0;
    for (const char* n : kStr) {
        total++; void* sym = dlsym(h, n); void* orig = nullptr;
        if (sym && DobbyHook(sym, (dobby_dummy_func_t)vp_noop_str, (dobby_dummy_func_t*)&orig) == 0) ok++;
        else BNM_FileCheckpointF("VoicePatrol: hook %s FAILED (sym=%p)", n, sym);
    }
    for (const char* n : kBool) {
        total++; void* sym = dlsym(h, n); void* orig = nullptr;
        if (sym && DobbyHook(sym, (dobby_dummy_func_t)vp_noop_false, (dobby_dummy_func_t*)&orig) == 0) ok++;
        else BNM_FileCheckpointF("VoicePatrol: hook %s FAILED (sym=%p)", n, sym);
    }
    BNM_FileCheckpointF("VoicePatrol: OFF (%d/%d exports no-op'd)", ok, total);
}

// Tomahawk (libTomahawkCore.so): Meta's networking/engagement core; it opens a
// network monitor and phones home. We don't need it — no-op its init and the
// network monitor so it never starts. (libVoicePatrol already handled above;
// GameAnalytics is killed in OnLoaded.) dlopen failing is harmless (the 2.4
// base has no such lib).
static void vp_noop_void() {}
static int  vp_noop_zero() { return 0; }
static int  tk_noop_true() { return 1; }  // IsComplete = true
static void InstallTomahawkOff() {
    void* h = dlopen("libTomahawkCore.so", RTLD_NOW);
    if (!h) { BNM_FileCheckpointF("Tomahawk: dlopen failed (%s)", dlerror()); return; }
    // Suppress init + network monitor (no phone-home).
    static const char* kVoid[] = {"tomahawkCoreInitialize", "tomahawkCoreStartNetworkMonitor",
        "tomahawkCoreStopNetworkMonitor", "setTomahawkLogCallback"};
    int ok = 0, total = 0;
    for (const char* n : kVoid) {
        total++; void* sym = dlsym(h, n); void* orig = nullptr;
        if (sym && DobbyHook(sym, (dobby_dummy_func_t)vp_noop_void, (dobby_dummy_func_t*)&orig) == 0) ok++;
        else BNM_FileCheckpointF("Tomahawk: hook %s FAILED (sym=%p)", n, sym);
    }
    // Status checks: IsFailed=false, GetErrorCount=0, GetStatus=0, IsNetworkAvailable=false,
    // IsVPNActive=false, isTomahawkReInit=false.
    static const char* kFalse[] = {"tomahawkCoreIsFailed", "tomahawkCoreGetErrorCount",
        "tomahawkCoreGetStatus", "tomahawkCoreIsNetworkAvailable", "tomahawkCoreIsVPNActive",
        "isTomahawkReInit"};
    for (const char* n : kFalse) {
        total++; void* sym = dlsym(h, n); void* orig = nullptr;
        if (sym && DobbyHook(sym, (dobby_dummy_func_t)vp_noop_zero, (dobby_dummy_func_t*)&orig) == 0) ok++;
        else BNM_FileCheckpointF("Tomahawk: hook %s FAILED (sym=%p)", n, sym);
    }
    // IsComplete=true so the game thinks the handshake finished cleanly.
    { total++; void* sym = dlsym(h, "tomahawkCoreIsComplete"); void* orig = nullptr;
      if (sym && DobbyHook(sym, (dobby_dummy_func_t)tk_noop_true, (dobby_dummy_func_t*)&orig) == 0) ok++;
      else BNM_FileCheckpointF("Tomahawk: hook tomahawkCoreIsComplete FAILED (sym=%p)", sym); }
    BNM_FileCheckpointF("Tomahawk: OFF (%d/%d exports no-op'd)", ok, total);
}

JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM* vm, [[maybe_unused]] void* reserved) {
    g_vm = vm;
    // Fresh checkpoint file per launch: the devlog tails it from offset 0, and a
    // file left by an older build (1.31 installs share this package/folder)
    // would be re-uploaded as if it were this run. Keep the old one as _prev.
    rename("/sdcard/Android/data/com.TrassGames.Yeeps/files/bnm_checkpoint.txt",
           "/sdcard/Android/data/com.TrassGames.Yeeps/files/bnm_checkpoint_prev.txt");
    BNM_FileCheckpoint("=== JNI_OnLoad entered (2.31.4-blaze) ===");
    __android_log_print(ANDROID_LOG_ERROR, "MYDBG", "JNI_OnLoad entered (2.31.4-blaze)");
    JNIEnv* env;
    vm->GetEnv((void**)&env, JNI_VERSION_1_6);
    std::thread(OvrHookPollThread).detach();
#ifndef PLAY_BUILD
    InstallVoicePatrolOff();               // 240b16: no voice moderation SDK at all
    // biglive hooks the REAL libTomahawkCore.so (large funcs, safe to inline-hook).
    // The playyeeps APK ships a STUB libTomahawkCore.so (tiny adjacent funcs) that
    // already returns the right values, so inline-hooking it would overrun one
    // stub into the next and corrupt them -> SIGSEGV at room load. Skip it here.
    // biglive20: libTomahawkCore.so is replaced by a stub library (no network, no
    // integrity nonce), so InstallTomahawkOff must NOT run: inline-hooking the tiny
    // stub functions overruns one into the next -> SIGSEGV at room load.
#endif
    // playyeeps: VoicePatrol + Tomahawk left as real Yeeps ships them (Tomahawk
    // is neutralized by the stub .so; voice moderation runs natively).
    BNM::Loading::AddOnLoadedEvent(OnLoaded);
    BNM::Loading::TryLoadByJNI(env);
    __android_log_print(ANDROID_LOG_ERROR, "MYDBG", "JNI_OnLoad: TryLoadByJNI returned (2.4.0-blaze)");
    BNM_FileCheckpoint("=== JNI_OnLoad: TryLoadByJNI returned (2.4.0-blaze) ===");
    return JNI_VERSION_1_6;
}
