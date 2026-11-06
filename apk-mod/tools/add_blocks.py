"""Add the 1.31 custom blocks to the 2.31.4 data (owner 2026-10-13: "add all the blocks").

  1x5x2_stuffedTextBlaze  "BLAZE Pillow"  the word BLAZE in cotton, 5x2x1, Trophies list.
      Built like the live 1x1 Pillow (cotton MapObject script, LOD child, LODGroup), and
      locked to the "blaze" role by the MapObject's own role field, the same way the
      BAM trophy is locked to "bamWinner" - no client hook needed.
  1x1_stuffedCucumberCat  "Cucumber Cat"  a 0.5 m cube with the picture on all six
      faces, Pillows list, 10000 cotton. Built like the 1.31 Screen blocks (YEEP-style
      decoration, slot 0 = hidden palette-colour triangle, slot 1 = the picture): the
      cotton version needed a client hook on MapObject.SetSharedMaterials to keep its
      picture, and that hook crashes the live game, so this one is a decoration.

Meshes come from the 1.31 builders (apk-work/131-notes); both use the live vertex
layout (pos f3, normal f3, color f4, uv f2). The MapObject raw layout is checked by
re-encoding three existing blocks byte-for-byte before anything is added.

Usage: add_blocks.py <in data.unity3d> <out data.unity3d> <cat picture>
"""
import copy
import importlib
import os
import struct
import sys

import UnityPy
from PIL import Image
from UnityPy.enums import TextureFormat

HERE = os.path.dirname(os.path.abspath(__file__))
APK_WORK = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(APK_WORK, "131-notes"))
sys.path.insert(0, os.path.join(APK_WORK, "big", "tools"))
import patterndb  # noqa: E402

blaze_131 = importlib.import_module("build_blaze_block")
cat_131 = importlib.import_module("build_cat_block")

SRC, DST, CAT_PIC = sys.argv[1:4]
RES_DIR = "mapobjects/complexdecorationstuffed/"
YEEP_PATH = "mapobjects/decoration/1x4x2_forbidden_textyeep"
PILLOW_PATH = "mapobjects/stuffed/1x1_stuffed"
SOLID_STUFFED, SOLID_DECO_0, SOLID_DECO_1 = "solid_stuffed", "solid_decoration_0", "solid_decoration_1"
TEX_SIZE = 512


def s(t):
    b = t.encode()
    return struct.pack("<i", len(b)) + b + b"\0" * ((-len(b)) % 4)


def pptr(pid):
    return {"m_FileID": 0, "m_PathID": pid}


# ---- MapObject (base and cotton scripts share this serialized layout)
def mo_parse(raw):
    r = patterndb.R(raw, 28)
    d = {"go": struct.unpack_from("<iq", raw, 0)[1], "script": raw[16:28], "name": r.s(),
         "size": [r.i(), r.i(), r.i()], "pre": [r.i() for _ in range(4)], "role": r.s(), "mid": r.i(),
         "category": r.s(), "place": r.s()}
    d["vol"] = struct.unpack_from("<f", raw, r.p)[0]; r.p += 4
    d.update(pickup=r.s(), brk=r.s(), footstep=r.s(), tail=[r.i(), r.i(), r.i()])
    assert r.p == len(raw), (r.p, len(raw))
    return d


def mo_raw(d):
    return (struct.pack("<iq", 0, d["go"]) + struct.pack("<i", 1) + d["script"] + s(d["name"])
            + struct.pack("<3i", *d["size"]) + struct.pack("<4i", *d["pre"]) + s(d["role"])
            + struct.pack("<i", d["mid"]) + s(d["category"]) + s(d["place"]) + struct.pack("<f", d["vol"])
            + s(d["pickup"]) + s(d["brk"]) + s(d["footstep"]) + struct.pack("<3i", *d["tail"]))


def set_mesh(mt, name, blob, index_bytes, vcount, submeshes, ext):
    mt["m_Name"] = name
    mt["m_VertexData"]["m_VertexCount"] = vcount
    mt["m_VertexData"]["m_DataSize"] = blob
    mt["m_IndexBuffer"] = list(index_bytes) if isinstance(mt["m_IndexBuffer"], list) else index_bytes
    mt["m_IndexFormat"] = 0
    aabb = {"m_Center": {"x": 0.0, "y": 0.0, "z": 0.0}, "m_Extent": ext}
    sm0 = mt["m_SubMeshes"][0]
    mt["m_SubMeshes"] = [{**sm0, **smv, "localAABB": aabb} for smv in submeshes]
    mt["m_LocalAABB"] = aabb
    for k in ("m_BakedConvexCollisionMesh", "m_BakedTriangleCollisionMesh"):
        if k in mt:
            mt[k] = b"" if isinstance(mt[k], (bytes, bytearray)) else []
    if isinstance(mt.get("m_StreamData"), dict):          # mesh data inline, not in .resS
        mt["m_StreamData"].update(offset=0, size=0, path="")


env = UnityPy.load(SRC)
rm_obj = next(o for o in env.objects if o.type.name == "ResourceManager")
rm_tree = rm_obj.read_typetree()
cont = dict((k, v) for k, v in rm_tree["m_Container"])
f = next(o.assets_file for o in env.objects if o.assets_file.name == "resources.assets")
res_fid = cont[YEEP_PATH]["m_FileID"]
assert os.path.basename(rm_obj.assets_file.externals[res_fid - 1].path) == "resources.assets"
for key in ("1x5x2_stuffedtextblaze", "1x1_stuffedcucumbercat"):
    if RES_DIR + key in cont:
        raise SystemExit(f"{key} already present")

yeep_go = f.objects[cont[YEEP_PATH]["m_PathID"]]
pillow_go = f.objects[cont[PILLOW_PATH]["m_PathID"]]
mats = {o.peek_name(): o.path_id for o in f.objects.values() if o.type.name == "Material"
        and o.peek_name() in (SOLID_STUFFED, SOLID_DECO_0, SOLID_DECO_1)}


def comps_of(go):
    out = {}
    for c in go.read_typetree()["m_Component"]:
        r = f.objects[c["component"]["m_PathID"]]
        out.setdefault(r.type.name, r)
    return out


yc, pc = comps_of(yeep_go), comps_of(pillow_go)
for mb in (yc["MonoBehaviour"], pc["MonoBehaviour"]):
    raw = mb.get_raw_data()
    assert mo_raw(mo_parse(raw)) == raw, "MapObject layout check failed"
print("MapObject layout check: OK")

nxt = max(f.objects) + 1


def new_ids(names):
    global nxt
    ids = {k: nxt + i for i, k in enumerate(names)}
    nxt += len(names)
    return ids


def clone(reader, pid):
    new = copy.copy(reader)
    new.path_id = pid
    f.objects[pid] = new
    return new


# ======================= BLAZE (cotton, blaze role) =======================
KEY = "1x5x2_stuffedTextBlaze"
ids = new_ids(["go", "tr", "mb", "bc", "lod", "cgo", "ctr", "cmf", "cmr", "mesh"])
lod0_tr = f.objects[pc["Transform"].read_typetree()["m_Children"][0]["m_PathID"]]
lod0_go = f.objects[lod0_tr.read_typetree()["m_GameObject"]["m_PathID"]]
lc = comps_of(lod0_go)
mesh_src = f.objects[lc["MeshFilter"].read_typetree()["m_Mesh"]["m_PathID"]]
mt = mesh_src.read_typetree()
blob, idx, vcount, ext = blaze_131.build_mesh()
set_mesh(mt, KEY, blob, struct.pack(f"<{len(idx)}H", *idx), vcount,
         [{"firstByte": 0, "indexCount": len(idx), "baseVertex": 0, "firstVertex": 0, "vertexCount": vcount}], ext)
clone(mesh_src, ids["mesh"]).save_typetree(mt)

t = lod0_go.read_typetree()
t["m_Name"] = "LOD0"
t["m_Component"] = [{"component": pptr(ids[k])} for k in ("ctr", "cmf", "cmr")]
clone(lod0_go, ids["cgo"]).save_typetree(t)
t = lod0_tr.read_typetree()
t.update(m_GameObject=pptr(ids["cgo"]), m_Father=pptr(ids["tr"]), m_Children=[])
clone(lod0_tr, ids["ctr"]).save_typetree(t)
t = lc["MeshFilter"].read_typetree()
t.update(m_GameObject=pptr(ids["cgo"]), m_Mesh=pptr(ids["mesh"]))
clone(lc["MeshFilter"], ids["cmf"]).save_typetree(t)
t = lc["MeshRenderer"].read_typetree()
t.update(m_GameObject=pptr(ids["cgo"]), m_Materials=[pptr(mats[SOLID_STUFFED])])
clone(lc["MeshRenderer"], ids["cmr"]).save_typetree(t)

t = pillow_go.read_typetree()
t["m_Name"] = KEY
t["m_Component"] = [{"component": pptr(ids[k])} for k in ("tr", "mb", "bc", "lod")]
clone(pillow_go, ids["go"]).save_typetree(t)
t = pc["Transform"].read_typetree()
t.update(m_GameObject=pptr(ids["go"]), m_Children=[pptr(ids["ctr"])], m_Father=pptr(0))
clone(pc["Transform"], ids["tr"]).save_typetree(t)
t = pc["BoxCollider"].read_typetree()
t.update(m_GameObject=pptr(ids["go"]), m_Size={"x": 2.5, "y": 1.0, "z": 0.5}, m_Center={"x": 0.0, "y": 0.0, "z": 0.0})
clone(pc["BoxCollider"], ids["bc"]).save_typetree(t)
t = pc["LODGroup"].read_typetree()
t.update(m_GameObject=pptr(ids["go"]), m_Size=2.5,
         m_LODs=[{"screenRelativeHeight": 0.0, "fadeTransitionWidth": 0.0, "renderers": [{"renderer": pptr(ids["cmr"])}]}])
clone(pc["LODGroup"], ids["lod"]).save_typetree(t)
d = mo_parse(pc["MonoBehaviour"].get_raw_data())
d.update(go=ids["go"], size=[5, 2, 1], pre=[0, 0, 0, 0], role="blaze", mid=0, category="",
         place="placePillow", vol=0.2, pickup="", brk="breakPillow", footstep="footstepPillow", tail=[0, 0, 1])
clone(pc["MonoBehaviour"], ids["mb"]).set_raw_data(mo_raw(d))
rm_tree["m_Container"].append((RES_DIR + KEY.lower(), {"m_FileID": res_fid, "m_PathID": ids["go"]}))
blaze_entry = {"key": KEY, "name": "BLAZE Pillow", "type": "mapObject", "a": [], "color": "red",
               "cost": 0, "b": 0, "variantOf": ""}
print(f"{KEY}: go {ids['go']}, mesh {vcount} verts / {len(idx) // 3} tris, role blaze")

# ======================= Cucumber Cat (picture decoration) =======================
CKEY = "1x1_stuffedCucumberCat"
ids = new_ids(["go", "tr", "mb", "mf", "mr", "bc", "mesh", "mat", "tex"])
tmpl = next((o for o in f.objects.values() if o.type.name == "Texture2D" and o.peek_name() == "discordIcon"), None)
if tmpl is None:
    tmpl = next(o for o in f.objects.values() if o.type.name == "Texture2D"
                and not (o.read_typetree().get("m_StreamData") or {}).get("path")
                and o.read_typetree()["m_TextureFormat"] in (3, 4))
tex = clone(tmpl, ids["tex"]).read()
tex.m_Name = "cucumberCat"
im = Image.open(CAT_PIC).convert("RGBA")
side = min(im.size)
im = im.crop(((im.width - side) // 2, (im.height - side) // 2, (im.width + side) // 2, (im.height + side) // 2))
tex.set_image(im.resize((TEX_SIZE, TEX_SIZE), Image.LANCZOS), target_format=TextureFormat.RGBA32, mipmap_count=1)
tex.save()

pic_src = f.objects[mats[SOLID_DECO_1]]
t = pic_src.read_typetree()
t["m_Name"] = "cucumberCat"
t["m_SavedProperties"]["m_TexEnvs"] = [(k, {**v, "m_Texture": pptr(ids["tex"])}) if k == "_MainTex" else (k, v)
                                       for k, v in t["m_SavedProperties"]["m_TexEnvs"]]
clone(pic_src, ids["mat"]).save_typetree(t)

mesh_src = f.objects[yc["MeshFilter"].read_typetree()["m_Mesh"]["m_PathID"]]
mt = mesh_src.read_typetree()
blob, idx0, idx, vcount, ext = cat_131.build_mesh()
set_mesh(mt, CKEY, blob, struct.pack(f"<{len(idx0) + len(idx)}H", *(idx0 + idx)), vcount,
         [{"firstByte": 0, "indexCount": len(idx0), "baseVertex": 0, "firstVertex": 0, "vertexCount": 3},
          {"firstByte": len(idx0) * 2, "indexCount": len(idx), "baseVertex": 0, "firstVertex": 3,
           "vertexCount": vcount - 3}], ext)
clone(mesh_src, ids["mesh"]).save_typetree(mt)

t = yeep_go.read_typetree()
t["m_Name"] = CKEY
t["m_Component"] = [{"component": pptr(ids[k])} for k in ("tr", "mb", "mf", "mr", "bc")]
clone(yeep_go, ids["go"]).save_typetree(t)
t = yc["Transform"].read_typetree()
t.update(m_GameObject=pptr(ids["go"]), m_Children=[], m_Father=pptr(0))
clone(yc["Transform"], ids["tr"]).save_typetree(t)
t = yc["MeshFilter"].read_typetree()
t.update(m_GameObject=pptr(ids["go"]), m_Mesh=pptr(ids["mesh"]))
clone(yc["MeshFilter"], ids["mf"]).save_typetree(t)
t = yc["MeshRenderer"].read_typetree()
t.update(m_GameObject=pptr(ids["go"]), m_Materials=[pptr(mats[SOLID_DECO_0]), pptr(ids["mat"])])
clone(yc["MeshRenderer"], ids["mr"]).save_typetree(t)
t = yc["BoxCollider"].read_typetree()
t.update(m_GameObject=pptr(ids["go"]), m_Size={"x": 0.5, "y": 0.5, "z": 0.5}, m_Center={"x": 0.0, "y": 0.0, "z": 0.0})
clone(yc["BoxCollider"], ids["bc"]).save_typetree(t)
d = mo_parse(yc["MonoBehaviour"].get_raw_data())
d.update(go=ids["go"], size=[1, 1, 1], place="placePillow", pickup="", brk="breakPillow", footstep="footstepPillow")
clone(yc["MonoBehaviour"], ids["mb"]).set_raw_data(mo_raw(d))
rm_tree["m_Container"].append((RES_DIR + CKEY.lower(), {"m_FileID": res_fid, "m_PathID": ids["go"]}))
cat_entry = {"key": CKEY, "name": "Cucumber Cat", "type": "mapObject", "a": [], "color": "white",
             "cost": 10000, "b": 0, "variantOf": ""}
print(f"{CKEY}: go {ids['go']}, picture {TEX_SIZE}px from {os.path.basename(CAT_PIC)} (template {tmpl.peek_name()})")

rm_obj.save_typetree(rm_tree)

# ======================= item list =======================
pm = next(o for o in env.objects if o.type.name == "MonoBehaviour" and o.assets_file.name == "level2"
          and b"2x2x2_stuffedTrophie_BAM2" in o.get_raw_data() and o.get_raw_data().count(b"mapObject") > 100)
raw = pm.get_raw_data()
head, groups, tail = patterndb.parse(raw, live=True)
assert patterndb.write(head, groups, tail, live=True) == raw
for gkey, after, entry in (("trophies", "2x2x2_stuffedTrophie_BAM2", blaze_entry), ("stuffed", "1x1_stuffed", cat_entry)):
    g = next(g for g in groups if g["key"] == gkey)
    i = next(i for i, e in enumerate(g["entries"]) if e["key"] == after)
    g["entries"].insert(i + 1, entry)
    print(f"item list: {entry['key']} added to {g.get('display')!r} after {after}")
pm.set_raw_data(patterndb.write(head, groups, tail, live=True))

open(DST, "wb").write(env.file.save(packer="lz4"))
print("saved", DST, os.path.getsize(DST))
