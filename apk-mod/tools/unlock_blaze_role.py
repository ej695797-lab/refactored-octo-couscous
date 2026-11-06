"""Clear the in-game "blaze" role lock on the BLAZE Pillow + BLAZE Screen (2026-10-17).

2.31.4/2.32 only honour roles in the game's role list (developer, tester, bamWinner,
cwVIP, ...); "blaze" isn't one, so blocks locked to it never appear for anyone.
The server already grants these keys only to blaze-role accounts (login.py
role_locks), so the MapObject role field is set to "" and ownership does the gating.

Usage: unlock_blaze_role.py <in data.unity3d> <out data.unity3d>
"""
import struct
import sys

import UnityPy

SRC, DST = sys.argv[1:3]
OLD = struct.pack("<i", 5) + b"blaze" + b"\0" * 3
NEW = struct.pack("<i", 0)
env = UnityPy.load(SRC)
n = 0
for o in env.objects:
    if o.type.name != "MonoBehaviour" or o.assets_file.name != "resources.assets":
        continue
    raw = o.get_raw_data()
    if OLD in raw and len(raw) < 300:          # the two small MapObject scripts (name field is empty)
        assert raw.count(OLD) == 1
        o.set_raw_data(raw.replace(OLD, NEW))
        n += 1
        print("unlocked MapObject", o.path_id, len(raw))
assert n == 2, n
open(DST, "wb").write(env.file.save(packer="lz4"))
print("saved", DST)
