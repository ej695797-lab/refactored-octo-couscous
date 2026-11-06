import os
"""Rebuild the ??? room (fever_dream_1) as a big "map mess": cubic chunks pulled from
many real maps, scattered/mixed randomly so recognizable connected pieces (a wall, a
wood cluster) land as parts of one huge jumble. Owner 2026-10-13.
  DB_NAME=Yeeps240 python3 tools/make_mess_map.py [--target N] [--push]
Without --push it only reports; with --push it backs up fever_dream_1$current and writes
the new map + multiplayer-ready room-doc fields.
"""
import argparse, os, sys, random, collections, datetime
sys.path.insert(0, os.getenv("VR_SERVER_DIR", "../../server-live/vr-server"))
from pymongo import MongoClient
from services.mapsync_decoder import decode_map
from services.mapsync_serializer import serialize_blocks_spam

ap = argparse.ArgumentParser()
ap.add_argument("--target", type=int, default=22000)
ap.add_argument("--push", action="store_true")
ap.add_argument("--seed", type=int, default=1337)
a = ap.parse_args()
random.seed(a.seed)
ROOM = "fever_dream_1"
OWNER = "o_OWNER0000000001"
db = MongoClient(os.environ["MONGO_URI"])[os.environ.get("DB_NAME", "Yeeps240")]

SOURCES = ["ch2_hub","ch2_horror_0","ch2_horror_2","ch2_horror_4","ch2_horror_5","ch2_horror_7","ch2_horror_8",
           "ch2_horror_9","ch2_horror_10","ch2_horror2_0","ch2_horror2_1","ch2_horror2_2","ch2_horror2_3","ch2_horror2_4",
           "ch2_old_monsterHouse","official_0","official_1","official_2","bam_0","bam_2","bam_winner","nexus","nexus_worlds"]
# Blocks that would warp a player out or confuse multiplayer: leave them out of the mess.
BAD = ("warpportal","teleport","portal","spawn","timemachine","checkpoint","transition","door_","worldmap","dispenser")
TILE = 24
MIN_CH, MAX_CH = 25, 500
RX = (4, 240); RY = (1, 96); RZ = (4, 240)
MAXD = (250, 160, 250)

pool = []
for rk in SOURCES:
    d = db.maps.find_one({"mapKey": f"{rk}$current"}, {"_id": 0, "saveData": 1})
    if not d:
        continue
    try:
        blocks = decode_map(d["saveData"])
    except Exception:
        continue
    blocks = [b for b in blocks if not any(x in str(b.get("name", "")).lower() for x in BAD)]
    if not blocks:
        continue
    mnx = min(b["x"] for b in blocks); mny = min(b["y"] for b in blocks); mnz = min(b["z"] for b in blocks)
    tiles = collections.defaultdict(list)
    for b in blocks:
        rx, ry, rz = b["x"] - mnx, b["y"] - mny, b["z"] - mnz
        tiles[(rx // TILE, ry // TILE, rz // TILE)].append(
            {"name": b["name"], "dx": rx % TILE, "dy": ry % TILE, "dz": rz % TILE,
             "fwd": b["fwd"], "up": b["up"], "color": b["color"]})
    pool.extend([t for t in tiles.values() if MIN_CH <= len(t) <= MAX_CH])

random.shuffle(pool)
cells = {}
i = parts = 0
while len(cells) < a.target and pool and i < 300000:
    ch = pool[i % len(pool)]; i += 1
    ox = random.randint(*RX); oy = random.randint(*RY); oz = random.randint(*RZ)
    for b in ch:
        x, y, z = ox + b["dx"], oy + b["dy"], oz + b["dz"]
        if x > MAXD[0] or y > MAXD[1] or z > MAXD[2]:
            continue
        cells.setdefault((x, y, z), {"name": b["name"], "x": x, "y": y, "z": z,
                                     "fwd": b["fwd"], "up": b["up"], "color": b["color"], "owner": OWNER})
    parts += 1

blocks = list(cells.values())
print(f"pool chunks {len(pool)} | placed parts {parts} | blocks {len(blocks)}")
save = serialize_blocks_spam(blocks)
print(f"SPAM {len(save)} chars")
if not a.push:
    print("(dry run; pass --push to write)")
    sys.exit(0)

now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y %m/%d %H:%M:%S")
old = db.maps.find_one({"mapKey": f"{ROOM}$current"}, {"_id": 0})
if old:
    db.map_history.insert_one({**old, "_backup": "mess-map", "_at": now})
db.maps.update_one({"mapKey": f"{ROOM}$current"},
    {"$set": {"mapKey": f"{ROOM}$current", "roomKey": ROOM, "saveData": save,
              "saveInternalStates": "U1BBTQMAAAAA", "mapDataVersion": 3, "liveMirror": 1}}, upsert=True)
db.rooms.update_one({"roomKey": ROOM},
    {"$set": {"roomName": "???", "dimensions": [250, 250, 250], "noSave": 0, "isSaveLocked": 1,
              "isModeLocked": 1, "isCreative": 0, "roomDataVersion": 3, "terrainDataVersion": 1,
              "lastValidSaveOffset": 0, "isOfficial": 0, "floorDepth": 0, "liveMirror": 1}})
print(f"pushed {len(blocks)} blocks to {ROOM}$current; room doc set (multiplayer, save-locked)")
