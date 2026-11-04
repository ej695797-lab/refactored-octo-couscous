"""Import a mirrored live-Yeeps world (tools/mirror_live_world.py output) into
this server's DB so live-base (biglive*) clients get the real rooms.

Usage:
    python3 tools/import_live_world.py <mirror.json> [--dry] [--rooms-only]

--rooms-only: write rooms + maps only and leave the stored room_map (world
layout) alone -- for adding rooms that are already nodes of the world.

Writes (all upserts, old docs are saved to live_world_backups first):
  rooms         one doc per kept room, exactly the real room settings
  maps          <roomKey>$current          blocks blob + terrain blob (liveMirror: 1)
                <roomKey>$terrain_current  terrain blob for the Dynamo terrain read
  room_maps     versionKey = the world key, trimmed to the kept rooms, with every
                passagewayLink that points outside the kept set removed
                (liveMirror: 1 -> services/big_world.py serves it as-is)
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, ".")


async def main():
    path = sys.argv[1]
    dry = "--dry" in sys.argv
    m = json.load(open(path))
    from services.db import rooms, maps, room_maps, db
    backups = db["live_world_backups"]

    keep = set(m["rooms"].keys())
    world_key = m["roomMapKey"]
    now = int(time.time())

    async def backup(kind, query, coll):
        old = await coll.find_one(query, {"_id": 0})
        if old and not dry:
            await backups.insert_one({"kind": kind, "query": query, "doc": old, "ts": now, "world": world_key})
        return bool(old)

    # ---- rooms ----
    n_rooms = 0
    for rk, doc in m["rooms"].items():
        doc = dict(doc)
        if not doc:
            print(f"  room {rk}: real Yeeps has no room doc (left as is)")
            continue
        doc["roomKey"] = rk
        doc["liveMirror"] = 1
        # The live client reads lastValidSaveOffset to know how many map chunks
        # to fetch; without it (real Yeeps doesn't always return it) the client
        # fetches NO blocks and the room loads empty. Fill it and the other
        # structural fields a normal room has, where the real doc omitted them.
        _defaults = {"lastValidSaveOffset": 0, "roomDataVersion": 3, "terrainDataVersion": 1,
                     "noSave": 0, "isDevLocked": 0, "isSaveLocked": 0, "isAutoCycling": 0,
                     "autoCycleTimestamp": 0, "previousEditedSessionID": 1,
                     "previousEditedTerrainSessionID": 1, "lastRoomDataEditorSessionID": 1,
                     "blockMatchmakingSegmentation": 1, "liquidType": 0, "liquidLevel": 0,
                     "liquidColor": "", "itemRestrictionsGroupKeys": [], "itemRestrictionsIsWhiteList": 0,
                     "door_up": {"block": 0, "position": [0, 0]}, "door_down": {"block": 0, "position": [0, 0]},
                     "door_left": {"block": 0, "position": [0, 0]}, "door_right": {"block": 0, "position": [0, 0]}}
        for _k, _v in _defaults.items():
            doc.setdefault(_k, _v)
        # MODE LOCK (anti-grief): people set official maps to creative and grief.
        # Force every non-private, non-Wilds room mode-locked and non-creative so
        # the game's own switch-to-creative UI is blocked (the APK creative guard
        # covers the runtime Photon prop). Creative stays ON only for private
        # worlds (p_*), the Wilds (wild*), and the Creative Classroom (official_4b).
        if not (rk.startswith(("p_", "wild")) or rk == "official_4b"):
            doc["isModeLocked"] = 1
            doc["isCreative"] = 0
        had = await backup("room", {"roomKey": rk}, rooms)
        if not dry:
            await rooms.update_one({"roomKey": rk}, {"$set": doc}, upsert=True)
        n_rooms += 1
        print(f"  room {rk:28} {str(doc.get('roomName')):26} {'(replaced)' if had else '(new)'}")

    # ---- maps ----
    n_maps = 0
    for rk, mm in m["maps"].items():
        blocks = (mm.get("blocks") or [""])[0] if mm.get("blocks") else ""
        terrain = (mm.get("terrain") or [""])[0] if mm.get("terrain") else ""
        cur = {
            "mapKey": f"{rk}$current", "roomKey": rk,
            "saveData": blocks, "mapDataVersion": mm.get("blocksVersion") or 3,
            "saveInternalStates": mm.get("internalStates") or "",
            "terrainSaveData": terrain, "terrainDataVersion": mm.get("terrainVersion") or 1,
            "liveMirror": 1, "savedBy": "live_mirror", "ts": now, "sourceWorld": world_key,
            # every real chunk (handlers/maps.py serves offsets 1.. from these)
            "saveChunks": list(mm.get("blocks") or []), "terrainChunks": list(mm.get("terrain") or []),
        }
        had = await backup("map", {"mapKey": cur["mapKey"]}, maps)
        if not dry:
            await maps.update_one({"mapKey": cur["mapKey"]}, {"$set": cur}, upsert=True)
            await maps.update_one({"mapKey": f"{rk}$terrain_current"}, {"$set": {
                "mapKey": f"{rk}$terrain_current", "roomKey": rk, "isTerrain": 1,
                "saveData": terrain, "terrainSaveData": terrain,
                "mapDataVersion": mm.get("terrainVersion") or 1,
                "liveMirror": 1, "savedBy": "live_mirror", "ts": now,
            }}, upsert=True)
        n_maps += 1
        print(f"  map  {rk:28} blocks {len(blocks):7}b  terrain {len(terrain):6}b {'(replaced)' if had else '(new)'}")

    if "--rooms-only" in sys.argv:
        print(f"{'DRY RUN - nothing written' if dry else 'done'}: {n_rooms} rooms, {n_maps} maps (room_map untouched)")
        return
    # ---- room map ----
    nodes = []
    dropped_links = 0
    for n in m["nodes"]:
        if n.get("roomKey") not in keep:
            continue
        n = dict(n)
        links = []
        for l in n.get("passagewayLinks") or []:
            if l.get("targetRoomKey") in keep:
                # the live source omits linkType on many links; the 2.31.4 client
                # treats a null linkType as an unlinked (dead) portal, so default
                # it to 0 (standard warp) to keep every kept portal walkable.
                if l.get("linkType") is None:
                    l = dict(l); l["linkType"] = 0
                links.append(l)
            else:
                dropped_links += 1
        n["passagewayLinks"] = links
        if n.get("lobbyWarpPortalRoomKey") and n["lobbyWarpPortalRoomKey"] not in keep:
            n.pop("lobbyWarpPortalRoomKey", None)
            n.pop("lobbyWarpPortalColorKey", None)
        nodes.append(n)
    rm_doc = {
        "versionKey": world_key, "roomMapKey": world_key, "uniqueID": world_key,
        "version": 1000 + int(m.get("roomMapVersion") or 1),
        "data": nodes, "liveMirror": 1, "homeRoom": "nexus",
        "fetchedAt": m.get("fetchedAt"), "importedAt": now,
    }
    had = await backup("room_map", {"versionKey": world_key}, room_maps)
    if not dry:
        await room_maps.update_one({"versionKey": world_key}, {"$set": rm_doc}, upsert=True)
    print(f"  room_map {world_key}: {len(nodes)} nodes kept of {len(m['nodes'])}, "
          f"{dropped_links} links to removed rooms dropped, version {rm_doc['version']} {'(replaced)' if had else '(new)'}")
    print(f"{'DRY RUN - nothing written' if dry else 'done'}: {n_rooms} rooms, {n_maps} maps")


asyncio.run(main())
