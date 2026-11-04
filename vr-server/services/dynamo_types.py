"""
DynamoDB ↔ MongoDB type translation layer.

DynamoDB uses typed attribute wrappers like {"S": "hello"}, {"N": "42"}.
MongoDB stores plain JSON. This module converts between the two formats.
"""


def from_dynamo(typed_val: dict):
    """Convert a DynamoDB-typed value to a plain Python value for MongoDB storage."""
    if not isinstance(typed_val, dict):
        return typed_val

    if "S" in typed_val:
        return typed_val["S"]
    if "N" in typed_val:
        val = typed_val["N"]
        # Try int first, fall back to float
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
        return [from_dynamo(v) for v in typed_val["L"]]
    if "M" in typed_val:
        return {k: from_dynamo(v) for k, v in typed_val["M"].items()}
    if "SS" in typed_val:
        return list(typed_val["SS"])
    if "NS" in typed_val:
        return [int(v) if "." not in v else float(v) for v in typed_val["NS"]]

    return None


def to_dynamo(value):
    """Convert a plain Python value to DynamoDB-typed format for wire responses."""
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, float):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, list):
        return {"L": [to_dynamo(v) for v in value]}
    if isinstance(value, dict):
        return {"M": {k: to_dynamo(v) for k, v in value.items()}}
    return {"S": str(value)}


def unwrap_key(key_dict: dict) -> dict:
    """
    Convert a DynamoDB Key dict into a plain MongoDB query.
    e.g. {"accountID": {"S": "o_123"}} → {"accountID": "o_123"}
    """
    result = {}
    for field, typed_val in key_dict.items():
        result[field] = from_dynamo(typed_val)
    return result


def unwrap_attribute_updates(updates: dict) -> dict:
    """
    Convert DynamoDB AttributeUpdates to a plain MongoDB $set dict.
    e.g. {"roomKey": {"Action": "PUT", "Value": {"S": "official_1"}}}
    → {"roomKey": "official_1"}
    """
    result = {}
    for field, update in updates.items():
        action = update.get("Action", "PUT")
        if action == "PUT":
            result[field] = from_dynamo(update.get("Value", {}))
        elif action == "DELETE":
            pass  # handled separately if needed
    return result


def wrap_item(doc: dict, attrs_to_get: list = None) -> dict:
    """
    Convert a MongoDB document to a DynamoDB Item response.
    Filters to attrs_to_get if provided. Excludes _id.
    """
    if doc is None:
        return {}

    item = {}
    source = doc

    if attrs_to_get:
        for attr in attrs_to_get:
            if attr in source:
                item[attr] = to_dynamo(source[attr])
            else:
                item[attr] = {"NULL": True}
    else:
        for k, v in source.items():
            if k == "_id":
                continue
            item[k] = to_dynamo(v)

    return item
