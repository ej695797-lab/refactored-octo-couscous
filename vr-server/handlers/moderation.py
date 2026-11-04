"""
In-game moderation: g2_moderatorBanAccount / g2_moderatorWarnAccount.

The client's mod menu calls these on the server, but every player has the
`moderator` role from the default loadout, so WITHOUT a gate any player could
ban anyone. We only apply a ban/warn when the acting account is real staff
(MOD_STAFF allowlist); everyone else gets a silent no-op success.

A ban sets `adminBlocked` (the existing block switch login.py already serves as
permabanned+isolated) plus `banExpireTimestamp` for timed bans (login
auto-clears it when it passes). A warn sets `hasPendingWarning` + `warnReason`,
which login serves once then clears.
"""

import os
from datetime import datetime, timedelta, timezone

from services.db import accounts

# Who can actually ban/warn. ONLY these accounts; every other player's ban/warn
# is a silent no-op (they all have the moderator role from the default loadout).
# Override via env MOD_STAFF (comma-separated IDs).
MOD_STAFF = set(filter(None, (
    os.getenv("MOD_STAFF") or "o_OWNER0000000001,o_PLAYER0000000003,o_STAFF00000000002"
).replace(" ", "").split(",")))


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y %m/%d %H:%M:%S")


# Remap the in-game ban-length options to shorter real durations (hours).
# The client sends the chosen length in hours via targetBanDurationHours:
#   3 days = 72h, 7 days = 168h, 30 days = 720h.
# Owner wants: 3-day -> 1 hour, 7-day -> 1 day (24h), 30-day -> 3 days (72h).
BAN_LENGTH_REMAP = {72: 1, 168: 24, 720: 72}


async def handle_moderator_ban_account(body: dict) -> dict:
    mod = body.get("accountID", "")
    target = body.get("targetAccountID", "")
    if mod not in MOD_STAFF or not target or target == mod:
        if target:
            print(f"[mod] IGNORED ban by non-staff {mod} on {target}")
        return {"success": 1}   # menu shows for everyone; only staff actually bans
    # The client's field is targetBanDurationHours (hours); fall back to other
    # likely names. 0 / missing = permanent.
    raw = (body.get("targetBanDurationHours")
           or body.get("banDurationHours")
           or body.get("banLength") or 0)
    try:
        length = int(raw or 0)
    except Exception:
        length = 0
    mapped = BAN_LENGTH_REMAP.get(length, length)   # remap known options; else as-is
    reason = (body.get("banReason") or "").strip() or "Banned by staff"
    now = datetime.now(timezone.utc)
    expire = _fmt(now + timedelta(hours=mapped)) if mapped > 0 else ""   # "" = permanent
    print(f"[mod] ban raw={raw} -> {length}h -> remapped {mapped}h")
    length = mapped
    await accounts.update_one({"accountID": target}, {
        "$set": {"adminBlocked": 1, "blockReason": reason, "banReason": reason,
                 "banCategory": "blocked", "banExpireTimestamp": expire,
                 "shouldForceRefresh": 1},
        "$inc": {"banCount": 1},
    })
    print(f"[mod] {mod} BANNED {target} for {length}h: {reason}")
    return {"success": 1}


async def handle_moderator_warn_account(body: dict) -> dict:
    mod = body.get("accountID", "")
    target = body.get("targetAccountID", "")
    if mod not in MOD_STAFF or not target or target == mod:
        if target:
            print(f"[mod] IGNORED warn by non-staff {mod} on {target}")
        return {"success": 1}
    reason = (body.get("banReason") or "").strip() or "Warned by staff"
    await accounts.update_one({"accountID": target}, {
        "$set": {"hasPendingWarning": 1, "warnReason": reason, "warnCategory": "warned",
                 "shouldForceRefresh": 1},
        "$inc": {"warnCount": 1},
    })
    print(f"[mod] {mod} WARNED {target}: {reason}")
    return {"success": 1}
