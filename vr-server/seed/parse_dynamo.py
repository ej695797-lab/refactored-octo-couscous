"""
Parse captured dynamo.txt log and extract seed data for MongoDB.
Extracts:
  - World map grid (G2_RoomMap / teams1)
  - Room names and metadata (G2_Rooms)
  - Global state flags (G2_Globals)
  - Bundle data (G2_Bundles)
  - Account data (G2_Accounts)
  - Map save data (G2_Maps)
  - Player statuses (G2_PlayerStatuses)
"""

import ast
import json
import re
from pathlib import Path


def parse_dynamo_log(log_path: Path) -> dict:
    """
    Parse the dynamo.txt log file and extract all response data.
    Returns a dict of collection_name → list of documents to insert.
    """
    if not log_path.exists():
        print(f"[seed] dynamo.txt not found at {log_path}")
        return {}

    text = log_path.read_text(errors="replace")
    lines = text.split("\n")

    # Collect all DynamoDB responses
    all_items = {
        "room_maps": [],   # G2_RoomMap
        "rooms": [],       # G2_Rooms
        "globals": [],     # G2_Globals
        "bundles": [],     # G2_Bundles
        "accounts": [],    # G2_Accounts
        "maps": [],        # G2_Maps
        "player_statuses": [],  # G2_PlayerStatuses
    }

    # Track what we've already seen to avoid duplicates
    seen_keys = {k: set() for k in all_items}

    for i, line in enumerate(lines):
        m_resp = re.match(r"DynamoDB response:\s*(\{.*\})\s*$", line)
        if not m_resp:
            continue

        try:
            payload = ast.literal_eval(m_resp.group(1))
        except Exception:
            continue

        if not isinstance(payload, dict):
            continue

        # Remove metadata
        payload.pop("ResponseMetadata", None)

        # Handle GetItem responses
        if "Item" in payload:
            item = payload["Item"]
            _extract_item(item, all_items, seen_keys, lines, i)

        # Handle BatchGetItem responses
        if "Responses" in payload:
            for table_name, items in payload["Responses"].items():
                for item in items:
                    _extract_batch_item(table_name, item, all_items, seen_keys)

    return all_items


def _from_dynamo(typed_val):
    """Convert a DynamoDB-typed value to plain Python."""
    if not isinstance(typed_val, dict):
        return typed_val

    if "S" in typed_val:
        return typed_val["S"]
    if "N" in typed_val:
        val = typed_val["N"]
        try:
            return int(val)
        except (ValueError, TypeError):
            try:
                return float(val)
            except (ValueError, TypeError):
                return 0
    if "BOOL" in typed_val:
        return typed_val["BOOL"]
    if "NULL" in typed_val:
        return None
    if "L" in typed_val:
        return [_from_dynamo(v) for v in typed_val["L"]]
    if "M" in typed_val:
        return {k: _from_dynamo(v) for k, v in typed_val["M"].items()}
    if "SS" in typed_val:
        return list(typed_val["SS"])
    if "NS" in typed_val:
        return [int(v) if "." not in v else float(v) for v in typed_val["NS"]]
    return None


def _unwrap_item(dynamo_item: dict) -> dict:
    """Convert a full DynamoDB item (all typed values) to plain dict."""
    return {k: _from_dynamo(v) for k, v in dynamo_item.items()}


def _extract_item(item: dict, all_items: dict, seen_keys: dict, lines: list, line_idx: int):
    """Extract data from a GetItem response by looking at nearby request context."""
    plain = _unwrap_item(item)

    # Look backwards for the request to determine the table
    for j in range(line_idx - 1, max(line_idx - 20, 0), -1):
        req_match = re.match(r"Original Incoming Request Data: b['\"](.*)['\"]\s*$", lines[j])
        if req_match:
            try:
                req_body = json.loads(req_match.group(1))
                table = req_body.get("TableName", "")
                _store_item(table, plain, all_items, seen_keys)
            except Exception:
                pass
            break


def _extract_batch_item(table_name: str, item: dict, all_items: dict, seen_keys: dict):
    """Extract data from a BatchGetItem response item."""
    plain = _unwrap_item(item)
    _store_item(table_name, plain, all_items, seen_keys)


def _store_item(table_name: str, plain: dict, all_items: dict, seen_keys: dict):
    """Store a plain item in the appropriate collection bucket."""
    collection_map = {
        "G2_RoomMap": ("room_maps", "versionKey"),
        "G2_Rooms": ("rooms", "roomKey"),
        "G2_Globals": ("globals", "key"),
        "G2_Bundles": ("bundles", "bundleKey"),
        "G2_Accounts": ("accounts", "accountID"),
        "G2_Maps": ("maps", "mapKey"),
        "G2_PlayerStatuses": ("player_statuses", "accountID"),
    }

    mapping = collection_map.get(table_name)
    if not mapping:
        return

    col_name, key_field = mapping

    # For globals, we need to reconstruct the key from the request
    # since GetItem responses don't include the key in the item
    key_val = plain.get(key_field, "")

    # Skip if we've already seen this key (deduplicate)
    if key_val and key_val in seen_keys[col_name]:
        return

    if key_val:
        seen_keys[col_name].add(key_val)

    all_items[col_name].append(plain)
