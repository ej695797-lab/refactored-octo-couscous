"""Turn on texture sampling for the picture materials (live39, 2026-10-17).

The decoration shader only samples _MainTex when the material has keyword
_SAMPLE_TEXTURE and float _SampleTexture = 1. cucumberCat / blazeScreen_blaze were
cloned from solid_decoration_1 (no texture) so they rendered plain white.
Usage: fix_picture_mats.py <in data.unity3d> <out data.unity3d>
"""
import sys

import UnityPy

SRC, DST = sys.argv[1:3]
NAMES = {"cucumberCat", "blazeScreen_blaze"}
env = UnityPy.load(SRC)
n = 0
for o in env.objects:
    if o.type.name != "Material" or o.assets_file.name != "resources.assets" or o.peek_name() not in NAMES:
        continue
    t = o.read_typetree()
    key = "m_ValidKeywords" if "m_ValidKeywords" in t else "m_ShaderKeywords"
    kw = t[key]
    if isinstance(kw, str):
        parts = kw.split()
        if "_SAMPLE_TEXTURE" not in parts:
            parts.append("_SAMPLE_TEXTURE")
        t[key] = " ".join(parts)
    elif "_SAMPLE_TEXTURE" not in kw:
        kw.append("_SAMPLE_TEXTURE")
    fl = t["m_SavedProperties"]["m_Floats"]
    d = dict(fl)
    print(o.peek_name(), "before: _SampleTexture", d.get("_SampleTexture"), "_TextureMode", d.get("_TextureMode"),
          "_UseVertexColors", d.get("_UseVertexColors"), "_TextureBoost", d.get("_TextureBoost"))
    if "_SampleTexture" in d:
        t["m_SavedProperties"]["m_Floats"] = [(k, 1.0 if k == "_SampleTexture" else v) for k, v in fl]
    else:
        fl.append(("_SampleTexture", 1.0))
    o.save_typetree(t)
    n += 1
assert n == 2, n
open(DST, "wb").write(env.file.save(packer="lz4"))
print("saved", DST)
