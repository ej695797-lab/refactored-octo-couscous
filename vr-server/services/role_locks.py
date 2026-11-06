"""
Role-locked patterns: special blocks only accounts with a given server-only role
may own (and therefore place). Applied to every place the client reads its
account: the login reply (handlers/login.py) and DynamoDB G2_Accounts reads
(services/dynamo_ops.py). Server-only roles aren't game roles, so they are
stripped from the roleKeys the client sees.

Grant: add the role to the account's roleKeys (editor / Mongo), e.g. "blaze".
"""

ROLE_LOCKED_PATTERNS = {"1x5x2_stuffedTextBlaze": "blaze",
                        "3x2x1_blazeScreenRole": "blaze"}
# Roles hidden from the client. "blaze" is NOT hidden any more: the BLAZE blocks
# carry MapObject.requiredRoleKey = "blaze" (APK 131b4+), and the game checks
# that against the roleKeys it sees, so it must see the role.
SERVER_ONLY_ROLES: set = set()
# Custom blocks every account owns (creative build menu). Kept out of the
# techweb list so survival players can't craft them.
# 3x3x1_blazeScreen REMOVED 2026-10-06: a placed 3x3 Screen in a big public room
# (official_1) crashed every client that loaded the room (SIGSEGV after the
# screen-material swap). Not owned by anyone until the APK is fixed.
ALWAYS_OWNED_PATTERNS: list = []
# Never served to anyone, even if stored on an account.
DISABLED_PATTERNS = {"3x3x1_blazeScreen",
                     # desert decorations with no mesh in 1.31 ("Could not find map
                     # object data with key ...") - placing them logs errors
                     "decoration_desert_arch0", "decoration_desert_pillar0",
                     "decoration_desert_pillar1", "decoration_desert_rock0",
                     "decoration_desert_rock1", "decoration_desert_cactus0",
                     "decoration_desert_cactus1", "decoration_desert_cactus2",
                     "decoration_desert_cactus3", "decoration_desert_cactus4",
                     "16x16x4_desertRock", "16x16x8_desertRock",
                     "2x2x2_desertRock", "8x8x4_desertRock"}


# Who may hold "blaze" (owner's rule, 2026-10-11: only the owner and StaffMemberA,
# and only the owner can give it). Stored in globals {"key": "blaze_holders",
# "ids": [...]}; the owner's editor edits keep it in step. Anyone else with
# "blaze" on their account has it stripped (login + every account read).
BLAZE_ROLE = "blaze"
_DEFAULT_BLAZE_HOLDERS = {"o_OWNER0000000001", "o_STAFF00000000002"}
_blaze = {"ids": set(_DEFAULT_BLAZE_HOLDERS), "ts": 0.0}


async def load_blaze_holders() -> set:
    import time
    if time.monotonic() - _blaze["ts"] > 30:
        try:
            from services.db import globals_col
            doc = await globals_col.find_one({"key": "blaze_holders"}, {"_id": 0, "ids": 1})
            if doc and isinstance(doc.get("ids"), list):
                _blaze["ids"] = set(doc["ids"])
        except Exception:
            pass
        _blaze["ts"] = time.monotonic()
    return _blaze["ids"]


async def set_blaze_holder(account_id: str, on: bool) -> None:
    from services.db import globals_col
    ids = set(await load_blaze_holders())
    (ids.add if on else ids.discard)(account_id)
    await globals_col.update_one({"key": "blaze_holders"}, {"$set": {"key": "blaze_holders", "ids": sorted(ids)}},
                                 upsert=True)
    _blaze["ids"], _blaze["ts"] = ids, 0.0


def is_blaze_holder(account_id) -> bool:
    return account_id in _blaze["ids"]


def apply(owned, roles, account_id=None) -> tuple[list, list]:
    """(ownedPatterns, roleKeys) as the client should see them."""
    roles = list(roles or [])
    if account_id is not None and BLAZE_ROLE in roles and not is_blaze_holder(account_id):
        roles = [r for r in roles if r != BLAZE_ROLE]
    owned = list(owned or [])
    have = set(roles)
    owned = [p for p in owned if p not in DISABLED_PATTERNS]
    for pattern in ALWAYS_OWNED_PATTERNS:
        if pattern not in owned:
            owned.append(pattern)
    for pattern, role in ROLE_LOCKED_PATTERNS.items():
        if role in have:
            if pattern not in owned:
                owned.append(pattern)
        elif pattern in owned:
            owned.remove(pattern)
    return owned, [r for r in roles if r not in SERVER_ONLY_ROLES]
