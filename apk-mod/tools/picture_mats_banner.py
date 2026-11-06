"""Move the Cucumber Cat + BLAZE Screen pictures onto the game's picture shader (live43).

The decoration shader (Custom/MapObjectSolidOrganic) kept rendering them white /
garbled even with _SAMPLE_TEXTURE on. Banners and key art - real blocks that show a
picture - use "Shader Graphs/MapObjectDephysicalizedTextured" with plain settings.
cucumberCat / blazeScreen_blaze become copies of the dephysicalized_banners material
(same shader + properties) with their own picture in _MainTex.
Usage: picture_mats_banner.py <in data.unity3d> <out data.unity3d>
"""
import copy
import sys

import UnityPy

SRC, DST = sys.argv[1:3]
env = UnityPy.load(SRC)
mats = {o.peek_name(): o for o in env.objects
        if o.type.name == "Material" and o.assets_file.name == "resources.assets"
        and o.peek_name() in ("dephysicalized_banners", "cucumberCat", "blazeScreen_blaze")}
tmpl = mats["dephysicalized_banners"].read_typetree()
for name in ("cucumberCat", "blazeScreen_blaze"):
    o = mats[name]
    old = o.read_typetree()
    pic = dict(old["m_SavedProperties"]["m_TexEnvs"])["_MainTex"]
    t = copy.deepcopy(tmpl)
    t["m_Name"] = name
    t["m_SavedProperties"]["m_TexEnvs"] = [(k, (dict(v, m_Texture=pic["m_Texture"], m_Scale={"x": 1.0, "y": 1.0},
                                                    m_Offset={"x": 0.0, "y": 0.0}) if k == "_MainTex" else v))
                                           for k, v in t["m_SavedProperties"]["m_TexEnvs"]]
    t["m_SavedProperties"]["m_Colors"] = [(k, ({"r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0} if k == "_BaseColor" else v))
                                          for k, v in t["m_SavedProperties"]["m_Colors"]]
    o.save_typetree(t)
    print(f"{name}: shader {t['m_Shader']} (banner shader), picture pathID {pic['m_Texture']['m_PathID']}")
open(DST, "wb").write(env.file.save(packer="lz4"))
print("saved", DST)
