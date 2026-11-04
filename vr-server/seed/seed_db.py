"""
Database seeding script.
Parses dynamo.txt to extract real game data and inserts it into MongoDB.
Only seeds collections that are empty (safe to run multiple times).
"""

from pathlib import Path

from services.db import (
    accounts, rooms, room_maps, maps,
    player_statuses, globals_col, bundles,
)
from seed.parse_dynamo import parse_dynamo_log
from services.dynamo_types import from_dynamo
import json
import os


# Path to the reference log files
REFS_DIR = Path(__file__).parent.parent / "Refrences"
DYNAMO_LOG = REFS_DIR / "dynamo.txt"


# Load world map data from JSON
DEFAULT_ROOM_MAP_PATH = Path(__file__).parent / "default_room_map.json"
WORLD_MAP_TEAMS1 = {}
if DEFAULT_ROOM_MAP_PATH.exists():
    try:
        WORLD_MAP_TEAMS1 = json.loads(DEFAULT_ROOM_MAP_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[seed] Error loading default_room_map.json: {e}")


# Official room names from the captured BatchGetItem response
OFFICIAL_ROOMS = [
    {"roomKey": "tutorial", "roomName": "Tutorial", "isOfficial": 1},
    {"roomKey": "official_0", "roomName": "Playground", "isOfficial": 1},
    {"roomKey": "official_1", "roomName": "Starter House", "isOfficial": 1},
    {"roomKey": "official_2", "roomName": "Blue House", "isOfficial": 1},
    {"roomKey": "researchFacility", "roomName": "Research Facility", "isOfficial": 1},
    {"roomKey": "halloween_1", "roomName": "Spider City", "isOfficial": 1},
    {"roomKey": "official_5", "roomName": "Outpost", "isOfficial": 1},
    {"roomKey": "official_4b", "roomName": "Fortress", "isOfficial": 1},
    {"roomKey": "bonus_6b", "roomName": "Boat Battle", "isOfficial": 1},
    {"roomKey": "official_9", "roomName": "Red vs Blue", "isOfficial": 1},
    {"roomKey": "official_10", "roomName": "Battlecrafts", "isOfficial": 1},
    {"roomKey": "official_8", "roomName": "Battle Arena", "isOfficial": 1},
    {"roomKey": "wild_0", "roomName": "Wilds 1", "isOfficial": 0, "isCreative": 1},
    {"roomKey": "wild_1_beach", "roomName": "Wilds 2", "isOfficial": 0, "isCreative": 1},
    {"roomKey": "wild_2_snow", "roomName": "Wilds 3", "isOfficial": 0, "isCreative": 1},
    {"roomKey": "bam_winner", "roomName": "Build-A-Map Winner", "isOfficial": 0},
    {"roomKey": "bam_1", "roomName": "Build-A-Map Finalist 2", "isOfficial": 0},
    {"roomKey": "bam_0", "roomName": "Build-A-Map Finalist 1", "isOfficial": 0},
    {"roomKey": "bam_2", "roomName": "Build-A-Map Finalist 3", "isOfficial": 0},
    {"roomKey": "bam_finalist_lobby", "roomName": "All Finalists", "isOfficial": 0},
]

# Default global state values
DEFAULT_GLOBALS = [
    {"key": "bamState", "data": 0},
    {"key": "activeBundleKey", "data": "bundle_commando"},
    {"key": "wishlistState", "data": 0},

    {
        "key": "announcementData",
        "data": [
            {
                "message": "Welcome to the Yeeps Private Server!\n\nThis is a custom backend replacing the original AWS infrastructure.",
                "activeTime": "2026 01/01 00:00:00"
            }
        ]
    },
]

# Default bundle data
DEFAULT_BUNDLES = [
    {
        "bundleKey": "bundle_commando",
        "displayName": "Commando Bundle",
        "startTimestamp": "2025 01/23 17:00:00",
        "endTimestamp": "2025 02/20 07:00:00",
        "itemKeys": ["commando", "explosiveGrenade_frag"],
        "currencyAmount": 10000,
        "centCost": 2999,
        "minVersion": "1.0.0",
    },
]

# Full geometry/detail field set every room needs -- without these, the
# client's RoomDatabase deserializer throws (missing fields => null =>
# ArgumentNullException on the client). Applied to every room in
# OFFICIAL_ROOMS before seeding, so no room is left half-populated.
DEFAULT_ROOM_GEOMETRY = {
    "dimensions": [80, 80, 80],
    "floorDepth": 0,
    "lobbyDirection": 1,
    "door_up": {"block": 0, "position": [0, 0]},
    "door_right": {"block": 0, "position": [0, 0]},
    "door_down": {"block": 0, "position": [0, 0]},
    "door_left": {"block": 0, "position": [0, 0]},
    "isCreative": 0,
    # Locking the mode stops the client letting you switch into build
    # mode, so leave it unlocked -- re-seeding with 1 here would undo
    # the unlocked rooms in the live data.
    "isModeLocked": 0,
    "isAutoCycling": 0,
    "autoCycleTimestamp": 0,
    "noSave": 0,
    "isDevLocked": 0,
    "liquidType": 0,
    "liquidColor": "",
    "liquidLevel": 0,
    "itemRestrictionsIsWhiteList": 0,
    "itemRestrictionsGroupKeys": [],
    "themeKey": "",
    "noPaste": 0,
    "previousEditedSessionID": 1,
    "previousMapProperties": {},
    # Matchmaking segmentation splits players into separate Photon instances
    # by segment (the client logs "You'll be matched with PMAs / normal /
    # new players in your next room"), and a segment instance is not the
    # room's main instance -- so building gets blocked there. Set to 1 to
    # block segmentation entirely and keep everyone in the canonical
    # instance, which is what a private server wants.
    "blockMatchmakingSegmentation": 1,
    # creativeSessionID is the "main instance" check specifically for
    # isCreative rooms (separate from lastEditedSessionID, which covers
    # normal official rooms) -- a real captured room record shows it kept
    # in lockstep with lastEditedSessionID. Missing entirely, this is why
    # only isCreative rooms (Wilds) rejected building as "not main instance".
    "creativeSessionID": 1,
    "isSaveLocked": 0,
    "terrainDataVersion": 1,
    "lastEditedTerrainSessionID": 1,
    "previousEditedTerrainSessionID": 1,
    "overrideSaveLockTimestamp": "1970 01/01 00:00:00",
    "customLightingKey": "",
    "lastRoomDataEditorSessionID": 1,
}

# Room-specific overrides on top of DEFAULT_ROOM_GEOMETRY (from GetItem
# responses for official_1; everything else uses the defaults above).
ROOM_DETAILS = [
    {
        "roomKey": "official_1",
        "roomName": "Starter House",
        "isOfficial": 1,
        "door_up": {"block": 0, "position": [26, 0]},
        "door_down": {"block": 0, "position": [-5, 8]},
        "lastEditedSessionID": 58,
        "roomDataVersion": 1,
    },
    {
        "roomKey": "tutorial",
        "roomName": "Tutorial",
        "isOfficial": 1,
        "lastEditedSessionID": 1,
        "roomDataVersion": 1,
        "lastValidSaveOffset": 0,
    },
]


async def seed_database():
    """
    Seed the MongoDB database with initial game data.
    Only inserts into empty collections (safe to run multiple times).
    """
    print("[seed] Checking database...")

    seeded_any = False

    # ── Seed world map ──
    if WORLD_MAP_TEAMS1:
        print("[seed] Syncing room_maps (world grid)...")
        await room_maps.update_one(
            {"versionKey": WORLD_MAP_TEAMS1.get("versionKey", "beta4")},
            {"$set": WORLD_MAP_TEAMS1},
            upsert=True,
        )
        print(f"[seed]   -> Upserted primary world map ({WORLD_MAP_TEAMS1.get('versionKey', 'beta4')})")

        seeded_any = True
    else:
        print("[seed] Skipping room_maps (no data found in default_room_map.json)")


    # ── Seed rooms ──
    # Always upsert room docs so missing numeric fields don't stay NULL.
    print("[seed] Syncing rooms...")
    for room in OFFICIAL_ROOMS:
        full_room = {**DEFAULT_ROOM_GEOMETRY, **room}
        full_room.setdefault("lastEditedSessionID", 1)
        full_room.setdefault("roomDataVersion", 1)
        full_room.setdefault("lastValidSaveOffset", 0)
        await rooms.update_one(
            {"roomKey": full_room["roomKey"]},
            {"$set": full_room},
            upsert=True,
        )

    # Insert detailed room data (merge into existing)
    for detail in ROOM_DETAILS:
        await rooms.update_one(
            {"roomKey": detail["roomKey"]},
            {"$set": detail},
            upsert=True,
        )

    seeded_any = True
    print(f"[seed]   -> Upserted {len(OFFICIAL_ROOMS)} rooms (+details)")

    # ── Seed globals ──
    if await globals_col.count_documents({}) == 0:
        print("[seed] Seeding globals...")
        for g in DEFAULT_GLOBALS:
            await globals_col.insert_one(g)
        seeded_any = True
        print(f"[seed]   -> Inserted {len(DEFAULT_GLOBALS)} global keys")

    # ── Seed bundles ──
    if await bundles.count_documents({}) == 0:
        print("[seed] Seeding bundles...")
        for b in DEFAULT_BUNDLES:
            await bundles.insert_one(b)
        seeded_any = True
        print(f"[seed]   -> Inserted {len(DEFAULT_BUNDLES)} bundles")
    else:
        # Keep the active bundle aligned with known-good captured payload.
        await bundles.update_one(
            {"bundleKey": "bundle_commando"},
            {"$set": {
                "endTimestamp": "2025 02/20 07:00:00",
                "minVersion": "1.0.0",
            }},
            upsert=False,
        )

    # ── Parse dynamo.txt for additional data ──
    if DYNAMO_LOG.exists():
        parsed = parse_dynamo_log(DYNAMO_LOG)
        if parsed:
            # Insert any additional room data we found
            for item in parsed.get("rooms", []):
                room_key = item.get("roomKey", "")
                if room_key:
                    existing = await rooms.find_one({"roomKey": room_key})
                    if existing:
                        # Merge new fields into existing
                        await rooms.update_one(
                            {"roomKey": room_key},
                            {"$set": item},
                        )
                    else:
                        await rooms.insert_one(item)

            # Insert map data
            for item in parsed.get("maps", []):
                map_key = item.get("mapKey", "")
                if map_key:
                    await maps.update_one(
                        {"mapKey": map_key},
                        {"$set": item},
                        upsert=True,
                    )

            dynamo_count = sum(len(v) for v in parsed.values())
            if dynamo_count > 0:
                print(f"[seed] Parsed {dynamo_count} additional items from dynamo.txt")
                seeded_any = True

    # ── Seed maps from save.txt ──
    save_txt_path = Path("save.txt")
    if save_txt_path.exists():
        try:
            save_payload = json.loads(save_txt_path.read_text(encoding="utf-8"))
            if "Item" in save_payload:
                item_doc = from_dynamo({"M": save_payload["Item"]})
                # Seed for both specific save and current map fallback
                for map_key in ["official_1$map_58_save0", "official_1$current"]:
                    item_doc["mapKey"] = map_key
                    await maps.update_one(
                        {"mapKey": map_key},
                        {"$set": item_doc},
                        upsert=True
                    )
                print("[seed] Seeded official_1 maps from save.txt")
                seeded_any = True
        except Exception as e:
            print(f"[seed] Failed to seed from save.txt: {e}")

    if not seeded_any:
        print("[seed] Database already seeded, skipping")
    else:
        print("[seed] [OK] Database seeding complete")
