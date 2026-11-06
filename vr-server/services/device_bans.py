"""
Headset bans (2026-10-10). An IP ban is easy to step around (home IPv6
addresses rotate, a phone hotspot or VPN gives a new one), and a new Meta
account means a new Yeeps account. The headset key (services/device_auth.py)
stays the same on the same Quest, so a permanent ban also bans the account's
bound headset(s): any account logging in or joining Photon from that headset
is treated as banned.

Stored in globals {"key": "banned_devices", "hashes": [...], "reasons": {...}}
as the same key hashes accounts keep in `deviceKeys`.
"""

import time

from services.db import globals_col

_KEY = "banned_devices"
_cache = {"set": set(), "ts": 0.0}


async def banned_hashes() -> set:
    if time.monotonic() - _cache["ts"] < 20:
        return _cache["set"]
    try:
        doc = await globals_col.find_one({"key": _KEY}, {"_id": 0, "hashes": 1})
        _cache["set"] = set((doc or {}).get("hashes") or [])
    except Exception:
        pass
    _cache["ts"] = time.monotonic()
    return _cache["set"]


async def ban_devices(hashes, reason: str = "") -> list:
    hashes = [h for h in (hashes or []) if h]
    if not hashes:
        return []
    sets = {f"reasons.{h}": {"reason": reason, "at": int(time.time())} for h in hashes}
    await globals_col.update_one({"key": _KEY},
                                 {"$addToSet": {"hashes": {"$each": hashes}}, "$set": sets}, upsert=True)
    _cache["ts"] = 0.0
    return hashes


async def unban_devices(hashes) -> list:
    hashes = [h for h in (hashes or []) if h]
    if not hashes:
        return []
    await globals_col.update_one({"key": _KEY}, {"$pull": {"hashes": {"$in": hashes}},
                                                 "$unset": {f"reasons.{h}": "" for h in hashes}})
    _cache["ts"] = 0.0
    return hashes


async def is_device_banned(raw_key: str) -> bool:
    """True if the headset sending this (raw) device key is banned."""
    from services import device_auth
    if not device_auth.valid_key(raw_key):
        return False
    return device_auth.key_hash(raw_key) in await banned_hashes()
