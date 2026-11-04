import sys, json; sys.path.insert(0, ".")
from services import photon_bot as p
v = p.view_state(); print(list(v.keys()))
for k, x in v.items():
    if isinstance(x, list) and k != "players": print(k, len(x), str(x[:2])[:200])
import inspect; print([n for n in dir(p) if not n.startswith("__")])
