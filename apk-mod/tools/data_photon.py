"""biglive20 data: the stock 2.31.4 data.unity3d with ONE change - PhotonServerSettings
points at the owner's Photon apps (realtime + voice) instead of Yeeps' own, so players
join our Photon (whose custom auth calls our server) and never Yeeps'.

The two app-id strings are same-length GUIDs at raw offsets 52 (realtime) and 100
(voice) of PhotonServerSettings; the values are taken from the 2.4 build's data
(apk-work/big/work/data_big51.unity3d), the owner's apps. Nothing else is touched.

Usage: data_photon.py <stock data.unity3d> <out data.unity3d>
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APK_WORK = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, APK_WORK)
import sfpatch240  # noqa: E402
import UnityPy     # noqa: E402

SRC, DST = sys.argv[1:3]
OWN_DATA = os.path.join(APK_WORK, "big", "work", "data_big51.unity3d")
PHOTON_OFFS = (52, 100)


def text_at(raw, off):
    n = struct.unpack_from("<i", raw, off)[0]
    if not 0 < n < 200 or off + 4 + n > len(raw):
        return None
    return raw[off + 4:off + 4 + n].decode("utf-8", "replace")


def photon_settings(env):
    for o in env.objects:
        if o.type.name == "MonoBehaviour":
            try:
                if o.peek_name() == "PhotonServerSettings":
                    return o
            except Exception:
                pass
    raise SystemExit("PhotonServerSettings not found")


own_raw = photon_settings(UnityPy.load(OWN_DATA)).get_raw_data()
env = UnityPy.load(SRC)
ps = photon_settings(env)
raw = bytearray(ps.get_raw_data())
for off, what in zip(PHOTON_OFFS, ("realtime", "voice")):
    want, have = text_at(own_raw, off), text_at(bytes(raw), off)
    assert want and have and len(want) == len(have) == 36, f"photon field @{off} unexpected"
    raw[off + 4:off + 40] = want.encode()
    print(f"Photon {what} app id -> {want}")
ps.set_raw_data(bytes(raw))
open(DST, "wb").write(sfpatch240.save_bundle(env, repack=()))
print("saved", DST, os.path.getsize(DST))
