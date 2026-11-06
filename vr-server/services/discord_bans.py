"""Discord-wide bans (owner 2026-11-02): banning an account also bans every account
linked to the same Discord (now or before an /unlink), and that Discord can't /link
again. Shared by the 1.31 and live servers: records live in Yeeps240.discord_bans.
Accounts banned this way carry bannedVia=<the account that was banned>, so unbanning
that account lifts them too."""
import os
from datetime import datetime, timezone

from services.db import client

_DB_131 = os.getenv("DB_131", "Yeeps119")
_DB_LIVE = os.getenv("DB_LIVE", "Yeeps240")
_DBS = (_DB_131, _DB_LIVE)


def _bans():
    return client[_DB_LIVE]["discord_bans"]


async def discords_of(account_id: str) -> set:
    """Every Discord ID this account is or was linked to, on both servers."""
    ids = set()
    for dbn in _DBS:
        d = await client[dbn]["accounts"].find_one(
            {"accountID": account_id}, {"_id": 0, "discordID": 1, "oldDiscords": 1})
        if d:
            ids.add(str(d.get("discordID") or ""))
            ids.update(str(o.get("discordID") or "") for o in (d.get("oldDiscords") or []))
        async for l in client[dbn]["discord_links"].find({"accountID": account_id}, {"_id": 0, "discordID": 1}):
            ids.add(str(l.get("discordID") or ""))
    async for u in client[_DB_LIVE]["discord_unlinks"].find({"accountID": account_id}, {"_id": 0, "discordID": 1}):
        ids.add(str(u.get("discordID") or ""))
    ids.discard("")
    return ids


async def accounts_of(discord_ids: set) -> set:
    """Every account that is or was linked to any of these Discord IDs."""
    ids = list(discord_ids)
    out = set()
    if not ids:
        return out
    for dbn in _DBS:
        async for a in client[dbn]["accounts"].find(
                {"$or": [{"discordID": {"$in": ids}}, {"oldDiscords.discordID": {"$in": ids}}]},
                {"_id": 0, "accountID": 1}):
            out.add(a["accountID"])
        async for l in client[dbn]["discord_links"].find({"discordID": {"$in": ids}}, {"_id": 0, "accountID": 1}):
            out.add(l["accountID"])
    out.discard(None)
    return out


async def ban_linked(account_id: str, reason: str, expire: str = "") -> tuple:
    """Ban every other account sharing a Discord with account_id, with the same
    expiry (expire "" = permanent). A permanent ban also blocks those Discords from
    /link. Returns (discord_ids, other_account_ids)."""
    dids = await discords_of(account_id)
    others = (await accounts_of(dids)) - {account_id}
    at = datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S")
    if not expire:
        for did in dids:
            await _bans().update_one({"discordID": did},
                                     {"$set": {"discordID": did, "accountID": account_id,
                                               "reason": reason, "at": at}}, upsert=True)
    if others:
        why = f"{reason} (shares a Discord with {account_id})"
        fields = {"adminBlocked": 1, "isPermabanned": 0 if expire else 1,
                  "blockReason": why, "banReason": why, "banCategory": "blocked",
                  "banExpireTimestamp": expire, "shouldForceRefresh": 1, "bannedVia": account_id}
        for dbn in _DBS:
            await client[dbn]["accounts"].update_many({"accountID": {"$in": list(others)}}, {"$set": fields})
    return dids, others


async def unban_linked(account_id: str) -> tuple:
    """Undo ban_linked: lift the Discord blocks and the accounts banned because of
    this account. Returns (discord_ids, account_ids_unbanned)."""
    dids = await discords_of(account_id)
    await _bans().delete_many({"$or": [{"accountID": account_id}, {"discordID": {"$in": list(dids)}}]})
    freed = set()
    for dbn in _DBS:
        async for a in client[dbn]["accounts"].find({"bannedVia": account_id}, {"_id": 0, "accountID": 1}):
            freed.add(a["accountID"])
        await client[dbn]["accounts"].update_many(
            {"bannedVia": account_id},
            {"$set": {"adminBlocked": 0, "isPermabanned": 0, "blockReason": "", "banReason": "",
                      "banExpireTimestamp": "", "banCategory": "", "shouldForceRefresh": 1},
             "$unset": {"bannedVia": ""}})
    return dids, freed


async def banned_discord(discord_id: str):
    """The ban record if this Discord is banned, else None."""
    if not discord_id:
        return None
    return await _bans().find_one({"discordID": str(discord_id)}, {"_id": 0})
