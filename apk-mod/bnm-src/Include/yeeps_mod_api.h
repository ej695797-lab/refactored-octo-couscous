// Yeeps mod API (version 1). Shared by the game's loader (libBNMExplorer.so)
// and the mod SDK (github.com/Blazeisodd/yeeps-mods). C ABI only, so mods
// built with any NDK work. Never reorder or remove fields: add new ones at the
// end and bump YEEPS_MOD_API_VERSION.
#pragma once
#include <stdint.h>

#define YEEPS_MOD_API_VERSION 1

#ifdef __cplusplus
extern "C" {
#endif

typedef void (*YeepsUpdateFn)(float dt);
typedef void (*YeepsEventFn)(void);

typedef struct YeepsModApi {
    uint32_t version;              // YEEPS_MOD_API_VERSION of the game
    uint32_t size;                 // sizeof(YeepsModApi) in the game

    // Real il2cpp API name ("il2cpp_class_from_name") -> function. Handles the
    // random export names each Yeeps version uses, so mods never need a name map.
    void* (*resolve)(const char* il2cppApiName);
    void* il2cppHandle;            // dlopen handle of libil2cpp.so

    // Hooks go through the game's single Dobby. Returns 0 on success, -1 if the
    // target is already hooked (by the game or another mod) or invalid.
    int (*hook)(void* target, void* replacement, void** original);
    int (*unhook)(void* target);

    // Called on the Unity main thread once per frame (local player's
    // LateUpdate). Do ALL Unity work here, never from your own threads.
    void (*onUpdate)(YeepsUpdateFn fn);
    // Called when the local player object changes (room/scene load). Drop every
    // cached GameObject/Component pointer here.
    void (*onRoomChange)(YeepsEventFn fn);

    void (*log)(const char* msg);              // game log (rate limited)
    int (*hasRole)(const char* roleKey);       // 1 if the server gave this account the role
    const char* gameVersion;                   // e.g. "2.32.0"
    const char* buildId;                       // e.g. "live84"
    const char* modsDir;                       // folder the mods were loaded from

    // The local player's MasterPlayer object (Il2CppObject*), or NULL before the
    // first frame. Changes on room/scene load: re-read it after onRoomChange.
    void* (*localPlayer)(void);
} YeepsModApi;

// A mod exports these two functions.
//   const char* YeepsMod_Name(void);                 // shown in the log
//   int YeepsMod_Init(const YeepsModApi* api);       // return 0 on success
// YeepsMod_Init runs on the il2cpp thread right after the game's il2cpp is
// ready, BEFORE any scene exists: set up BNM and hooks there, touch Unity
// objects only from onUpdate.

#ifdef __cplusplus
}
#endif
