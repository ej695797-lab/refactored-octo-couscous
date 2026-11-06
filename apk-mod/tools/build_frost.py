"""Bake the FrostOverYeti cosmetic into the live 2.31.4 data (owner 2026-10-17).

The game can't add a brand-new cosmetic (catalog MonoBehaviour has no typetree),
so FrostOverYeti REPLACES the geometry of existing cosmetics:
  head / torso_overLayer / arms_overLayer -> cosmetic_yeti_* (+ their _LOD1)
  back_backpack                           -> cosmetic_alienUFO_back_tube (+ _LOD1)
    (alienUFO back tube: worn by nobody, checked against every account)

Source: ~/Downloads/FrostOverYeti(1).fbx (other AI's re-rig): same 14 Yeeps bones on
GorillaArmature2, verified by the game's bone-path hashes (Mesh.m_BoneNameHashes =
crc32 of "GorillaArmature2/<path>"), and vertex positions already in the game's mesh
space (metres, Z-up; extents match the yeti within mm) -> the target's bind poses,
bone hashes and root are kept; only the vertex/index data is replaced.

Per target the vertex layout is kept exactly (read from the target's channels):
  stream 0: position f3, normal f3, tangent f4
  stream 1: color f4, uv0 f2
  stream 2: blend weights f(N), blend indices u32(N)   (N = 4, or 2 for the arms)
streams 16-byte aligned. Colours/UVs come straight from the FBX (vertex colours +
UVs on the palette, the same scheme the yeti uses). Triangle winding is matched to
the target by comparing triangle orientation with the normals.

Usage: build_frost.py <in data.unity3d> <out data.unity3d> <FrostOverYeti.fbx>
"""
import importlib.util
import os
import struct
import sys
import zlib

import UnityPy

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "fbx_skin", os.path.join(HERE, "..", "..", "fbx-work", "fbx_skin.py"))
fbx_skin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fbx_skin)

SRC, DST, FBX = sys.argv[1:4]
MAP = {
    "cosmetic_frostOverYeti_head": ["cosmetic_yeti_head", "cosmetic_yeti_head_LOD1"],
    "cosmetic_frostOverYeti_torso_overLayer": ["cosmetic_yeti_torso_overLayer", "cosmetic_yeti_torso_overLayer_LOD1"],
    "cosmetic_frostOverYeti_arms_overLayer": ["cosmetic_yeti_arms_overLayer", "cosmetic_yeti_arms_overLayer_LOD1"],
    "cosmetic_frostOverYeti_back_backpack": ["cosmetic_alienUFO_back_tube", "cosmetic_alienUFO_back_tube_LOD1"],
}


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def winding_sign(tris, pos, nrm):
    """+1 if (b-a)x(c-a) points along the normal for most triangles, else -1."""
    s = 0
    for a, b, c in tris[:400]:
        n = cross(sub(pos[b], pos[a]), sub(pos[c], pos[a]))
        avg = tuple(nrm[a][i] + nrm[b][i] + nrm[c][i] for i in range(3))
        s += 1 if dot(n, avg) > 0 else -1
    return 1 if s >= 0 else -1


def _mat(m):
    return [[m[f"e{r}{c}"] for c in range(4)] for r in range(4)]


def _mul(M, v):
    return [sum(M[r][c] * v[c] for c in range(4)) for r in range(4)]


def _inv_rigid(M):
    """Inverse of a rotation+translation 4x4 (Unity bind poses)."""
    R = [[M[c][r] for c in range(3)] for r in range(3)]
    t = [-sum(R[r][c] * M[c][3] for c in range(3)) for r in range(3)]
    return [R[0] + [t[0]], R[1] + [t[1]], R[2] + [t[2]], [0, 0, 0, 1]]


def align_to_bones(fmesh, P, t, hashes, bone_idx):
    """The FBX arms were rigged ~18 cm off the game's arm bones (owner: "arms are not
    lined up"). Per bone, centre the FBX geometry on the game mesh's own bone bounds
    (m_BonesAABB, bone space) and move each vertex by its weight-blended offset."""
    W = fmesh["weights"]
    G = [_mat(m) for m in t["m_BindPose"]]
    Ginv = [_inv_rigid(g) for g in G]
    pts = {}
    for ci, ws in W.items():
        b = max(ws, key=lambda x: x[1])[0]
        if b in bone_idx:
            pts.setdefault(bone_idx[b], []).append(_mul(G[bone_idx[b]], [*P[ci], 1.0])[:3])
    off = {}
    for i, q in pts.items():
        a = t["m_BonesAABB"][i]
        fc = [(min(x[k] for x in q) + max(x[k] for x in q)) / 2 for k in range(3)]
        gc = [(a["m_Min"][k] + a["m_Max"][k]) / 2 for k in "xyz"]
        # only the axis across the limb (x/y match already: the limb runs along y)
        off[i] = [0.0, 0.0, gc[2] - fc[2]]
        print(f"    align bone {i}: z offset {off[i][2]:+.3f} m")
    out = list(P)
    for ci, ws in W.items():
        acc, tot = [0.0, 0.0, 0.0], 0.0
        for b, w in ws:
            i = bone_idx.get(b)
            if i is None or i not in off:
                continue
            lp = _mul(G[i], [*P[ci], 1.0])
            lp = [lp[0] + off[i][0], lp[1] + off[i][1], lp[2] + off[i][2], 1.0]
            mp = _mul(Ginv[i], lp)
            acc = [acc[k] + w * mp[k] for k in range(3)]
            tot += w
        if tot:
            out[ci] = tuple(acc[k] / tot for k in range(3))
    return out


def read_target(t):
    """Unpack a target mesh's positions/normals/triangles (for its winding sign)."""
    vd = t["m_VertexData"]
    n = vd["m_VertexCount"]
    d = bytes(vd["m_DataSize"])
    ch = vd["m_Channels"]
    strides = {}
    for c in ch:
        if c["dimension"]:
            strides[c["stream"]] = max(strides.get(c["stream"], 0), c["offset"] + 4 * c["dimension"])
    offs, cur = {}, 0
    for s in sorted(strides):
        offs[s] = cur
        cur += strides[s] * n
        cur = (cur + 15) // 16 * 16
    pos = [struct.unpack_from("<3f", d, offs[0] + i * strides[0]) for i in range(n)]
    nrm = [struct.unpack_from("<3f", d, offs[0] + i * strides[0] + 12) for i in range(n)]
    ib = bytes(t["m_IndexBuffer"])
    fmt = "<H" if t["m_IndexFormat"] == 0 else "<I"
    w = 2 if t["m_IndexFormat"] == 0 else 4
    idx = [struct.unpack_from(fmt, ib, i)[0] for i in range(0, len(ib), w)]
    tris = [tuple(idx[i:i + 3]) for i in range(0, len(idx) - 2, 3)]
    return pos, nrm, tris, ch


def build(fmesh, bones, t, fallback_hashes=None, align_bones=False):
    pos_t, nrm_t, tris_t, ch = read_target(t)
    target_sign = winding_sign(tris_t, pos_t, nrm_t)
    nweights = next(c["dimension"] for i, c in enumerate(ch) if i == 12)
    sig = [(c["stream"], c["offset"], c["format"], c["dimension"]) for c in ch if c["dimension"]]
    want = [(0, 0, 0, 3), (0, 12, 0, 3), (0, 24, 0, 4), (1, 0, 0, 4), (1, 16, 0, 2),
            (2, 0, 0, nweights), (2, 4 * nweights, 10, nweights)]
    if sig != want or nweights not in (2, 4):
        raise ValueError(f"unexpected vertex layout {sig}")
    hashes = [h & 0xFFFFFFFF for h in t["m_BoneNameHashes"]]
    if not hashes and fallback_hashes:
        # LOD1 meshes store no bone names (bones come from the renderer) but carry
        # the same bind poses as the LOD0 mesh -> same bone order.
        hashes = list(fallback_hashes)
    # FBX bone name -> target bone index via path hash
    by_mid = bones
    name_path = {}
    for mid, b in by_mid.items():
        parts, m = [], mid
        while m in by_mid:
            parts.append(by_mid[m]["name"])
            m = by_mid[m]["parent"]
        name_path[b["name"]] = "/".join(reversed(parts))
    bone_idx = {}
    for name, path in name_path.items():
        h = zlib.crc32(path.encode()) & 0xFFFFFFFF
        if h in hashes:
            bone_idx[name] = hashes.index(h)
    # per-corner vertices, deduplicated
    P, C = fmesh["pos"], fmesh["corners"]
    if align_bones:
        P = align_to_bones(fmesh, P, t, hashes, bone_idx)
    N, U, K = fmesh["normals"], fmesh["uvs"], fmesh["colors"]
    W = fmesh["weights"]
    verts, remap, out_pos, out_nrm = [], {}, [], []
    corner_v = []
    for k, ci in enumerate(C):
        nn = N[k] if N else (0.0, 0.0, 1.0)
        uv = U[k] if U else (0.0, 0.0)
        col = K[k] if K else (1.0, 1.0, 1.0, 1.0)
        key = (ci, tuple(round(x, 4) for x in nn), tuple(round(x, 5) for x in uv), tuple(round(x, 4) for x in col))
        v = remap.get(key)
        if v is None:
            ws = sorted(W.get(ci, []), key=lambda x: -x[1])
            ws = [(bone_idx[b], w) for b, w in ws if b in bone_idx][:nweights]
            tot = sum(w for _, w in ws) or 1.0
            ws = [(i, w / tot) for i, w in ws] + [(0, 0.0)] * (nweights - len(ws))
            v = len(verts)
            remap[key] = v
            verts.append((P[ci], nn, uv, col, ws))
            out_pos.append(P[ci])
            out_nrm.append(nn)
        corner_v.append(v)
    tris = [(corner_v[a], corner_v[b], corner_v[c]) for a, b, c in fmesh["tris"]]
    if winding_sign(tris, out_pos, out_nrm) != target_sign:
        tris = [(a, c, b) for a, b, c in tris]
    n = len(verts)
    s0, s1, s2 = 40, 24, 8 * nweights
    buf = bytearray()
    for p, nn, uv, col, ws in verts:
        buf += struct.pack("<3f3f4f", *p, *nn, 1.0, 0.0, 0.0, 1.0)
    buf += b"\0" * ((-len(buf)) % 16)
    for p, nn, uv, col, ws in verts:
        buf += struct.pack("<4f2f", *col, *uv)
    buf += b"\0" * ((-len(buf)) % 16)
    for p, nn, uv, col, ws in verts:
        buf += struct.pack(f"<{nweights}f", *[w for _, w in ws]) + struct.pack(f"<{nweights}I", *[i for i, _ in ws])
    buf += b"\0" * ((-len(buf)) % 16)
    big = n > 65535
    flat = [i for tri in tris for i in tri]
    ib = struct.pack(f"<{len(flat)}{'I' if big else 'H'}", *flat)
    mn = [min(p[i] for p in out_pos) for i in range(3)]
    mx = [max(p[i] for p in out_pos) for i in range(3)]
    aabb = {"m_Center": {"x": (mn[0] + mx[0]) / 2, "y": (mn[1] + mx[1]) / 2, "z": (mn[2] + mx[2]) / 2},
            "m_Extent": {"x": (mx[0] - mn[0]) / 2, "y": (mx[1] - mn[1]) / 2, "z": (mx[2] - mn[2]) / 2}}
    vd = t["m_VertexData"]
    vd["m_VertexCount"] = n
    vd["m_DataSize"] = bytes(buf)
    t["m_IndexFormat"] = 1 if big else 0
    t["m_IndexBuffer"] = list(ib) if isinstance(t["m_IndexBuffer"], list) else ib
    sm = dict(t["m_SubMeshes"][0])
    sm.update(firstByte=0, indexCount=len(flat), topology=0, baseVertex=0, firstVertex=0,
              vertexCount=n, localAABB=aabb)
    t["m_SubMeshes"] = [sm]
    t["m_LocalAABB"] = aabb
    # per-bone bounds (culling): vertices that bone influences
    bb = []
    for bi in range(len(hashes)):
        pts = [v[0] for v in verts if any(i == bi and w > 0 for i, w in v[4])]
        if pts:
            bb.append({"m_Min": dict(zip("xyz", [min(p[i] for p in pts) for i in range(3)])),
                       "m_Max": dict(zip("xyz", [max(p[i] for p in pts) for i in range(3)]))})
        else:
            bb.append(t["m_BonesAABB"][bi])
    t["m_BonesAABB"] = bb
    if isinstance(t.get("m_Shapes"), dict):
        for k in ("vertices", "shapes", "channels", "fullWeights"):
            if k in t["m_Shapes"]:
                t["m_Shapes"][k] = []
    for k in ("m_BakedConvexCollisionMesh", "m_BakedTriangleCollisionMesh"):
        if k in t:
            t[k] = b"" if isinstance(t[k], (bytes, bytearray)) else []
    return n, len(tris), nweights, len(bone_idx), target_sign


fbx = fbx_skin.load(FBX)
env = UnityPy.load(SRC)
targets = {o.peek_name(): o for o in env.objects if o.type.name == "Mesh"
           and o.peek_name() in {x for v in MAP.values() for x in v}}
for src, names in MAP.items():
    fm = fbx["meshes"][src]
    for name in names:
        o = targets.get(name)
        if o is None:
            print(f"  (no {name} in data, skipped)")
            continue
        t = o.read_typetree()
        fb = None
        if name.endswith("_LOD1") and name[:-5] in targets:
            base = targets[name[:-5]].read_typetree()
            same = all(abs(a[k] - b[k]) < 1e-4 for a, b in zip(t["m_BindPose"], base["m_BindPose"]) for k in a)
            if same and len(t["m_BindPose"]) == len(base["m_BindPose"]):
                fb = [h & 0xFFFFFFFF for h in base["m_BoneNameHashes"]]
            else:
                print(f"  SKIPPED {name}: bind poses differ from {name[:-5]}")
                continue
        try:
            n, ntri, nw, nb, sign = build(fm, fbx["bones"], t, fb, align_bones="arms" in src)
        except ValueError as e:
            print(f"  SKIPPED {name}: {e}")
            continue
        o.save_typetree(t)
        print(f"{name}: {n} verts, {ntri} tris, {nw} weights/vert, {nb}/14 bones mapped, winding {sign:+d}")
open(DST, "wb").write(env.file.save(packer="lz4"))
print("saved", DST, os.path.getsize(DST))
