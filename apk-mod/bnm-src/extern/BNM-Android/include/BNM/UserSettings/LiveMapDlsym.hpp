#pragma once
// Symbol resolver for the live 2.31.4 base build. That binary's obfuscator
// renamed every il2cpp API export to a fully-random 11-char name with NO
// readable suffix, so SuffixDlsym can't work. But the live libil2cpp and our
// 2.4 reference libil2cpp are the same Unity version (6000.1.17f1), so the API
// export block aligns 1:1 by address order. LiveIl2CppNameMap.hpp is the baked
// real-name -> live-random-name table produced from that alignment
// (apk-work/big/work/livebase/il2cpp_api_namemap.tsv). We translate the
// requested real il2cpp name to the live random name, then dlsym() that.

#include <dlfcn.h>
#include "SuffixDlsym.hpp"       // for BNM_FileCheckpoint / BNM_FileCheckpointF
#include "LiveIl2CppNameMap.hpp" // for BNM_LiveMapLookup()
#include "LiveIl2CppNameMap232.hpp" // 2.32.0_lab1 base: same table, its own random names
#include "LiveIl2CppNameMapV3.hpp"    // 2026-10-19 new Yeeps version: third random naming
#include "LiveIl2CppNameMapV4.hpp"    // 2026-10-22 public 2.32.0 (versionCode 957): fourth random naming

inline void *BNM_dlsym_livemap_impl(void *handle, const char *name) {
    // Non-API symbols (not il2cpp_*) are requested verbatim; try exact first.
    if (!name || name[0] != 'i' || strncmp(name, "il2cpp_", 7) != 0) {
        return dlsym(handle, name);
    }
    const char *live = BNM_LiveMapLookup(name);
    if (live) {
        void *p = dlsym(handle, live);
        if (p) return p;
        // 2.32 base (live32+): the same export block under new random names.
        int i = BNM_LiveMapIndex(name);
        if (i >= 0 && (p = dlsym(handle, kBNMLiveNameMap232[i]))) return p;
        if (i >= 0 && (p = dlsym(handle, kBNMLiveNameMapV3[i]))) return p;
        if (i >= 0 && (p = dlsym(handle, kBNMLiveNameMapV4[i]))) return p;
        BNM_FileCheckpointF("LiveMap: '%s' -> '%s' / '%s' / '%s' / '%s' but dlsym missed", name, live,
                            i >= 0 ? kBNMLiveNameMap232[i] : "?", i >= 0 ? kBNMLiveNameMapV3[i] : "?",
                            i >= 0 ? kBNMLiveNameMapV4[i] : "?");
        return nullptr;
    }
    // Not in the map: either a non-exported il2cpp type/global, or an API
    // function this Unity build doesn't export (our 2.4 reference lacks it too,
    // and BNM guards those). Fall back to an exact lookup just in case.
    BNM_FileCheckpointF("LiveMap: no mapping for '%s'", name);
    return dlsym(handle, name);
}
