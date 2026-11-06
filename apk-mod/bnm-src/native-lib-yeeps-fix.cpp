#include <jni.h>
#include <unistd.h>
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
#include "explorer/luabnm.hpp"

#define LOGD(...) __android_log_print(ANDROID_LOG_DEBUG, "YeepsFix", __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, "YeepsFix", __VA_ARGS__)

using namespace BNM;
using namespace BNM::IL2CPP;
using namespace BNM::Structures::Unity;

static int g_port = 8080;
static std::atomic<bool> g_permissionHooked{false};
static std::atomic<bool> g_joinNudgeActive{false};
static std::atomic<bool> g_justConnectedToMaster{false};
static std::atomic<double> g_connectionTime{0.0};

static Il2CppObject* sysTypeOf(const std::string& aqn) {
    Class tc("System", "Type", Image("mscorlib.dll"));
    if (!tc.IsValid()) return nullptr;
    Method<Il2CppObject*> gt(tc.GetMethod("GetType", 1));
    return gt.IsValid() ? gt(CreateMonoString(aqn.c_str())) : nullptr;
}
// Forward declarations for our hooks
static MethodBase findObjsMethod() {
    static MethodBase m;
    if (m.IsValid()) return m;
    Class oc("UnityEngine", "Object");
    for (auto& mt : oc.GetMethods(false)) {
        auto* mi = mt.GetInfo();
        if (mi && mi->name && strcmp(mi->name, "FindObjectsOfType") == 0 && mi->parameters_count == 1)
            return m = mt;
    }
    return m;
}
static bool (*orig_GetHasPermissionToJoinRoom)(void*, void*, void*, void*, void*, void*, void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void (*orig_OnConnectedToMaster)(void*, IL2CPP::MethodInfo*) = nullptr;

// Hook for GetHasPermissionToJoinRoom - always grant permission
static bool hook_GetHasPermissionToJoinRoom(void* roomKey, void* node, void* resultCallback,
                                          void* unused1, void* unused2, void* unused3,
                                          void* unused4, void* unused5, void* unused6, IL2CPP::MethodInfo* mi) {
    LOGD("hook_GetHasPermissionToJoinRoom: granting permission");

    // Invoke the resultCallback (arg2) with true
    // resultCallback is an Action<bool> - we need to invoke it with true
    if (resultCallback) {
        // Create a managed bool true value
        // System.Boolean true is represented as 1 byte with value 1
        uint8_t trueValue = 1;

        // Invoke the callback: resultCallback.Invoke(trueValue)
        // Get the method info from the delegate type
        Class delegateCls(((Il2CppObject*)resultCallback)->klass);
        if (delegateCls.IsValid()) {
            Method<void> invokeDelegate(delegateCls.GetMethod("Invoke", 1));
            if (invokeDelegate.IsValid()) {
                invokeDelegate(resultCallback, &trueValue);
                LOGD("hook_GetHasPermissionToJoinRoom: invoked callback with true");
            } else {
                LOGD("hook_GetHasPermissionToJoinRoom: failed to get Invoke method on callback");
            }
        } else {
            LOGD("hook_GetHasPermissionToJoinRoom: failed to get class of callback");
        }
    } else {
        LOGD("hook_GetHasPermissionToJoinRoom: resultCallback is null");
    }

    // Return true to indicate we handled it
    return true;
}

// Hook for OnConnectedToMaster to detect when we connect
static void hook_OnConnectedToMaster(void* self, IL2CPP::MethodInfo* mi) {
    LOGD("hook_OnConnectedToMaster: connected to master");
    g_justConnectedToMaster = true;
    g_connectionTime = std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();

    // Call original
    if (orig_OnConnectedToMaster) {
        orig_OnConnectedToMaster(self, mi);
    }
}

// Thread to monitor for join nudge
static void joinNudgeMonitorThread() {
    while (true) {
        std::this_thread::sleep_for(std::chrono::milliseconds(500));

        if (!g_joinNudgeActive.load()) {
            continue;
        }

        double now = std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
        if (g_justConnectedToMaster.load() &&
            (now - g_connectionTime.load()) > 3.0) { // 3 second timeout

            LOGD("joinNudgeMonitor: triggering join nudge (no join after 3s)");

            // Try to find and call JoinRoom on PhotonConnectionManager
            try {
                Class pc("", "PhotonConnectionManager", Image("Assembly-CSharp.dll"));
                if (pc.IsValid()) {
                    MethodBase joinRoom = pc.GetMethod("JoinRoom", 0);
                    if (joinRoom.IsValid()) {
                        // Get the instance
                        Method<Il2CppObject*> getInstance(pc.GetMethod("get_Instance", 0));
                        if (getInstance.IsValid()) {
                            Il2CppObject* instance = getInstance(nullptr, nullptr);
                            if (instance) {
                                LOGD("joinNudgeMonitor: calling JoinRoom via PhotonConnectionManager.Instance");
                                do { Il2CppException* exc = nullptr; void* ret = BNM::Internal::il2cppMethods.il2cpp_runtime_invoke(joinRoom.GetInfo(), instance, nullptr, &exc); if (exc) { LOGE("joinNudgeMonitor: exception calling JoinRoom"); } } while(0);
                                g_justConnectedToMaster = false; // Reset after attempt
                                continue;
                            }
                        }
                    }
                }

                // Fallback: try to find any MonoBehaviour that might have JoinRoom
                LOGD("joinNudgeMonitor: trying to find JoinRoom via broad search");
                Class mb("UnityEngine", "MonoBehaviour", Image("UnityEngine.CoreModule"));
                if (mb.IsValid()) {
                    auto fom = findObjsMethod();
                    if (fom.IsValid()) {
                        Il2CppObject* monoType = nullptr;
                        Class mbCls("UnityEngine", "MonoBehaviour");
                        if (mbCls.IsValid()) {
                            monoType = (Il2CppObject*)mbCls.GetMonoType();
                        }
                        if (!monoType) {
                            monoType = sysTypeOf("UnityEngine.MonoBehaviour, UnityEngine.CoreModule");
                        }
                        if (!monoType) {
                            monoType = sysTypeOf("UnityEngine.MonoBehaviour");
                        }

                        if (fom.IsValid() && monoType) {
                            auto* allMonos = Method<Array<Il2CppObject*>*>(fom)(monoType);
                            if (allMonos && allMonos->capacity > 0) {
                                for (int i = 0; i < allMonos->capacity; i++) {
                                    Il2CppObject* mono = allMonos->m_Items[i];
                                    if (!mono) continue;

                                    // Check if this mono has a JoinRoom method
                                    Class monoClass(mono->klass);
                                    MethodBase joinRoom = monoClass.GetMethod("JoinRoom", 0);
                                    if (joinRoom.IsValid()) {
                                        LOGD("joinNudgeMonitor: found JoinRoom on %s, calling it",
                                             monoClass.GetClass()->name ? monoClass.GetClass()->name : "unknown");
                                        do { Il2CppException* exc = nullptr; void* ret = BNM::Internal::il2cppMethods.il2cpp_runtime_invoke(joinRoom.GetInfo(), mono, nullptr, &exc); if (exc) { LOGE("joinNudgeMonitor: exception calling JoinRoom"); } } while(0);
                                        g_justConnectedToMaster = false;
                                        break;
                                    }
                                }
                            }
                        }
                    }
                }
            } catch (...) {
                LOGE("joinNudgeMonitor: exception in join nudge");
            }

            g_justConnectedToMaster = false; // Reset to avoid spamming
        }
    }
}

// Initialize hooks
static void InstallYeepsFixHooks() {
    try {
        LOGD("InstallYeepsFixHooks: starting");

        // Get il2cpp base
        void* ans = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_array_new_specific");
        uint8_t* il2cppBase = ans ? ((uint8_t*)ans - 0x41fc160) : nullptr;
        if (!il2cppBase) {
            LOGE("InstallYeepsFixHooks: failed to get il2cppBase");
            return;
        }

        // Hook GetHasPermissionToJoinRoom
        // We need to find this method. Let's search for it in common assemblies
        std::vector<std::string> assemblyNames = {
            "Assembly-CSharp.dll",
            "Assembly-CSharp-firstpass.dll",
            "Assembly-UnityScript.dll",
            "UnityEngine.UI.dll"
        };

        bool permissionHooked = false;
        for (const auto& asmName : assemblyNames) {
            Image img(asmName.c_str());
            if (!img.IsValid()) continue;

            // Search for GetHasPermissionToJoinRoom
            for (auto& cls : img.GetClasses(false)) {
                if (!cls.IsValid()) continue;
                MethodBase method = cls.GetMethod("GetHasPermissionToJoinRoom", 9); // 9 parameters based on context
                if (method.IsValid()) {
                    LOGD("InstallYeepsFixHooks: found GetHasPermissionToJoinRoom in %s", asmName.c_str());
                    void* addr = (void*)method.GetInfo()->methodPointer;
                    if (addr) {
                        DobbyHook(addr, (dobby_dummy_func_t)hook_GetHasPermissionToJoinRoom, (dobby_dummy_func_t*)&orig_GetHasPermissionToJoinRoom);
                        permissionHooked = true;
                        g_permissionHooked = true;
                        LOGD("InstallYeepsFixHooks: hooked GetHasPermissionToJoinRoom at %p", addr);
                        break;
                    }
                }
            }
            if (permissionHooked) break;
        }

        if (!permissionHooked) {
            LOGE("InstallYeepsFixHooks: failed to find GetHasPermissionToJoinRoom");
        }

        // Hook OnConnectedToMaster in PhotonConnectionManager
        Class pc("", "PhotonConnectionManager", Image("Assembly-CSharp.dll"));
        if (pc.IsValid()) {
            MethodBase onConnectedToMaster = pc.GetMethod("OnConnectedToMaster", 0);
            if (onConnectedToMaster.IsValid()) {
                LOGD("InstallYeepsFixHooks: found OnConnectedToMaster in PhotonConnectionManager");
                void* addr = (void*)onConnectedToMaster.GetInfo()->methodPointer;
                if (addr) {
                    DobbyHook(addr, (dobby_dummy_func_t)hook_OnConnectedToMaster, (dobby_dummy_func_t*)&orig_OnConnectedToMaster);
                    LOGD("InstallYeepsFixHooks: hooked OnConnectedToMaster at %p", addr);
                }
            } else {
                LOGE("InstallYeepsFixHooks: failed to find OnConnectedToMaster in PhotonConnectionManager");
            }
        } else {
            LOGE("InstallYeepsFixHooks: failed to find PhotonConnectionManager class");
        }

        // Start the join nudge monitor thread
        g_joinNudgeActive = true;
        std::thread monitorThread(joinNudgeMonitorThread);
        monitorThread.detach();
        LOGD("InstallYeepsFixHooks: join nudge monitor started");

    } catch (...) {
        LOGE("InstallYeepsFixHooks: EXCEPTION");
    }
}

// Attribute to run this when the library loads
__attribute__((constructor))
static void initialize() {
    LOGD("YeepsFix library initializing");
    InstallYeepsFixHooks();
}

__attribute__((destructor))
static void cleanup() {
    LOGD("YeepsFix library cleaning up");
    g_joinNudgeActive = false;
    g_permissionHooked = false;
}
