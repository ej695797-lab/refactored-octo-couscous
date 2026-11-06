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


async def _staff_check(body: dict) -> str:
    """"" if the caller really is staff on their own headset, else why not.

    The game sends accountID in the request body, so anyone can CLAIM to be a
    staff account through the raw API (2026-10-10: a script logged in as an
    owner account and spammed bans). A ban/warn therefore needs the staff
    account's bound headset key and the current game build, not just the id.
    """
    mod = body.get("accountID", "")
    if mod not in MOD_STAFF:
        return "not staff"
    from services import device_auth
    from services.build_gate import current_status
    key = body.get("_deviceKey", "")
    bound = await device_auth._keys_of(mod)
    if not bound:
        return "staff account has no headset bound yet"
    if not device_auth.valid_key(key) or device_auth.key_hash(key) not in bound:
        return "not from the staff member's headset"
    # The game only tags its login with the build, not these calls, so use the
    # build the staff account last logged in with.
    doc = await accounts.find_one({"accountID": mod}, {"_id": 0, "clientBuildTag": 1, "clientBuildStatus": 1})
    if current_status(doc) != "ok":
        return "not on the current game build"
    return ""


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
    why = await _staff_check(body) if target and target != mod else "no target"
    if why:
        if target:
            print(f"[mod] IGNORED ban by {mod} on {target} ({why}) ip={body.get('_clientIP', '')}")
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
    why = await _staff_check(body) if target and target != mod else "no target"
    if why:
        if target:
            print(f"[mod] IGNORED warn by {mod} on {target} ({why}) ip={body.get('_clientIP', '')}")
        return {"success": 1}
    reason = (body.get("banReason") or "").strip() or "Warned by staff"
    await accounts.update_one({"accountID": target}, {
        "$set": {"hasPendingWarning": 1, "warnReason": reason, "warnCategory": "warned",
                 "shouldForceRefresh": 1},
        "$inc": {"warnCount": 1},
    })
    print(f"[mod] {mod} WARNED {target}: {reason}")
    return {"success": 1}
