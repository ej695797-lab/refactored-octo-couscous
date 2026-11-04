"""
Account fields that must never leave the server, and must never be written by a
client.

The game API has no per-player authentication (login and the DynamoDB calls
only carry an accountID, and every client shares the same guest AWS creds), so
anything served for an account is effectively public: anyone who knows an
accountID (it's visible in-game) could read it. These fields are therefore
stripped from every account read (Dynamo GetItem/BatchGetItem, login,
g2_fetchAccountDatas) and dropped from every client account write. The server
itself still reads/writes them in Mongo directly (IP bans, /link, the editor).
"""

PRIVATE_ACCOUNT_FIELDS = frozenset({
    # network / identity
    "lastIP", "recentIPs",
    "discordID", "discordUsername",
    "oculusID",
    # mobile login secrets (/link used to copy players' REAL Yeeps mobile code)
    "mobileCode", "mobilePassword", "mobileID",
    # stored plaintext credentials (the login response sets fresh copies)
    "accessKey", "secretKey", "sessionToken",
    # anti-tamper / moderation internals
    "attestHash", "clientBuildTag", "clientBuildStatus", "clientBuildSeenAt", "lastClientVersion",
    "shadowBanned", "isBot", "importedFrom",
    # headset binding (services/device_auth.py)
    "deviceKeys", "deviceKeyBoundAt", "deviceBindOpen",
})
# NOT private on purpose: e_accessKey / e_secretKey are the SAME shared guest
# creds for every player (encrypted with the client's baked key) and the 1.31
# client needs them from login to sign its next calls - stripping them made
# every login hang right after "questLogIntoAccount" (2026-10-09). The
# encrypted*/oculusIDHistory/nonceHistory fields are game fields that are empty
# on this server; the account parser expects them present.

# What the client gets instead when it asks for one of these (so nothing is
# missing from a response the client expects to contain the key).
_SAFE_VALUES = {"mobileCode": "0000"}


def public_view(doc: dict) -> dict:
    """The account without its private fields."""
    if not isinstance(doc, dict):
        return doc
    return {k: v for k, v in doc.items() if k not in PRIVATE_ACCOUNT_FIELDS}


def safe_value(field: str, default):
    return _SAFE_VALUES.get(field, default)


def strip_private_writes(fields: dict) -> list:
    """Drop private fields from a client write; returns what was dropped."""
    if not isinstance(fields, dict):
        return []
    hit = [k for k in list(fields) if k in PRIVATE_ACCOUNT_FIELDS]
    for k in hit:
        fields.pop(k, None)
    return hit
