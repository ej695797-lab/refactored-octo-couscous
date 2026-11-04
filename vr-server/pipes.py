"""3D pipes screensaver, live through the Photon bot. Pipes (2x2 thick, glowing
colors, ball joints at turns) grow out from the middle of the room one piece at a
time; when the block budget is used up the pipes are taken back out in reverse
order and it starts over. Runs until /tmp/pipes.stop exists or the bot leaves.
Run from the live vr-server dir: nohup python3 pipes.py > /tmp/pipes.log 2>&1 &"""
import os
import random
import sys
import time

sys.path.insert(0, ".")
from services import photon_bot as p  # noqa: E402

DX, DY, DZ = 128, 96, 128
BUDGET = int(os.getenv("PIPES_BUDGET", "5600"))
RATE = float(os.getenv("PIPES_RATE", "0.06"))   # seconds between blocks while growing
STOP = "/tmp/pipes.stop"
COLORS = ["red_emissive", "green_emissive", "cyan_emissive", "yellow_emissive", "purple_emissive",
          "orange_emissive", "blue_emissive", "red", "cyan", "yellow", "white", "pink", "blurple", "green"]
# bar = (pattern, length): rotation giving that length along X / Y / Z, other two sides 2 and 1
BARS = {16: "2x16_wooden", 8: "2x8_wooden", 4: "2x4_wooden"}
ROT = {"x": (5, 3), "y": (5, 1), "z": (1, 3)}          # extents (L,1,2) / (1,L,2) / (2,1,L)
STACK = {"x": (0, 1, 0), "y": (1, 0, 0), "z": (0, 1, 0)}  # second bar beside the first
NX, NY, NZ = DX // 2, DY // 2, DZ // 2                 # 2-cell lattice
DIRS = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]


def send(cmd):
    for _ in range(100):
        if p.send(cmd):
            return True
        time.sleep(0.1)
    return False


def alive():
    """Not stopped, and the bot's view of its room is fresh (updated in the last 30 s)."""
    if os.path.exists(STOP):
        return False
    v = p.view_state() or {}
    return bool(v.get("room")) and time.time() - v.get("t", 0) < 30


def run_cycle(rnd):
    used = set()
    placed = []                                        # origins, in order

    def put(name, x, y, z, f, u, c):
        send(f"vput {name} {x} {y} {z} {c} {f} {u}")
        placed.append((x, y, z))
        time.sleep(RATE)

    def segment(a, b, c):
        """Pipe body from just past node a up to and including node b."""
        axis = "x" if a[0] != b[0] else "y" if a[1] != b[1] else "z"
        i = "xyz".index(axis)
        lo, hi = sorted((a[i], b[i]))
        start = lo * 2 + 2 if a[i] < b[i] else lo * 2      # after a's joint, through b's node
        end = hi * 2 + 2 if a[i] < b[i] else hi * 2
        n = end - start
        base = [a[0] * 2, a[1] * 2, a[2] * 2]
        pos = start
        while n > 0 and alive():
            L = 16 if n >= 16 else 8 if n >= 8 else 4 if n >= 4 else 2
            o = list(base)
            o[i] = pos
            if L == 2:
                put("2x2x2_wooden", o[0], o[1], o[2], 5, 3, c)
            else:
                f, u = ROT[axis]
                s = STACK[axis]
                put(BARS[L], o[0], o[1], o[2], f, u, c)
                put(BARS[L], o[0] + s[0], o[1] + s[1], o[2] + s[2], f, u, c)
            pos += L
            n -= L

    def joint(n, c):
        put("2x2x2_wooden", n[0] * 2, n[1] * 2, n[2] * 2, 5, 3, c)

    def free(n):
        return 0 < n[0] < NX - 1 and 0 < n[1] < NY - 1 and 0 < n[2] < NZ - 1 and n not in used

    first = True
    while len(placed) < BUDGET and alive():
        c = rnd.choice(COLORS)
        spread = 2 if first else rnd.randint(2, 12)    # new pipes start near the middle
        for _ in range(60):
            cur = (NX // 2 + rnd.randint(-spread, spread), NY // 2 + rnd.randint(-spread, spread),
                   NZ // 2 + rnd.randint(-spread, spread))
            if free(cur):
                break
        else:
            break
        first = False
        used.add(cur)
        joint(cur, c)
        d = rnd.choice(DIRS)
        for _ in range(rnd.randint(8, 25)):            # turns per pipe
            if len(placed) >= BUDGET or not alive():
                break
            run = rnd.randint(2, 9)
            nxt = cur
            for _ in range(run):
                t = (nxt[0] + d[0], nxt[1] + d[1], nxt[2] + d[2])
                if not free(t):
                    break
                nxt = t
            if nxt != cur:
                k = cur
                while k != nxt:                        # reserve the run
                    k = (k[0] + d[0], k[1] + d[1], k[2] + d[2])
                    used.add(k)
                segment(cur, nxt, c)
                cur = nxt
            opts = [e for e in DIRS if e != d and e != (-d[0], -d[1], -d[2])
                    and free((cur[0] + e[0], cur[1] + e[1], cur[2] + e[2]))]
            if not opts:
                break
            d = rnd.choice(opts)
    print(time.strftime("%H:%M:%S"), "cycle full:", len(placed), "blocks", flush=True)
    time.sleep(8)
    for x, y, z in reversed(placed):                    # rewind
        send(f"vdel {x} {y} {z}")
        time.sleep(0.03)
    time.sleep(3)


if __name__ == "__main__":
    rnd = random.Random()
    if os.path.exists(STOP):
        os.remove(STOP)
    cycle = 0
    while alive():
        cycle += 1
        print(time.strftime("%H:%M:%S"), "cycle", cycle, "room", p.current_room(), flush=True)
        run_cycle(rnd)
    print(time.strftime("%H:%M:%S"), "stopped", flush=True)
