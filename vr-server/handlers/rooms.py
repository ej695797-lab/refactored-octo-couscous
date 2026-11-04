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
    # Community / private worlds: the live 2.31.4 client fetches the cw/pw room
    # map through THIS lambda (not just DynamoDB), so build the same grid the
    # Dynamo path serves (via _get_fallback_doc) and return it here. Without this
    # the client gets an empty node list -> "Failed to get room map node ...
    # NO_NODE_FOUND" and can't enter the world.
    if room_map_key.startswith(("c_", "p_", "o_")):
        try:
            from services.dynamo_ops import _get_fallback_doc
            cwdoc = await _get_fallback_doc("G2_RoomMap", {"versionKey": room_map_key})
            if cwdoc and isinstance(cwdoc.get("data"), list):
                # Pass the real uniqueID through (GetItem serves it too, so the
                # REST and DynamoDB paths agree on it as well as on version).
                inner = {"data": cwdoc["data"],
                         "version": int(cwdoc.get("version", 1) or 1),
                         "uniqueID": cwdoc.get("uniqueID") or room_map_key}
                # The live 2.31.4 client fetches the CW room map through THIS
                # lambda and its propertiesToGet requests a set of cw_* attrs
                # (cw_initialized, cw_isLocked, cw_category, ...). Per the CW entry
                # gate, if ANY requested cw_* attr is missing from the RESPONSE the
                # client discards the whole item -> the nodes never register in
                # RoomDatabase -> "Failed to get room map node ... NO_DATA_FOUND".
                # Echo every requested cw_* from the mirrored doc; default any the
                # real item doesn't carry (e.g. cw_isOfficial/cw_isLocked) to 0 so
                # it comes back non-null rather than dropped.
                props = body.get("propertiesToGet") or []
                cw_props = [k for k in props if isinstance(k, str) and k.startswith("cw_")]
                if cw_props:
                    for k in cw_props:
                        inner[k] = cwdoc.get(k, 0)
                else:
                    for k, v in cwdoc.items():
                        if k.startswith("cw_"):
                            inner[k] = v
                return {"data": inner}
        except Exception as e:
            print(f"[rooms] cw/pw room map build failed for {room_map_key}: {e}")
    # REAL-YEEPS passthrough (playyeeps line): live/play clients get the real
    # live room map from Yeeps' AWS (guest-cred read of G2_RoomMap), so this REST
    # path agrees with the DynamoDB GetItem path (both return the real map/
    # version) and the player loads the real world instead of our beta4 one.
    from services import big_patterns as _bp
    if room_map_key and not room_map_key.startswith(("p_", "c_", "o_")) \
            and _bp.is_play(body.get("_clientBuildTag")):
        from services import real_yeeps
        real = await real_yeeps.get_item_plain("G2_RoomMap", {"versionKey": room_map_key})
        if real and isinstance(real.get("data"), list):
            return {"data": {"data": real["data"],
                             "version": int(real.get("version", 1) or 1),
                             "uniqueID": room_map_key}}
    doc = None
    if room_map_key:
        doc = await room_maps.find_one({"roomMapKey": room_map_key}, {"_id": 0})
    graph = (doc or {}).get("graph")
    # A mirrored REAL live-Yeeps world (tools/import_live_world.py) stores its
    # node list under "data" with liveMirror:1 -- serve it directly as the graph.
    if not graph and doc and doc.get("liveMirror") and isinstance(doc.get("data"), list):
        graph = {"data": doc["data"], "version": doc.get("version", 1),
                 "uniqueID": room_map_key}
        print(f"[rooms] served live-mirror map ({len(doc['data'])} nodes) for '{room_map_key}'")
    # Big / live-base clients spawn into a seasonal hub map (e.g. halloween26_1)
    # that isn't in our DB, so the lookup above returns nothing and the client
    # gets an empty node list -> "Failed to get room map node for room
    # official_1: NO_NODE_FOUND". Serve our big_world map (which has the
    # official_1 Starter House spawn node) for any hub map a big client asks for
    # that we don't otherwise have.
    # A hub map we don't have (e.g. the live client's seasonal halloween26_1)
    # falls back to our big_world map, which has the official_1 Starter House
    # spawn node. Private/community worlds (p_/c_/o_) have their own handling and
    # are left alone. Not gated on the build tag: lambda calls don't carry it,
    # and only live-base clients ever request these hub keys (stock clients use
    # freebuild_0, which also maps to big_world here -- correct for big clients).
    # The Time-Machine room (fever_dream_1) is a STANDALONE room reached via the
    # time-machine rotation, not a node in any hub map. When the client is inside
    # it and asks for ITS own room map it has no grid -> it loops "Fetching room
    # map for room fever_dream_1" forever. Serve a dedicated grid: the fever node
    # + a "public" transition doorway so the player can leave back to the hub.
    if not graph and room_map_key == "fever_dream_1":
        graph = {"version": 1, "uniqueID": room_map_key, "data": [
            {"themeKey": "backrooms", "gridPosition": [0, 0],
             "transitionKey": "public", "blockLeft": 1, "blockRight": 1},
            {"roomKey": "fever_dream_1", "themeKey": "backrooms",
             "gridPosition": [0, 1], "displayType": 1, "blockAll": 1,
             "blockLeft": 1, "blockRight": 1, "blockUp": 1},
        ]}
        print("[rooms] served fever_dream_1 standalone room map")
    if not graph and room_map_key and not room_map_key.startswith(("p_", "c_", "o_")):
        try:
            from services import big_world
            bw = await big_world.room_map_doc(room_map_key)
            if bw and bw.get("data"):
                graph = {"data": bw["data"], "version": bw.get("version", 1),
                         "uniqueID": room_map_key}
                print(f"[rooms] served big_world map ({len(bw['data'])} nodes) for hub '{room_map_key}'")
        except Exception as e:
            print(f"[rooms] big_world map fallback failed for {room_map_key}: {e}")
    if not graph:
        graph = {"data": [], "version": 0, "uniqueID": room_map_key}
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
    # Match the real 1.31 Starter House room doc (Yeeps119/Yeeps240 official_1)
    # so the layout -- door placement especially -- is identical to 1.31. These
    # are the fallback values; the live DB doc normally overrides them.
    "roomName": "Starter House",
    "themeKey": "suburb",
    "dimensions": [80, 80, 80],
    "floorDepth": 0,
    "lobbyDirection": 1,
    "door_up": {"block": 0, "position": [26, 0]},
    "door_right": {"block": 0, "position": [0, 0]},
    "door_down": {"block": 0, "position": [-5, 8]},
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

    # REAL-YEEPS passthrough (playyeeps line): live/play clients get the real
    # live room from Yeeps' AWS (guest-cred read of G2_Rooms) -- the REAL nexus,
    # ch2_hub, etc. (no beta4 official_1 aliasing). Personal/community rooms
    # (p_/o_/c_) and anything real Yeeps lacks fall through to local handling.
    from services import big_patterns as _bprt
    if room_key and not room_key.startswith(("p_", "c_", "o_")) \
            and _bprt.is_play(body.get("_clientBuildTag")):
        from services import real_yeeps
        _real = await real_yeeps.get_item_plain("G2_Rooms", {"roomKey": room_key})
        if _real:
            _real["roomKey"] = room_key
            from config import UNLOCK_ALL_MODES as _UAM
            if _UAM:
                _real["isModeLocked"] = 0
            if props:
                _out = {p: _real.get(p, default_for_property(p)) for p in props}
                # live client NREs on ""/null typed fields -> drop them so it
                # uses its own defaults (same as the local live-base path).
                return {p: v for p, v in _out.items() if v not in ("", None)}
            return _real

    # Community (c_) / private (p_, o_) worlds: serve the mirrored full room doc
    # (_get_fallback_doc builds the 37-field cw/pw room with the right dims,
    # floorDepth, lastValidSaveOffset). The generic path below returns a
    # "tutorial" default for these, which gives the client wrong/garbage room
    # data and breaks CW/PW entry.
    # Community worlds: TRUE verbatim real-Yeeps relay. Return exactly the keys
    # real Yeeps returns for the requested props -- NO default injection, NO added
    # fields, NO dropped fields (keep previousMapProperties + real
    # previousEditedSessionID). Any reconstruction (our old 35-key doc vs real's
    # 14) fails the client's RoomDatabaseEntry validation -> stuck at "Fetching
    # room database entry", never fetches blocks.
    if room_key.startswith("c_"):
        from services.dynamo_ops import _cw_fetch_room_doc
        real = await _cw_fetch_room_doc(room_key)
        if real:
            if props:
                return {p: real[p] for p in props if p in real}
            return dict(real)
        # fall through to the reconstructed fallback only if the live fetch fails
    if room_key.startswith(("c_", "p_", "o_")):
        from services.dynamo_ops import _get_fallback_doc
        _cwdoc = await _get_fallback_doc("G2_Rooms", {"roomKey": room_key})
        if _cwdoc:
            _cwdoc = dict(_cwdoc)
            _cwdoc["roomKey"] = room_key
            if props:
                _out = {p: _cwdoc.get(p, default_for_property(p)) for p in props}
                return {p: v for p, v in _out.items() if v not in ("", None)}
            return _cwdoc

    # Try to get room from DB. The live 2.31.4 client's hardcoded default spawn
    # is "nexus", which has no room doc of its own -- look up the real official_1
    # Starter House doc instead so its doors / floorDepth / dimensions (hence the
    # whole layout) match 1.31 exactly. The requested key (nexus) is restored on
    # the response below so the client still sees the room it asked for.
    # When the REAL nexus room has been mirrored in (liveMirror), it is served
    # as itself; the Starter House alias is only the fallback for a missing one.
    room_doc = None
    if room_key:
        room_doc = await rooms.find_one({"roomKey": room_key}, {"_id": 0})
    _nexus_fallback = (room_key == "nexus" and not room_doc)
    if _nexus_fallback:
        room_doc = await rooms.find_one({"roomKey": "official_1"}, {"_id": 0})

    # Build base values. Real room data is seeded into Mongo from the actual
    # Yeeps API (g2_getRoomData) - including distinct rooms like ch2_hub_creative
    # ("Build Central"), which is NOT a variant of ch2_hub ("Gnome Capital").
    # Defaults below are only a fallback for rooms not yet seeded.
    if not room_key or room_key == "tutorial":
        base = dict(TUTORIAL_DEFAULTS)
    elif room_key == "official_1" or _nexus_fallback:
        # nexus is the live 2.31.4 client's default spawn; without a real nexus
        # doc, serve it the Starter House room data so it's a valid room.
        base = dict(OFFICIAL_1_DEFAULTS)
    else:
        base = dict(TUTORIAL_DEFAULTS)
        base.update(NON_TUTORIAL_OVERRIDES)
        base["roomName"] = room_key

    # Merge in real DB values (seeded from the real Yeeps API)
    if room_doc:
        base.update(room_doc)
        # Keep the room key the client actually asked for (e.g. "nexus" maps to
        # the official_1 doc above, but the client requested nexus).
        base["roomKey"] = room_key

    # TESTING: un-mode-lock every room so modes can be switched freely.
    # Toggle off with env UNLOCK_ALL_MODES=0 after testing.
    from config import UNLOCK_ALL_MODES
    if UNLOCK_ALL_MODES:
        base["isModeLocked"] = 0
    elif room_key and not room_key.startswith(("p_", "c_", "o_")):
        # shared maps stay mode-locked except for map builders (creative_builders)
        from services import creative_builders as _cb
        base["isModeLocked"] = 0 if await _cb.is_builder(body.get("accountID", ""), body.get("_deviceKey", "")) else 1

    # Ensure requested properties are always present even if DB/defaults miss one.
    for p in props:
        if p not in base:
            base[p] = default_for_property(p)

    # Match legacy behavior: when propertiesToGet is provided, return only
    # those requested properties.
    if props:
        out = {p: base[p] for p in props}
        # Live 2.31.4 base: several newer room fields are object/typed on the
        # client (teamItemRestrictions, overrideSpawnPosition, gravityType, ...).
        # We store them as null and serve "" for a missing value, but the live
        # client deserializes "" into a null object and throws a
        # NullReferenceException right after "Fetched room database entry",
        # failing the room load. Drop empty/null props so it uses its own
        # defaults instead (same idea as the G2_Rooms empty-"S" strip on the
        # Dynamo path). Structured fields that DO have a value are kept.
        from services import big_patterns as _bp
        if _bp.is_livebase(body.get("_clientBuildTag")):
            out = {p: v for p, v in out.items() if v not in ("", None)}
        return out
    return base


# 2.x deployment: standard hub/creative build-space size when a room's real data
# has no explicit dimensions (better than the tiny [52,20,52] tutorial default).
from config import IS_V2 as _IS_V2
if _IS_V2:
    NON_TUTORIAL_OVERRIDES.setdefault("dimensions", [128, 96, 128])


async def handle_fetch_room_map_extra_properties(body: dict) -> dict:
    """
    g2_fetchRoomMapExtraProperties: the client loads per-room properties for
    every room in a room map at once. The REAL Yeeps backend returns, verbatim:
        {"data": {"responses": {"<roomKey>": {roomKey, roomName, isOfficial,
                                              themeKey, ...}, ...}}}
    i.e. a dict keyed by roomKey, with PLAIN JSON values (NOT DynamoDB-typed,
    NOT a list). Any other shape makes the live client log "Room Map: Could not
    get supplemental data" and mark every warp portal "not linked". Matched to a
    live capture (real_yeeps.invoke_lambda) on 2026-10-13.
    """
    props = list(body.get("propertiesToGet") or [])
    if props and "roomKey" not in props:
        props.append("roomKey")
    keys = [k for k in (body.get("roomKeys") or []) if isinstance(k, str) and k]
    # 2026-10-31: was one room at a time (~100 rooms -> ~10 s per call, part of the
    # slow loading). Now 10 at a time; same per-room handler and output.
    import asyncio as _aio
    _sem = _aio.Semaphore(10)

    async def _one(key):
        async with _sem:
            try:
                item = await handle_room_data({"roomKey": key, "propertiesToGet": props})
            except Exception as e:
                print(f"[extraprops] {key} failed: {type(e).__name__}: {str(e)[:80]}")
                item = {}
            return key, {k: v for k, v in (item or {}).items() if v not in ("", None)}

    responses = {}
    for key, val in await _aio.gather(*(_one(k) for k in keys)):
        responses[key] = val
    # Our "timeMachine" node serves custom content; the live backend omits it,
    # but we include it so its portal resolves.
    from handlers.challenges import TIME_MACHINE_ROOM_KEY
    if TIME_MACHINE_ROOM_KEY in keys and "timeMachine" not in responses:
        tm = await handle_room_data({"roomKey": TIME_MACHINE_ROOM_KEY, "propertiesToGet": props})
        tm = {k: v for k, v in tm.items() if v not in ("", None)}
        tm["roomKey"] = "timeMachine"
        responses["timeMachine"] = tm
    return {"data": {"responses": responses}}
