"""Map builders: players allowed to put SHARED maps in creative and save them
(owner 2026-10-18: "make only specific players can set maps to creative and make
it so those maps save").

Everyone else keeps the grief lock: shared maps are served mode-locked, the APK
reverts creative, and their saves are dropped (handlers/maps.py). The list lives
in globals key "creative_builders" (editor Roles tab) and always includes the
editor admins. Saves still need the builder's own bound headset.
"""
import time

from services.db import accounts, globals_col

KEY = "creative_builders"
_TTL = 30.0
_cache = {"at": 0.0, "ids": set(), "hashes": set()}


def _admins() -> set:
    try:
        from routers.editor_router import EDITOR_ADMINS
        return set(EDITOR_ADMINS)
    except Exception:
        return {"o_OWNER0000000001"}


async def stored_ids() -> list:
    doc = await globals_col.find_one({"key": KEY}, {"_id": 0, "value": 1})
    v = (doc or {}).get("value")
    return [str(x) for x in v if isinstance(x, str) and x.startswith("o_")] if isinstance(v, list) else []


async def _refresh() -> None:
    if time.monotonic() - _cache["at"] < _TTL:
        return
    ids = set(await stored_ids()) | _admins()
    hashes = set()
    async for a in accounts.find({"accountID": {"$in": list(ids)}}, {"_id": 0, "deviceKeys": 1}):
        hashes.update(a.get("deviceKeys") or [])
    _cache.update(at=time.monotonic(), ids=ids, hashes=hashes)


async def builder_ids() -> set:
    await _refresh()
    return set(_cache["ids"])


async def set_ids(ids: list) -> list:
    clean = sorted({str(i).strip() for i in ids if str(i).strip().startswith("o_")})
    await globals_col.update_one({"key": KEY}, {"$set": {"key": KEY, "value": clean}}, upsert=True)
    _cache["at"] = 0.0
    return clean


async def is_builder(account_id: str = "", device_key: str = "") -> bool:
    """For READS (mode unlock): the account is a builder, or the request comes from
    a headset bound to a builder account."""
    await _refresh()
    if account_id and account_id in _cache["ids"]:
        return True
    if device_key:
        from services import device_auth
        return device_auth.key_hash(device_key) in _cache["hashes"]
    return False


async def is_builder_on_own_headset(account_id: str, device_key: str) -> bool:
    """For WRITES (saves, creative switch): a builder account AND its own bound headset."""
    await _refresh()
    if not account_id or account_id not in _cache["ids"]:
        return False
    from services import device_auth
    bound = await device_auth._keys_of(account_id)
    return bool(bound and device_auth.valid_key(device_key) and device_auth.key_hash(device_key) in bound)
