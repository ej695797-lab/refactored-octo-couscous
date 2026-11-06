"""Give the Cucumber Cat + BLAZE Screen picture materials a Resources path (live35),
so the native lean picture hook can Resources.Load them:
  blazecat/mat_cucumbercat  -> Material "cucumberCat"        (resources.assets)
  blazescreen/mat_blaze     -> Material "blazeScreen_blaze"  (resources.assets)
Usage: add_picture_paths.py <in data.unity3d> <out data.unity3d>
"""
import sys

import UnityPy

SRC, DST = sys.argv[1:3]
WANT = {"cucumberCat": "blazecat/mat_cucumbercat", "blazeScreen_blaze": "blazescreen/mat_blaze"}
env = UnityPy.load(SRC)
rm_obj = next(o for o in env.objects if o.type.name == "ResourceManager")
rm = rm_obj.read_typetree()
cont = dict((k, v) for k, v in rm["m_Container"])
res_fid = cont["mapobjects/decoration/1x4x2_forbidden_textyeep"]["m_FileID"]
mats = {o.peek_name(): o.path_id for o in env.objects
        if o.type.name == "Material" and o.assets_file.name == "resources.assets" and o.peek_name() in WANT}
assert set(mats) == set(WANT), mats
for name, path in WANT.items():
    if path in cont:
        print("already", path); continue
    rm["m_Container"].append((path, {"m_FileID": res_fid, "m_PathID": mats[name]}))
    print(f"{path} -> {name} (pathID {mats[name]}, fileID {res_fid})")
rm_obj.save_typetree(rm)
open(DST, "wb").write(env.file.save(packer="lz4"))
print("saved", DST)
