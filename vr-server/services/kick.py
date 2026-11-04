"""
Kick (boot) players: one account, or everyone currently online.

A kick is a ONE-SHOT boot, not a ban. It sets `pendingKick` + `kickReason` and
`shouldForceRefresh`. The client polls shouldForceRefresh on G2_Accounts (1.31
does this every ~45 s), re-pulls its account, and login.py serves that one
re-pull isolated (short timed suspension showing the reason), then clears the
flag. The player can relaunch and rejoin immediately.

Callers: the editor API (/editor/api/player/{id}/kick, /editor/api/kick_all).
Build any other UI (Discord commands, buttons) on these functions.
"""

from datetime import datetime, timezone

from services.db import accounts, player_statuses

DEFAULT_KICK_REASON = "You have been kicked (just rejoin). Dont ping Blaze."


def kick_message(reason: str = "") -> str:
    """Every kick says it's a kick (and to just rejoin); a staff reason is appended."""
    reason = (reason or "").strip()
    return DEFAULT_KICK_REASON + (f" Reason: {reason}" if reason else "")


async def online_account_ids() -> list[str]:
    """Accounts whose presence (G2_PlayerStatuses expireTimestamp) is still live.
    Same rule as the Discord /online command."""
    now = datetime.now(timezone.utc)
    ids = []
    async for s in player_statuses.find({}, {"_id": 0, "accountID": 1, "expireTimestamp": 1}):
        try:
            exp = datetime.strptime(s.get("expireTimestamp") or "",
                                    "%Y %m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if exp > now and s.get("accountID"):
            ids.append(s["accountID"])
    return ids


async def kick_account(account_id: str, reason: str = "") -> bool:
    """Boot one account once. Returns False if the account doesn't exist."""
    reason = kick_message(reason)
    res = await accounts.update_one(
        {"accountID": account_id},
        {"$set": {"pendingKick": 1, "kickReason": reason, "shouldForceRefresh": 1}})
    return res.matched_count > 0


async def kick_all(reason: str = "", exclude: list[str] | None = None) -> list[str]:
    """Boot everyone currently online (minus `exclude`). Returns the kicked IDs."""
    skip = set(exclude or [])
    ids = [a for a in await online_account_ids() if a not in skip]
    if ids:
        reason = kick_message(reason)
        await accounts.update_many(
            {"accountID": {"$in": ids}},
            {"$set": {"pendingKick": 1, "kickReason": reason, "shouldForceRefresh": 1}})
    return ids


async def kick_wrong_build(reason: str = "") -> list[str]:
    """Boot everyone online whose recorded APK build isn't an allowed release
    (clientBuildStatus != "ok", see services/build_gate.py)."""
    ids = await online_account_ids()
    if not ids:
        return []
    from services.build_gate import current_status
    wrong = [a["accountID"] async for a in accounts.find(
        {"accountID": {"$in": ids}},
        {"_id": 0, "accountID": 1, "clientBuildTag": 1, "clientBuildStatus": 1})
        if current_status(a) != "ok"]
    if wrong:
        from config import REQUIRED_CLIENT_VERSION
        reason = (reason or "").strip() or (
            f"Wrong game version. Install the latest {REQUIRED_CLIENT_VERSION} APK.")
        await accounts.update_many(
            {"accountID": {"$in": wrong}},
            {"$set": {"pendingKick": 1, "kickReason": reason, "shouldForceRefresh": 1}})
    return wrong
