"""The big APK's world = the 1.31.0 world.

2.4.0 always loads the room map "freebuild_0" (the ch2_* world: Gnome Capital
hub, ...). Big APK clients (build tag "big*") are sent the 1.31.0 world map
instead - the "beta4" room map read live from the 1.31 database (CARRYOVER_DB,
read-only) - under that same key, so its layout always matches 1.31.0
(tutorial, Playground, Starter House, Blue House, ..., Wilds, Build-A-Map).
Its version is offset so clients never reuse a cached ch2 map.

Big clients whose spawn room isn't in the 1.31 world start in 1.31's default
(official_1, Starter House). Stock 2.4.0 clients are not changed.
"""
import os
import time

MAIN_KEY = "freebuild_0"
SOURCE_KEY = "beta4"
VERSION_BASE = 1310
BIG_SPAWN = "official_1"
CARRYOVER_DB = os.getenv("CARRYOVER_DB", "")

_cache = {"ts": 0.0, "doc": None, "big_rooms": set()}


async def _refresh():
    if time.time() - _cache["ts"] < 30:
        return
    from services.db import client, room_maps
    src = None
    if CARRYOVER_DB:
        src = await client[CARRYOVER_DB]["room_maps"].find_one({"versionKey": SOURCE_KEY}, {"_id": 0})
    if not src:   # 1.31 DB unavailable: the copy of the same world kept in this DB
        src = await room_maps.find_one({"versionKey": SOURCE_KEY}, {"_id": 0})
    doc = None
    if src and isinstance(src.get("data"), list):
        try:
            v = int(src.get("version") or 1)
        except (TypeError, ValueError):
            v = 1
        # Base doc = the pure 1.31 world (the beta4 node grid, all connected).
        # Stock / big51 (2.4-base) clients spawn into official_1, which is a real
        # node here, so they get this untouched. The live 2.31.4 spawn ("nexus")
        # is handled by _live_spawn_data() per requested map key below.
        data = list(src["data"])
        doc = {"versionKey": MAIN_KEY, "roomMapKey": MAIN_KEY, "version": VERSION_BASE + v, "data": data}
    _cache.update(
        ts=time.time(), doc=doc,
        big_rooms={n.get("roomKey") for n in (doc or {}).get("data", []) if isinstance(n, dict) and n.get("roomKey")})


def _live_spawn_data(data):
    """Transform the world grid for a live 2.31.4 client.

    The live client's hardcoded default spawn room is "nexus" (its seasonal
    spawn halloween2026Spawn isn't a node here, so it falls back to nexus). If
    nexus is a lone node off to the side, the Starter House has no neighbours and
    its doors lead nowhere. Instead, make nexus *be* the official_1 node: rename
    official_1 -> nexus in place so nexus keeps official_1's grid cell [1,-2] and
    all of its connections to the rest of the world (official_0, tutorial, the
    wilds, ...). The room data for nexus is still served as the Starter House
    (handlers/rooms.py aliases nexus -> official_1). Any stray standalone nexus
    node is dropped. Stock / big51 clients never get this (they request the
    freebuild_0 map; this runs only for other hub keys)."""
    out = []
    renamed = False
    for n in data:
        if not isinstance(n, dict):
            out.append(n)
            continue
        rk = n.get("roomKey")
        if rk == "nexus":
            continue  # drop any lone nexus node; the renamed official_1 is nexus
        m = dict(n)
        if rk == "official_1" and not renamed:
            m["roomKey"] = "nexus"
            renamed = True
        out.append(m)
    if not renamed:
        # official_1 wasn't present for some reason: fall back to a lone nexus so
        # the live client still has somewhere to spawn.
        out.append({"roomKey": "nexus", "themeKey": "suburb", "gridPosition": [1, -2]})
    # NOTE: just naming nodes "ch2_hub"/"ch2_hub_creative" did NOT clear the
    # client's "no builder hub or main hub" nav warning (tested 2026-10-12). The
    # 2.31.4 hallway nav/signs + cw/pw portal arrows need the 2.x passagewayLinks
    # format on nodes (placedPosition/targetRoomKey/facing/material), which the
    # 1.31 world lacks. That's a room-map format conversion, handled separately.
    return out


_mirror_cache = {}   # requested_key -> (expires, doc or None)


async def _newest_mirror():
    hit = _mirror_cache.get("__newest__")
    if hit and hit[0] > time.time():
        return hit[1]
    from services.db import room_maps
    doc = await room_maps.find_one({"liveMirror": 1, "data.roomKey": "nexus"}, {"_id": 0},
                                   sort=[("importedAt", -1)])
    _mirror_cache["__newest__"] = (time.time() + 30, doc)
    return doc


async def _mirror_doc(key: str):
    """The stored liveMirror room map for a key (None if there isn't one).
    Cached 30s: the client fires hundreds of version GetItems per room load."""
    hit = _mirror_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    from services.db import room_maps
    doc = await room_maps.find_one({"versionKey": key, "liveMirror": 1}, {"_id": 0})
    _mirror_cache[key] = (time.time() + 30, doc)
    return doc


async def mirror_rooms() -> set:
    """Room keys of every stored liveMirror world."""
    from services.db import room_maps
    out = set()
    async for d in room_maps.find({"liveMirror": 1}, {"_id": 0, "data": 1}):
        out |= {n.get("roomKey") for n in d.get("data", []) if isinstance(n, dict) and n.get("roomKey")}
    return out


async def room_map_doc(requested_key=None):
    """The 1.31 world map, keyed as 2.4's main room map (None if unavailable).

    requested_key: the map key the client actually asked for. freebuild_0 (or
    None) returns the untouched grid for stock/big51 clients. Any other hub key
    (the live 2.31.4 seasonal maps, e.g. halloween26_1) gets the live-spawn
    transform so "nexus" is the connected Starter House, and the doc is re-keyed
    to the requested key so the client's version/data reads stay consistent."""
    # A mirrored REAL live-Yeeps world (tools/mirror_live_world.py +
    # tools/import_live_world.py) is stored in room_maps under its real key with
    # liveMirror: 1 and already has the 2.x node format (passagewayLinks etc.),
    # so it is served exactly as stored -- no 1.31 transform.
    if requested_key and requested_key != MAIN_KEY:
        mirror = await _mirror_doc(requested_key)
        if mirror:
            return mirror
        # A new seasonal world key we haven't mirrored (2.32 asked for
        # "trickOrTreat_0" while we only had halloween26_1, and got the old 1.31
        # layout with most maps missing): serve the newest live world, re-keyed.
        if not requested_key.startswith(("p_", "c_", "o_")) and requested_key != "public":
            newest = await _newest_mirror()
            if newest:
                return dict(newest, versionKey=requested_key, roomMapKey=requested_key,
                            uniqueID=requested_key)
    await _refresh()
    doc = _cache["doc"]
    if doc and requested_key and requested_key != MAIN_KEY:
        return {
            "versionKey": requested_key,
            "roomMapKey": requested_key,
            "uniqueID": requested_key,
            "version": doc["version"],
            "data": _live_spawn_data(doc["data"]),
        }
    return doc


def _personal(room_key: str) -> bool:
    return room_key.startswith(("p_o_", "o_", "c_"))


async def big_spawn_room(room_key: str) -> str:
    """Spawn room for a big client: unchanged if it is in the 1.31 world (or personal)."""
    room_key = str(room_key or "")
    if _personal(room_key):
        return room_key
    await _refresh()
    if _cache["big_rooms"] and room_key not in _cache["big_rooms"]:
        return BIG_SPAWN
    return room_key
