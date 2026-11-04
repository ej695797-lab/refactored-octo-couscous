"""
2.4.0 only: carry a player's 1.31.0-blaze profile over to their 2.4.0 account.

Runs at login (handlers/login.py) once per account (stamp carry131At), and
every login keeps the account owning everything in the default override.
Copied from the 1.31 DB (CARRYOVER_DB, READ-ONLY - never written here):
  roleKeys        union of both (+ tester, debug)
  eyeColor/skinColor  when the 1.31 value is a real non-zero int32
  activeCosmetics only keys this build has (in the owned list)
  displayName     once (carry131NameAt), so a 2.4 rename afterwards sticks
Disabled unless CARRYOVER_DB is set (1.31 never sets it).
"""
import os
from datetime import datetime, timezone

CARRYOVER_DB = os.getenv("CARRYOVER_DB", "")
BASE_ROLES = ["tester", "debug"]
_src = None


def _src_accounts():
    global _src
    if _src is None:
        from services.db import client
        _src = client[CARRYOVER_DB]["accounts"]
    return _src


def _int32(v):
    return isinstance(v, int) and not isinstance(v, bool) and -2**31 <= v < 2**31


def _union(*lists):
    out, seen = [], set()
    for l in lists:
        for x in l or []:
            if isinstance(x, str) and x and x not in seen:
                seen.add(x); out.append(x)
    return out


async def build_update(account: dict, default_owned: list, force: bool = False) -> dict:
    """$set fields for this account (empty dict = nothing to change)."""
    upd = {}
    owned = _union(account.get("ownedPatterns"), default_owned)
    if len(owned) != len(account.get("ownedPatterns") or []):
        upd["ownedPatterns"] = owned
    # broken colours (floats / out of range) break other players' account loads
    for k, d in (("eyeColor", -2039846657), ("skinColor", -3489025)):
        if not _int32(account.get(k)):
            upd[k] = d
    roles = _union(account.get("roleKeys"), BASE_ROLES)
    if CARRYOVER_DB and not account.get("carry131NameAt"):
        src = await _src_accounts().find_one({"accountID": account.get("accountID")},
                                             {"_id": 0, "displayName": 1})
        nm = (src or {}).get("displayName")
        if isinstance(nm, str) and nm.strip() and nm != account.get("displayName"):
            from services.names import unique_name
            upd["displayName"] = await unique_name(nm, account.get("accountID"))
        upd["carry131NameAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if CARRYOVER_DB and (force or not account.get("carry131At")):
        src = await _src_accounts().find_one(
            {"accountID": account.get("accountID")},
            {"_id": 0, "roleKeys": 1, "eyeColor": 1, "skinColor": 1, "activeCosmetics": 1})
        if src:
            roles = _union(roles, src.get("roleKeys"))
            for k in ("eyeColor", "skinColor"):
                v = src.get(k)
                if _int32(v) and v != 0 and v != account.get(k):
                    upd[k] = v
            ownset = set(owned)
            cos = [c for c in (src.get("activeCosmetics") or []) if c in ownset]
            if cos and cos != account.get("activeCosmetics"):
                upd["activeCosmetics"] = cos
        upd["carry131At"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if roles != (account.get("roleKeys") or []):
        upd["roleKeys"] = roles
    return upd
