// libTomahawkCore.so replacement for the 2.31.4-blaze build (biglive20, 2026-10-13).
// Tomahawk is Yeeps' network/integrity SDK: it reads the APK's package signature,
// produces a "session nonce" and runs its own network monitor (a full curl stack).
// The owner wants it removed, so every one of the real library's 19 exports is here
// as a harmless no-op. Values match what already worked on the live builds, where
// tomahawkCoreInitialize was a no-op: status stays 0 (not started, not complete, not
// failed), nonce/error/check buffers come back empty, and the network reports "up".
// Signatures follow the real library's code: the buffer functions take (char*, int)
// and write a NUL; GetError takes (index, char*, int).
#include <jni.h>

#define EXPORT __attribute__((visibility("default")))

static int empty_buf(char* buf, int len) {
    if (buf && len > 0) buf[0] = 0;
    return 0;
}

EXPORT jint JNI_OnLoad(JavaVM* vm, void* reserved) { (void)vm; (void)reserved; return JNI_VERSION_1_6; }

EXPORT void tomahawkCoreInitialize(void) {}
EXPORT int  tomahawkCoreGetStatus(void) { return 0; }
EXPORT int  tomahawkCoreIsComplete(void) { return 0; }
EXPORT int  tomahawkCoreIsFailed(void) { return 0; }
EXPORT int  tomahawkCoreGetErrorCount(void) { return 0; }
EXPORT int  tomahawkCoreGetError(int index, char* buf, int len) { (void)index; return empty_buf(buf, len); }
EXPORT int  tomahawkCoreGetSessionNonce(char* buf, int len) { return empty_buf(buf, len); }
EXPORT void tomahawkCoreStartNetworkMonitor(void) {}
EXPORT void tomahawkCoreStopNetworkMonitor(void) {}
EXPORT void tomahawkCoreSetNetworkCallback(void* cb) { (void)cb; }
EXPORT int  tomahawkCoreIsNetworkAvailable(void) { return 1; }
EXPORT int  tomahawkCoreIsVPNActive(void) { return 0; }
EXPORT int  isTomahawkReInit(void) { return 0; }
EXPORT void setTomahawkLogCallback(void* cb) { (void)cb; }

EXPORT int  T_CV(char* buf, int len) { return empty_buf(buf, len); }
EXPORT int  T_CVR(char* buf, int len) { return empty_buf(buf, len); }
EXPORT void T_FC(void* ctx) { (void)ctx; }
EXPORT void T_SD(void) {}
