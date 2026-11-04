"""
MongoDB connection manager.
Uses motor (async pymongo) for FastAPI compatibility.
"""

import motor.motor_asyncio
from config import MONGO_URI, DB_NAME

client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = client[DB_NAME]

# ─── Collection accessors ────────────────────────────────────────────

accounts       = db["accounts"]        # Key: accountID
rooms          = db["rooms"]           # Key: roomKey
room_maps      = db["room_maps"]       # Key: versionKey
maps           = db["maps"]           # Key: mapKey
player_statuses = db["player_statuses"] # Key: accountID
globals_col    = db["globals"]         # Key: key
bundles        = db["bundles"]         # Key: bundleKey
debug_logs     = db["debug_logs"]      # client g2_logDebug payloads
imported_maps  = db["imported_maps"]   # Key: roomKey — editor-imported real maps

# DynamoDB TableName → MongoDB collection mapping
TABLE_MAP = {
    "G2_Accounts":       accounts,
    "G2_Rooms":          rooms,
    "G2_RoomMap":        room_maps,
    "G2_Maps":           maps,
    "G2_PlayerStatuses": player_statuses,
    "G2_Globals":        globals_col,
    "G2_Bundles":        bundles,
}

# DynamoDB TableName → primary key field name
TABLE_KEY_FIELDS = {
    "G2_Accounts":       "accountID",
    "G2_Rooms":          "roomKey",
    "G2_RoomMap":        "versionKey",
    "G2_Maps":           "mapKey",
    "G2_PlayerStatuses": "accountID",
    "G2_Globals":        "key",
    "G2_Bundles":        "bundleKey",
}


def get_collection(table_name: str):
    """Get the MongoDB collection for a DynamoDB table name."""
    return TABLE_MAP.get(table_name)


def get_key_field(table_name: str) -> str:
    """Get the primary key field name for a DynamoDB table name."""
    return TABLE_KEY_FIELDS.get(table_name, "")


# ---- Shared private-world maps (owner 2026-10-17) ---------------------------
# A private world (room keys "p_...") has ONE copy, shared by the 1.31 and live
# servers: it lives in the 1.31 DB (CARRYOVER_DB, Yeeps119) "maps" collection,
# which the 1.31 server already uses. Before this, live kept its own copy and
# ignored 1.31 edits once a player had saved on live ("updating their pw only
# updates in 1.0"), and the duplicates wasted DB space.
import os as _os_shared


def is_pw_key(key) -> bool:
    return isinstance(key, str) and key.startswith("p_")


def maps_for(key):
    """The maps collection to read/write for a room key or map key."""
    cdb = _os_shared.getenv("CARRYOVER_DB", "")
    if cdb and is_pw_key(key):
        return client[cdb]["maps"]
    return maps
