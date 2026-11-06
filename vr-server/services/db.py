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
