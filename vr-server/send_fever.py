import json, sys, time; sys.path.insert(0, ".")
from services import photon_bot as p
rows = json.load(open("/tmp/fever_rows.json"))
ok = 0
for r in rows:
    n, x, y, z, f, u, c = r[:7]
    for _ in range(50):
        if p.send(f"vput {n} {x} {y} {z} {c} {f} {u}"): ok += 1; break
        time.sleep(0.2)
    time.sleep(0.04)
print("sent", ok, "of", len(rows))
