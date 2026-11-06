"""live50: BLAZE Screen + BLAZE Pillow build-menu cost 0 -> 150 (like the BAM trophy).
Cost-0 menu entries only show in creative mode ("the screen is still counted as a
creative mode block"). Owners get them through ownership anyway.
Usage: blaze_menu_cost.py <in data.unity3d> <out data.unity3d>
"""
import sys

import UnityPy

sys.path.insert(0, "../big/tools")
import patterndb  # noqa: E402

SRC, DST = sys.argv[1:3]
env = UnityPy.load(SRC)
pm = next(o for o in env.objects if o.type.name == "MonoBehaviour" and o.assets_file.name == "level2"
          and b"2x2x2_stuffedTrophie_BAM2" in o.get_raw_data() and o.get_raw_data().count(b"mapObject") > 100)
raw = pm.get_raw_data()
head, groups, tail = patterndb.parse(raw, live=True)
assert patterndb.write(head, groups, tail, live=True) == raw
n = 0
for g in groups:
    for e in g["entries"]:
        if e["key"] in ("3x2x1_blazeScreenRole", "1x5x2_stuffedTextBlaze"):
            print(e["key"], "cost", e["cost"], "-> 150"); e["cost"] = 150; n += 1
assert n == 2, n
pm.set_raw_data(patterndb.write(head, groups, tail, live=True))
open(DST, "wb").write(env.file.save(packer="lz4"))
print("saved", DST)
