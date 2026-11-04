"""Export the live game's cosmetics for the View (static/view-cos/).

Reads the game data bundle (data.unity3d) with UnityPy: the cosmetic list MonoBehaviour
(key strings each followed by PPtrs to their meshes in sharedassets2) gives key -> meshes;
every non-LOD cosmetic mesh is written as <mesh>.json in the same layout as the View's rig
body (pos/nrm/col/si/sw/idx/bind) plus its bone NAMES (from the mesh's bone name hashes =
CRC32 of the bone path, matched against the View rig). index.json = {key: [mesh, ...]}.

usage: export_view_cosmetics.py <data.unity3d> <view-yeep.json> <out dir>
"""
import json
import os
import re
import struct
import sys
import zlib

import UnityPy
from UnityPy.helpers.MeshHelper import MeshHandler

DATA, RIG, OUT = sys.argv[1:4]
os.makedirs(OUT, exist_ok=True)

# bone name hash -> rig bone name (hash = CRC32 of any suffix of the bone's path)
nodes = json.load(open(RIG))["nodes"]
hash_name = {}
for i, n in enumerate(nodes):
    path, j = [], i
    while j is not None and j >= 0:
        path.append(nodes[j]["n"]); j = nodes[j].get("p")
    path.reverse()
    for k in range(len(path)):
        hash_name.setdefault(zlib.crc32("/".join(path[k:]).encode()), n["n"])

env = UnityPy.load(DATA)
meshes = {}                    # (assets file name lower, path_id) -> object
lists = []
for obj in env.objects:
    t = obj.type.name
    if t == "Mesh":
        meshes[(obj.assets_file.name.lower(), obj.path_id)] = obj
    elif t == "MonoBehaviour":
        lists.append(obj)

KEY = re.compile(rb"([\x03-\x40])\x00\x00\x00([a-z][A-Za-z0-9_]{2,63})")
index = {}
for obj in lists:
    raw = obj.get_raw_data()
    if b"cosmetic" not in raw and b"hipsterGlasses" not in raw:
        continue
    exts = [e.path.lower() for e in obj.assets_file.externals]
    found = [(m.start(), m.end(), m.group(2).decode()) for m in KEY.finditer(raw)
             if m.group(1)[0] == len(m.group(2))]
    for n_, (s, e, key) in enumerate(found):
        end = found[n_ + 1][0] if n_ + 1 < len(found) else min(len(raw), e + 400)
        names = []
        for off in range(e + (-e % 4), end - 11, 4):
            fid, pid = struct.unpack_from("<iq", raw, off)
            if 1 <= fid <= len(exts) and 0 < pid < 1 << 40:
                m = meshes.get((os.path.basename(exts[fid - 1]), pid))
                if m is not None:
                    nm = m.peek_name()
                    if nm.startswith("cosmetic_") and not nm.endswith("_LOD1") and nm not in names:
                        names.append(nm)
        if names and key not in index:
            index[key] = names

def rnd(v, k):
    return [round(float(x), k) for x in v]

done, skipped = set(), []
by_name = {m.peek_name(): m for m in meshes.values() if m.peek_name().startswith("cosmetic_")}
for key, names in index.items():
    for nm in names:
        if nm in done:
            continue
        done.add(nm)
        mo = by_name[nm].read()
        h = MeshHandler(mo); h.process()
        bones = [hash_name.get(x) for x in mo.m_BoneNameHashes]
        if not bones or None in bones:
            skipped.append(nm); continue
        nv = h.m_VertexCount
        si, sw = [], []
        bi, bw = h.m_BoneIndices or [], h.m_BoneWeights or []
        for v in range(nv):
            ids = list(bi[v]) if v < len(bi) else [0]
            ws = list(bw[v]) if v < len(bw) and bw else [1.0]
            ids = (ids + [0, 0, 0, 0])[:4]; ws = (ws + [0, 0, 0, 0])[:4]
            si += [int(x) for x in ids]; sw += ws
        idx = []
        for sm in h.m_IndexBuffer if isinstance(h.m_IndexBuffer[0], (list, tuple)) else [h.m_IndexBuffer]:
            idx += [int(x) for x in sm]
        out = {"pos": rnd([c for p in h.m_Vertices for c in p], 5),
               "nrm": rnd([c for p in (h.m_Normals or []) for c in p], 4) or None,
               "col": rnd([c for p in (h.m_Colors or []) for c in p], 3) or None,
               "uv": rnd([c for p in (h.m_UV0 or []) for c in p], 5) or None,
               "si": si, "sw": rnd(sw, 4), "idx": idx, "bones": bones,
               "bind": [rnd([getattr(b, f"e{r}{c}") for c in range(4) for r in range(4)], 6) for b in mo.m_BindPose]}
        json.dump(out, open(os.path.join(OUT, nm + ".json"), "w"), separators=(",", ":"))
index = {k: [n for n in v if n not in skipped] for k, v in index.items()}
index = {k: v for k, v in index.items() if v}
json.dump(index, open(os.path.join(OUT, "index.json"), "w"), separators=(",", ":"), sort_keys=True)
print(f"{len(index)} cosmetics, {len(done) - len(skipped)} meshes written, {len(skipped)} skipped (unknown bones)")
