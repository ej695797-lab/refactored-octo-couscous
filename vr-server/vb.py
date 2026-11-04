import sys, json; sys.path.insert(0, ".")
from services import photon_bot as p
b = p.view_blocks()
print(type(b), (len(b) if hasattr(b, "__len__") else ""), str(b)[:400])
