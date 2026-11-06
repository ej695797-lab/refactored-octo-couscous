"""live47: the BLAZE Screen becomes a pillow block (owner 2026-10-17: "the screen block
can't be picked up, make it a pillow block").

Its MapObject now uses the game's cotton/pillow script (the one 1x1_stuffed and the
BLAZE Pillow use) with the pillow category + sounds, so it can be grabbed and moved like
a pillow. Mesh, picture and size are unchanged.
Usage: screen_as_pillow.py <in data.unity3d> <out data.unity3d>
"""
import struct
import sys

import UnityPy

sys.path.insert(0, "../big/tools")
import patterndb  # noqa: E402

SRC, DST = sys.argv[1:3]


def s(t):
    b = t.encode()
    return struct.pack("<i", len(b)) + b + b"\0" * ((-len(b)) % 4)


def mo_parse(raw):
    r = patterndb.R(raw, 28)
    d = {"head": raw[:16], "script": raw[16:28], "name": r.s(), "size": [r.i(), r.i(), r.i()],
         "pre": [r.i() for _ in range(4)], "role": r.s(), "mid": r.i(), "category": r.s(), "place": r.s()}
    d["vol"] = struct.unpack_from("<f", raw, r.p)[0]; r.p += 4
    d.update(pickup=r.s(), brk=r.s(), footstep=r.s(), tail=[r.i(), r.i(), r.i()])
    assert r.p == len(raw)
    return d


def mo_raw(d):
    return (d["head"] + d["script"] + s(d["name"]) + struct.pack("<3i", *d["size"]) + struct.pack("<4i", *d["pre"])
            + s(d["role"]) + struct.pack("<i", d["mid"]) + s(d["category"]) + s(d["place"]) + struct.pack("<f", d["vol"])
            + s(d["pickup"]) + s(d["brk"]) + s(d["footstep"]) + struct.pack("<3i", *d["tail"]))


env = UnityPy.load(SRC)
res = {o.path_id: o for o in env.objects if o.assets_file.name == "resources.assets"}
def mb_of(goname):
    go = next(o for o in res.values() if o.type.name == "GameObject" and o.peek_name() == goname)
    for c in go.read_typetree()["m_Component"]:
        o = res.get(c["component"]["m_PathID"])
        if o and o.type.name == "MonoBehaviour":
            return o
pillow = mo_parse(mb_of("1x1_stuffed").get_raw_data())
scr_o = mb_of("3x2x1_blazeScreenRole")
raw = scr_o.get_raw_data()
d = mo_parse(raw)
assert mo_raw(d) == raw, "re-encode mismatch"
d.update(script=pillow["script"], category="stuffed", place="placePillow", pickup="pickupPillow",
         brk="breakPillow", footstep="footstepPillow")
scr_o.set_raw_data(mo_raw(d))
# Grabbable pillows (1x1_stuffed, BLAZE Pillow, BAM trophy) carry GameObject tag 20000;
# the screen had tag 0, so the game never let it be picked up (live48 test).
pgo = next(o for o in res.values() if o.type.name == "GameObject" and o.peek_name() == "1x1_stuffed")
sgo = next(o for o in res.values() if o.type.name == "GameObject" and o.peek_name() == "3x2x1_blazeScreenRole")
tag = pgo.read_typetree()["m_Tag"]
st = sgo.read_typetree(); st["m_Tag"] = tag; sgo.save_typetree(st)
print("screen GameObject tag ->", tag)
print("screen script", raw[16:28].hex(), "->", pillow["script"].hex(), "category stuffed, pillow sounds")
open(DST, "wb").write(env.file.save(packer="lz4"))
print("saved", DST)
