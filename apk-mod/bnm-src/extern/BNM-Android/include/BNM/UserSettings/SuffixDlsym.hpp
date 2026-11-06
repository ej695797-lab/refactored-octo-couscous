#pragma once
// Fallback symbol resolver for IL2CPP builds where the toolchain has
// renamed exported symbols to random strings but kept the original
// "<library_prefix>_<rest_of_name>" shape intact (e.g. "il2cpp_init"
// becomes "Xk3fQz_init"). Tries an exact dlsym() first; if that misses,
// walks the target module's own ELF dynamic symbol table looking for a
// name whose portion after its first underscore matches the requested
// symbol's portion after its first underscore.

#include <dlfcn.h>
#include <link.h>
#include <elf.h>
#include <cstring>
#include <cstdint>
#include <cstdio>
#include <cstdarg>
#include <android/log.h>

#define BNM_SUFFIX_TAG "BNM-SuffixDlsym"

// Plain-file checkpoint logging: bypasses logcat/log-hook timing races
// entirely. Verifiable with a simple `adb pull` after the fact.
inline void BNM_FileCheckpoint(const char *msg) {
    FILE *f = fopen("/sdcard/Android/data/com.TrassGames.Yeeps/files/bnm_checkpoint.txt", "a");
    if (!f) f = fopen("/data/local/tmp/bnm_checkpoint.txt", "a");
    if (!f) f = fopen("/sdcard/bnm_checkpoint.txt", "a");
    if (f) {
        fprintf(f, "%s\n", msg);
        fclose(f);
    }
}

inline void BNM_FileCheckpointF(const char *fmt, ...) {
    char buf[512];
    va_list args;
    va_start(args, fmt);
    vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    BNM_FileCheckpoint(buf);
}

struct BNM_SuffixFindCtx {
    const char *targetSuffix;
    void *result;
};

inline int BNM_SuffixPhdrCallback(struct dl_phdr_info *info, size_t, void *data) {
    auto *ctx = static_cast<BNM_SuffixFindCtx *>(data);

    // We only care about the currently-loaded libil2cpp.so; identify it by
    // name substring rather than by handle (avoids needing RTLD_DI_LINKMAP,
    // which Bionic's dlfcn.h doesn't expose).
    if (!info->dlpi_name || !strstr(info->dlpi_name, "il2cpp")) return 0;

    auto base = static_cast<uintptr_t>(info->dlpi_addr);
    auto *ehdr = reinterpret_cast<const Elf64_Ehdr *>(base);
    if (memcmp(ehdr->e_ident, ELFMAG, SELFMAG) != 0) return 0;

    auto *phdrs = reinterpret_cast<const Elf64_Phdr *>(base + ehdr->e_phoff);
    const Elf64_Dyn *dynamic = nullptr;
    for (int i = 0; i < ehdr->e_phnum; i++) {
        if (phdrs[i].p_type == PT_DYNAMIC) {
            dynamic = reinterpret_cast<const Elf64_Dyn *>(base + phdrs[i].p_vaddr);
            break;
        }
    }
    if (!dynamic) return 0;

    const Elf64_Sym *symtab = nullptr;
    const char *strtab = nullptr;
    size_t strsz = 0;
    size_t symCount = 0;
    const uint32_t *gnuHash = nullptr;

    for (const Elf64_Dyn *d = dynamic; d->d_tag != DT_NULL; d++) {
        switch (d->d_tag) {
            case DT_SYMTAB:
                symtab = reinterpret_cast<const Elf64_Sym *>(base + d->d_un.d_ptr);
                break;
            case DT_STRTAB:
                strtab = reinterpret_cast<const char *>(base + d->d_un.d_ptr);
                break;
            case DT_STRSZ:
                strsz = d->d_un.d_val;
                break;
            case DT_HASH: {
                const auto *hash = reinterpret_cast<const uint32_t *>(base + d->d_un.d_ptr);
                symCount = hash[1]; // nchain == total symbol count for classic hash
                break;
            }
            case DT_GNU_HASH:
                gnuHash = reinterpret_cast<const uint32_t *>(base + d->d_un.d_ptr);
                break;
            default:
                break;
        }
    }
    if (!symtab || !strtab) return 0;

    __android_log_print(ANDROID_LOG_ERROR, BNM_SUFFIX_TAG, "CHECKPOINT A: found %s, have symtab+strtab, gnuHash=%p, hashCount=%zu",
                         info->dlpi_name, (void*)gnuHash, symCount);
    BNM_FileCheckpointF("CHECKPOINT A: found %s, have symtab+strtab, gnuHash=%p, hashCount=%zu",
                         info->dlpi_name, (void*)gnuHash, symCount);

    if (symCount == 0 && gnuHash) {
        uint32_t nbuckets = gnuHash[0];
        uint32_t symoffset = gnuHash[1];
        uint32_t bloomSize = gnuHash[2];
        const uint32_t *buckets = gnuHash + 4 + bloomSize * 2; // 2 * uint32 per 8-byte bloom word
        const uint32_t *chain = buckets + nbuckets;

        uint32_t maxIdx = symoffset;
        for (uint32_t i = 0; i < nbuckets; i++) {
            if (buckets[i] > maxIdx) maxIdx = buckets[i];
        }
        __android_log_print(ANDROID_LOG_ERROR, BNM_SUFFIX_TAG,
                             "CHECKPOINT B: nbuckets=%u symoffset=%u bloomSize=%u maxIdx=%u",
                             nbuckets, symoffset, bloomSize, maxIdx);
    BNM_FileCheckpointF("CHECKPOINT B: nbuckets=%u symoffset=%u bloomSize=%u maxIdx=%u",
                             nbuckets, symoffset, bloomSize, maxIdx);
        if (maxIdx >= symoffset) {
            uint32_t idx = maxIdx;
            uint32_t guard = 0;
            while (guard < 200000) {
                uint32_t chainVal = chain[idx - symoffset];
                idx++;
                guard++;
                if (chainVal & 1) break; // low bit set marks end of this bucket's chain
            }
            symCount = idx;
        }
        __android_log_print(ANDROID_LOG_ERROR, BNM_SUFFIX_TAG,
                             "CHECKPOINT C: gnu_hash-derived symCount=%zu", symCount);
    BNM_FileCheckpointF("CHECKPOINT C: gnu_hash-derived symCount=%zu", symCount);
    }

    if (symCount == 0) {
        __android_log_print(ANDROID_LOG_WARN, BNM_SUFFIX_TAG,
                             "could not determine symbol count for %s", info->dlpi_name);
    BNM_FileCheckpointF("could not determine symbol count for %s", info->dlpi_name);
        return 0;
    }

    // Safety cap: a mis-derived GNU_HASH count could otherwise turn this
    // into an effectively-infinite loop over garbage memory.
    size_t cappedCount = symCount;
    if (cappedCount > 300000) {
        __android_log_print(ANDROID_LOG_ERROR, BNM_SUFFIX_TAG,
                             "CHECKPOINT D: symCount %zu exceeds sanity cap, clamping to 300000", cappedCount);
    BNM_FileCheckpointF("CHECKPOINT D: symCount %zu exceeds sanity cap, clamping to 300000", cappedCount);
        cappedCount = 300000;
    }
    symCount = cappedCount;

    __android_log_print(ANDROID_LOG_ERROR, BNM_SUFFIX_TAG, "CHECKPOINT E: starting scan loop, symCount=%zu targetSuffix=%s",
                         symCount, ctx->targetSuffix);
    BNM_FileCheckpointF("CHECKPOINT E: starting scan loop, symCount=%zu targetSuffix=%s",
                         symCount, ctx->targetSuffix);

    // Trass's obfuscator replaces the real "il2cpp_" prefix (including its
    // separating underscore) with a random 7-character blob whose charset
    // can itself include underscores (observed: "Oxqvhf_init" - 7 chars
    // "Oxqvhf_"; "nRfgo__runtime_invoke" - 7 chars "nRfgo__" with the random
    // part ending in "_"; "nx_VkO_type_get_class_or_element_class" - 7
    // chars "nx_VkO_" with an underscore embedded mid-blob). Splitting on
    // "the first underscore" breaks whenever the random blob contains one
    // itself, so match by total length instead: real suffix length + a
    // fixed 7-character prefix width, verified via a trailing-substring
    // compare. This also naturally rejects unrelated real symbols that
    // happen to share a suffix, e.g. libc's "sem_init" (prefix width 4,
    // not 7) colliding with "il2cpp_init".
    size_t targetSuffixLen = strlen(ctx->targetSuffix);
    for (size_t i = 0; i < symCount; i++) {
        uint32_t nameOff = symtab[i].st_name;
        if (nameOff >= strsz) continue;
        const char *symName = strtab + nameOff;
        size_t nameLen = strlen(symName);
        if (nameLen != targetSuffixLen + 7) continue;
        const char *symSuffix = symName + 7;
        if (strcmp(symSuffix, ctx->targetSuffix) == 0) {
            uintptr_t addr = base + symtab[i].st_value;
            ctx->result = reinterpret_cast<void *>(addr);
            __android_log_print(ANDROID_LOG_INFO, BNM_SUFFIX_TAG,
                                 "resolved via suffix match -> %s @ %p", symName, ctx->result);
    BNM_FileCheckpointF("resolved via suffix match -> %s @ %p", symName, ctx->result);
            return 1; // stop iterating, found it
        }
    }

    __android_log_print(ANDROID_LOG_WARN, BNM_SUFFIX_TAG,
                         "CHECKPOINT F: no suffix match for '%s' (scanned %zu symbols in %s)",
                         ctx->targetSuffix, symCount, info->dlpi_name);
    BNM_FileCheckpointF("CHECKPOINT F: no suffix match for '%s' (scanned %zu symbols in %s)",
                         ctx->targetSuffix, symCount, info->dlpi_name);
    return 1; // stop after scanning the il2cpp module regardless of match
}

inline void *BNM_dlsym_suffix_impl(void *handle, const char *name) {
    __android_log_print(ANDROID_LOG_ERROR, BNM_SUFFIX_TAG, "CHECKPOINT 0: BNM_dlsym_suffix_impl('%s')", name);
    BNM_FileCheckpointF("CHECKPOINT 0: BNM_dlsym_suffix_impl('%s')", name);
    void *exact = dlsym(handle, name);
    if (exact) {
        __android_log_print(ANDROID_LOG_ERROR, BNM_SUFFIX_TAG, "CHECKPOINT 1: exact dlsym hit for '%s'", name);
    BNM_FileCheckpointF("CHECKPOINT 1: exact dlsym hit for '%s'", name);
        return exact;
    }

    const char *firstUnderscore = strchr(name, '_');
    if (!firstUnderscore) return nullptr;
    const char *targetSuffix = firstUnderscore + 1;
    if (*targetSuffix == '\0') return nullptr;

    __android_log_print(ANDROID_LOG_ERROR, BNM_SUFFIX_TAG, "CHECKPOINT 2: starting dl_iterate_phdr for suffix '%s'", targetSuffix);
    BNM_FileCheckpointF("CHECKPOINT 2: starting dl_iterate_phdr for suffix '%s'", targetSuffix);
    BNM_SuffixFindCtx ctx{targetSuffix, nullptr};
    dl_iterate_phdr(BNM_SuffixPhdrCallback, &ctx);
    __android_log_print(ANDROID_LOG_ERROR, BNM_SUFFIX_TAG, "CHECKPOINT 3: dl_iterate_phdr returned, result=%p", ctx.result);
    BNM_FileCheckpointF("CHECKPOINT 3: dl_iterate_phdr returned, result=%p", ctx.result);
    return ctx.result;
}
