LOCAL_PATH := $(call my-dir)

include $(CLEAR_VARS)

LOCAL_MODULE := YeepsFix
LOCAL_SRC_FILES := native-lib-yeeps-fix.cpp

# Lua sources
LUA_SOURCES := lua/lapi.c lua/lauxlib.c lua/lbaselib.c lua/lcode.c lua/ldebug.c \
               lua/ldo.c lua/ldump.c lua/lfunc.c lua/lgc.c lua/llex.c lua/lmathlib.c \
               lua/lmem.c lua/lobject.c lua/lopcodes.c lua/lparser.c lua/lstate.c \
               lua/lstring.c lua/lstrlib.c lua/ltable.c lua/ltablib.c lua/ltm.c \
               lua/lundump.c lua/lvm.c lua/lzio.c lua/linit.c

LOCAL_SRC_FILES += $(LUA_SOURCES)

LOCAL_CPP_FEATURES += exceptions
LOCAL_CPPFLAGS += -std=c++20 -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0

LOCAL_C_INCLUDES := \
    $(LOCAL_PATH)/extern/BNM-Android/include \
    $(LOCAL_PATH)/extern/BNM-Android/external/include \
    $(LOCAL_PATH)/extern/BNM-Android/external \
    $(LOCAL_PATH)/extern/BNM-Android/external/utf8 \
    $(LOCAL_PATH)/extern/BNM-Android/src/private \
    $(LOCAL_PATH)/extern/Dobby/include \
    $(LOCAL_PATH)/Include \
    $(LOCAL_PATH)/explorer \
    $(LOCAL_PATH)/lua

# Include BNM as a static library
LOCAL_STATIC_LIBRARIES := BNM

# Include Dobby
LOCAL_STATIC_LIBRARIES += dobby

# Include OpenSSL
LOCAL_STATIC_LIBRARIES += crypto ssl

LOCAL_LDLIBS := -landroid -llog

include $(BUILD_SHARED_LIBRARY)

# Import BNM module
$(call import-add-path,$(LOCAL_PATH)/extern/BNM-Android)
$(call import-module,)

# Import Dobby
$(call import-add-path,$(LOCAL_PATH)/extern/Dobby)
$(call import-module,)

# Import OpenSSL
$(call import-add-path,$(LOCAL_PATH)/extern/OpenSSL/$(TARGET_ARCH_ABI))
$(call import-module,)
