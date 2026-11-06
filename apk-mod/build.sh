#!/bin/bash
# 2.31.4-blaze from the STOCK APK (Blaze's ~/Downloads/2.31.4-Yeeps.apk), only the
# needed changes (see assemble.py). Usage: ./build.sh [DATA] [ENTRY=PATH ...]
#   DATA defaults to data/data_photon.unity3d (stock data + our Photon app ids).
set -e
set -o pipefail   # a failed ninja must stop the build (it used to package the old .so)
cd "$(dirname "$0")"
# Default = the current live base (2.32.0 public 957). Building on 2.31.4 by accident made a bad APK.
STOCK=${STOCK:?set STOCK to the stock Yeeps APK}   # override: STOCK=... VER=... ./build.sh
VER=${VER:-2.32.0}
BID=${BID:-$(grep -oE 'kBuildId = "(biglive|live)[0-9]+"' bnm-src/native-lib.cpp | grep -oE '(biglive|live)[0-9]+')}
DATA=${1:-data/data_photon.unity3d}; shift || true
REPL=(); for r in "$@"; do REPL+=(--replace "$r"); done
# Owner 2026-10-30: the player mod loader ships in EVERY build, like BNM. Refuse to
# build if any part of it went missing (e.g. after rebasing native-lib.cpp).
for need in 'LoadPlayerMods();' 'ModCrashMark(' 'g_modMasterPlayerClass' '#include "Include/yeeps_mod_api.h"'; do
    grep -qF "$need" bnm-src/native-lib.cpp || { echo "MOD LOADER MISSING: '$need' not in native-lib.cpp" >&2; exit 1; }
done
[ -f bnm-src/Include/yeeps_mod_api.h ] || { echo "MOD LOADER MISSING: bnm-src/Include/yeeps_mod_api.h" >&2; exit 1; }
(cd bnm-build && ninja | tail -1)
grep -qa "YeepsMod_Init" bnm-build/libBNMExplorer.so || { echo "MOD LOADER MISSING from libBNMExplorer.so" >&2; exit 1; }
TAG=$(BID=$BID STOCK=$STOCK python3 - <<'PY'
import os, zipfile
def fnv(b):
    h = 1469598103934665603
    for x in b:
        h ^= x; h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h
il = fnv(zipfile.ZipFile(os.environ["STOCK"]).read("lib/arm64-v8a/libil2cpp.so"))
bn = fnv(open("bnm-build/libBNMExplorer.so", "rb").read())
print(f"{os.environ['BID']}.{il:016x}.{bn:016x}")
PY
)
echo "build $BID  tag $TAG"
mkdir -p out
python3 assemble.py "$STOCK" out/_unsigned.apk --data "$DATA" "${REPL[@]}"
zipalign -p -f 4 out/_unsigned.apk out/_aligned.apk
apksigner sign --ks "${KEYSTORE:?set KEYSTORE to your signing keystore}" --ks-pass env:KS_PASS --out "out/Yeeps-$VER-blaze-$BID.apk" out/_aligned.apk
rm -f out/_unsigned.apk out/_aligned.apk
echo "$TAG" > "out/expected_build_tag_$BID.txt"
apksigner verify "out/Yeeps-$VER-blaze-$BID.apk" && echo "signature OK"
unzip -v "out/Yeeps-$VER-blaze-$BID.apk" | grep -E "data.unity3d|\.resource$|classes.dex|libBNMExplorer|libTomahawkCore" | awk '{print $2, $1, $NF}'
ls -la "out/Yeeps-$VER-blaze-$BID.apk"
# Owner 2026-10-17: keep only the newest build (no pile of 700 MB APKs).
for f in out/Yeeps-*-blaze-*.apk out/Yeeps-*-blaze-*.apk.idsig; do
    case "$f" in "out/Yeeps-$VER-blaze-$BID.apk"|"out/Yeeps-$VER-blaze-$BID.apk.idsig") ;; *) rm -f "$f" ;; esac
done
