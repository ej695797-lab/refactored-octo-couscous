"""live44: Cucumber Cat + BLAZE Screen pictures on an OPAQUE unlit shader, and the
BLAZE Screen mesh at the right size.

- live43 used the banner shader (MapObjectDephysicalizedTextured) which is see-through.
  Now both materials use "Universal Render Pipeline/Unlit" (the shader of the scenery
  backdrop pictures, SceneryBeachFlatsMat) set fully opaque: _Surface 0, no alpha clip,
  RenderType Opaque, picture in _BaseMap and _MainTex.
- 3x2x1_blazeScreenRole mesh was built at 1 m per block unit (3 x 2 x 1 m) but the game
  uses 0.5 m (its BoxCollider is 1.5 x 1.0 x 0.5): vertex positions scaled by 0.5.
Usage: picture_fix_unlit.py <in data.unity3d> <out data.unity3d>
"""
import copy
import os
import struct
import sys

import UnityPy

SRC, DST = sys.argv[1:3]
env = UnityPy.load(SRC)
objs = list(env.objects)
tmpl_o = next(o for o in objs if o.type.name == "Material" and o.peek_name() == "SceneryBeachFlatsMat")
tmpl = tmpl_o.read_typetree()
# the template's shader lives in another file: re-express that pointer from resources.assets
src_ext = tmpl_o.assets_file.externals[tmpl["m_Shader"]["m_FileID"] - 1].path
res_file = next(o.assets_file for o in objs if o.assets_file.name == "resources.assets")
ext_names = [os.path.basename(e.path) for e in res_file.externals]
fid = ext_names.index(os.path.basename(src_ext)) + 1
shader_ptr = {"m_FileID": fid, "m_PathID": tmpl["m_Shader"]["m_PathID"]}
print("URP Unlit shader:", os.path.basename(src_ext), "pathID", shader_ptr["m_PathID"], "-> fileID", fid, "from resources.assets")

for o in objs:
    if o.type.name != "Material" or o.assets_file.name != "resources.assets" or o.peek_name() not in ("cucumberCat", "blazeScreen_blaze"):
        continue
    old = o.read_typetree()
    pic = dict(old["m_SavedProperties"]["m_TexEnvs"])["_MainTex"]["m_Texture"]
    t = copy.deepcopy(tmpl)
    t["m_Name"] = old["m_Name"]
    t["m_Shader"] = shader_ptr
    t["m_ValidKeywords"] = []
    if "m_InvalidKeywords" in t:
        t["m_InvalidKeywords"] = []
    t["m_CustomRenderQueue"] = -1
    t["stringTagMap"] = [("RenderType", "Opaque")]
    t["m_SavedProperties"]["m_TexEnvs"] = [
        (k, dict(v, m_Texture=pic, m_Scale={"x": 1.0, "y": 1.0}, m_Offset={"x": 0.0, "y": 0.0}) if k in ("_BaseMap", "_MainTex") else v)
        for k, v in t["m_SavedProperties"]["m_TexEnvs"]]
    t["m_SavedProperties"]["m_Floats"] = [(k, 0.0 if k in ("_AlphaClip", "_Surface", "_Blend") else v)
                                          for k, v in t["m_SavedProperties"]["m_Floats"]]
    t["m_SavedProperties"]["m_Colors"] = [(k, {"r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0} if k in ("_BaseColor", "_Color") else v)
                                          for k, v in t["m_SavedProperties"]["m_Colors"]]
    o.save_typetree(t)
    print(f"{t['m_Name']}: opaque URP Unlit, picture pathID {pic['m_PathID']}")

# ---- screen mesh at 0.5 m per block unit
mo = next(o for o in objs if o.type.name == "Mesh" and o.assets_file.name == "resources.assets"
          and o.peek_name() == "3x2x1_blazeScreenRole")
m = mo.read_typetree()
vd = m["m_VertexData"]; n = vd["m_VertexCount"]; data = bytearray(bytes(vd["m_DataSize"]))
ch = vd["m_Channels"]
assert ch[0]["dimension"] == 3 and ch[0]["format"] == 0, ch[0]
stream = ch[0]["stream"]; poff = ch[0]["offset"]
strides = {}
for c in ch:
    if c["dimension"]:
        strides[c["stream"]] = max(strides.get(c["stream"], 0), c["offset"] + 4 * c["dimension"] if c["format"] == 0 else c["offset"] + c["dimension"] * {1: 2, 2: 1, 10: 4}.get(c["format"], 4))
base, cur = {}, 0
for st in sorted(strides):
    base[st] = cur; cur += strides[st] * n; cur = (cur + 15) // 16 * 16
for i in range(n):
    at = base[stream] + i * strides[stream] + poff
    x, y, z = struct.unpack_from("<3f", data, at)
    struct.pack_into("<3f", data, at, x * 0.5, y * 0.5, z * 0.5)
vd["m_DataSize"] = bytes(data)
def half(a):
    return {"m_Center": {k: a["m_Center"][k] * 0.5 for k in "xyz"}, "m_Extent": {k: a["m_Extent"][k] * 0.5 for k in "xyz"}}
m["m_LocalAABB"] = half(m["m_LocalAABB"])
for sm in m["m_SubMeshes"]:
    sm["localAABB"] = half(sm["localAABB"])
mo.save_typetree(m)
print(f"screen mesh: {n} verts scaled 0.5 -> extent {m['m_LocalAABB']['m_Extent']}")
open(DST, "wb").write(env.file.save(packer="lz4"))
print("saved", DST)
