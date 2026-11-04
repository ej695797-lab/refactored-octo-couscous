import sys, time, json; sys.path.insert(0, ".")
from services import photon_bot as p
pts = []
for i in range(40):
    v = p.view_state()
    for pl in v.get("players", []):
        pts.append((round(pl["x"]), round(pl["y"], 1), round(pl["z"]), pl["n"]))
    time.sleep(1.5)
json.dump(pts, open("/tmp/ground_pts.json", "w"))
for q in sorted(set(pts))[:400:3]: print(q)
