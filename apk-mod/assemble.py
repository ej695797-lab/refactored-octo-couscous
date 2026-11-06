"""Make the biglive APK from the STOCK 2.31.4 APK by swapping individual zip entries
(no full apktool rebuild): every entry keeps its original order and compression
(data.unity3d / *.resource / classes.dex stay Stored, libs stay Deflated), the old
signature is dropped, and only these change:
  classes.dex                         <- dec/build/apk/classes.dex (BNM load + Talsec off)
  lib/arm64-v8a/libBNMExplorer.so     <- bnm-build/libBNMExplorer.so (added)
  lib/arm64-v8a/libTomahawkCore.so    <- tomahawk_stub/libTomahawkCore.so
  assets/bin/Data/data.unity3d        <- the data given with --data
  extra --replace entry=path pairs (sharedassets*.resource for baked songs, ...)

Usage: assemble.py STOCK.apk OUT_unsigned.apk --data DATA [--replace ENTRY=PATH ...]
"""
import argparse
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("stock")
ap.add_argument("out")
ap.add_argument("--data", required=True)
ap.add_argument("--replace", action="append", default=[])
a = ap.parse_args()

swap = {
    "classes.dex": os.path.join(HERE, "dec", "build", "apk", "classes.dex"),
    "lib/arm64-v8a/libTomahawkCore.so": os.path.join(HERE, "tomahawk_stub", "libTomahawkCore.so"),
    "assets/bin/Data/data.unity3d": a.data,
}
for r in a.replace:
    k, v = r.split("=", 1)
    swap[k] = v
add = {"lib/arm64-v8a/libBNMExplorer.so": os.path.join(HERE, "bnm-build", "libBNMExplorer.so")}
for p in list(swap.values()) + list(add.values()):
    assert os.path.isfile(p), f"missing {p}"


def is_signature(name):
    if not name.startswith("META-INF/"):
        return False
    base = name[len("META-INF/"):]
    return "/" not in base and (base == "MANIFEST.MF" or base.upper().endswith((".SF", ".RSA", ".DSA", ".EC")))


src = zipfile.ZipFile(a.stock)
done = set()
with zipfile.ZipFile(a.out, "w", allowZip64=True) as out:
    for info in src.infolist():
        if is_signature(info.filename):
            continue
        zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
        zi.compress_type = info.compress_type
        zi.external_attr = info.external_attr
        if info.filename in swap:
            with open(swap[info.filename], "rb") as f:
                out.writestr(zi, f.read())
            done.add(info.filename)
            print(f"replaced {info.filename} ({'Stored' if zi.compress_type == 0 else 'Deflated'})")
        else:
            with src.open(info) as f:
                out.writestr(zi, f.read())
        if info.filename == "lib/arm64-v8a/libil2cpp.so":
            for name, path in add.items():
                zn = zipfile.ZipInfo(name, date_time=info.date_time)
                zn.compress_type = zipfile.ZIP_DEFLATED
                zn.external_attr = info.external_attr
                with open(path, "rb") as f:
                    out.writestr(zn, f.read())
                done.add(name)
                print(f"added {name}")
missing = (set(swap) | set(add)) - done
assert not missing, f"not written: {missing}"
print("wrote", a.out, os.path.getsize(a.out))
