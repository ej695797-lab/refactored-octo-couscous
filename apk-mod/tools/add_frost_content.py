"""Frost content for the live 2.32 data (owner 2026-10-18).

1. Trading cards: staffcarda, staffcardb, yeepswholarpstuff, bluemagnifyingglass.
   TradingCardManager.cardConfigs entry layout (decoded 2026-10-18):
     key s, category i, frontStyle i (0 item render, 1 mob, 2 avatar, 3 map, 4 custom),
     rarity i, itemKey s, params list<s>, patternKey s, 4 ints, useCustomName i, customName s,
     13 ints (zero for item cards)
   New cards are item-style clones of the magnifyingGlass card with a custom name; the
   owner's full-card artwork is applied natively (CardArtTick). Gold = <key>_legendary,
   which the game derives itself (TradingCardManager.legendaryKeySuffix).
   Ownership: patterns tradingCard_<key> / tradingCard_<key>_legendary (item tradingCard,
   param = card key, like the built-in tradingCard_chainsaw), granted by the server only.

2. Ice pillows: 1x1x1_icePillow + 2x2x2_icePillow from ~/Downloads/IceBlocks.blend
   (meshes 1x1_iceCube / 2x2_iceCube, already 0.5 m / 1 m cubes; submesh 0 ice, 1 cracks).
   Built from 2x2_stuffed (root MeshFilter + MapObject, tag 20000 = grabbable pillow) with
   the LocomotionSurface of 2x2_ice (slippery) and the game's ice material as the data
   fallback; the native bundle tick swaps in the bundle's ice materials. Pillow list,
   cost 350 / 550, name with "by Staff Member A" on a second line.

Usage: add_frost_content.py <in data.unity3d> <out data.unity3d> <meshes.pkl from blendmesh.py>
"""
import copy
import math
import os
import pickle
import struct
import sys

import UnityPy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "big", "tools"))
import patterndb  # noqa: E402

SRC, DST, MESHES = sys.argv[1:4]

CARDS = [("staffcarda", "Staff Member A"), ("staffcardb", "Staff Member B"),
         ("yeepswholarpstuff", "Yeeps Who Larp Stuff"), ("bluemagnifyingglass", "Blue Magnifying Glass")]
# Ice pillows ABANDONED by the owner 2026-10-18 ("blocks are very buggy, remove them");
# the code stays for reference but builds nothing.
PILLOWS = []  # was [("1x1x1_icePillow", "1x1_iceCube", (1, 1, 1), 350), ("2x2x2_icePillow", "2x2_iceCube", (2, 2, 2), 550)]
AUTHOR_LINE = "\nby Staff Member A"


def s(t):
    b = t.encode()
    return struct.pack("<i", len(b)) + b + b"\0" * ((-len(b)) % 4)


def pptr(pid):
    return {"m_FileID": 0, "m_PathID": pid}


env = UnityPy.load(SRC)

# ---------------------------------------------------------------- 1. cards
def card_entry(key, name):
    return (s(key) + struct.pack("<3i", 0, 0, 3) + s("magnifyingGlass") + struct.pack("<i", 0)
            + s("magnifyingGlass") + struct.pack("<4i", 0, 0, 0, 0) + struct.pack("<i", 1) + s(name)
            + struct.pack("<13i", *([0] * 13)))


TEMPLATE_TAIL = s("magnifyingGlass") + struct.pack("<3i", 0, 0, 3) + s("magnifyingGlass") + struct.pack("<i", 0) + s("magnifyingGlass")
n_mgr = 0
for o in env.objects:
    if o.type.name != "MonoBehaviour" or o.assets_file.name not in ("level2", "level4"):
        continue
    raw = o.get_raw_data()
    i = raw.find(TEMPLATE_TAIL)
    if i < 0 or b"anniversary26KeyArt" not in raw:
        continue
    count = struct.unpack_from("<i", raw, 32)[0]
    # the template entry: header + 4 ints + bool + empty name + 13 ints
    end = i + len(TEMPLATE_TAIL) + 16 + 4 + 4 + 52
    assert raw[i + len(TEMPLATE_TAIL):end] == b"\0" * (end - i - len(TEMPLATE_TAIL)), "template entry layout changed"
    assert raw[end:end + 4] == struct.pack("<i", 5) and raw[end + 4:end + 9] == b"match", raw[end:end + 12]
    have = [k for k, _ in CARDS if s(k) + struct.pack("<i", 0) in raw]
    if have:
        raise SystemExit(f"cards already present in {o.assets_file.name}/{o.path_id}: {have}")
    add = b"".join(card_entry(k, n) for k, n in CARDS)
    raw = raw[:32] + struct.pack("<i", count + len(CARDS)) + raw[36:end] + add + raw[end:]
    o.set_raw_data(raw)
    n_mgr += 1
    print(f"cards: {o.assets_file.name}/{o.path_id} cardConfigs {count} -> {count + len(CARDS)}")
assert n_mgr >= 1, "TradingCardManager not found"

# ---------------------------------------------------------------- 2. ice pillows (objects)
rm_obj = next(o for o in env.objects if o.type.name == "ResourceManager")
rm_tree = rm_obj.read_typetree()
cont = dict((k, v) for k, v in rm_tree["m_Container"])
f = next(o.assets_file for o in env.objects if o.assets_file.name == "resources.assets")
STUFFED2 = next(k for k in cont if k.endswith("/2x2_stuffed"))
ICE = next(k for k in cont if k.endswith("/2x2_ice"))
# the game only loads blocks from MapObjects/... ("mapobjectstemp lol/" is a leftover
# folder - blocks registered there give "Could not find map object data", live54)
res_dir = "mapobjects/stuffed/"
assert any(k.startswith(res_dir) for k in cont)
res_fid = cont[STUFFED2]["m_FileID"]
for key, *_ in PILLOWS:
    if res_dir + key.lower() in cont:
        raise SystemExit(f"{key} already present")


def comps_of(go):
    out = {}
    for c in go.read_typetree()["m_Component"]:
        r = f.objects[c["component"]["m_PathID"]]
        out.setdefault(r.type.name, []).append(r)
    return out


st_go = f.objects[cont[STUFFED2]["m_PathID"]]
ice_go = f.objects[cont[ICE]["m_PathID"]]
sc, ic = comps_of(st_go), comps_of(ice_go)
ice_mat = ic["MeshRenderer"][0].read_typetree()["m_Materials"][0]["m_PathID"]
loco = next(r for r in ic["MonoBehaviour"] if len(r.get_raw_data()) == 36)
mo_src = sc["MonoBehaviour"][0]
mesh_src = f.objects[sc["MeshFilter"][0].read_typetree()["m_Mesh"]["m_PathID"]]
mt0 = mesh_src.read_typetree()
chans = mt0["m_VertexData"]["m_Channels"]
print("template mesh channels:", [(i, c["stream"], c["offset"], c["format"], c["dimension"] & 15) for i, c in enumerate(chans) if c["dimension"]])

blend = pickle.load(open(MESHES, "rb"))


def build_mesh(bm):
    tv, tp, tl = bm["tv"], bm["tp"], bm["tl"]
    A = bm["attrs"]
    P = struct.unpack(f"<{tv * 3}f", A["position"]["raw"])
    # Blender Z-up right-handed -> Unity Y-up left-handed: (x, y, z) -> (x, z, y) (mirror; winding fixed below)
    pos = [(P[3 * i], P[3 * i + 2], P[3 * i + 1]) for i in range(tv)]
    cv = struct.unpack(f"<{tl}i", A[".corner_vert"]["raw"])
    UV = struct.unpack(f"<{tl * 2}f", A["UVMap"]["raw"])
    mi = struct.unpack(f"<{tp}i", A["material_index"]["raw"])
    sharp = A["sharp_face"]["raw"] if "sharp_face" in A else b"\0" * tp
    po = bm["polyoff"]

    def sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])

    def cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])

    def norm(v):
        l = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2) or 1.0
        return (v[0] / l, v[1] / l, v[2] / l)

    fn = []
    for p in range(tp):
        c = [pos[cv[k]] for k in range(po[p], po[p + 1])]
        n = (0.0, 0.0, 0.0)
        for k in range(1, len(c) - 1):
            x = cross(sub(c[k], c[0]), sub(c[k + 1], c[0]))
            n = (n[0] + x[0], n[1] + x[1], n[2] + x[2])
        fn.append(n)
    # outward check on the body (submesh 0): face normals should point away from the centre
    score = sum(fn[p][0] * pos[cv[po[p]]][0] + fn[p][1] * pos[cv[po[p]]][1] + fn[p][2] * pos[cv[po[p]]][2]
                for p in range(tp) if mi[p] == 0)
    flip = score < 0
    fn = [norm((-n[0], -n[1], -n[2]) if flip else n) for n in fn]
    vn = [(0.0, 0.0, 0.0)] * tv
    for p in range(tp):
        for k in range(po[p], po[p + 1]):
            a = vn[cv[k]]; vn[cv[k]] = (a[0] + fn[p][0], a[1] + fn[p][1], a[2] + fn[p][2])
    vn = [norm(v) for v in vn]
    verts, index, tris = [], {}, {0: [], 1: []}
    for p in range(tp):
        ids = []
        for k in range(po[p], po[p + 1]):
            n = fn[p] if sharp[p] else vn[cv[k]]
            key = (cv[k], round(UV[2 * k], 5), round(UV[2 * k + 1], 5), round(n[0], 4), round(n[1], 4), round(n[2], 4))
            if key not in index:
                index[key] = len(verts)
                verts.append((pos[cv[k]], n, (UV[2 * k], UV[2 * k + 1])))
            ids.append(index[key])
        for k in range(1, len(ids) - 1):
            t = (ids[0], ids[k], ids[k + 1])
            if flip:
                t = (t[0], t[2], t[1])
            tris[1 if mi[p] else 0].append(t)
    # Unity treats a triangle as front-facing where (b-a)x(c-a) points (checked on Unity's
    # built-in quad), so after the flip above every triangle faces outward already
    return verts, tris


FMT = {0: ("f", 4), 1: ("e", 2), 2: ("B", 1)}


def pack_vertices(verts):
    """Vertex blob in the template mesh's own layout (streams 16-byte aligned)."""
    used = [(i, c) for i, c in enumerate(chans) if c["dimension"] & 15]
    streams = sorted({c["stream"] for _, c in used})
    blob = bytearray()
    for st in streams:
        cs = [(i, c) for i, c in used if c["stream"] == st]
        stride = max(c["offset"] + (c["dimension"] & 15) * FMT[c["format"]][1] for _, c in cs)
        part = bytearray(stride * len(verts))
        for vi, (p, n, uv) in enumerate(verts):
            for i, c in cs:
                dim = c["dimension"] & 15
                val = {0: p, 1: n, 2: (1.0, 0.0, 0.0, 1.0), 3: (1.0, 1.0, 1.0, 1.0), 4: uv}.get(i, (0.0,) * 4)
                val = (list(val) + [0.0] * 4)[:dim]
                code, sz = FMT[c["format"]]
                if code == "B":
                    val = [max(0, min(255, int(round(x * 255)))) for x in val]
                struct.pack_into(f"<{dim}{code}", part, vi * stride + c["offset"], *val)
        blob += part
        blob += b"\0" * ((-len(blob)) % 16)
    return bytes(blob)


nxt = max(f.objects) + 1


def clone(reader, pid):
    new = copy.copy(reader)
    new.path_id = pid
    f.objects[pid] = new
    return new


def mo_fix(raw, go_pid, size):
    raw = bytearray(raw)
    struct.pack_into("<iq", raw, 0, 0, go_pid)
    # name s (empty for 2x2_stuffed) then size 3i
    L = struct.unpack_from("<i", raw, 28)[0]
    p = 32 + L + ((-L) % 4)
    struct.pack_into("<3i", raw, p, *size)
    return bytes(raw)


new_entries = []
for key, mname, size, cost in PILLOWS:
    ids = {k: nxt + i for i, k in enumerate(["go", "tr", "mb", "mf", "mr", "bc", "lo", "mesh"])}
    nxt += len(ids)
    verts, tris = build_mesh(blend["ME" + mname])
    allt = tris[0] + tris[1]
    mt = copy.deepcopy(mt0)
    mt["m_Name"] = key
    mt["m_VertexData"]["m_VertexCount"] = len(verts)
    blob = pack_vertices(verts)
    mt["m_VertexData"]["m_DataSize"] = blob
    ib = struct.pack(f"<{len(allt) * 3}H", *[x for t in allt for x in t])
    mt["m_IndexBuffer"] = list(ib) if isinstance(mt0["m_IndexBuffer"], list) else ib
    mt["m_IndexFormat"] = 0
    half = size[0] * 0.25
    ext = {"x": half * 1.01, "y": half * 1.01, "z": half * 1.01}
    aabb = {"m_Center": {"x": 0.0, "y": 0.0, "z": 0.0}, "m_Extent": ext}
    sm0 = mt0["m_SubMeshes"][0]
    mt["m_SubMeshes"] = [
        {**sm0, "firstByte": 0, "indexCount": len(tris[0]) * 3, "baseVertex": 0, "firstVertex": 0,
         "vertexCount": len(verts), "topology": 0, "localAABB": aabb},
        {**sm0, "firstByte": len(tris[0]) * 6, "indexCount": len(tris[1]) * 3, "baseVertex": 0, "firstVertex": 0,
         "vertexCount": len(verts), "topology": 0, "localAABB": aabb}]
    mt["m_LocalAABB"] = aabb
    for k in ("m_BakedConvexCollisionMesh", "m_BakedTriangleCollisionMesh"):
        if k in mt:
            mt[k] = b"" if isinstance(mt[k], (bytes, bytearray)) else []
    if isinstance(mt.get("m_StreamData"), dict):
        mt["m_StreamData"].update(offset=0, size=0, path="")
    if "m_Shapes" in mt and isinstance(mt["m_Shapes"], dict):
        for k in ("vertices", "shapes", "channels", "fullWeights"):
            if k in mt["m_Shapes"]:
                mt["m_Shapes"][k] = []
    clone(mesh_src, ids["mesh"]).save_typetree(mt)

    t = st_go.read_typetree()
    t["m_Name"] = key
    t["m_Component"] = [{"component": pptr(ids[k])} for k in ("tr", "mb", "mf", "mr", "bc", "lo")]
    clone(st_go, ids["go"]).save_typetree(t)
    t = sc["Transform"][0].read_typetree()
    t.update(m_GameObject=pptr(ids["go"]), m_Children=[], m_Father=pptr(0))
    clone(sc["Transform"][0], ids["tr"]).save_typetree(t)
    t = sc["MeshFilter"][0].read_typetree()
    t.update(m_GameObject=pptr(ids["go"]), m_Mesh=pptr(ids["mesh"]))
    clone(sc["MeshFilter"][0], ids["mf"]).save_typetree(t)
    t = sc["MeshRenderer"][0].read_typetree()
    t.update(m_GameObject=pptr(ids["go"]), m_Materials=[pptr(ice_mat), pptr(ice_mat)])
    clone(sc["MeshRenderer"][0], ids["mr"]).save_typetree(t)
    t = sc["BoxCollider"][0].read_typetree()
    t.update(m_GameObject=pptr(ids["go"]), m_Size={"x": size[0] * 0.5, "y": size[1] * 0.5, "z": size[2] * 0.5},
             m_Center={"x": 0.0, "y": 0.0, "z": 0.0})
    clone(sc["BoxCollider"][0], ids["bc"]).save_typetree(t)
    clone(mo_src, ids["mb"]).set_raw_data(mo_fix(mo_src.get_raw_data(), ids["go"], size))
    lraw = bytearray(loco.get_raw_data()); struct.pack_into("<iq", lraw, 0, 0, ids["go"])
    clone(loco, ids["lo"]).set_raw_data(bytes(lraw))
    rm_tree["m_Container"].append((res_dir + key.lower(), {"m_FileID": res_fid, "m_PathID": ids["go"]}))
    print(f"{key}: go {ids['go']} mesh {len(verts)} verts, {len(tris[0])}+{len(tris[1])} tris, size {size}")
    new_entries.append((key, cost))
rm_obj.save_typetree(rm_tree)

# ---------------------------------------------------------------- pattern list
pm = next(o for o in env.objects if o.type.name == "MonoBehaviour" and o.assets_file.name == "level2"
          and b"tradingCard_chainsaw" in o.get_raw_data() and o.get_raw_data().count(b"mapObject") > 100)
raw = pm.get_raw_data()
head, groups, tail = patterndb.parse(raw, live=True)
assert patterndb.write(head, groups, tail, live=True) == raw
gc = next(g for g in groups if any(e["key"] == "tradingCard_chainsaw" for e in g["entries"]))
ck = [e["key"] for e in gc["entries"]]
tmpl = gc["entries"][ck.index("tradingCard_chainsaw")]
pos = ck.index("tradingCard_chainsaw") + 1
for key, name in CARDS:
    for suffix, label in (("", ""), ("_legendary", " (Gold)")):
        gc["entries"].insert(pos, {**tmpl, "key": f"tradingCard_{key}{suffix}", "name": f"{name}{label} Card",
                                   "a": [key + suffix]})
        pos += 1
print(f"patterns: 8 card patterns added to group {gc['key']!r}")
gs = next(g for g in groups if g["key"] == "stuffed")
sk = [e["key"] for e in gs["entries"]]
st_t = gs["entries"][sk.index("2x2x2_stuffed") if "2x2x2_stuffed" in sk else 0]
after = sk.index("2x2x2_stuffed") + 1 if "2x2x2_stuffed" in sk else len(sk)
for i, (key, cost) in enumerate(new_entries):
    gs["entries"].insert(after + i, {**st_t, "key": key, "name": "Ice Pillow" + AUTHOR_LINE, "type": "mapObject",
                                     "a": [], "cost": cost, "variantOf": ""})
print(f"pillows: {[k for k, _ in new_entries]} added to 'stuffed' (template {st_t['key']} cost {st_t['cost']})")
pm.set_raw_data(patterndb.write(head, groups, tail, live=True))

open(DST, "wb").write(env.file.save(packer="lz4"))
print("saved", DST, os.path.getsize(DST))
