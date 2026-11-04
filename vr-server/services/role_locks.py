"""
Role-locked patterns: special blocks only accounts with a given server-only role
may own (and therefore place). Applied to every place the client reads its
account: the login reply (handlers/login.py) and DynamoDB G2_Accounts reads
(services/dynamo_ops.py). Server-only roles aren't game roles, so they are
stripped from the roleKeys the client sees.

Grant: add the role to the account's roleKeys (editor / Mongo), e.g. "blaze".
"""

ROLE_LOCKED_PATTERNS = {"1x5x2_stuffedTextBlaze": "blaze",
                        "3x2x1_blazeScreenRole": "blaze",
                        # FrostOverYeti (owner 2026-10-17: only Blaze + Staff Member A own it).
                        # The live APK swaps these four cosmetics to the Frost bundle.
                        "yetiHead": "frost", "yetiBody": "frost", "yetiArms": "frost",
                        "alienBundleBackpack": "frost",
                        # Frost Over backpack (owner 2026-10-18): the live APK (live54+) swaps
                        # the lifeguard backpack to frostpack.bundle. Only Staff Member A and the
                        # people he allows (role "frostpack").
                        "lifeguardBackpack": "frostpack",
                        # (Icy Magnifying Glass dropped by the owner 2026-10-18: both normal glasses are owned by everyone again)
                        # (dev trading cards dropped by the owner 2026-10-18)
                        }
# Roles hidden from the client. "blaze" is NOT hidden any more: the BLAZE blocks
# carry MapObject.requiredRoleKey = "blaze" (APK 131b4+), and the game checks
# that against the roleKeys it sees, so it must see the role.
SERVER_ONLY_ROLES: set = {"frost", "frostpack", "icyglass", "carddev"}  # icyglass/carddev: dropped features, still hidden    # not game roles: hidden from the client
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


def apply(owned, roles) -> tuple[list, list]:
    """(ownedPatterns, roleKeys) as the client should see them."""
    roles = list(roles or [])
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


# Cosmetic bundles unlock every piece in them, so the Frost pieces' bundles are
# role-locked too (bundle_yeti: hood/suit/sleeves; bundle_alien: the backpack).
ROLE_LOCKED_BUNDLES = {"bundle_yeti": "frost", "bundle_alien": "frost"}


def apply_bundles(bundles, roles) -> list:
    """ownedBundles as the client should see them (locked bundles only for role holders)."""
    have = set(roles or [])
    out = [b for b in (bundles or []) if ROLE_LOCKED_BUNDLES.get(b) in (None, *have)]
    for b, role in ROLE_LOCKED_BUNDLES.items():
        if role in have and b not in out:
            out.append(b)
    return out
