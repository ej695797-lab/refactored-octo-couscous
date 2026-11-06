"""Add the BLAZE Screen (3x2x1_blazeScreenRole) to the 2.31.4 data with a FIXED picture.

Owner 2026-10-17: "i mean the staff member a screen". On 1.31 this block showed a
picture pushed live by the server (remote screen slot "blaze"); the live build has
no remote-screen feed, so the current slot picture ("SOG DEX", 288x184) is baked in
as a static texture instead.

Built exactly like the Cucumber Cat in add_blocks.py (YEEP-style decoration:
submesh 0 = frame with the decoration colour material, submesh 1 = picture with
its own clone of solid_decoration_1 pointing at the baked texture), using the 1.31
screen mesh (131-notes/build_screen_blocks.build_mesh: frame box + inset picture
quads front/back, vertex layout pos f3 / normal f3 / color f4 / uv f2).
Role "blaze" (MapObject role field), listed in Trophies after the BAM trophy.

Usage: add_screen.py <in data.unity3d> <out data.unity3d> <picture>
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
sys.path.insert(0, HERE)
import patterndb  # noqa: E402

screen_131 = importlib.import_module("build_screen_blocks")

SRC, DST, PIC = sys.argv[1:4]
KEY = "3x2x1_blazeScreenRole"
RES_DIR = "mapobjects/complexdecorationstuffed/"
YEEP_PATH = "mapobjects/decoration/1x4x2_forbidden_textyeep"
SOLID_DECO_0, SOLID_DECO_1 = "solid_decoration_0", "solid_decoration_1"
TEX_W, TEX_H = 288, 184          # picture area of the 3x2 screen (1.31 slot size)
SIZE = (3, 2, 1)


def s(t):
    b = t.encode()
    return struct.pack("<i", len(b)) + b + b"\0" * ((-len(b)) % 4)


def pptr(pid):
    return {"m_FileID": 0, "m_PathID": pid}


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
    if isinstance(mt.get("m_StreamData"), dict):
        mt["m_StreamData"].update(offset=0, size=0, path="")


env = UnityPy.load(SRC)
rm_obj = next(o for o in env.objects if o.type.name == "ResourceManager")
rm_tree = rm_obj.read_typetree()
cont = dict((k, v) for k, v in rm_tree["m_Container"])
f = next(o.assets_file for o in env.objects if o.assets_file.name == "resources.assets")
res_fid = cont[YEEP_PATH]["m_FileID"]
if RES_DIR + KEY.lower() in cont:
    raise SystemExit(f"{KEY} already present")

yeep_go = f.objects[cont[YEEP_PATH]["m_PathID"]]
mats = {o.peek_name(): o.path_id for o in f.objects.values() if o.type.name == "Material"
        and o.peek_name() in (SOLID_DECO_0, SOLID_DECO_1)}


def comps_of(go):
    out = {}
    for c in go.read_typetree()["m_Component"]:
        r = f.objects[c["component"]["m_PathID"]]
        out.setdefault(r.type.name, r)
    return out


yc = comps_of(yeep_go)
raw = yc["MonoBehaviour"].get_raw_data()
assert mo_raw(mo_parse(raw)) == raw, "MapObject layout check failed"
print("MapObject layout check: OK")

nxt = max(f.objects) + 1
ids = {k: nxt + i for i, k in enumerate(["go", "tr", "mb", "mf", "mr", "bc", "mesh", "mat", "tex"])}


def clone(reader, pid):
    new = copy.copy(reader)
    new.path_id = pid
    f.objects[pid] = new
    return new


# picture texture (same template choice as the cat)
tmpl = next((o for o in f.objects.values() if o.type.name == "Texture2D" and o.peek_name() == "discordIcon"), None)
if tmpl is None:
    tmpl = next(o for o in f.objects.values() if o.type.name == "Texture2D"
                and not (o.read_typetree().get("m_StreamData") or {}).get("path")
                and o.read_typetree()["m_TextureFormat"] in (3, 4))
tex = clone(tmpl, ids["tex"]).read()
tex.m_Name = "blazeScreen_blaze"
im = Image.open(PIC).convert("RGBA").resize((TEX_W, TEX_H), Image.LANCZOS)
tex.set_image(im, target_format=TextureFormat.RGBA32, mipmap_count=1)
tex.save()

pic_src = f.objects[mats[SOLID_DECO_1]]
t = pic_src.read_typetree()
t["m_Name"] = "blazeScreen_blaze"
t["m_SavedProperties"]["m_TexEnvs"] = [(k, {**v, "m_Texture": pptr(ids["tex"])}) if k == "_MainTex" else (k, v)
                                       for k, v in t["m_SavedProperties"]["m_TexEnvs"]]
clone(pic_src, ids["mat"]).save_typetree(t)

mesh_src = f.objects[yc["MeshFilter"].read_typetree()["m_Mesh"]["m_PathID"]]
mt = mesh_src.read_typetree()
blob, idx0, idx1, vcount, ext = screen_131.build_mesh(*SIZE)
# submesh 0 = frame (idx0), submesh 1 = picture (idx1); indices index the whole vertex list
set_mesh(mt, KEY, blob, struct.pack(f"<{len(idx0) + len(idx1)}H", *(idx0 + idx1)), vcount,
         [{"firstByte": 0, "indexCount": len(idx0), "baseVertex": 0, "firstVertex": 0, "vertexCount": vcount},
          {"firstByte": len(idx0) * 2, "indexCount": len(idx1), "baseVertex": 0, "firstVertex": 0,
           "vertexCount": vcount}], ext)
clone(mesh_src, ids["mesh"]).save_typetree(mt)

t = yeep_go.read_typetree()
t["m_Name"] = KEY
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
t.update(m_GameObject=pptr(ids["go"]), m_Size={"x": SIZE[0] * 0.5, "y": SIZE[1] * 0.5, "z": SIZE[2] * 0.5},
         m_Center={"x": 0.0, "y": 0.0, "z": 0.0})
clone(yc["BoxCollider"], ids["bc"]).save_typetree(t)
d = mo_parse(yc["MonoBehaviour"].get_raw_data())
d.update(go=ids["go"], size=list(SIZE), role="blaze")
clone(yc["MonoBehaviour"], ids["mb"]).set_raw_data(mo_raw(d))
rm_tree["m_Container"].append((RES_DIR + KEY.lower(), {"m_FileID": res_fid, "m_PathID": ids["go"]}))
rm_obj.save_typetree(rm_tree)
print(f"{KEY}: go {ids['go']}, mesh {vcount} verts, picture {TEX_W}x{TEX_H} from {os.path.basename(PIC)}, role blaze")

# item list: Trophies, right after the BLAZE Pillow (or the BAM trophy)
pm = next(o for o in env.objects if o.type.name == "MonoBehaviour" and o.assets_file.name == "level2"
          and b"2x2x2_stuffedTrophie_BAM2" in o.get_raw_data() and o.get_raw_data().count(b"mapObject") > 100)
raw = pm.get_raw_data()
head, groups, tail = patterndb.parse(raw, live=True)
assert patterndb.write(head, groups, tail, live=True) == raw
g = next(g for g in groups if g["key"] == "trophies")
keys = [e["key"] for e in g["entries"]]
after = "1x5x2_stuffedTextBlaze" if "1x5x2_stuffedTextBlaze" in keys else "2x2x2_stuffedTrophie_BAM2"
g["entries"].insert(keys.index(after) + 1, {"key": KEY, "name": "BLAZE Screen", "type": "mapObject", "a": [],
                                            "color": "white", "cost": 0, "b": 0, "variantOf": ""})
pm.set_raw_data(patterndb.write(head, groups, tail, live=True))
print(f"item list: {KEY} added to {g.get('display')!r} after {after}")

open(DST, "wb").write(env.file.save(packer="lz4"))
print("saved", DST, os.path.getsize(DST))
