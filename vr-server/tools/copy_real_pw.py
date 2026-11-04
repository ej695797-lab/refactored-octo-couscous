"""Copy a player's REAL live-Yeeps private world onto this server (owner 2026-10-18).

Real Yeeps lets only the OWNER read private-world blocks (other accounts get 403),
so this logs in with the owner's own Meta username + mobile code. The code is asked
for at the prompt (never stored, never in shell history). Every call to real Yeeps
is a read (services/yeeps_direct refuses anything that is not fetch*).

Usage (from vr-server/):
    python3 tools/copy_real_pw.py <metaUsername> [--dry]
    echo <code> | python3 tools/copy_real_pw.py <metaUsername>   (no hidden prompt)

Writes (old docs saved to live_world_backups first):
  rooms      (live DB)            every room of the world, real settings
  maps       (shared pw store)    <roomKey>$current + <roomKey>$terrain_current
  room_maps  (live DB)            the real world layout (liveMirror: 1)
A JSON copy of everything fetched is also saved under logs/pw_copies/.
"""
import asyncio
import getpass
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)


def _load_env():
    # same parsing as systemd EnvironmentFile (values may contain spaces)
    for line in open(os.path.expanduser("~/.config/yeeps/vr-server-240.env")):
        line = line.rstrip("\n")
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


_load_env()

from services import yeeps_direct  # noqa: E402
from tools.mirror_live_world import ROOM_PROPS  # noqa: E402

ACCT = "pw_owner_copy"      # in-memory login slot only; nothing is stored in globals


def add_entry_door(nodes, base):
    """Our 2.32 client walks into a pw through a "public" transition node right
    below the sandbox (services/dynamo_ops._sandbox_room_map). Real Yeeps layouts
    don't carry one, so without it there is no door to walk in from."""
    nodes = [dict(n) for n in nodes if not n.get("transitionKey")]
    sb = next((n for n in nodes if n.get("roomKey") == f"{base}_sandbox"), None)
    if not sb:
        return nodes
    x, y = sb.get("gridPosition") or [0, 0]
    taken = {tuple(n.get("gridPosition") or ()) for n in nodes}
    pos = [x, y - 1]
    if tuple(pos) in taken:
        print(f"  warning: grid {pos} below the sandbox is taken, door not added")
        return nodes
    return [{"themeKey": "sandbox", "gridPosition": pos, "transitionKey": "public",
             "blockLeft": 1, "blockRight": 1}] + nodes


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    meta = sys.argv[1].strip()
    dry = "--dry" in sys.argv
    # piped in (echo 5011 | ...) when the terminal can't take a hidden prompt
    code = (sys.stdin.readline() if not sys.stdin.isatty()
            else getpass.getpass(f"Mobile code for {meta}: ")).strip()
    if not (code.isdigit() and len(code) == 4):
        print("The mobile code is 4 digits.")
        return 1

    async def _creds(account):
        return (meta, code) if account == ACCT else (None, "")
    yeeps_direct._service_account = _creds

    s = await yeeps_direct._login(ACCT)
    aid = s["accountID"]
    base = f"p_{aid}"
    print(f"Logged in as {meta} -> {aid}; world {base}")

    from services.dynamo_ops import _cw_real_room_map
    rm = await _cw_real_room_map(base)
    if not rm or not isinstance(rm.get("data"), list) or not rm["data"]:
        print("Real Yeeps has no private world layout for this account.")
        return 1
    keys = [n["roomKey"] for n in rm["data"] if n.get("roomKey")]
    print(f"{len(keys)} rooms: {', '.join(keys)}")

    async def chunks(rk, sid, terrain):
        out, ver, internal = [], None, ""
        for off in range(60):
            d = await yeeps_direct.invoke("fetchMapData", {
                "roomKey": rk, "targetSessionID": sid, "saveOffset": off,
                "isTerrain": 1 if terrain else 0}, account=ACCT)
            sd = (d or {}).get("saveData")
            if not sd or (out and sd == out[-1]):
                break
            if ver is None:
                ver = d.get("mapDataVersion")
            if off == 0:
                internal = d.get("saveInternalStates") or ""
            out.append(sd)
        return out, ver, internal

    rooms, maps_ = {}, {}
    for rk in keys:
        rd = await yeeps_direct.invoke("fetchRoomData", {"roomKey": rk, "propertiesToGet": ROOM_PROPS},
                                       account=ACCT) or {}
        b, bv, internal = await chunks(rk, rd.get("lastEditedSessionID") or 0, False)
        t, tv, _ = await chunks(rk, rd.get("lastEditedTerrainSessionID") or 0, True)
        rooms[rk] = rd
        maps_[rk] = {"blocks": b, "blocksVersion": bv, "internalStates": internal,
                     "terrain": t, "terrainVersion": tv}
        print(f"  {rk:34} {str(rd.get('roomName')):18} blocks {sum(map(len, b)):8}b "
              f"({len(b)} chunk) terrain {sum(map(len, t)):7}b")
        await asyncio.sleep(0.3)

    os.makedirs("logs/pw_copies", exist_ok=True)
    snap = f"logs/pw_copies/{base}_{int(time.time())}.json"
    json.dump({"accountID": aid, "meta": meta, "roomMap": rm, "rooms": rooms, "maps": maps_},
              open(snap, "w"))
    print(f"Saved a copy of the real world to {snap}")
    if dry:
        print("--dry: nothing written to the server.")
        return 0

    from services.db import rooms as rooms_col, room_maps, db, maps_for
    backups = db["live_world_backups"]
    now = int(time.time())

    async def backup(kind, query, coll):
        old = await coll.find_one(query, {"_id": 0})
        if old:
            await backups.insert_one({"kind": kind, "query": query, "doc": old, "ts": now, "world": base})

    defaults = {"lastValidSaveOffset": 0, "roomDataVersion": 3, "terrainDataVersion": 1,
                "noSave": 0, "isDevLocked": 0, "isSaveLocked": 0, "isAutoCycling": 0,
                "autoCycleTimestamp": 0, "previousEditedSessionID": 1,
                "previousEditedTerrainSessionID": 1, "lastRoomDataEditorSessionID": 1,
                "blockMatchmakingSegmentation": 1, "liquidType": 0, "liquidLevel": 0,
                "liquidColor": "", "customLightingKey": "", "itemRestrictionsGroupKeys": [],
                "itemRestrictionsIsWhiteList": 0, "floorDepth": 0, "noPaste": 0,
                "overrideSaveLockTimestamp": "1970 01/01 00:00:00", "previousMapProperties": {},
                "creativeSessionID": 1, "isOfficial": 0, "isModeLocked": 0,
                "door_up": {"block": 0, "position": [0, 0]}, "door_down": {"block": 0, "position": [0, 0]},
                "door_left": {"block": 0, "position": [0, 0]}, "door_right": {"block": 0, "position": [0, 0]}}
    for rk, rd in rooms.items():
        if not rd:
            print(f"  room {rk}: real Yeeps returned no settings, skipped")
            continue
        doc = {k: v for k, v in rd.items() if v is not None}
        for k, v in defaults.items():
            doc.setdefault(k, v)
        doc.update(roomKey=rk, liveMirror=1, copiedFromRealAt=now)
        await backup("room", {"roomKey": rk}, rooms_col)
        await rooms_col.update_one({"roomKey": rk}, {"$set": doc}, upsert=True)

    for rk, mm in maps_.items():
        col = maps_for(rk)
        blocks = mm["blocks"][0] if mm["blocks"] else ""
        terrain = mm["terrain"][0] if mm["terrain"] else ""
        cur = {"mapKey": f"{rk}$current", "roomKey": rk,
               "saveData": blocks, "mapDataVersion": mm["blocksVersion"] or 3,
               "saveInternalStates": mm["internalStates"] or "",
               "terrainSaveData": terrain, "terrainDataVersion": mm["terrainVersion"] or 1,
               "saveChunks": list(mm["blocks"]), "terrainChunks": list(mm["terrain"]),
               "liveMirror": 1, "savedBy": "real_pw_copy", "ts": now}
        await backup("map", {"mapKey": cur["mapKey"]}, col)
        await col.update_one({"mapKey": cur["mapKey"]}, {"$set": cur}, upsert=True)
        tkey = f"{rk}$terrain_current"
        await backup("map", {"mapKey": tkey}, col)
        await col.update_one({"mapKey": tkey}, {"$set": {
            "mapKey": tkey, "roomKey": rk, "isTerrain": 1, "saveData": terrain,
            "terrainSaveData": terrain, "mapDataVersion": mm["terrainVersion"] or 1,
            "liveMirror": 1, "savedBy": "real_pw_copy", "ts": now}}, upsert=True)

    rm_doc = dict(rm)
    rm_doc["data"] = add_entry_door(rm["data"], base)
    rm_doc.update(versionKey=base, roomMapKey=base, liveMirror=1, copiedFromRealAt=now,
                  version=max(int(rm.get("version") or 1), 2) + 1)
    await backup("room_map", {"versionKey": base}, room_maps)
    await room_maps.update_one({"versionKey": base}, {"$set": rm_doc}, upsert=True)
    print(f"Done: {len(rooms)} rooms copied into {base}. The player rejoins their world to see it.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
