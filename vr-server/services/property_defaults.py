"""
Default value generator for propertiesToGet fields.
Mirrors the _default_for_property logic from server.py.
"""


def default_for_property(name: str):
    """Best-guess default value for a propertiesToGet field name."""
    n = name.lower()

    # Known account flags expected as numeric toggle fields.
    if n == "shouldforcerefresh":
        return 0

    # 1.31 room field. The client reads the attribute's S and TryParses it as a
    # date (falls back to a default on failure), so serve the date-string form
    # like the other Yeeps timestamps rather than the generic int 0.
    if n == "goingawaytimestamp":
        return "1970 01/01 00:00:00"

    # 1.31 G2_Rooms field the client reads with its GetInt (Int32.Parse of the
    # attribute's N). The generic fallback below made it "" (an S), so N was
    # null -> "ArgumentNullException: Parameter name: s" -> spawn room fails.
    if n == "randomization_type":
        return 0

    # 1.31 G2_AccountStats map (roomKey -> visits); never null.
    if n == "roomvisits_public":
        return {}

    # Door-shaped objects
    if n.startswith("door_"):
        return {"block": 0, "position": [0, 0]}

    # Boolean-ish ints
    if n.startswith(("is", "has", "no", "block", "skip", "first")):
        return 0

    # Counters / versions
    if n.endswith(("version", "depth", "level", "count", "id", "direction",
                   "sessionid", "timestamp", "offset")):
        return 0

    # Lists
    if n.endswith(("keys", "ids", "patterns", "cosmetics", "bundles",
                   "messages", "events")):
        return []

    # Maps / dicts
    if n == "savedata":
        return ""
    if n.endswith(("data", "states", "map")):
        return {}

    # Names / strings
    if any(s in n for s in ("name", "key", "color", "message", "reason", "code")):
        return ""

    # Dimensions
    if "dimension" in n:
        return [100, 100, 100]

    # Numeric defaults
    if "currency" in n or "amount" in n:
        return 0

    return ""
