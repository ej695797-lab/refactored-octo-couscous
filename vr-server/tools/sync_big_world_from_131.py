"""One-time copy of the 1.31.0 world's room settings (+ chosen maps) into the
2.4 database, for the big APK (which plays the 1.31 world, services/big_world.py).

Rooms: every room of the 1.31 "beta4" room map gets 1.31's room document
(dimensions, floor depth, theme, name, locks, ...). Maps: only the rooms listed
in MAPS are copied (the others are already identical; BAM rooms stay with 2.4's
own Build-A-Map), and rooms that 2.4's own world also has are left alone. Previous 2.4 documents are saved in big_world_backups first.
1.31's database is only read.

Usage: python3 tools/sync_big_world_from_131.py [--apply]
"""
import os
import sys
from datetime import datetime, timezone

from pymongo import MongoClient

MAPS = ["wild_0", "wild_1_beach", "wild_2_snow"]
SKIP_FIELDS = {"_id"}

env = {}
for l in open(os.path.expanduser("~/.config/yeeps/vr-server-240.env")):
    if "=" in l and not l.startswith("#"):
        k, v = l.strip().split("=", 1)
        env[k] = v
c = MongoClient(env["MONGO_URI"])
src, dst = c[env["CARRYOVER_DB"]], c[env["DB_NAME"]]
apply = "--apply" in sys.argv
now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
nodes = src.room_maps.find_one({"versionKey": "beta4"})["data"]
# rooms that are also in 2.4's own world (e.g. bam_winner) stay as they are
stock = {n.get("roomKey") for n in dst.room_maps.find_one({"versionKey": "freebuild_0"})["data"]}
for n in nodes:
    rk = n.get("roomKey")
    if not rk or rk in stock:
        if rk:
            print(f"room {rk}: in 2.4's world too - left alone")
        continue
    a = src.rooms.find_one({"roomKey": rk})
    if not a:
        continue
    b = dst.rooms.find_one({"roomKey": rk})
    new = {k: v for k, v in a.items() if k not in SKIP_FIELDS}
    changed = sorted(k for k in new if (b or {}).get(k) != new[k])
    print(f"room {rk}: {len(changed)} fields {changed[:8]}")
    if apply and changed:
        dst.big_world_backups.insert_one({"at": now, "kind": "room", "roomKey": rk, "doc": b})
        dst.rooms.update_one({"roomKey": rk}, {"$set": new}, upsert=True)
for rk in MAPS:
    a = src.maps.find_one({"mapKey": rk + "$current"})
    b = dst.maps.find_one({"mapKey": rk + "$current"})
    if not a:
        continue
    same = b and b.get("saveData") == a.get("saveData")
    print(f"map {rk}: {'same' if same else 'copy'} (1.31 saved {a.get('savedAt')}, 2.4 saved {(b or {}).get('savedAt')})")
    if apply and not same:
        dst.big_world_backups.insert_one({"at": now, "kind": "map", "mapKey": rk + "$current", "doc": b})
        dst.maps.update_one({"mapKey": rk + "$current"}, {"$set": {k: v for k, v in a.items() if k not in SKIP_FIELDS}}, upsert=True)
print("applied" if apply else "dry run (use --apply)")
