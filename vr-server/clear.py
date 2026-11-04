import json, sys, time; sys.path.insert(0, ".")
from services import photon_bot as p
D = json.load(open("/tmp/clear.json")); P = json.load(open("/tmp/none.json"))
def send(c):
    for _ in range(50):
        if p.send(c): return
        time.sleep(0.2)
t0 = p.log_tail(max_bytes=4000000, max_lines=2000000)
r0, p0, b0 = t0.count("view: removed"), t0.count("view: placed"), t0.count("can't place")
for r in D: send(f"vdel {r[1]} {r[2]} {r[3]}"); time.sleep(0.03)
time.sleep(2)
for r in P: send(f"vput {r[0]} {r[1]} {r[2]} {r[3]} {r[6]} {r[4]} {r[5]}"); time.sleep(0.045)
time.sleep(5)
t = p.log_tail(max_bytes=4000000, max_lines=2000000)
print("removed", t.count("view: removed") - r0, "/", len(D), "placed", t.count("view: placed") - p0, "/", len(P), "refused", t.count("can't place") - b0)
print([l for l in t.splitlines()[-3000:] if "can't place" in l][-8:])
