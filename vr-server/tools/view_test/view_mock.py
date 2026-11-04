"""Mock of the editor's View-tab endpoints with a REAL map (nexus, from the DB) and
players driven by a REAL captured VR pose, for checking static/editor_view.html
without the live bot. Serves the real page + the real assets."""
import json
import math
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PAGE, MAPS, ASSETS, AVATAR = sys.argv[1:5]
PORT = int(sys.argv[5]) if len(sys.argv) > 5 else 8799
REAL = json.load(open(MAPS))
INDEX = json.load(open(os.path.join(ASSETS, "index.json")))
T0 = time.time()
state = {"room": "nexus", "rev": 1, "log": "view: (mock) ready", "blocks": list(REAL["nexus"]["blocks"])}
DIRS = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]


def ext(n, f, u):
    w, h, d = (INDEX.get(n) or {}).get("s") or (1, 1, 1)
    F, U = DIRS[f], DIRS[u]
    R = (U[1] * F[2] - U[2] * F[1], U[2] * F[0] - U[0] * F[2], U[0] * F[1] - U[1] * F[0])
    return [abs(R[i]) * w + abs(U[i]) * h + abs(F[i]) * d for i in range(3)]


def ground(x, z):
    top = 0
    for n, bx, by, bz, f, u, _c in state["blocks"]:
        e = ext(n, f, u)
        if bx <= x < bx + e[0] and bz <= z < bz + e[2]:
            top = max(top, by + e[1])
    return top


# A real pose captured from a player (body-local head + hands), from the bot's logs.
SAMPLE = {"q": [5.7e-06, 0.518, 0.0003, -0.8554],
          "h": [-0.0331, 0.2373, 0.0166, -0.0139, 0.00097, -0.0692, -0.9975],
          "r": [0.189, 0.0052, 0.15, -0.397, -0.1165, -0.4501, 0.7914, 0.5, 0.5, 0.0],
          "l": [-0.2004, -0.1253, 0.4359, -0.2057, 0.1259, 0.3088, -0.92, 0.5, 0.5, 0.0]}
SPOTS = [(60, 70), (66, 66), (70, 60)]
GROUND = {s: ground(*s) for s in SPOTS}


def yaw_q(a):
    return [0.0, math.sin(a / 2), 0.0, math.cos(a / 2)]


def players():
    t = time.time() - T0
    out = []
    for i, (n, skin) in enumerate((("Mocky", "#ffc8c3"), ("CabbageVR", "#9ee37a"), ("lazerfresh", "#7fb0ff"))):
        gx, gz = SPOTS[i]
        p = {"a": i + 2, "n": n, "id": f"o_{1000 + i}", "x": gx + .5, "y": GROUND[(gx, gz)] + 0.9 + 0.05,
             "z": gz + .5, "t": i == 0, "skin": skin, "eye": "#3a6fd8"}
        if i == 0:
            p.update({k: list(v) for k, v in SAMPLE.items()})
        else:
            a = t * (0.6 if i == 1 else -0.4) + i
            p["q"] = yaw_q(a)
            p["h"] = [0, 0.237, 0.01] + [0.0, math.sin(math.sin(t * 2) * 0.3 / 2), 0.0, math.cos(math.sin(t * 2) * 0.3 / 2)]
            wave = math.sin(t * 4 + i)
            p["r"] = [0.22, 0.05 + 0.18 * wave, 0.22, 0, 0, 0, 1, 0.5, 0.5, 0]
            p["l"] = [-0.2, -0.15, 0.2, 0, 0, 0, 1, 0.5, 0.5, 0]
        p["yaw"] = 0
        out.append(p)
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_bytes(self, b, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def send_json(self, obj, code=200):
        self.send_bytes(json.dumps(obj).encode(), "application/json", code)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        if p in ("/", "/editor/view"):
            self.send_bytes(open(PAGE, "rb").read(), "text/html; charset=utf-8")
        elif p == "/editor/view/a/yeep.json":
            self.send_bytes(open(AVATAR, "rb").read(), "application/json")
        elif p.startswith("/editor/view/a/"):
            f = os.path.realpath(os.path.join(ASSETS, p[len("/editor/view/a/"):]))
            if f.startswith(os.path.realpath(ASSETS)) and os.path.isfile(f):
                self.send_bytes(open(f, "rb").read(), "image/png" if f.endswith(".png") else
                                "application/json" if f.endswith(".json") else "application/octet-stream")
            else:
                self.send_json({"detail": "nf"}, 404)
        elif p == "/editor/api/photon/view":
            self.send_json({"now": time.time(), "view": {
                "t": time.time(), "sid": "mock-" + state["room"], "room": state["room"] + "_photon", "rk": state["room"],
                "dims": REAL["nexus"]["dims"], "rev": state["rev"], "loaded": True, "target": "o_1000",
                "targetName": "Mocky", "players": players()}})
        elif p == "/editor/api/photon/view/blocks":
            sid = "mock-" + state["room"]
            if q.get("rev", ["-1"])[0] == str(state["rev"]) and q.get("sid", [""])[0] == sid:
                self.send_json({"same": True})
            else:
                self.send_json({"rev": state["rev"], "sid": sid, "rk": state["room"], "blocks": state["blocks"]})
        elif p == "/editor/api/photon/view/patterns":
            self.send_json({"patterns": sorted(INDEX)[:300]})
        elif p == "/editor/api/photon/players":
            ps = [{"accountID": f"o_{1000 + i}", "name": n, "roomKey": "nexus", "roomName": "Nexus",
                   "photonRoom": "nexus_photon"} for i, n in enumerate(("Mocky", "CabbageVR", "lazerfresh"))]
            ps += [{"accountID": "o_2000", "name": "Puma_lover", "roomKey": "wild_ch2_suburb",
                    "roomName": "Suburb", "photonRoom": "suburb_photon"}]
            self.send_json({"players": ps})
        elif p == "/editor/api/photon/status":
            self.send_json({"running": True, "log": "joined\n" + state["log"]})
        else:
            self.send_json({"detail": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if u.path == "/editor/api/photon/view/edit":
            x, y, z = int(body["x"]), int(body["y"]), int(body["z"])
            if body.get("op") == "put":
                state["blocks"].append([body["pattern"], x, y, z, int(body.get("fwd", 4)), int(body.get("up", 2)),
                                        body.get("color", "white")])
                state["log"] = f"view: placed {body['pattern']} ({body.get('color')}) at {x},{y},{z}"
            else:
                before = len(state["blocks"])
                state["blocks"] = [b for b in state["blocks"] if (b[1], b[2], b[3]) != (x, y, z)]
                state["log"] = f"view: removed {before - len(state['blocks'])} at {x},{y},{z}"
            state["rev"] += 1
            self.send_json({"ok": True})
        else:
            self.send_json({"ok": True, "was": True})


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
