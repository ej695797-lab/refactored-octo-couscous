"""
DynamoDB operation handlers.
Translates DynamoDB API calls into MongoDB operations.
"""

import asyncio
import base64
import json
import time
import zlib
import uuid
from pathlib import Path

import httpx

# The client deserializes saveData with .NET's BinaryFormatter directly into
# a "MapSyncData" object with one field, "MapObjectSyncData[] mapObjects".
# This is a real BinaryFormatter stream for a MapSyncData with an empty
# mapObjects array, representing "0 blocks".
EMPTY_MAP_SAVE_DATA = (
    "AAEAAAD/////AQAAAAAAAAAMAgAAAEZBc3NlbWJseS1DU2hhcnAsIFZlcnNpb249MC4wLjAuMCwgQ3VsdHVyZT1uZXV0"
    "cmFsLCBQdWJsaWNLZXlUb2tlbj1udWxsBQEAAAALTWFwU3luY0RhdGEBAAAACm1hcE9iamVjdHMEE01hcE9iamVjdFN5"
    "bmNEYXRhW10CAAAAAgAAAAkDAAAABwMAAAAAAQAAAAAAAAAEEU1hcE9iamVjdFN5bmNEYXRhAgAAAAs="
)

# Empty internal-states blob as the 1.31 client stores it with a SPAM map ("SPAM", v3, 0 states).
EMPTY_SPAM_STATES = "U1BBTQMAAAAA"

from services.db import get_collection, get_key_field
from services.dynamo_types import unwrap_key, unwrap_attribute_updates, wrap_item, from_dynamo, to_dynamo
from services.property_defaults import default_for_property
from services.mapsync_serializer import serialize_blocks
from config import YEEPS_PROXY_URL, REQUIRED_CLIENT_VERSION


# ── Live community-world mirroring ──────────────────────────────────────────
# Community worlds (c_<name>) are pulled LIVE from real Yeeps on entry: the
# server logs in with a configured service account (globals "cw_fetch_account")
# and calls the yeeps-proxy relay's /rooms (grid) and /map (blocks) endpoints,
# converts the 2.x SPAM blocks to the 1.19 BinaryFormatter format, and serves
# them read-only. A short TTL cache only dedups the burst of identical GetItem
# calls the client fires during a single room load (so one entry = one real
# fetch, not dozens) -- it is not a persistent cache.
_CW_TTL = 600   # Yeeps locks an account out of a call when it is used too often; keep 10 min in memory
_cw_cache: dict = {}
# The 1.31 client's own block list (apk-work/131-notes). Community-world blocks outside it
# (2.x-only blocks, logic blocks) are not served: the 1.31 client has no mesh for them.
_V131_KEYS = frozenset(json.loads((Path(__file__).parent.parent / "services" / "data"
                                   / "mapobject_keys_131.json").read_text(encoding="utf-8")))
# World info per community world (owner, description), filled from the relay's /rooms.
_CW_WORLD: dict = {}
# Display names of community-world owners who never played here (fetchAccountDatas).
CW_OWNER_NAMES: dict = {}
# Served room key -> real room key, for rooms whose real keys the client can't parse.
_CW_ALIAS: dict = {}


def _cw_cache_get(k):
    v = _cw_cache.get(k)
    if v and v[0] > time.time():
        return v[1]
    return None


def _cw_cache_put(k, val, ttl=None):
    _cw_cache[k] = (time.time() + (ttl or _CW_TTL), val)


async def _cw_account(key: str = "cw_fetch_account"):
    """(oculusID, mobileCode) of a service account used to fetch real cw data, read from
    globals (never the repo). 'cw_grid_account' reads room maps, 'cw_fetch_account' the rest."""
    try:
        from services.db import globals_col
        acc = await globals_col.find_one({"key": key}, {"_id": 0, "value": 1})
        v = (acc or {}).get("value") or {}
        if v.get("oculusID") and v.get("mobileCode"):
            return v["oculusID"], str(v["mobileCode"])
    except Exception as e:
        print(f"[cw] account load failed: {e}")
    return None, None


# Real G2_Rooms fields of a cw room served as-is. (customLightingKey is left out: a 2.x
# lighting key the 1.31 client does not have could break the room.)
_CW_ROOM_FIELDS = ["lobbyDirection", "liquidType", "liquidColor", "liquidLevel",
                   "itemRestrictionsIsWhiteList", "itemRestrictionsGroupKeys",
                   "door_up", "door_right", "door_down", "door_left"]


def _parse_spam_states(raw: bytes) -> list:
    """Internal states as Yeeps and the 1.31 client store them: "SPAM", u8 3, i32 count, then
    per entry i32 x, y, z (the block's position) + a .NET string (sign text "<owner>|<text>",
    dispenser item, tape, ...). Keyed by position, so dropping other blocks never shifts them."""
    import struct
    if raw[:4] != b"SPAM" or len(raw) < 9:
        return []
    out, p = [], 9
    try:
        for _ in range(struct.unpack_from("<i", raw, 5)[0]):
            x, y, z = struct.unpack_from("<iii", raw, p); p += 12
            n = shift = 0
            while True:
                byte = raw[p]; p += 1
                n |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            out.append(((x, y, z), raw[p:p + n].decode("utf-8", "replace"))); p += n
    except Exception as e:
        print(f"[cw] internal states unreadable after {len(out)} entries: {e}")
    return out


def _build_spam_states(entries) -> str:
    import struct
    buf = bytearray(b"SPAM") + bytes([3]) + struct.pack("<i", len(entries))
    for (x, y, z), text in entries:
        buf += struct.pack("<iii", x, y, z)
        data = text.encode("utf-8")
        n = len(data)
        while True:
            seven = n & 0x7F
            n >>= 7
            buf.append(seven | (0x80 if n else 0))
            if not n:
                break
        buf += data
    return base64.b64encode(bytes(buf)).decode("ascii")


async def _cw_fetch_room(room_key: str):
    """One cw room's real content straight from Yeeps (fetchRoomData + the fetchMapData
    chunks, decoded here) -> {dimensions, floorDepth, saveData, blockCount} or None.
    Kept in memory only (10 min), never in the database."""
    ck = f"room:{room_key}"
    hit = _cw_cache_get(ck)
    if hit is not None:
        return hit or None
    from services.yeeps_direct import invoke
    from services.mapsync_decoder import _decode_spam
    try:
        info = await invoke("fetchRoomData", {"roomKey": room_key,
                            "propertiesToGet": ["lastEditedSessionID", "dimensions", "floorDepth"] + _CW_ROOM_FIELDS})
        sid = info.get("lastEditedSessionID") or 0
        seen, all_blocks, states = set(), [], {}
        for offset in range(50):
            d = await invoke("fetchMapData", {"roomKey": room_key, "targetSessionID": sid,
                                              "saveOffset": offset, "isTerrain": 0})
            raw = base64.b64decode(d.get("saveData") or "")
            if raw[:4] != b"SPAM":
                break
            for pos, text in _parse_spam_states(base64.b64decode(d.get("saveInternalStates") or "")):
                states[pos] = text
            fresh = []
            for b in _decode_spam(raw):
                for axis in ("x", "y", "z"):          # stored as 16-bit: 65xxx is a negative position
                    v = int(b.get(axis, 0) or 0)
                    b[axis] = v - 65536 if v >= 32768 else v
                k = (b.get("name"), b["x"], b["y"], b["z"])
                if k not in seen:
                    seen.add(k)
                    fresh.append(b)
            if not fresh:
                break
            all_blocks.extend(fresh)
            if len(fresh) < 50:
                break
    except Exception as e:
        print(f"[cw] room fetch failed for {room_key}: {e}")
        _cw_cache_put(ck, False, ttl=60)
        return None
    from services.role_locks import DISABLED_PATTERNS
    blocks = [b for b in all_blocks if b.get("name") in _V131_KEYS and b.get("name") not in DISABLED_PATTERNS]
    if len(blocks) < len(all_blocks):
        print(f"[cw] {room_key}: {len(all_blocks) - len(blocks)} blocks not in 1.31.0 left out")
    try:
        from services.mapsync_serializer import serialize_blocks_spam
        save = serialize_blocks_spam(blocks)        # the format the 1.31 client saves itself
    except Exception as e:
        print(f"[cw] serialize failed for {room_key}: {e}")
        return None
    # The room is its real size, or bigger when a block reaches past it.
    dims = [int(v) for v in (info.get("dimensions") or [80, 80, 80])]
    if blocks:
        for i, axis in enumerate(("x", "y", "z")):
            dims[i] = max(dims[i], max(int(b.get(axis, 0) or 0) for b in blocks) + 1)
    out = {
        "dimensions": dims,
        "floorDepth": info.get("floorDepth") if info.get("floorDepth") is not None else 0,
        "saveData": save,
        "blockCount": len(blocks),
        # the real room's own layout (which way it faces, its liquid), not our defaults
        "room": {k: info[k] for k in _CW_ROOM_FIELDS if info.get(k) is not None},
        # signs, dispensers, tapes...: the real states of the blocks we kept
        "states": _build_spam_states([(p, t) for p, t in states.items()
                                      if p in {(b["x"], b["y"], b["z"]) for b in blocks}]),
    }
    _cw_cache_put(ck, out)
    print(f"[cw] live room {room_key}: {out['blockCount']} blocks, dims {out['dimensions']}")
    return out


async def _cw_fetch_grid(base: str):
    """A cw's real room grid straight from Yeeps (fetchRoomMapData + each room's name), with
    its cw_ world fields and owner. Kept in memory only (10 min), never in the database."""
    ck = f"grid:{base}"
    hit = _cw_cache_get(ck)
    if hit is not None:
        return hit or None
    from services.yeeps_direct import invoke, fetch_accounts
    acct = "cw_grid_account" if (await _cw_account("cw_grid_account"))[0] else "cw_fetch_account"
    try:
        data = await invoke("fetchRoomMapData", {"roomMapKey": base,
                            "propertiesToGet": ["uniqueID", "data", "version"] + _CW_PROP_KEYS}, account=acct)
    except Exception as e:
        print(f"[cw] room map fetch failed for {base}: {e}")
        _cw_cache_put(ck, False, ttl=60)
        return None
    nodes = [n for n in (data.get("data") or []) if isinstance(n, dict) and n.get("roomKey")]
    if not nodes:
        _cw_cache_put(ck, False, ttl=300)          # no such world
        return None
    rooms = [{"roomKey": n["roomKey"], "gridPosition": n.get("gridPosition"), "themeKey": n.get("themeKey")}
             for n in nodes]

    async def _name(r):
        try:
            got = await invoke("fetchRoomData", {"roomKey": r["roomKey"], "propertiesToGet": ["roomName"]}, account=acct)
            r["roomName"] = got.get("roomName")
        except Exception:
            r["roomName"] = None
    await asyncio.gather(*(_name(r) for r in rooms))
    props = {k: v for k, v in data.items() if k.startswith("cw_") and v is not None}
    _cw_cache_put(f"props:{base}", props)
    owner, owner_name = props.get("cw_adminAccountID"), None
    if owner:
        try:
            owner_name = ((await fetch_accounts([owner], ["displayName"])).get(owner) or {}).get("displayName")
        except Exception:
            pass
    _CW_WORLD[base] = {"ownerAccountID": owner, "ownerName": owner_name, "description": props.get("cw_description")}
    if owner and owner_name:
        CW_OWNER_NAMES[owner] = owner_name
    rooms = _cw_assign_aliases(base, rooms)
    _cw_cache_put(ck, rooms)
    return rooms


def _cw_key_safe(room_key: str) -> bool:
    """True if the 1.19 client can parse this community-world room key.

    The 1.19 client derives grid coordinates by Int32.Parse-ing the part of the
    room key after the world base. Real 2.x keys encode coords in a newer format
    the old parser chokes on (Int32.Parse(null) -> crash): a '.' (c_X_room_0.1),
    an 'n' for negatives (c_X_room_n4) or a '-' (c_X_room_-1.0). Only the plain
    '<base>_sandbox' entry and '<base>_room_<digits>' are safe.
    """
    import re as _re
    if room_key.endswith("_sandbox"):
        return True
    return bool(_re.fullmatch(r"c_.+_room_\d+", room_key))


def _cw_assign_aliases(base: str, rooms: list) -> list:
    """Rooms whose real keys the client can't parse (2.x coordinate names such as
    c_X_room_0.1, c_X_room_-1.0 or c_X_room_n4) are served under numbered keys
    c_X_room_<n>. _CW_ALIAS maps each one back to its real key for the block reads."""
    rooms = [n for n in (rooms or []) if n.get("roomKey")]
    used = {n["roomKey"] for n in rooms if _cw_key_safe(n["roomKey"])}
    out, nxt = [], 1
    for n in sorted(rooms, key=lambda r: r["roomKey"]):
        rk = n["roomKey"]
        if _cw_key_safe(rk):
            out.append(n)
            continue
        while f"{base}_room_{nxt}" in used:
            nxt += 1
        alias = f"{base}_room_{nxt}"
        used.add(alias)
        _CW_ALIAS[alias] = rk
        out.append({**n, "roomKey": alias})
    return out


async def _cw_real_key(room_key: str) -> str:
    """The real room key behind a served community-world room key."""
    if room_key not in _CW_ALIAS and room_key.startswith("c_"):
        await _cw_fetch_grid(_cw_base(room_key))      # after a restart: rebuild the aliases
    return _CW_ALIAS.get(room_key, room_key)


# The cw_ fields the game reads with the world's room map (seen in its GetItem request).
_CW_PROP_KEYS = ["cw_initialized", "cw_adminAccountID", "cw_staffAccountIDs", "cw_payout_staffAccountIDs",
                 "cw_payout_coins", "cw_staffPermissions", "cw_isOfficial", "cw_isLocked",
                 "cw_fuelExpireTimestamp", "cw_visits_total", "cw_permissions", "cw_description"]


async def _cw_world_props(v_key: str) -> dict:
    """The world's real cw_ fields; the room-grid fetch reads them in the same Yeeps call."""
    hit = _cw_cache_get(f"props:{v_key}")
    if hit is None:
        await _cw_fetch_grid(v_key)
        hit = _cw_cache_get(f"props:{v_key}")
    return hit or {}


# Room themes the 1.31 client has (the editor's biome list). A community-world room
# using a newer theme shows as suburb instead.
_THEMES_131 = {"suburb", "beach", "snow", "fantasy", "backrooms", "researchFacility",
               "sandbox", "tutorial"}   # desert is not in 1.31 (owner 2026-10-11) -> suburb


def _cw_theme(theme) -> str:
    if not theme:
        return "sandbox"
    return theme if theme in _THEMES_131 else "suburb"


def _cw_base(room_key: str) -> str:
    """c_NAME for a community-world room key (c_NAME_sandbox, c_NAME_room_<n> or c_NAME)."""
    if room_key.endswith("_sandbox"):
        return room_key[: -len("_sandbox")]
    if "_room_" in room_key:
        return room_key.split("_room_", 1)[0]
    return room_key


def _cw_room_map_from_grid(v_key: str, rooms: list) -> dict:
    """Build a 1.19 room-map grid from the real cw nodes, adding a 'public'
    doorway so the player can arrive/leave.

    Only rooms whose keys the 1.19 client can parse are included (see
    _cw_key_safe); 2.x-coordinate-encoded keys (c_X_room_0.1 / _n4 / _-1.0)
    would make RoomMapManager.GetRoomMap throw Int32.Parse(null) and crash, so
    they are dropped. The '_sandbox' entry is always kept.
    """
    safe = [n for n in (rooms or []) if n.get("roomKey") and _cw_key_safe(n["roomKey"])]
    if not safe:
        return _sandbox_room_map(v_key)
    data = []
    seen_pos = set()
    for n in safe:
        gp = n.get("gridPosition") or [0, 0]
        pos = (int(gp[0]), int(gp[1]))
        if pos in seen_pos:      # client rejects duplicate grid positions
            continue
        seen_pos.add(pos)
        data.append({
            "roomKey": n["roomKey"],
            "themeKey": _cw_theme(n.get("themeKey")),
            "gridPosition": [pos[0], pos[1]],
        })
    # Doorway back to the public world, just below the entry (_sandbox) node.
    entry = next((n for n in safe if str(n.get("roomKey", "")).endswith("_sandbox")), safe[0])
    egp = entry.get("gridPosition") or [0, 0]
    door_pos = (int(egp[0]), int(egp[1]) - 1)
    while door_pos in seen_pos:
        door_pos = (door_pos[0], door_pos[1] - 1)
    data.append({
        "themeKey": "sandbox",
        "gridPosition": [door_pos[0], door_pos[1]],
        "transitionKey": "public",
        "blockLeft": 1, "blockRight": 1,
    })
    out = {"versionKey": v_key, "version": 1, "data": data}
    world = _CW_WORLD.get(v_key) or {}
    if world.get("ownerAccountID"):
        out["cw_adminAccountID"] = world["ownerAccountID"]
    if world.get("description"):
        out["cw_description"] = world["description"]
    return out




def _aws_id() -> str:
    return uuid.uuid4().hex.upper()


def _make_response(payload: dict) -> tuple[str, dict]:
    """Build a DynamoDB-formatted response: body text + headers."""
    payload_text = json.dumps(payload)
    crc32 = str(zlib.crc32(payload_text.encode("utf-8")) & 0xFFFFFFFF)
    headers = {
        "Content-Type": "application/x-amz-json-1.0",
        "x-amzn-RequestId": _aws_id(),
        "x-amz-crc32": crc32,
    }
    return payload_text, headers


def _is_119_map(doc) -> bool:
    """True for a map blob the current client can read.

    1.19-era maps are .NET BinaryFormatter data (base64 "AAEAAAD..."). The
    1.31.0-blaze client SAVES in the SPAM format ("U1BBTQ...", mapDataVersion 3)
    and reads both, so serve either. (Serving only "AAEAAAD" made every 1.31
    save come back as an empty map after a relaunch.)
    """
    save = (doc or {}).get("saveData")
    return isinstance(save, str) and (save.startswith("AAEAAAD") or save.startswith("U1BBTQ"))


async def _map_doc(map_key) -> dict:
    """The G2_Maps document to serve for a map key.

    The client reads "{roomKey}$map_{lastEditedSessionID}_save{offset}", and its
    own saves are found under that exact key. For the first chunk only, an
    editor-imported map or the room's "{roomKey}$current" snapshot is used
    instead, so one blob is never re-served as save1, save2... Anything else
    gets an empty (0-block) map. Fields a stored map lacks are filled by the
    normal G2_Maps default injection, as in the original server.
    """
    map_key = map_key if isinstance(map_key, str) else ""
    room_key = map_key.split("$", 1)[0]

    # Community worlds mirror real Yeeps content live. Serve the full converted
    # map on the first chunk only (save0 / current / un-chunked); later chunks
    # return empty so one blob is never re-served as save1, save2...
    if room_key.startswith("c_"):
        first_chunk = (map_key.endswith("_save0")
                       or map_key.endswith("$current")
                       or "$map_" not in map_key)
        # SPAM + mapDataVersion 3, like the game's own saves (the old 1.19 blob with
        # version 1 failed in 1.31 with "null map: INVALID_FORMAT").
        from services.mapsync_serializer import serialize_blocks_spam
        if first_chunk:
            live = await _cw_fetch_room(await _cw_real_key(room_key))
            if live and live.get("saveData"):
                return {
                    "mapKey": map_key,
                    "roomKey": room_key,
                    "saveData": live["saveData"],
                    "saveInternalStates": live.get("states") or EMPTY_SPAM_STATES,
                    "mapDataVersion": 3,
                }
        return {
            "mapKey": map_key,
            "roomKey": room_key,
            "saveData": serialize_blocks_spam([]),
            "saveInternalStates": EMPTY_SPAM_STATES,
            "mapDataVersion": 3,
        }

    collection = get_collection("G2_Maps")
    found = None
    if collection is not None and map_key:
        found = await collection.find_one({"mapKey": map_key}, {"_id": 0})
    if not _is_119_map(found) and map_key.endswith("_save0"):
        try:
            from services.db import imported_maps
            found = await imported_maps.find_one({"roomKey": room_key}, {"_id": 0})
        except Exception as e:
            print(f"[dynamo] imported-map lookup failed: {e}")
            found = None
        if not _is_119_map(found) and collection is not None:
            found = await collection.find_one({"mapKey": f"{room_key}$current"}, {"_id": 0})
    if _is_119_map(found):
        out = {**found, "mapKey": map_key, "roomKey": room_key}
        # 1.31: a map can carry its FULL sign/dispenser internal states in
        # saveInternalStatesFull (Starter House). Those crash APKs older than
        # build_gate.GUARD_MIN_BUILD, so serve them only when every build that can
        # currently play is new enough; otherwise the safe saveInternalStates.
        full = out.pop("saveInternalStatesFull", None)
        if full:
            try:
                from services.build_gate import only_guard_builds
                if await only_guard_builds():
                    out["saveInternalStates"] = full
            except Exception as e:
                print(f"[dynamo] full-states gate failed for {map_key}: {e}")
        return out
    return {
        "mapKey": map_key,
        "roomKey": room_key,
        "saveData": EMPTY_MAP_SAVE_DATA,
        "saveInternalStates": EMPTY_MAP_SAVE_DATA,
        "mapDataVersion": 1,
    }


def _sandbox_base(v_key: str) -> str:
    """Base key of a private/community world. p_o_123 / c_NAME stay as-is;
    a bare o_123 maps to its private world p_o_123."""
    if v_key.startswith("p_") or v_key.startswith("c_"):
        return v_key
    return f"p_{v_key}"


def _sandbox_room_map(v_key: str, main_theme: str = "suburb",
                      sandbox_theme: str = "sandbox") -> dict:
    """Room-map grid for a private (p_o_<acct>) or community (c_<name>) world.

    Layout is a single column:  [public doorway] -> [sandbox] -> [main].
    The "public" transition node is REQUIRED: when you travel in via the
    world map's "private"/"communityWorlds" node, the client runs
    GetTransitionNode on the destination map looking for the doorway back,
    and throws NO_TRANSITION_NODE_FOUND if it isn't there.

    The node themeKeys drive the in-game scene biome; main_theme/sandbox_theme
    are read from the stored room docs so editor biome changes take effect.
    """
    base = _sandbox_base(v_key)
    return {
        "versionKey": v_key,
        "version": 1,
        "data": [
            {"themeKey": "sandbox", "gridPosition": [0, 0], "transitionKey": "public",
             "blockLeft": 1, "blockRight": 1},
            {"roomKey": f"{base}_sandbox", "themeKey": sandbox_theme, "gridPosition": [0, 1],
             "blockLeft": 1, "blockRight": 1},
            {"roomKey": base, "themeKey": main_theme, "gridPosition": [0, 2],
             "blockLeft": 1, "blockRight": 1, "blockUp": 1},
        ],
    }


async def _pw_room_map(v_key: str) -> dict:
    """Private-world room map with node biomes taken from the stored room docs
    (falls back to suburb/sandbox when a room hasn't been themed)."""
    base = _sandbox_base(v_key)
    main_theme, sandbox_theme = "suburb", "sandbox"
    try:
        collection = get_collection("G2_Rooms")
        if collection is not None:
            md = await collection.find_one({"roomKey": base}, {"_id": 0, "themeKey": 1})
            if md and md.get("themeKey"):
                main_theme = md["themeKey"]
            sd = await collection.find_one({"roomKey": f"{base}_sandbox"}, {"_id": 0, "themeKey": 1})
            if sd and sd.get("themeKey"):
                sandbox_theme = sd["themeKey"]
    except Exception as e:
        print(f"[pw] room-map theme lookup failed: {e}")
    return _sandbox_room_map(v_key, main_theme=main_theme, sandbox_theme=sandbox_theme)


def _full_room(room_key: str, room_name: str, dims: list,
               read_only: bool = False, theme: str = "sandbox", lobby: int = 1) -> dict:
    """Complete room-data document. Private/community rooms need the FULL field
    set (doors, liquid, lighting, terrain/save version fields) or the client
    throws ArgumentNullException while building the RoomDatabaseEntry.

    read_only=True (community worlds): not Creative, mode-locked and save-locked
    so the mirrored real-Yeeps content can be walked but never edited or saved
    back. creativeSessionID is still non-null so the client treats it as the
    main instance (otherwise it spams "not in the main instance of this room").
    """
    door = {"block": 0, "position": [0, 0]}
    return {
        "roomKey": room_key,
        "roomName": room_name,
        "themeKey": theme,
        "dimensions": dims,
        "isOfficial": 0,
        # Creative + a non-null creativeSessionID is the "main instance" check
        # the client runs; without it building is refused as "not in the main
        # instance of this room".
        "isCreative": 0 if read_only else 1,
        "creativeSessionID": 1,
        "blockMatchmakingSegmentation": 1,
        "isModeLocked": 1 if read_only else 0,
        "isDevLocked": 0,
        "isSaveLocked": 1 if read_only else 0,
        # noSave tells the game the room keeps no map at all (it then never loads one);
        # read-only rooms still load theirs - saving is blocked by isSaveLocked + the server.
        "noSave": 0,
        "noPaste": 1 if read_only else 0,
        "floorDepth": 0,
        "lobbyDirection": lobby,
        "customLightingKey": "",
        "liquidColor": "",
        "liquidLevel": 0,
        "liquidType": 0,
        "autoCycleTimestamp": 0,
        "isAutoCycling": 0,
        "itemRestrictionsGroupKeys": [],
        "itemRestrictionsIsWhiteList": 0,
        "door_up": door, "door_down": door, "door_left": door, "door_right": door,
        "lastEditedSessionID": 1,
        "lastEditedTerrainSessionID": 1,
        "lastRoomDataEditorSessionID": 1,
        "previousEditedSessionID": 1,
        "previousEditedTerrainSessionID": 1,
        "lastValidSaveOffset": 0,
        "overrideSaveLockTimestamp": "1970 01/01 00:00:00",
        "previousMapProperties": {},
        "roomDataVersion": 1,
        "terrainDataVersion": 1,
    }


async def _get_fallback_doc(table_name: str, query: dict, attrs=None) -> dict:
    """Provide default documents for sensitive tables when missing from DB."""
    # 1. G2_RoomMap fallback (World map / grid)
    if table_name == "G2_RoomMap":
        v_key = query.get("versionKey")
        if isinstance(v_key, str) and v_key.startswith("c_"):
            # Community world: mirror the real Yeeps room grid and world fields live.
            rooms = await _cw_fetch_grid(v_key)
            props = await _cw_world_props(v_key)
            if not rooms and not props.get("cw_initialized"):
                return None                      # no such world on Yeeps -> the game says not found
            doc = _cw_room_map_from_grid(v_key, rooms) if rooms else _sandbox_room_map(v_key)
            doc.update(props)
            return doc
        if isinstance(v_key, str) and (v_key.startswith("o_")
                                       or v_key.startswith("p_o_")):
            return await _pw_room_map(v_key)
        # Generic fallback for room map
        collection = get_collection("G2_RoomMap")
        if collection is not None:
            return await collection.find_one({"versionKey": "beta4"}, {"_id": 0})

    # 2. G2_Rooms fallback (Individual room data)
    if table_name == "G2_Rooms":
        room_key = query.get("roomKey")
        if isinstance(room_key, str):
            is_sandbox = (room_key.startswith("o_")
                          or room_key.startswith("p_o_")
                          or room_key.startswith("c_"))
            if room_key.startswith("c_"):
                # Community world: read-only mirror. Use the real room's live
                # dimensions/floorDepth so the floor plane matches the content.
                # A look-up that only wants the name (world lists) costs no Yeeps calls.
                if attrs and not any(a in ("dimensions", "floorDepth") for a in attrs):
                    base = _cw_base(room_key)
                    node = next((n for n in (_cw_cache_get(f"grid:{base}") or [])
                                 if n.get("roomKey") == room_key), {})
                    return _full_room(room_key, node.get("roomName") or base[2:], [80, 80, 80],
                                      read_only=True, theme=_cw_theme(node.get("themeKey")))
                live = await _cw_fetch_room(await _cw_real_key(room_key))
                if room_key.endswith("_sandbox"):
                    dims = (live or {}).get("dimensions") or [32, 80, 32]
                else:
                    dims = (live or {}).get("dimensions") or [80, 80, 80]
                node = next((n for n in (await _cw_fetch_grid(_cw_base(room_key)) or [])
                             if n.get("roomKey") == room_key), {})
                r = _full_room(room_key, node.get("roomName") or "Community World", dims,
                               read_only=True, theme=_cw_theme(node.get("themeKey")))
                if live and live.get("floorDepth") is not None:
                    r["floorDepth"] = live["floorDepth"]
                r.update((live or {}).get("room") or {})      # real lobby direction, liquid, doors
                return r
            if is_sandbox:
                # Private worlds (p_o_<acct>) stay editable/saveable.
                # _sandbox suffix = the small 32x80x32 starter room; the bare
                # base key = the big 80x80x80 main room.
                if room_key.endswith("_sandbox"):
                    return _full_room(room_key, "Sandbox", [32, 80, 32])
                # The big main room uses a real biome, not the flat sandbox theme.
                # lobbyDirection 3 is the standard orientation for the pw main room.
                return _full_room(room_key, "Private World", [80, 80, 80], theme="suburb", lobby=3)
            # Official / public room fallback.
            return {
                "roomKey": room_key,
                "roomName": room_key,
                "isOfficial": 1,
                "themeKey": "suburb",
                "dimensions": [100, 100, 100],
                "floorDepth": 0,
                "lobbyDirection": 1,
                # Official rooms are play-mode (2026-10-11: Playground kept showing
                # as creative on some screens). Wilds + pw are set elsewhere.
                "isCreative": 0,
                # Official / public rooms are mode-locked: can't be flipped to
                # creative (wilds + pw are exempt and set elsewhere).
                "isModeLocked": 1,
                "noSave": 0,
                "lastEditedSessionID": 1,
                "roomDataVersion": 1,
                "creativeSessionID": 1,
                "blockMatchmakingSegmentation": 1,
            }

    # 3. G2_Maps fallback (Saved block data)
    if table_name == "G2_Maps":
        return await _map_doc(query.get("mapKey", ""))

    # 4. G2_Globals fallback
    if table_name == "G2_Globals":
        key = query.get("key")
        if key == "activeBundleKey":
            return {"key": "activeBundleKey", "data": "bundle_commando"}
        return {"key": key, "data": 0}

    return None


# shouldForceRefresh stickiness: the client polls this single attr every ~30s
# and only reloads on a *persistent* 1 (serving 1 once then clearing let it
# re-read a 0 mid-refresh and abort). We keep serving 1 until the client
# actually re-pulls its account — consumed in login.py (questLogIntoAccount) or
# by a full-account GetItem here. Live testing proved the client NEVER acts on
# the flag while standing in a loaded room (it just keeps polling), so there is
# no refresh-loop risk; the backstop is just a long safety expiry so a flag set
# on an account that never logs back in can't persist indefinitely. The flag now
# survives long enough to be picked up on the player's next room transition /
# relog. Only warned/banned accounts ever have it set.
# Maps accountID -> monotonic time we first served a 1.
_FR_FIRST_SEEN: dict[str, float] = {}
_FR_BACKSTOP_SEC = 600.0


async def _clear_force_refresh(account_id: str) -> None:
    _FR_FIRST_SEEN.pop(account_id, None)
    if not account_id:
        return
    try:
        from services.db import accounts as _accts
        await _accts.update_one({"accountID": account_id},
                                {"$set": {"shouldForceRefresh": 0}})
    except Exception as _e:
        print(f"[dynamo] shouldForceRefresh clear failed: {_e}")


from services.account_privacy import PRIVATE_ACCOUNT_FIELDS, public_view

# Server-only G2_Globals entries (service logins, editor state) never served.
PRIVATE_GLOBAL_KEYS = {"cw_fetch_account", "cw_grid_account", "editorPwWindow"}


def _role_locked_view(doc: dict) -> dict:
    """Account as the client may see it: role-locked patterns only for holders
    of their role; server-only roles hidden (services/role_locks.py)."""
    from services import role_locks
    if "ownedPatterns" not in doc and "roleKeys" not in doc:
        return doc
    doc = dict(doc)
    owned, roles = role_locks.apply(doc.get("ownedPatterns"), doc.get("roleKeys"), doc.get("accountID", ""))
    if "ownedPatterns" in doc:
        doc["ownedPatterns"] = owned
    if "roleKeys" in doc:
        doc["roleKeys"] = roles
    return doc


_LIVE_ACCT: dict = {}   # accountID -> (expires, doc); memory only, never the database


async def _live_account(account_id) -> dict | None:
    """A player we have no record of: their public look straight from the real Yeeps API
    (fetchAccountDatas via services/yeeps_direct - no relay hop). Cached 10 min in memory."""
    import re as _re
    import time as _time
    if not isinstance(account_id, str) or not _re.fullmatch(r"o_\d+", account_id):
        return None
    hit = _LIVE_ACCT.get(account_id)
    if hit and hit[0] > _time.time():
        return hit[1]
    doc = None
    try:
        from services.yeeps_direct import fetch_accounts
        found = (await fetch_accounts([account_id], ["displayName", "activeCosmetics", "skinColor", "eyeColor"])).get(account_id)
        if found:
            doc = {"accountID": account_id, "displayName": found.get("displayName") or "",
                   "activeCosmetics": found.get("activeCosmetics") or [],
                   "skinColor": found.get("skinColor"), "eyeColor": found.get("eyeColor")}
            doc = {k: v for k, v in doc.items() if v is not None}
    except Exception as e:
        print(f"[live] Yeeps lookup failed for {account_id}: {e}")
    _LIVE_ACCT[account_id] = (_time.time() + (600 if doc else 60), doc)
    return doc


def _fill_requested(table_name: str, item: dict, attrs: list, live: bool = False) -> None:
    """A requested room attribute the record lacks must not reach the client as NULL: it
    parses that as a null string/number (ArgumentNullException / NO_DATA_FOUND)."""
    if table_name == "G2_Accounts" and not live:
        return                      # only live look-ups get defaults; our own accounts are untouched
    if table_name not in ("G2_RoomMap", "G2_Rooms", "G2_Accounts") or not attrs:
        return
    account_defaults = {"displayName": "", "activeCosmetics": [], "skinColor": 0, "eyeColor": 0}
    for a_ in attrs:
        if item.get(a_) == {"NULL": True}:
            if table_name == "G2_Accounts":
                value = account_defaults.get(a_, default_for_property(a_))
            elif table_name == "G2_RoomMap":
                value = _roommap_default(a_)
            else:
                value = default_for_property(a_)
            item[a_] = to_dynamo(value)


def _roommap_default(attr: str):
    """Value for a G2_RoomMap attribute the client asked for but the document lacks.
    NULL here reaches the client as a null string/number it parses -> ArgumentNullException
    (parameter s), the same failure property_defaults fixed for G2_Rooms."""
    if attr == "version":
        return 1
    if attr in ("data", "cw_staff", "cw_staffAccountIDs", "cw_admin", "cw_favorites"):
        return []
    if attr in ("cw_payout_staffAccountIDs", "cw_payout_coins", "cw_staffPermissions"):
        return {}
    if attr == "cw_description" or attr == "cw_adminAccountID":
        return ""
    if attr == "cw_initialized":
        return 1
    if attr == "cw_fuelExpireTimestamp":
        return "2099 01/01 00:00:00"
    if attr.endswith("Timestamp"):
        return "1970 01/01 00:00:00"
    if attr.startswith("cw_"):
        return 0
    return default_for_property(attr)


async def handle_get_item(body: dict) -> tuple[str, dict]:
    """Handle DynamoDB GetItem operation."""
    table_name = body.get("TableName", "")
    key = body.get("Key", {})
    attrs_to_get = body.get("AttributesToGet", [])
    requested_attrs = list(attrs_to_get)

    collection = get_collection(table_name)
    query = unwrap_key(key)

    # 1. Database lookup
    doc = None
    if table_name == "G2_Maps":
        doc = await _map_doc(query.get("mapKey", ""))
    elif collection is not None:
        doc = await collection.find_one(query, {"_id": 0})

    # 2. Fallback logic
    if doc is None:
        doc = await _get_fallback_doc(table_name, query, requested_attrs)
    live_account = False
    if doc is None and table_name == "G2_Accounts":
        doc = await _live_account(query.get("accountID"))
        live_account = doc is not None

    # 3. Build item and post-process
    if table_name == "G2_Globals" and query.get("key") in PRIVATE_GLOBAL_KEYS:
        doc = None
    if doc:
        if table_name == "G2_Accounts" and isinstance(doc, dict):
            doc = public_view(_role_locked_view(doc))
        item = wrap_item(doc, requested_attrs if requested_attrs else None)
        _fill_requested(table_name, item, requested_attrs, live=live_account)
        
        # Specialized G2_Bundles logic (MinVersion, etc)
        if table_name == "G2_Bundles" and isinstance(doc, dict):
            if doc.get("bundleKey") == "bundle_commando":
                item["endTimestamp"] = {"S": "2025 02/20 07:00:00"}
                item["minVersion"] = {"S": "1.0.0"}
            elif "minVersion" in doc:
                item["minVersion"] = to_dynamo(doc["minVersion"])
    else:
        # Not found — initialize with NULLs to be filled by defaults
        item = {a: {"NULL": True} for a in requested_attrs}

    # Inject defaults for sensitive tables
    if table_name in ["G2_Rooms", "G2_Maps", "G2_Accounts", "G2_Bundles", "G2_Globals", "G2_AccountStats"] and item:
        for attr in list(item.keys()):
            # shouldForceRefresh: STICKY until the client actually consumes it.
            # Set to 1 on an in-game ban/warn so the client force-refreshes and
            # picks up adminBlocked / hasPendingWarning mid-session. See the
            # _FR_FIRST_SEEN note above for why we don't clear on the first poll.
            if table_name == "G2_Accounts" and attr == "shouldForceRefresh":
                stored = 0
                try:
                    stored = int((doc or {}).get("shouldForceRefresh", 0) or 0)
                except Exception:
                    stored = 0
                _aid = query.get("accountID") or ""
                if stored != 1:
                    item[attr] = {"N": "0"}
                    _FR_FIRST_SEEN.pop(_aid, None)
                    continue
                # A full-account read (more than just this attr) IS the refresh
                # the flag asks for -> serve 1 and consume it so it fires once.
                if len(requested_attrs) > 1:
                    item[attr] = {"N": "1"}
                    await _clear_force_refresh(_aid)
                    continue
                # Single-attr poll: keep serving 1 (sticky) until consumed, with
                # a monotonic backstop so it can never force-refresh forever.
                now = time.monotonic()
                first = _FR_FIRST_SEEN.setdefault(_aid, now)
                if now - first > _FR_BACKSTOP_SEC:
                    item[attr] = {"N": "0"}
                    await _clear_force_refresh(_aid)
                else:
                    item[attr] = {"N": "1"}
                continue
            
            # TESTING: un-mode-lock every room so modes can be switched freely.
            # Toggle off with env UNLOCK_ALL_MODES=0 after testing.
            if table_name == "G2_Rooms" and attr == "isModeLocked":
                from config import UNLOCK_ALL_MODES
                if UNLOCK_ALL_MODES:
                    item[attr] = {"N": "0"}
                    continue

            # General default injection
            if item[attr] == {"NULL": True}:
                item[attr] = to_dynamo(default_for_property(attr))

    _k = str(query.get("versionKey") or query.get("roomKey") or query.get("mapKey") or "")
    if table_name == "G2_RoomMap" or _k.startswith("c_"):   # TEMP diagnostic (cw travel, 2026-10-11)
        print(f"[roomdiag] GetItem {table_name} key={query} attrs={requested_attrs} -> "
              f"{sorted(item.keys()) if item else 'NO ITEM'}")
    if not item:
        return _make_response({})

    return _make_response({"Item": item})


async def handle_batch_get_item(body: dict) -> tuple[str, dict]:
    """Handle DynamoDB BatchGetItem operation."""
    request_items = body.get("RequestItems", {})
    responses = {}

    for table_name, table_req in request_items.items():
        collection = get_collection(table_name)
        keys = table_req.get("Keys", [])
        attrs_to_get = table_req.get("AttributesToGet", [])

        items = []
        for dynamo_key in keys:
            query = unwrap_key(dynamo_key)
            doc = None
            if table_name == "G2_Maps":
                doc = await _map_doc(query.get("mapKey", ""))
            elif collection is not None:
                doc = await collection.find_one(query, {"_id": 0})

            # Fallback logic
            if doc is None:
                doc = await _get_fallback_doc(table_name, query, attrs_to_get)
            live = False
            if doc is None and table_name == "G2_Accounts":
                doc = await _live_account(query.get("accountID"))
                live = doc is not None

            if table_name == "G2_Globals" and query.get("key") in PRIVATE_GLOBAL_KEYS:
                doc = None
            # Build item
            if doc:
                if table_name == "G2_Accounts" and isinstance(doc, dict):
                    doc = public_view(_role_locked_view(doc))
                item = wrap_item(doc, attrs_to_get if attrs_to_get else None)
                _fill_requested(table_name, item, attrs_to_get, live=live)
                _k = str(query.get("versionKey") or query.get("roomKey") or query.get("mapKey") or "")
                if table_name == "G2_RoomMap" or _k.startswith("c_"):   # TEMP diagnostic
                    print(f"[roomdiag] BatchGet {table_name} key={query} attrs={attrs_to_get} -> {sorted(item.keys())}")
            elif table_name in ["G2_Rooms", "G2_Maps", "G2_Accounts", "G2_Bundles", "G2_Globals"]:
                # If sensitive table and no doc, provide an empty item with NULLs (to be filled below)
                item = {a: {"NULL": True} for a in attrs_to_get}
            else:
                # For other tables, skip missing items (standard DynamoDB behavior)
                continue

            # Specialized logic (MinVersion, etc)
            if table_name == "G2_Bundles" and doc and isinstance(doc, dict):
                if doc.get("bundleKey") == "bundle_commando":
                    item["endTimestamp"] = {"S": "2025 02/20 07:00:00"}
                    item["minVersion"] = {"S": "1.0.0"}
                elif "minVersion" in doc:
                    item["minVersion"] = to_dynamo(doc["minVersion"])
            
            # Inject defaults for sensitive tables
            if table_name in ["G2_Rooms", "G2_Maps", "G2_Accounts", "G2_Bundles", "G2_Globals", "G2_AccountStats"] and item:
                for attr in list(item.keys()):
                    # Special case for accounts
                    if table_name == "G2_Accounts" and attr == "shouldForceRefresh":
                        item[attr] = {"N": "0"}
                        continue
                    
                    # General default injection
                    if item[attr] == {"NULL": True}:
                        item[attr] = to_dynamo(default_for_property(attr))
                            
            items.append(item)

        responses[table_name] = items

    return _make_response({
        "Responses": responses,
        "UnprocessedKeys": {},
    })

async def _record_and_force_presence_version(table_name: str, doc: dict) -> None:
    """Learn each account's REAL build from its presence write, then force the
    shared presence version to the current build.

    The game's login call carries NO client version, so we can't gate there
    directly. But the client DOES report its version in the G2_PlayerStatuses
    presence write (and the attestation heartbeat). We persist that real value
    onto the account as `lastClientVersion`; login.py then isolates any account
    whose last-known build is outdated — which fires on the next relaunch /
    re-login. We also rewrite the shared presence version to
    REQUIRED_CLIENT_VERSION so up-to-date clients match for the in-room join
    check while older clients ("1.19.0") don't.
    """
    if "G2_PlayerStatuses" not in (table_name or "") or "applicationVersion" not in doc:
        return
    real = str(doc.get("applicationVersion") or "")
    acct = doc.get("accountID")
    if acct and real:
        try:
            from services.db import accounts
            await accounts.update_one({"accountID": acct},
                                      {"$set": {"lastClientVersion": real}})
        except Exception as e:
            print(f"[ver] lastClientVersion persist failed for {acct}: {e}")
    doc["applicationVersion"] = REQUIRED_CLIENT_VERSION


# Account fields a CLIENT may never write via the DynamoDB API. These are
# moderation/security/role fields the SERVER owns: bans are applied only through
# g2_moderatorBanAccount (gated to MOD_STAFF) and the editor, which write to
# Mongo directly (NOT through these handlers), so stripping them here blocks a
# modded client from banning anyone (or clearing its own ban, or granting itself
# roles) by writing straight to G2_Accounts — without affecting legit bans or a
# client's normal writes (currency, cosmetics, settings, ...).
PROTECTED_ACCOUNT_FIELDS = {
    "adminBlocked", "isPermabanned", "isMutebanned", "isIsolated",
    "banExpireTimestamp", "banReason", "blockReason", "banCategory",
    "banCount", "remainingBanHours", "pendingBanDuration", "longestBanDuration",
    "shadowBanned", "hasPendingWarning", "warnReason", "warnCategory",
    "warnCount", "shouldForceRefresh", "roleKeys", "attestHash",
    "pendingKick", "kickReason",
    "clientBuildTag", "clientBuildStatus", "clientBuildSeenAt",
}


def _strip_protected_account_fields(table_name: str, fields: dict, who: str = "") -> None:
    """Drop server-owned moderation/security fields from a CLIENT account write."""
    if table_name != "G2_Accounts" or not isinstance(fields, dict):
        return
    hit = [k for k in list(fields.keys()) if k in PROTECTED_ACCOUNT_FIELDS or k in PRIVATE_ACCOUNT_FIELDS]
    for k in hit:
        fields.pop(k, None)
    if hit:
        print(f"[guard] stripped protected account fields from client write "
              f"to {who or '?'}: {hit}")


def _take_owned_patterns(table_name: str, fields: dict) -> list:
    """Clients must never SHRINK a player's owned blocks (everyone owns all of
    them; a client write of its own, shorter list used to replace the stored
    one). Pull ownedPatterns out of a client account write; the caller only
    ADDS those keys ($addToSet). Role-locked keys are never added by a client."""
    if table_name != "G2_Accounts" or not isinstance(fields, dict) or "ownedPatterns" not in fields:
        return []
    from services.role_locks import ROLE_LOCKED_PATTERNS
    owned = fields.pop("ownedPatterns") or []
    return [k for k in owned if isinstance(k, str) and k not in ROLE_LOCKED_PATTERNS]


async def handle_update_item(body: dict) -> tuple[str, dict]:
    """Handle DynamoDB UpdateItem operation."""
    table_name = body.get("TableName", "")
    key = body.get("Key", {})
    updates = body.get("AttributeUpdates", {})

    collection = get_collection(table_name)
    if collection is not None:
        query = unwrap_key(key)
        set_fields = unwrap_attribute_updates(updates)
        # Block ban/role/security field writes from clients (ban-bypass guard).
        _strip_protected_account_fields(table_name, set_fields, query.get("accountID", ""))
        if table_name == "G2_Accounts" and "activeCosmetics" in set_fields:
            from services.hair_colors import clamp_active_cosmetics
            set_fields["activeCosmetics"] = clamp_active_cosmetics(set_fields["activeCosmetics"])
        add_owned = _take_owned_patterns(table_name, set_fields)
        if add_owned and query:
            await collection.update_one(query, {"$addToSet": {"ownedPatterns": {"$each": add_owned}}})
        if set_fields:
            # Merge key fields (accountID lives in the Key, not the updates) in
            # first so the version recorder can see whose presence this is.
            set_fields.update(query)
            await _record_and_force_presence_version(table_name, set_fields)
            await collection.update_one(
                query,
                {"$set": set_fields},
                upsert=True,
            )

    return _make_response({})


async def handle_put_item(body: dict) -> tuple[str, dict]:
    """Handle DynamoDB PutItem operation."""
    table_name = body.get("TableName", "")
    item = body.get("Item", {})

    collection = get_collection(table_name)
    if collection is not None and item:
        # Convert DynamoDB-typed Item to plain doc
        doc = {}
        for k, v in item.items():
            doc[k] = from_dynamo(v)

        # Block ban/role/security field writes from clients (ban-bypass guard).
        # On PutItem this also means a banned client can't overwrite its own
        # account to clear adminBlocked — the stripped field keeps its DB value.
        _strip_protected_account_fields(table_name, doc, doc.get("accountID", ""))
        if table_name == "G2_Accounts" and "activeCosmetics" in doc:
            from services.hair_colors import clamp_active_cosmetics
            doc["activeCosmetics"] = clamp_active_cosmetics(doc["activeCosmetics"])
        add_owned = _take_owned_patterns(table_name, doc)
        if add_owned and doc.get("accountID"):
            await collection.update_one({"accountID": doc["accountID"]},
                                        {"$addToSet": {"ownedPatterns": {"$each": add_owned}}})

        await _record_and_force_presence_version(table_name, doc)

        key_field = get_key_field(table_name)
        if key_field and key_field in doc:
            await collection.update_one(
                {key_field: doc[key_field]},
                {"$set": doc},
                upsert=True,
            )
        else:
            await collection.insert_one(doc)

    return _make_response({})


async def handle_delete_item(body: dict) -> tuple[str, dict]:
    """Handle DynamoDB DeleteItem operation."""
    table_name = body.get("TableName", "")
    key = body.get("Key", {})

    # A client must never delete an account via the DynamoDB API (a modded client
    # could wipe other players). The editor deletes via Mongo directly, so it is
    # unaffected.
    if table_name == "G2_Accounts":
        print(f"[guard] blocked client DeleteItem on G2_Accounts: {unwrap_key(key)}")
        return _make_response({})

    collection = get_collection(table_name)
    if collection is not None:
        query = unwrap_key(key)
        await collection.delete_one(query)

    return _make_response({})


async def handle_query(body: dict) -> tuple[str, dict]:
    """Handle DynamoDB Query operation (stub — returns empty)."""
    return _make_response({"Items": [], "Count": 0, "ScannedCount": 0})


async def handle_scan(body: dict) -> tuple[str, dict]:
    """Handle DynamoDB Scan operation (stub — returns empty)."""
    return _make_response({"Items": [], "Count": 0, "ScannedCount": 0})


async def handle_describe_table(body: dict) -> tuple[str, dict]:
    """Handle DynamoDB DescribeTable operation."""
    return _make_response({
        "Table": {
            "TableName": body.get("TableName", "Unknown"),
            "TableStatus": "ACTIVE",
            "ItemCount": 0,
        }
    })


# Operation dispatcher
OPERATION_HANDLERS = {
    "GetItem":       handle_get_item,
    "BatchGetItem":  handle_batch_get_item,
    "UpdateItem":    handle_update_item,
    "PutItem":       handle_put_item,
    "DeleteItem":    handle_delete_item,
    "Query":         handle_query,
    "Scan":          handle_scan,
    "DescribeTable": handle_describe_table,
}


# The only DynamoDB write the real game makes (measured on a normal day, Oct 8:
# 25k UpdateItem on G2_PlayerStatuses and nothing else). Every other write or
# delete through this unauthenticated API came from raiders (Oct 9: deleted
# rooms, maps, room maps, globals, bundles). Those are acknowledged but ignored.
ALLOWED_CLIENT_WRITES = {("UpdateItem", "G2_PlayerStatuses")}
_WRITE_OPS = {"UpdateItem", "PutItem", "DeleteItem", "BatchWriteItem", "TransactWriteItems"}


def _write_tables(op: str, body: dict) -> list:
    if op in ("BatchWriteItem",):
        return list((body.get("RequestItems") or {}).keys()) or ["?"]
    if op in ("TransactWriteItems",):
        return ["?"]
    return [body.get("TableName", "") or "?"]


async def dispatch_dynamo(target: str, body: dict, client_ip: str = "", device_key: str = "") -> tuple[str, dict]:
    """
    Dispatch a DynamoDB operation by X-Amz-Target.
    Returns (body_text, headers).
    """
    op = (target or "").rsplit(".", 1)[-1]
    if op in _WRITE_OPS:
        tables = _write_tables(op, body if isinstance(body, dict) else {})
        if not all((op, t) in ALLOWED_CLIENT_WRITES for t in tables):
            try:
                key = json.dumps(body.get("Key") or {k: body.get("Item", {}).get(k) for k in list(body.get("Item") or {})[:2]})[:200]
            except Exception:
                key = "?"
            print(f"[guard] BLOCKED client {op} on {','.join(tables)} from {client_ip or '?'}: {key}")
            # The game never makes these writes, so whoever sent one is raiding:
            # IP-ban them (Oct 10: three addresses tried to wipe lobby maps).
            if client_ip and client_ip != "?" and not client_ip.startswith(("127.", "::1", "10.", "192.168.")):
                try:
                    from services.ip_bans import ban_ip
                    await ban_ip(client_ip, f"raw DB {op} on {','.join(tables)} {key[:80]}")
                    print(f"[guard] IP-banned {client_ip}")
                except Exception as _e:
                    print(f"[guard] auto IP-ban failed for {client_ip}: {_e}")
            return _make_response({})
        # The allowed write (a player's own presence) must come from that player's headset.
        from services import device_auth
        _acct = unwrap_key(body.get("Key") or {}).get("accountID", "") if isinstance(body, dict) else ""
        _ok, _ = await device_auth.check_request(_acct, device_key, f"{op} {tables[0]}", client_ip)
        if _ok:
            _ok = await device_auth.check_write(_acct, device_key, f"{op} {tables[0]}", client_ip)
        if not _ok:
            return _make_response({})
    handler = OPERATION_HANDLERS.get(op)

    if handler:
        return await handler(body)

    # Unknown operation — return empty
    return _make_response({})
