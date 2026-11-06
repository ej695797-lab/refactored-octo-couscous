#pragma once
#include <string>
#include <sstream>
#include <vector>
#include <mutex>
#include <cstring>
#include <cstdlib>

extern "C" {
#include "lua.h"
#include "lauxlib.h"
#include "lualib.h"
}

using namespace BNM;
using namespace BNM::IL2CPP;
using namespace BNM::Structures::Unity;

static std::mutex g_luaOutMtx;
static std::string g_luaOutput;

static void luaAppend(const std::string& s) {
    std::lock_guard<std::mutex> lk(g_luaOutMtx);
    if (!g_luaOutput.empty()) g_luaOutput += "\n";
    g_luaOutput += s;
}

struct LuaAssembly { std::string name; };
struct LuaNameSpace { std::string asm_; std::string ns; };
struct LuaClass { std::string asm_; std::string ns; std::string cls; uintptr_t inst; };
struct LuaGameObject { uintptr_t addr; };

static const char* MT_ASM = "LuaBNM.Assembly";
static const char* MT_NS = "LuaBNM.NameSpace";
static const char* MT_CLS = "LuaBNM.Class";
static const char* MT_GO = "LuaBNM.GameObject";
// yes i know i could make hpp file with all types like system.single and more but i didn't do it
static std::string lua_typeName(const Il2CppType* t) {
    if (!t) return "?";
    try {
        Class c(t);
        if (!c.IsValid()) return "?";
        auto* r = c.GetClass();
        if (!r || !r->name) return "?";
        std::string n = r->name;
        if (r->namespaze && r->namespaze[0] != '\0')
            n = std::string(r->namespaze) + "." + n;
        return n;
    } catch (...) { return "?"; }
}

static void pushIl2cppValue(lua_State* L, const std::string& rtn, void* ret) {
    if (!ret) { lua_pushnil(L); return; }
    if (rtn=="System.Void"||rtn=="Void"||rtn=="void") {
        lua_pushnil(L);
    } else if (rtn=="System.Single"||rtn=="Single"||rtn=="float") {
        lua_pushnumber(L, *(float*)((uint8_t*)ret + sizeof(Il2CppObject)));
    } else if (rtn=="System.Double"||rtn=="Double"||rtn=="double") {
        lua_pushnumber(L, *(double*)((uint8_t*)ret + sizeof(Il2CppObject)));
    } else if (rtn=="System.Int32"||rtn=="Int32"||rtn=="int") {
        lua_pushinteger(L, *(int32_t*)((uint8_t*)ret + sizeof(Il2CppObject)));
    } else if (rtn=="System.Int64"||rtn=="Int64"||rtn=="long") {
        lua_pushnumber(L, (double)*(int64_t*)((uint8_t*)ret + sizeof(Il2CppObject)));
    } else if (rtn=="System.Boolean"||rtn=="Boolean"||rtn=="bool") {
        lua_pushboolean(L, *(bool*)((uint8_t*)ret + sizeof(Il2CppObject)));
    } else if (rtn=="System.Byte"||rtn=="Byte"||rtn=="byte") {
        lua_pushinteger(L, *(uint8_t*)((uint8_t*)ret + sizeof(Il2CppObject)));
    } else if (rtn=="System.String"||rtn=="String"||rtn=="string") {
        auto* s = (BNM::Structures::Mono::String*)ret;
        if (s) lua_pushstring(L, s->str().c_str()); else lua_pushnil(L);
    } else {
        lua_pushnumber(L, (double)(uintptr_t)ret);
    }
}

static void* luaToArg(lua_State* L, int idx, const std::string& t,
    std::vector<int32_t>& vi, std::vector<float>& vf, std::vector<double>& vd,
    std::vector<uint8_t>& vb, std::vector<int64_t>& vi64) {
    if (t=="System.Int32"||t=="Int32"||t=="int") {
        vi.push_back((int32_t)luaL_checkinteger(L, idx)); return &vi.back();
    } else if (t=="System.Single"||t=="Single"||t=="float") {
        vf.push_back((float)luaL_checknumber(L, idx)); return &vf.back();
    } else if (t=="System.Double"||t=="Double"||t=="double") {
        vd.push_back(luaL_checknumber(L, idx)); return &vd.back();
    } else if (t=="System.Boolean"||t=="Boolean"||t=="bool") {
        vb.push_back(lua_toboolean(L, idx) ? 1 : 0); return &vb.back();
    } else if (t=="System.Int64"||t=="Int64"||t=="long") {
        vi64.push_back((int64_t)luaL_checknumber(L, idx)); return &vi64.back();
    } else if (t=="System.Byte"||t=="Byte"||t=="byte") {
        vb.push_back((uint8_t)luaL_checkinteger(L, idx)); return &vb.back();
    } else if (t=="System.String"||t=="String"||t=="string") {
        return CreateMonoString(luaL_checkstring(L, idx));
    } else {
        return (void*)(uintptr_t)luaL_checknumber(L, idx);
    }
}

static int l_print(lua_State* L) {
    int n = lua_gettop(L);
    std::ostringstream ss;
    for (int i = 1; i <= n; i++) {
        if (i > 1) ss << "\t";
        if (lua_isnil(L, i)) ss << "nil";
        else if (lua_isboolean(L, i)) ss << (lua_toboolean(L, i) ? "true" : "false");
        else if (lua_isnumber(L, i)) ss << lua_tonumber(L, i);
        else ss << lua_tostring(L, i);
    }
    luaAppend(ss.str());
    return 0;
}

static int l_assembly_new(lua_State* L) {
    const char* name = luaL_checkstring(L, 1);
    auto* ud = (LuaAssembly*)lua_newuserdata(L, sizeof(LuaAssembly));
    new(ud) LuaAssembly{name};
    luaL_getmetatable(L, MT_ASM);
    lua_setmetatable(L, -2);
    return 1;
}

static int l_asm_namespace(lua_State* L) {
    auto* a = (LuaAssembly*)luaL_checkudata(L, 1, MT_ASM);
    const char* ns = luaL_checkstring(L, 2);
    auto* ud = (LuaNameSpace*)lua_newuserdata(L, sizeof(LuaNameSpace));
    new(ud) LuaNameSpace{a->name, ns};
    luaL_getmetatable(L, MT_NS);
    lua_setmetatable(L, -2);
    return 1;
}

static int l_asm_class(lua_State* L) {
    auto* a = (LuaAssembly*)luaL_checkudata(L, 1, MT_ASM);
    const char* cls = luaL_checkstring(L, 2);
    uintptr_t inst = lua_gettop(L) >= 3 ? (uintptr_t)luaL_checknumber(L, 3) : 0;
    auto* ud = (LuaClass*)lua_newuserdata(L, sizeof(LuaClass));
    new(ud) LuaClass{a->name, "", cls, inst};
    luaL_getmetatable(L, MT_CLS);
    lua_setmetatable(L, -2);
    return 1;
}

static int l_asm_index(lua_State* L) {
    const char* key = luaL_checkstring(L, 2);
    if (strcmp(key, "NameSpace") == 0) { lua_pushcfunction(L, l_asm_namespace); return 1; }
    if (strcmp(key, "Class") == 0) { lua_pushcfunction(L, l_asm_class); return 1; }
    return luaL_error(L, "Assembly has no member '%s'", key);
}

static int l_ns_class(lua_State* L) {
    auto* ns = (LuaNameSpace*)luaL_checkudata(L, 1, MT_NS);
    const char* cls = luaL_checkstring(L, 2);
    uintptr_t inst = lua_gettop(L) >= 3 ? (uintptr_t)luaL_checknumber(L, 3) : 0;
    auto* ud = (LuaClass*)lua_newuserdata(L, sizeof(LuaClass));
    new(ud) LuaClass{ns->asm_, ns->ns, cls, inst};
    luaL_getmetatable(L, MT_CLS);
    lua_setmetatable(L, -2);
    return 1;
}

static int l_ns_index(lua_State* L) {
    const char* key = luaL_checkstring(L, 2);
    if (strcmp(key, "Class") == 0) { lua_pushcfunction(L, l_ns_class); return 1; }
    return luaL_error(L, "NameSpace has no member '%s'", key);
}

static int l_cls_callmethod(lua_State* L) {
    auto* c = (LuaClass*)lua_touserdata(L, lua_upvalueindex(1));
    const char* mname = lua_tostring(L, lua_upvalueindex(2));
    int nargs = lua_gettop(L);

    BNM::AttachIl2Cpp();
    Class cls(c->ns.c_str(), c->cls.c_str(), Image(c->asm_.c_str()));
    if (!cls.IsValid()) return luaL_error(L, "Class '%s' not found", c->cls.c_str());

    MethodBase mb = cls.GetMethod(mname, nargs);
    if (!mb.IsValid()) mb = cls.GetMethod(mname);
    if (!mb.IsValid()) {
        for (Class cur = cls.GetParent(); cur.IsValid() && cur.GetClass() && !mb.IsValid(); cur = cur.GetParent()) {
            mb = cur.GetMethod(mname, nargs);
            if (!mb.IsValid()) mb = cur.GetMethod(mname);
        }
    }
    if (!mb.IsValid()) return luaL_error(L, "Method '%s' not found", mname);

    auto* mi = mb.GetInfo();
    if (!mi || !mi->methodPointer) return luaL_error(L, "Method '%s' has no pointer", mname);
    if (ClassChainIsGameManager(cls.GetClass()) || (mi->klass && ClassChainIsGameManager(mi->klass))
        || (c->inst && ObjectIsGameManager((void*)c->inst))) GameManagerTrap("lua-call");

    std::vector<void*> runtimeArgs;
    std::vector<int32_t> vi; std::vector<float> vf; std::vector<double> vd;
    std::vector<uint8_t> vb; std::vector<int64_t> vi64;

    for (int i = 0; i < nargs; i++) {
        std::string pt = "?";
        if (mi->parameters && i < (int)mi->parameters_count && mi->parameters[i])
            pt = lua_typeName(mi->parameters[i]);
        runtimeArgs.push_back(luaToArg(L, i+1, pt, vi, vf, vd, vb, vi64));
    }

    bool isStatic = c->inst == 0;
    void* inst = isStatic ? nullptr : (void*)c->inst;
    std::string rtn = lua_typeName(mi->return_type);

    Il2CppException* exc = nullptr;
    void* ret = BNM::Internal::il2cppMethods.il2cpp_runtime_invoke(
        mi, inst, runtimeArgs.empty() ? nullptr : runtimeArgs.data(), &exc);

    if (exc) return luaL_error(L, "IL2CPP exception");

    if (rtn=="System.Void"||rtn=="Void"||rtn=="void") return 0;
    pushIl2cppValue(L, rtn, ret);
    return 1;
}

static int l_cls_readfield(lua_State* L, LuaClass* c, const char* fname) {
    BNM::AttachIl2Cpp();
    Class cls(c->ns.c_str(), c->cls.c_str(), Image(c->asm_.c_str()));
    if (!cls.IsValid()) return luaL_error(L, "Class not found");

    bool isStatic = c->inst == 0;
    Il2CppObject* inst = isStatic ? nullptr : (Il2CppObject*)c->inst;

    for (Class cur = cls; cur.IsValid() && cur.GetClass(); cur = cur.GetParent()) {
        for (auto& p : cur.GetProperties(false)) {
            auto* pi = p._data;
            if (!pi || !pi->name || strcmp(pi->name, fname) != 0) continue;
            if (pi->get) {
                MethodBase getter(pi->get);
                auto* gi = getter.GetInfo();
                if (gi && gi->methodPointer) {
                    std::string rtn = lua_typeName(gi->return_type);
                    Il2CppException* exc = nullptr;
                    void* ret = BNM::Internal::il2cppMethods.il2cpp_runtime_invoke(gi, inst, nullptr, &exc);
                    if (exc) return luaL_error(L, "Exception reading property");
                    pushIl2cppValue(L, rtn, ret);
                    return 1;
                }
            }
        }
        for (auto& f : cur.GetFields(false)) {
            auto* fi = f.GetInfo();
            if (!fi || !fi->name || strcmp(fi->name, fname) != 0) continue;
            std::string ft = lua_typeName(fi->type);
            bool fStatic = (fi->type->attrs & 0x10) != 0;
            if (ft=="System.Single"||ft=="Single"||ft=="float") {
                Field<float> fld = cur.GetField(fname); if(fld.IsValid()){if(!fStatic)fld.SetInstance(inst); lua_pushnumber(L,fld()); return 1;}
            } else if (ft=="System.Int32"||ft=="Int32"||ft=="int") {
                Field<int> fld = cur.GetField(fname); if(fld.IsValid()){if(!fStatic)fld.SetInstance(inst); lua_pushinteger(L,fld()); return 1;}
            } else if (ft=="System.Boolean"||ft=="Boolean"||ft=="bool") {
                Field<bool> fld = cur.GetField(fname); if(fld.IsValid()){if(!fStatic)fld.SetInstance(inst); lua_pushboolean(L,fld()); return 1;}
            } else if (ft=="System.String"||ft=="String"||ft=="string") {
                Field<BNM::Structures::Mono::String*> fld = cur.GetField(fname);
                if(fld.IsValid()){if(!fStatic)fld.SetInstance(inst); auto*s=fld(); lua_pushstring(L,s?s->str().c_str():""); return 1;}
            } else if (ft=="System.Double"||ft=="Double"||ft=="double") {
                Field<double> fld = cur.GetField(fname); if(fld.IsValid()){if(!fStatic)fld.SetInstance(inst); lua_pushnumber(L,fld()); return 1;}
            } else if (ft=="System.Int64"||ft=="Int64"||ft=="long") {
                Field<int64_t> fld = cur.GetField(fname); if(fld.IsValid()){if(!fStatic)fld.SetInstance(inst); lua_pushnumber(L,(double)fld()); return 1;}
            } else if (ft=="System.Byte"||ft=="Byte"||ft=="byte") {
                Field<uint8_t> fld = cur.GetField(fname); if(fld.IsValid()){if(!fStatic)fld.SetInstance(inst); lua_pushinteger(L,fld()); return 1;}
            } else {
                Field<void*> fld = cur.GetField(fname); if(fld.IsValid()){if(!fStatic)fld.SetInstance(inst); lua_pushnumber(L,(double)(uintptr_t)fld()); return 1;}
            }
        }
    }
    return luaL_error(L, "Field/property '%s' not found", fname);
}

static int l_cls_writefield(lua_State* L, LuaClass* c, const char* fname) {
    BNM::AttachIl2Cpp();
    Class cls(c->ns.c_str(), c->cls.c_str(), Image(c->asm_.c_str()));
    if (!cls.IsValid()) return luaL_error(L, "Class not found");
    if (ClassChainIsGameManager(cls.GetClass()) || (c->inst && ObjectIsGameManager((void*)c->inst))) GameManagerTrap("lua-write");

    bool isStatic = c->inst == 0;
    Il2CppObject* inst = isStatic ? nullptr : (Il2CppObject*)c->inst;

    for (Class cur = cls; cur.IsValid() && cur.GetClass(); cur = cur.GetParent()) {
        for (auto& p : cur.GetProperties(false)) {
            auto* pi = p._data;
            if (!pi || !pi->name || strcmp(pi->name, fname) != 0) continue;
            if (pi->set) {
                MethodBase setter(pi->set);
                auto* si = setter.GetInfo();
                if (si && si->methodPointer && si->parameters_count == 1) {
                    std::string pt = lua_typeName(si->parameters[0]);
                    std::vector<int32_t> vi; std::vector<float> vf; std::vector<double> vd;
                    std::vector<uint8_t> vb; std::vector<int64_t> vi64;
                    void* arg = luaToArg(L, 3, pt, vi, vf, vd, vb, vi64);
                    void* args[] = {arg};
                    Il2CppException* exc = nullptr;
                    BNM::Internal::il2cppMethods.il2cpp_runtime_invoke(si, inst, args, &exc);
                    return 0;
                }
            }
        }
        for (auto& f : cur.GetFields(false)) {
            auto* fi = f.GetInfo();
            if (!fi || !fi->name || strcmp(fi->name, fname) != 0) continue;
            std::string ft = lua_typeName(fi->type);
            bool fStatic = (fi->type->attrs & 0x10) != 0;
            if (ft=="System.Single"||ft=="Single"||ft=="float") {
                Field<float> fld = cur.GetField(fname); if(fld.IsValid()){if(!fStatic)fld.SetInstance(inst); fld.Set((float)luaL_checknumber(L,3)); return 0;}
            } else if (ft=="System.Int32"||ft=="Int32"||ft=="int") {
                Field<int> fld = cur.GetField(fname); if(fld.IsValid()){if(!fStatic)fld.SetInstance(inst); fld.Set((int)luaL_checkinteger(L,3)); return 0;}
            } else if (ft=="System.Boolean"||ft=="Boolean"||ft=="bool") {
                Field<bool> fld = cur.GetField(fname); if(fld.IsValid()){if(!fStatic)fld.SetInstance(inst); fld.Set(lua_toboolean(L,3)!=0); return 0;}
            } else if (ft=="System.String"||ft=="String"||ft=="string") {
                Field<BNM::Structures::Mono::String*> fld = cur.GetField(fname);
                if(fld.IsValid()){if(!fStatic)fld.SetInstance(inst); fld.Set(CreateMonoString(luaL_checkstring(L,3))); return 0;}
            } else if (ft=="System.Double"||ft=="Double"||ft=="double") {
                Field<double> fld = cur.GetField(fname); if(fld.IsValid()){if(!fStatic)fld.SetInstance(inst); fld.Set(luaL_checknumber(L,3)); return 0;}
            } else {
                return luaL_error(L, "Cannot write field type '%s'", ft.c_str());
            }
        }
    }
    return luaL_error(L, "Field/property '%s' not found for writing", fname);
}

static int l_cls_index(lua_State* L) {
    auto* c = (LuaClass*)luaL_checkudata(L, 1, MT_CLS);
    const char* key = luaL_checkstring(L, 2);
    if (strcmp(key, "addr") == 0) { lua_pushnumber(L, (double)c->inst); return 1; }

    BNM::AttachIl2Cpp();
    Class cls(c->ns.c_str(), c->cls.c_str(), Image(c->asm_.c_str()));
    if (!cls.IsValid()) return luaL_error(L, "Class not found");

    for (Class cur = cls; cur.IsValid() && cur.GetClass(); cur = cur.GetParent()) {
        for (auto& m : cur.GetMethods(false)) {
            auto* mi = m.GetInfo();
            if (mi && mi->name && strcmp(mi->name, key) == 0) {
                lua_pushlightuserdata(L, c);
                lua_pushstring(L, key);
                lua_pushcclosure(L, l_cls_callmethod, 2);
                return 1;
            }
        }
    }

    return l_cls_readfield(L, c, key);
}

static int l_cls_newindex(lua_State* L) {
    auto* c = (LuaClass*)luaL_checkudata(L, 1, MT_CLS);
    const char* key = luaL_checkstring(L, 2);
    return l_cls_writefield(L, c, key);
}

static int l_cls_tostring(lua_State* L) {
    auto* c = (LuaClass*)luaL_checkudata(L, 1, MT_CLS);
    char buf[256];
    snprintf(buf, sizeof(buf), "Class<%s.%s @ 0x%llX>", c->ns.c_str(), c->cls.c_str(), (unsigned long long)c->inst);
    lua_pushstring(L, buf);
    return 1;
}

static int l_go_getcomponent(lua_State* L) {
    auto* go = (LuaGameObject*)luaL_checkudata(L, 1, MT_GO);
    const char* compName = luaL_checkstring(L, 2);
    BNM::AttachIl2Cpp();

    Method<Il2CppObject*> getComp(Class("UnityEngine","GameObject").GetMethod("GetComponent", 1));
    if (!getComp.IsValid()) return luaL_error(L, "GetComponent not found");

    Il2CppObject* obj = (Il2CppObject*)go->addr;
    Il2CppObject* comp = getComp[obj](CreateMonoString(compName));
    if (!comp) { lua_pushnil(L); return 1; }

    std::string asm_ = "", ns_ = "", cls_ = compName;
    if (comp->klass) {
        if (comp->klass->name) cls_ = comp->klass->name;
        if (comp->klass->namespaze) ns_ = comp->klass->namespaze;
        if (comp->klass->image && comp->klass->image->name) asm_ = comp->klass->image->name;
    }

    auto* ud = (LuaClass*)lua_newuserdata(L, sizeof(LuaClass));
    new(ud) LuaClass{asm_, ns_, cls_, (uintptr_t)comp};
    luaL_getmetatable(L, MT_CLS);
    lua_setmetatable(L, -2);
    return 1;
}

static int l_go_index(lua_State* L) {
    auto* go = (LuaGameObject*)luaL_checkudata(L, 1, MT_GO);
    const char* key = luaL_checkstring(L, 2);

    if (strcmp(key, "addr") == 0) { lua_pushnumber(L, (double)go->addr); return 1; }
    if (strcmp(key, "GetComponent") == 0) { lua_pushcfunction(L, l_go_getcomponent); return 1; }

    BNM::AttachIl2Cpp();
    if (strcmp(key, "Name") == 0 || strcmp(key, "name") == 0) {
        Method<BNM::Structures::Mono::String*> gn(Class("UnityEngine","Object").GetMethod("get_name"));
        if (gn.IsValid()) { auto* s = gn[(Il2CppObject*)go->addr](); lua_pushstring(L, s ? s->str().c_str() : ""); return 1; }
    }
    if (strcmp(key, "active") == 0) {
        Method<bool> ga(Class("UnityEngine","GameObject").GetMethod("get_activeSelf"));
        if (ga.IsValid()) { lua_pushboolean(L, ga[(Il2CppObject*)go->addr]()); return 1; }
    }
    if (strcmp(key, "transform") == 0) {
        Method<Il2CppObject*> gt(Class("UnityEngine","GameObject").GetMethod("get_transform"));
        if (gt.IsValid()) {
            Il2CppObject* tr = gt[(Il2CppObject*)go->addr]();
            if (tr) {
                auto* ud = (LuaClass*)lua_newuserdata(L, sizeof(LuaClass));
                new(ud) LuaClass{"UnityEngine.CoreModule", "UnityEngine", "Transform", (uintptr_t)tr};
                luaL_getmetatable(L, MT_CLS);
                lua_setmetatable(L, -2);
                return 1;
            }
        }
    }
    return luaL_error(L, "GameObject has no member '%s'", key);
}

static int l_go_tostring(lua_State* L) {
    auto* go = (LuaGameObject*)luaL_checkudata(L, 1, MT_GO);
    BNM::AttachIl2Cpp();
    Method<BNM::Structures::Mono::String*> gn(Class("UnityEngine","Object").GetMethod("get_name"));
    std::string name = "?";
    if (gn.IsValid()) { auto* s = gn[(Il2CppObject*)go->addr](); if (s) name = s->str(); }
    char buf[256];
    snprintf(buf, sizeof(buf), "GameObject<%s @ 0x%llX>", name.c_str(), (unsigned long long)go->addr);
    lua_pushstring(L, buf);
    return 1;
}

static int l_scene_index(lua_State* L);
static int l_scene_getobjects(lua_State* L);
static int l_scene_findobjects(lua_State* L);

static MethodBase lua_findObjsMethod() {
    Class objCls("UnityEngine", "Object");
    if (!objCls.IsValid()) return {};
    MethodBase mb = objCls.GetMethod("FindObjectsOfType", 1);
    if (!mb.IsValid()) mb = objCls.GetMethod("FindObjectsByType", 1);
    return mb;
}

static Il2CppObject* lua_sysTypeOf(const std::string& full) {
    Class typeCls("System", "Type", Image("mscorlib.dll"));
    if (!typeCls.IsValid()) return nullptr;
    MethodBase mb = typeCls.GetMethod("GetType", 1);
    if (!mb.IsValid()) return nullptr;
    auto* mi = mb.GetInfo();
    if (!mi || !mi->methodPointer) return nullptr;
    void* args[] = { CreateMonoString(full) };
    Il2CppException* exc = nullptr;
    return (Il2CppObject*)BNM::Internal::il2cppMethods.il2cpp_runtime_invoke(mi, nullptr, args, &exc);
}

static int l_scene_getobjects(lua_State* L) {
    BNM::AttachIl2Cpp();
    auto fm = lua_findObjsMethod();
    if (!fm.IsValid()) return luaL_error(L, "FindObjectsOfType not found");

    Il2CppObject* st = lua_sysTypeOf("UnityEngine.GameObject, UnityEngine.CoreModule");
    if (!st) st = lua_sysTypeOf("UnityEngine.GameObject, UnityEngine");
    if (!st) st = lua_sysTypeOf("UnityEngine.GameObject");
    if (!st) return luaL_error(L, "GameObject type not found");

    auto* arr = Method<Array<Il2CppObject*>*>(fm)(st);
    lua_newtable(L);
    if (arr && arr->capacity > 0) {
        for (int i = 0; i < arr->capacity; i++) {
            if (!arr->m_Items[i]) continue;
            auto* ud = (LuaGameObject*)lua_newuserdata(L, sizeof(LuaGameObject));
            new(ud) LuaGameObject{(uintptr_t)arr->m_Items[i]};
            luaL_getmetatable(L, MT_GO);
            lua_setmetatable(L, -2);
            lua_rawseti(L, -2, i+1);
        }
    }
    return 1;
}

static int l_scene_findobjectsoftype(lua_State* L) {
    const char* typeName = luaL_checkstring(L, 1);
    BNM::AttachIl2Cpp();
    auto fm = lua_findObjsMethod();
    if (!fm.IsValid()) return luaL_error(L, "FindObjectsOfType not found");

    Il2CppObject* st = lua_sysTypeOf(typeName);
    if (!st) {
        std::string alt = std::string(typeName) + ", UnityEngine.CoreModule";
        st = lua_sysTypeOf(alt);
    }
    if (!st) return luaL_error(L, "Type '%s' not found", typeName);

    auto* arr = Method<Array<Il2CppObject*>*>(fm)(st);
    lua_newtable(L);
    if (arr && arr->capacity > 0) {
        for (int i = 0; i < arr->capacity; i++) {
            if (!arr->m_Items[i]) continue;
            Il2CppObject* obj = arr->m_Items[i];
            std::string a = "", n = "", c = "Object";
            if (obj->klass) {
                if (obj->klass->name) c = obj->klass->name;
                if (obj->klass->namespaze) n = obj->klass->namespaze;
                if (obj->klass->image && obj->klass->image->name) a = obj->klass->image->name;
            }
            auto* ud = (LuaClass*)lua_newuserdata(L, sizeof(LuaClass));
            new(ud) LuaClass{a, n, c, (uintptr_t)obj};
            luaL_getmetatable(L, MT_CLS);
            lua_setmetatable(L, -2);
            lua_rawseti(L, -2, i+1);
        }
    }
    return 1;
}

static int l_scene_findobject(lua_State* L) {
    const char* name = luaL_checkstring(L, 1);
    BNM::AttachIl2Cpp();
    auto fm = lua_findObjsMethod();
    if (!fm.IsValid()) return luaL_error(L, "FindObjectsOfType not found");

    Il2CppObject* st = lua_sysTypeOf("UnityEngine.GameObject, UnityEngine.CoreModule");
    if (!st) st = lua_sysTypeOf("UnityEngine.GameObject, UnityEngine");
    if (!st) st = lua_sysTypeOf("UnityEngine.GameObject");
    if (!st) { lua_pushnil(L); return 1; }

    Method<BNM::Structures::Mono::String*> gn(Class("UnityEngine","Object").GetMethod("get_name"));
    auto* arr = Method<Array<Il2CppObject*>*>(fm)(st);
    if (arr && arr->capacity > 0 && gn.IsValid()) {
        for (int i = 0; i < arr->capacity; i++) {
            if (!arr->m_Items[i]) continue;
            auto* s = gn[arr->m_Items[i]]();
            if (s && s->str() == name) {
                auto* ud = (LuaGameObject*)lua_newuserdata(L, sizeof(LuaGameObject));
                new(ud) LuaGameObject{(uintptr_t)arr->m_Items[i]};
                luaL_getmetatable(L, MT_GO);
                lua_setmetatable(L, -2);
                return 1;
            }
        }
    }
    lua_pushnil(L);
    return 1;
}

static int l_scene_index(lua_State* L) {
    const char* key = luaL_checkstring(L, 2);
    if (strcmp(key, "GetObjects") == 0) { lua_pushcfunction(L, l_scene_getobjects); return 1; }
    if (strcmp(key, "FindObjectsOfType") == 0) { lua_pushcfunction(L, l_scene_findobjectsoftype); return 1; }
    if (strcmp(key, "FindObject") == 0) { lua_pushcfunction(L, l_scene_findobject); return 1; }
    lua_pushstring(L, key);
    lua_replace(L, 1);
    return l_scene_findobject(L);
}

static int l_wrap(lua_State* L) {
    uintptr_t addr = (uintptr_t)luaL_checknumber(L, 1);
    if (!addr) { lua_pushnil(L); return 1; }
    Il2CppObject* obj = (Il2CppObject*)addr;
    std::string a = "", n = "", c = "Object";
    if (obj->klass) {
        if (obj->klass->name) c = obj->klass->name;
        if (obj->klass->namespaze) n = obj->klass->namespaze;
        if (obj->klass->image && obj->klass->image->name) a = obj->klass->image->name;
    }
    auto* ud = (LuaClass*)lua_newuserdata(L, sizeof(LuaClass));
    new(ud) LuaClass{a, n, c, addr};
    luaL_getmetatable(L, MT_CLS);
    lua_setmetatable(L, -2);
    return 1;
}

static bool isUdata(lua_State* L, int idx, const char* mt) {
    if (!lua_isuserdata(L, idx)) return false;
    lua_getmetatable(L, idx);
    if (!lua_istable(L, -1)) { lua_pop(L, 1); return false; }
    luaL_getmetatable(L, mt);
    int eq = lua_rawequal(L, -1, -2);
    lua_pop(L, 2);
    return eq != 0;
}

static int l_typeof(lua_State* L) {
    if (isUdata(L, 1, MT_CLS)) {
        auto* c = (LuaClass*)lua_touserdata(L, 1);
        std::string full = c->ns.empty() ? c->cls : c->ns + "." + c->cls;
        lua_pushstring(L, full.c_str());
        return 1;
    }
    if (isUdata(L, 1, MT_GO)) {
        lua_pushstring(L, "GameObject");
        return 1;
    }
    lua_pushstring(L, luaL_typename(L, 1));
    return 1;
}

static void luabnm_openlibs(lua_State* L) {
    // Safe libraries only (biglive22): no io / os / package / debug (io could write
    // /proc/self/mem and edit GameManager without the explorer API), and no loaders
    // (a crafted Lua 5.1 bytecode chunk can write to any address).
    const luaL_Reg safe[] = {{"", luaopen_base}, {LUA_TABLIBNAME, luaopen_table},
                             {LUA_STRLIBNAME, luaopen_string}, {LUA_MATHLIBNAME, luaopen_math}};
    for (const auto& lib : safe) { lua_pushcfunction(L, lib.func); lua_pushstring(L, lib.name); lua_call(L, 1, 0); }
    for (const char* g : {"dofile", "loadfile", "load", "loadstring"}) { lua_pushnil(L); lua_setglobal(L, g); }
    lua_getglobal(L, LUA_STRLIBNAME);
    if (lua_istable(L, -1)) { lua_pushnil(L); lua_setfield(L, -2, "dump"); }
    lua_pop(L, 1);

    luaL_newmetatable(L, MT_ASM);
    lua_pushstring(L, "__index"); lua_pushcfunction(L, l_asm_index); lua_settable(L, -3);
    lua_pop(L, 1);

    luaL_newmetatable(L, MT_NS);
    lua_pushstring(L, "__index"); lua_pushcfunction(L, l_ns_index); lua_settable(L, -3);
    lua_pop(L, 1);

    luaL_newmetatable(L, MT_CLS);
    lua_pushstring(L, "__index"); lua_pushcfunction(L, l_cls_index); lua_settable(L, -3);
    lua_pushstring(L, "__newindex"); lua_pushcfunction(L, l_cls_newindex); lua_settable(L, -3);
    lua_pushstring(L, "__tostring"); lua_pushcfunction(L, l_cls_tostring); lua_settable(L, -3);
    lua_pop(L, 1);

    luaL_newmetatable(L, MT_GO);
    lua_pushstring(L, "__index"); lua_pushcfunction(L, l_go_index); lua_settable(L, -3);
    lua_pushstring(L, "__tostring"); lua_pushcfunction(L, l_go_tostring); lua_settable(L, -3);
    lua_pop(L, 1);

    lua_register(L, "Assembly", l_assembly_new);
    lua_register(L, "print", l_print);
    lua_register(L, "wrap", l_wrap);
    lua_register(L, "typeof_", l_typeof);
    lua_register(L, "FindObjectsOfType", l_scene_findobjectsoftype);
    lua_register(L, "FindObject", l_scene_findobject);

    lua_newtable(L);
    lua_newtable(L);
    lua_pushstring(L, "__index"); lua_pushcfunction(L, l_scene_index); lua_settable(L, -3);
    lua_setmetatable(L, -2);
    lua_setglobal(L, "scene");
}

static std::string executeLuaBNM(const std::string& code) {
    try {
        {
            std::lock_guard<std::mutex> lk(g_luaOutMtx);
            g_luaOutput.clear();
        }

        lua_State* L = luaL_newstate();
        if (!L) return "Error: failed to create Lua state";

        luabnm_openlibs(L);

        if (!code.empty() && code[0] == LUA_SIGNATURE[0]) { lua_close(L); return "Error: bytecode is not allowed"; }
        int err = luaL_loadbuffer(L, code.c_str(), code.size(), "=lua");
        if (err) {
            std::string e = lua_tostring(L, -1);
            lua_close(L);
            return "SyntaxError: " + e;
        }

        err = lua_pcall(L, 0, LUA_MULTRET, 0);
        if (err) {
            std::string e = lua_tostring(L, -1);
            lua_close(L);
            return "RuntimeError: " + e;
        }

        int top = lua_gettop(L);
        if (top > 0) {
            for (int i = 1; i <= top; i++) {
                if (lua_isnil(L, i)) luaAppend("nil");
                else if (lua_isboolean(L, i)) luaAppend(lua_toboolean(L, i) ? "true" : "false");
                else if (lua_isnumber(L, i)) {
                    char buf[64]; snprintf(buf, sizeof(buf), "%g", lua_tonumber(L, i));
                    luaAppend(buf);
                } else if (lua_isstring(L, i)) luaAppend(lua_tostring(L, i));
                else luaAppend(luaL_typename(L, i));
            }
        }

        lua_close(L);

        std::lock_guard<std::mutex> lk(g_luaOutMtx);
        return g_luaOutput;
    } catch (const std::exception& e) {
        return std::string("C++ Exception: ") + e.what();
    } catch (...) {
        return "C++ Exception: unknown";
    }
}
