import sys; sys.path.insert(0, ".")
from services import photon_bot as p
t = p.log_tail(max_bytes=400000, max_lines=100000)
bad = [l for l in t.splitlines() if "can't place" in l]
print("placed", t.count("view: placed"), "refused", len(bad), "running-room", p.current_room())
print("\n".join(bad[:6])); print("\n".join(t.splitlines()[-3:]))
