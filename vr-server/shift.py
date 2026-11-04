import json, sys, time, re; sys.path.insert(0, ".")
from services import photon_bot as p
rows = json.load(open("/tmp/g_rows.json"))
t = p.log_tail(max_bytes=3000000, max_lines=1000000)
bad = set(re.findall(r"can't place (\S+) at (\d+),(\d+),(\d+)", t))
rows = [r for r in rows if (r[0], str(r[1]), str(r[2]), str(r[3])) not in bad]
print("rows", len(rows), "skipping refused", len(bad))
def send(c):
    for _ in range(50):
        if p.send(c): return
        time.sleep(0.2)
n0 = t.count("view: removed")
for r in sorted(rows, key=lambda r: r[2]):
    n, x, y, z, f, u, c = r[:7]
    send(f"vdel {x} {y} {z}"); time.sleep(0.03)
    send(f"vput {n} {x} {y-1} {z} {c} {f} {u}"); time.sleep(0.04)
# close the 1-cell gap between the bus legs and the bus floor
for lx, lz, c in json.load(open("/tmp/legs.json")):
    send(f"vput 2x2_wooden {lx} 39 {lz} {c} 4 2"); time.sleep(0.05)
time.sleep(4)
t2 = p.log_tail(max_bytes=3000000, max_lines=1000000)
print("removed", t2.count("view: removed") - n0)
print([l for l in t2.splitlines()[-4000:] if "can't place" in l or "nothing at" in l][-12:])
