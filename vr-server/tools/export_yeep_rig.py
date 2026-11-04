"""Export the real Yeep player rig for the editor's View tab (static/view-yeep.json).

Source: the Photon bot Unity project (an AssetRipper export of the game), read as text,
no Unity: the SimpleAvatar_Puppet skeleton from Prefabs/machine/targetDummy.prefab, the
player's skinned body SimpleAvatar2 + SimpleHand meshes with their bind poses, and the
two eye quads. Coordinates stay in Unity space; the page mirrors Z like the block view.

usage: export_yeep_rig.py <YeepsAssets dir> <out json>
"""
import json
import math
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unity_yaml import guid_index, load_docs, read_mesh  # noqa: E402

A, OUT = sys.argv[1], sys.argv[2]
G = guid_index(os.path.join(A, "Mesh"))
docs = load_docs(os.path.join(A, "Prefabs", "machine", "targetDummy.prefab"))
go = {fid: v for fid, (_c, k, v) in docs.items() if k == "GameObject"}
tr = {fid: v for fid, (_c, k, v) in docs.items() if k == "Transform"}


def gname(t):
    return go.get((t.get("m_GameObject") or {}).get("fileID"), {}).get("m_Name")


# the skeleton: everything under playerModel (Unity local TRS)
root = next(fid for fid, t in tr.items() if gname(t) == "playerModel")
nodes, index = [], {}


def walk(fid, parent):
    t = tr[fid]
    i = len(nodes)
    index[fid] = i
    p, r, s = t["m_LocalPosition"], t["m_LocalRotation"], t["m_LocalScale"]
    nodes.append({"n": gname(t), "p": parent, "t": [p["x"], p["y"], p["z"]],
                  "r": [r["x"], r["y"], r["z"], r["w"]], "s": [s["x"], s["y"], s["z"]]})
    for c in t.get("m_Children") or []:
        if c["fileID"] in tr:
            walk(c["fileID"], i)


walk(root, -1)
nodes[0]["t"], nodes[0]["r"], nodes[0]["s"] = [0, 0, 0], [0, 0, 0, 1], [1, 1, 1]
by_name = {}
for i, n in enumerate(nodes):
    by_name.setdefault(n["n"], []).append(i)


def rnd(vals, k=5):
    return [round(v, k) for v in vals] if vals else None


def pack(m):
    out = {"pos": rnd(m["pos"][0]), "nrm": rnd(m["nrm"][0], 4) if m["nrm"] else None,
           "uv": rnd(m["uv"][0], 5) if m["uv"] else None,
           "col": rnd(m["col"][0], 3) if m["col"] else None,
           "sw": rnd(m["sw"][0], 4) if m["sw"] else None,
           "si": [int(x) for x in m["si"][0]] if m["si"] else None,
           "idx": m["idx"], "subs": m["subs"], "bind": [rnd(b, 6) for b in m["bind"]]}
    return out


def mesh_path(name):
    p = os.path.join(A, "Mesh", name + ".asset")
    if not os.path.isfile(p):
        raise SystemExit(f"missing mesh {p}")
    return p


# body: SimpleAvatar2 (the real player). Its bone list comes from the prefab's body
# renderer (SimpleAvatar2_Wooden, the same rig); that's only safe if every bind pose
# matches, so check that (this export's bone-name hashes are unreadable).
body = read_mesh(mesh_path("SimpleAvatar2"))
wood_smr = next(v for (_c, k, v) in docs.values() if k == "SkinnedMeshRenderer"
                and os.path.basename(G.get((v.get("m_Mesh") or {}).get("guid"), "")) == "SimpleAvatar2_Wooden.asset")
body_bones = [index[b["fileID"]] for b in wood_smr["m_Bones"]]
wood = read_mesh(mesh_path("SimpleAvatar2_Wooden"))
if len(wood["bind"]) != len(body["bind"]) or any(
        max(abs(a - b) for a, b in zip(x, y)) > 1e-3 for x, y in zip(wood["bind"], body["bind"])):
    raise SystemExit("SimpleAvatar2 bind poses differ from the wooden rig's: bone order can't be trusted")

# hands: one SimpleHand skin per side, bones from that side's renderer in the prefab
hands = {}
for fid, (_c, k, v) in docs.items():
    if k != "SkinnedMeshRenderer":
        continue
    g = (v.get("m_Mesh") or {}).get("guid")
    if os.path.basename(G.get(g, "")) != "SimpleHand.asset":
        continue
    bones = [index[b["fileID"]] for b in v.get("m_Bones") or [] if b.get("fileID") in index]
    if len(bones) != len(v.get("m_Bones") or []):
        continue
    # which side: walk up from the first bone to a Wrist.L / Wrist.R
    i, side = bones[0], None
    while i >= 0 and side is None:
        side = {"Wrist.L": "l", "Wrist.R": "r"}.get(nodes[i]["n"])
        i = nodes[i]["p"]
    if side and side not in hands:
        hands[side] = bones
hand = read_mesh(mesh_path("SimpleHand"))

# eyes: the Eye0 / Eye1 quads (built-in Quad mesh, PlayerEye material)
eyes = [by_name[n][0] for n in ("Eye0", "Eye1") if by_name.get(n)]


# Face the body frame (+Z forward, +X right) like the network pose: if the right
# shoulder is on -X the model faces -Z, so turn the root half round.
def world(i):
    import numpy as np
    M = np.eye(4)
    chain = []
    while i >= 0:
        chain.append(i)
        i = nodes[i]["p"]
    for j in reversed(chain):
        n = nodes[j]
        x, y, z, w = n["r"]
        R = np.array([[1 - 2 * (y*y + z*z), 2 * (x*y - z*w), 2 * (x*z + y*w)],
                      [2 * (x*y + z*w), 1 - 2 * (x*x + z*z), 2 * (y*z - x*w)],
                      [2 * (x*z - y*w), 2 * (y*z + x*w), 1 - 2 * (x*x + y*y)]])
        T = np.eye(4)
        T[:3, :3] = R @ np.diag(n["s"])
        T[:3, 3] = n["t"]
        M = M @ T
    return M


if world(by_name["UpperArm.R"][0])[0, 3] < 0:
    nodes[0]["r"] = [0, 1, 0, 0]
head_w = world(by_name["Head"][0])[:3, 3]

out = {"note": "Yeep player rig from the live game (via the Photon bot Unity project). Unity space, metres.",
       "nodes": nodes, "body": {**pack(body), "bones": body_bones},
       "hand": pack(hand), "hands": hands, "eyes": eyes,
       "headRest": [round(float(v), 4) for v in head_w]}
json.dump(out, open(OUT, "w"), separators=(",", ":"))
print(f"nodes {len(nodes)}  body {body['n']} verts {len(body['subs'])} submeshes {len(body_bones)} bones  "
      f"hand {hand['n']} verts  hands {sorted(hands)}  eyes {len(eyes)}  head at {out['headRest']}  "
      f"{os.path.getsize(OUT) // 1024} KB")
