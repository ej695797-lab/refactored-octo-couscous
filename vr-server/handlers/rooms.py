"""
Room data handlers: g2_getRoomData / g2_fetchRoomData
"""

from services.db import rooms, room_maps
from services.property_defaults import default_for_property


async def handle_room_map_data(body: dict) -> dict:
    """
    fetchRoomMapData: returns the room-map graph (the list of connected room
    nodes with themes/grid positions/links) for a roomMapKey (e.g. freebuild_0).
    Seeded from the real Yeeps backend. The client needs this to build the hub;
    without it the room can't load ("no room is loaded"). Returns the full
    {"data": {uniqueID, data:[nodes], version}} envelope so the router passes it
    through unwrapped.
    """
    room_map_key = body.get("roomMapKey", "")
    doc = None
    if room_map_key:
        doc = await room_maps.find_one({"roomMapKey": room_map_key}, {"_id": 0})
    graph = (doc or {}).get("graph") or {"data": [], "version": 0, "uniqueID": room_map_key}
    return {"data": graph}


# Base defaults for the tutorial room
TUTORIAL_DEFAULTS = {
    "roomKey": "tutorial",
    "roomName": "Tutorial",
    "isOfficial": 1,
    "dimensions": [52, 20, 52],
    "floorDepth": 0,
    "lobbyDirection": 1,
    "door_up": {"block": 1, "position": [0, 0]},
    "door_right": {"block": 1, "position": [0, 0]},
    "door_down": {"block": 1, "position": [0, 0]},
    "door_left": {"block": 1, "position": [0, 0]},
    "isCreative": 0,
    "isModeLocked": 1,
    "isAutoCycling": 0,
    "autoCycleTimestamp": 0,
    "noSave": 1,
    "isDevLocked": 0,
    "lastEditedSessionID": 1,
    "roomDataVersion": 1,
    # Frequently requested optional room fields in older/newer clients
    "liquidType": 0,
    "liquidColor": "",
    "liquidLevel": 0,
    "itemRestrictionsIsWhiteList": 0,
    "itemRestrictionsGroupKeys": [],
    "minVersion": "1.0.0",
    "maxVersion": "",
    "versionKey": "teams1",
    "themeKey": "tutorial",
    "isVisible": 1,
}

# Overrides for non-tutorial rooms
NON_TUTORIAL_OVERRIDES = {
    "door_up": {"block": 0, "position": [0, 0]},
    "door_right": {"block": 0, "position": [0, 0]},
    "door_down": {"block": 0, "position": [0, 0]},
    "door_left": {"block": 0, "position": [0, 0]},
    "isCreative": 0,
    "isModeLocked": 0,
    "isAutoCycling": 0,
    "autoCycleTimestamp": 0,
    "noSave": 1,
    "isDevLocked": 0,
    "lastEditedSessionID": 1,
    "roomDataVersion": 1,
}

# Fallback payload for official_1 matching the older working traffic.
OFFICIAL_1_DEFAULTS = {
    "roomKey": "official_1",
    "roomName": "Tung Tung Sahur world",
    "themeKey": "suburb",
    "dimensions": [80, 80, 80],
    "floorDepth": 9,
    "lobbyDirection": 1,
    "door_up": {"block": 0, "position": [0, 0]},
    "door_right": {"block": 0, "position": [0, 0]},
    "door_down": {"block": 0, "position": [0, 0]},
    "door_left": {"block": 0, "position": [0, 0]},
    "isCreative": 0,
    "isModeLocked": 0,
    "isAutoCycling": 0,
    "autoCycleTimestamp": 0,
    "noSave": 0,
    "isDevLocked": 0,
    "lastEditedSessionID": 1,
    "roomDataVersion": 1,
}


async def handle_room_data(body: dict) -> dict:
    """
    Fetch room data for a given roomKey.
    Tries MongoDB first, falls back to tutorial/non-tutorial defaults.
    """
    room_key = body.get("roomKey", "")
    props = body.get("propertiesToGet", [])

    # Try to get room from DB
    room_doc = None
    if room_key:
        room_doc = await rooms.find_one({"roomKey": room_key}, {"_id": 0})

    # Build base values. Real room data is seeded into Mongo from the actual
    # Yeeps API (g2_getRoomData) - including distinct rooms like ch2_hub_creative
    # ("Build Central"), which is NOT a variant of ch2_hub ("Gnome Capital").
    # Defaults below are only a fallback for rooms not yet seeded.
    if not room_key or room_key == "tutorial":
        base = dict(TUTORIAL_DEFAULTS)
    elif room_key == "official_1":
        base = dict(OFFICIAL_1_DEFAULTS)
    else:
        base = dict(TUTORIAL_DEFAULTS)
        base.update(NON_TUTORIAL_OVERRIDES)
        base["roomName"] = room_key

    # Merge in real DB values (seeded from the real Yeeps API)
    if room_doc:
        base.update(room_doc)

    # TESTING: un-mode-lock every room so modes can be switched freely.
    # Toggle off with env UNLOCK_ALL_MODES=0 after testing.
    from config import UNLOCK_ALL_MODES
    if UNLOCK_ALL_MODES:
        base["isModeLocked"] = 0

    # Ensure requested properties are always present even if DB/defaults miss one.
    for p in props:
        if p not in base:
            base[p] = default_for_property(p)

    # Match legacy behavior: when propertiesToGet is provided, return only
    # those requested properties.
    if props:
        return {p: base[p] for p in props}
    return base


# 2.x deployment: standard hub/creative build-space size when a room's real data
# has no explicit dimensions (better than the tiny [52,20,52] tutorial default).
from config import IS_V2 as _IS_V2
if _IS_V2:
    NON_TUTORIAL_OVERRIDES.setdefault("dimensions", [128, 96, 128])
