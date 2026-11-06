"""Build the accountID <-> name index used by the relay's POST /username.

Real Yeeps has no "find player by name", so we keep our own index of every
(accountID, name) pair our servers have seen:
  - accounts in both game DBs (Yeeps240 live, Yeeps119 1.31): displayName + oculusID
    (oculusID = the Meta username the player logged in with)
  - fetchAccountDatas replies in the traffic logs ("o_x": {"displayName": "Name"})
  - Photon user IDs in the client logs ("o_x$VR$Name")
Log files are read incrementally (byte offsets kept in the index), so only the
first run scans the whole multi-GB logs.

Output: ~/.local/state/yeeps/name_index.json
  {"offsets": {path: bytes}, "accounts": {accountID: {"names": {name: lastSeenUnix},
   "meta": metaUsername-or-null, "src": [sources]}}}
Run: python3 tools/name_index.py  (systemd timer yeeps-name-index runs it every 10 min)
"""
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # yeeps-proxy
CARRY = os.path.dirname(ROOT)
OUT = os.path.expanduser("~/.local/state/yeeps/name_index.json")
LOGS = [
    os.path.join(CARRY, "yeeps-proxy-240/vr-server/logs/lambda_traffic.log"),
    os.path.join(CARRY, "yeeps-proxy-240/vr-server/logs/login_traffic.log"),
    os.path.join(CARRY, "yeeps-proxy-240/vr-server/logs/devlog.log"),
    os.path.join(CARRY, "yeeps-proxy/vr-server/logs/lambda_traffic.log"),
    os.path.join(CARRY, "yeeps-proxy/vr-server/logs/login_traffic.log"),
]
ENV_FILES = [os.path.expanduser("~/.config/yeeps/vr-server-240.env")]

# "o_123": {"displayName": "Name"   (also with \" escaping inside JSON-in-JSON)
RX_FETCH = re.compile(rb'(o_\d{6,20})\\*"\s*:\s*\{\s*\\*"displayName\\*"\s*:\s*\\*"([^"\\\r\n]{1,40})\\*"')
# o_123$VR$Name  (Photon user ID; may carry a trailing $roleMask)
RX_PHOTON = re.compile(rb'(o_\d{6,20})\$VR\$([^\s"\\$,\']{1,40})')

CHUNK = 8 << 20


def load():
    try:
        with open(OUT) as f:
            d = json.load(f)
        d.setdefault("offsets", {})
        d.setdefault("accounts", {})
        return d
    except Exception:
        return {"offsets": {}, "accounts": {}}


def add(idx, aid, name, src, ts=None, meta=None):
    if not aid or not aid.startswith("o_"):
        return
    a = idx["accounts"].setdefault(aid, {"names": {}, "meta": None, "src": []})
    if name:
        name = name.strip()
        if name and name != aid:
            a["names"][name] = max(a["names"].get(name, 0), int(ts or time.time()))
    if meta:
        a["meta"] = meta.strip()
    if src not in a["src"]:
        a["src"].append(src)


def scan_log(idx, path):
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    off = idx["offsets"].get(path, 0)
    if off > size:          # log was rotated/truncated
        off = 0
    if off == size:
        return 0
    mtime = int(os.path.getmtime(path))
    src = "log:" + os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path)))) + "/" + os.path.basename(path)
    n = 0
    tail = b""
    with open(path, "rb") as f:
        f.seek(off)
        while True:
            buf = f.read(CHUNK)
            if not buf:
                break
            data = tail + buf
            # keep a small overlap so a match split across chunks isn't lost
            cut = max(0, len(data) - 256)
            for rx in (RX_FETCH, RX_PHOTON):
                for m in rx.finditer(data, 0, len(data)):
                    if m.start() >= cut and buf:
                        continue        # handled with the next chunk
                    add(idx, m.group(1).decode(), m.group(2).decode("utf-8", "replace"), src, mtime)
                    n += 1
            tail = data[cut:]
            off += len(buf)
    # leftover tail matches
    for rx in (RX_FETCH, RX_PHOTON):
        for m in rx.finditer(tail):
            add(idx, m.group(1).decode(), m.group(2).decode("utf-8", "replace"), src, mtime)
    idx["offsets"][path] = size
    return n


def scan_dbs(idx):
    for envf in ENV_FILES:
        try:
            for line in open(envf):
                if line.startswith("MONGO_URI="):
                    os.environ.setdefault("MONGO_URI", line.split("=", 1)[1].strip().strip('"'))
        except OSError:
            pass
    uri = os.environ.get("MONGO_URI")
    if not uri:
        print("no MONGO_URI, skipping DBs")
        return 0
    from pymongo import MongoClient
    cli = MongoClient(uri, serverSelectionTimeoutMS=20000)
    n = 0
    for dbn in ("Yeeps240", "Yeeps119"):
        for d in cli[dbn]["accounts"].find({}, {"_id": 0, "accountID": 1, "displayName": 1, "oculusID": 1}):
            meta = d.get("oculusID") if isinstance(d.get("oculusID"), str) else None
            name = d.get("displayName") if isinstance(d.get("displayName"), str) else None
            add(idx, d.get("accountID") or "", name, "db:" + dbn, None, meta)
            n += 1
    cli.close()
    return n


def main():
    t = time.time()
    idx = load()
    counts = {"db": scan_dbs(idx)}
    for p in LOGS:
        counts[os.path.basename(p)] = counts.get(os.path.basename(p), 0) + scan_log(idx, p)
    idx["updated"] = int(time.time())
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(idx, f, separators=(",", ":"))
    os.replace(tmp, OUT)
    print(f"name index: {len(idx['accounts'])} accounts, {counts}, {time.time() - t:.0f}s")


if __name__ == "__main__":
    sys.exit(main())
