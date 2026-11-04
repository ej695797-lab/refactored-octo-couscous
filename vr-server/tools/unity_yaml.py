"""Read Unity text (YAML) assets without Unity: prefabs/scenes and Mesh .asset files.

Used to rebuild the real Yeep player rig for the editor's View tab from the Photon bot
Unity project (an AssetRipper export of the game). Only what that needs:
  load_docs(path)    -> {fileID: (classID, body dict)}
  guid_index(roots)  -> {guid: asset path}  (from the .meta files)
  read_mesh(path)    -> positions/normals/uvs/skin weights+indices/indices/bindposes/bone hashes
"""
import os
import re
import struct

import yaml

_HDR = re.compile(r"^--- !u!(\d+) &(-?\d+)(.*)$", re.M)


def load_docs(path):
    text = open(path, encoding="utf-8", errors="replace").read()
    out = {}
    heads = list(_HDR.finditer(text))
    for i, h in enumerate(heads):
        body = text[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        try:
            d = yaml.safe_load(body) or {}
        except yaml.YAMLError:
            continue
        (kind, val), = d.items() if len(d) == 1 else (("?", d),)
        out[int(h.group(2))] = (int(h.group(1)), kind, val or {})
    return out


def guid_index(*roots):
    idx = {}
    for root in roots:
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                if fn.endswith(".meta"):
                    try:
                        with open(os.path.join(dp, fn), encoding="utf-8", errors="replace") as f:
                            for line in f:
                                if line.startswith("guid:"):
                                    idx[line.split()[1]] = os.path.join(dp, fn[:-5])
                                    break
                    except OSError:
                        pass
    return idx


# VertexAttributeFormat -> (struct code, bytes)
_FMT = {0: ("f", 4), 1: ("e", 2), 2: ("B", 1), 3: ("b", 1), 4: ("H", 2), 5: ("h", 2),
        6: ("B", 1), 7: ("b", 1), 8: ("H", 2), 9: ("h", 2), 10: ("I", 4), 11: ("i", 4)}
# normalised integer formats -> divide by
_NORM = {2: 255.0, 3: 127.0, 4: 65535.0, 5: 32767.0}


def read_mesh(path):
    docs = load_docs(path)
    mesh = next(v for (_c, k, v) in docs.values() if k == "Mesh")
    vd = mesh["m_VertexData"]
    n = int(vd["m_VertexCount"])
    raw = bytes.fromhex(vd["_typelessdata"])
    chans = vd["m_Channels"]
    # stride of each stream, then stream offsets (each stream starts 16-byte aligned)
    strides = {}
    for c in chans:
        if c["dimension"]:
            s = c["stream"]
            strides[s] = max(strides.get(s, 0), c["offset"] + _FMT[c["format"]][1] * c["dimension"])
    starts, pos = {}, 0
    for s in sorted(strides):
        pos = (pos + 15) // 16 * 16
        starts[s] = pos
        pos += strides[s] * n

    def channel(i):
        if i >= len(chans) or not chans[i]["dimension"]:
            return None
        c = chans[i]
        code, size = _FMT[c["format"]]
        dim, st, off, stride = c["dimension"], starts[c["stream"]], c["offset"], strides[c["stream"]]
        fmt = "<" + code * dim
        norm = _NORM.get(c["format"])
        vals = []
        for v in range(n):
            t = struct.unpack_from(fmt, raw, st + v * stride + off)
            vals.extend(x / norm for x in t) if norm else vals.extend(t)
        return vals, dim

    ib = bytes.fromhex(mesh.get("m_IndexBuffer") or "")
    idx = list(struct.unpack("<%dI" % (len(ib) // 4), ib) if mesh.get("m_IndexFormat") == 1
               else struct.unpack("<%dH" % (len(ib) // 2), ib))
    subs = [(int(s["firstByte"]) // (4 if mesh.get("m_IndexFormat") == 1 else 2), int(s["indexCount"]))
            for s in mesh.get("m_SubMeshes") or []]
    bind = [[float(b[f"e{r}{c}"]) for c in range(4) for r in range(4)] for b in mesh.get("m_BindPose") or []]
    try:       # some AssetRipper exports write this field with stray non-hex characters
        hb = bytes.fromhex(mesh.get("m_BoneNameHashes") or "") if isinstance(mesh.get("m_BoneNameHashes"), str) else b""
        hashes = list(struct.unpack("<%dI" % (len(hb) // 4), hb))
    except ValueError:
        hashes = None
    return {"name": mesh.get("m_Name"), "n": n, "pos": channel(0), "nrm": channel(1), "uv": channel(4),
            "col": channel(3), "sw": channel(12), "si": channel(13), "idx": idx, "subs": subs,
            "bind": bind, "boneHashes": hashes, "rootHash": mesh.get("m_RootBoneNameHash")}
