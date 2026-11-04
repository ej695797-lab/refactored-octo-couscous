"""
Challenge & store rotation handlers:
  - g2_redeemChallengeReward
  - g2_fetchGlobalRotation
"""

from handlers.login import ALL_TECHWEB_ITEMS


async def handle_redeem_challenge_reward(body: dict) -> dict:
    return {
        "success": 1,
        "currencyAmount": 0,
        "newCurrency": 0,
        "items": [],
        "newLastChallengeRedeemedTime": "2099 01/01 00:00:00",
    }


_tw_cache = {"at": 0.0, "items": None}
_TW_TTL = 120   # 2026-10-31: was a 167 KB Mongo read on EVERY fetchGlobalRotation
                # (~1.3-1.7 s each, hundreds per hour) -> the whole server queued
                # behind it and every load call took ~9 s. Edits still apply in <= 2 min.


async def techweb_items() -> list:
    """The techweb (build menu) item list. Remotely editable: globals key
    "techweb_items" (a list of pattern keys) overrides the built-in 1.19-era
    ALL_TECHWEB_ITEMS, so blocks can be added/removed without a redeploy.
    Cached in memory for _TW_TTL seconds."""
    import time as _t
    if _tw_cache["items"] is not None and _t.time() - _tw_cache["at"] < _TW_TTL:
        return _tw_cache["items"]
    items = await _techweb_items_db()
    _tw_cache["items"], _tw_cache["at"] = items, _t.time()
    return items


async def _techweb_items_db() -> list:
    try:
        from services.db import globals_col
        doc = await globals_col.find_one({"key": "techweb_items"}, {"_id": 0, "value": 1})
        if doc and isinstance(doc.get("value"), list) and doc["value"]:
            return doc["value"]
    except Exception as e:
        print(f"[techweb] override load failed: {e}")
    return ALL_TECHWEB_ITEMS


# ---- Daily challenges (the "daily" tab) ------------------------------------
# Real format captured from live Yeeps 2026-10-17 (fetchGlobalRotation
# rotationKey="challenges"): itemKeys = {cardChallenges/easy/hard/login:
# [{key, value}]}, window 08:00 UTC -> next day 08:00 UTC. Fetched from live Yeeps
# at most once per daily window (read-only, via the cw_fetch_staffalias account)
# and cached in Mongo; clients poll this ~50k times/day so NEVER per request.
# Falls back to the captured set (with today's window) if live is unavailable.
_DAILY_FALLBACK_ITEMKEYS = {
    "cardChallenges": [{"key": "playDrumInRooms", "value": 50},
                       {"key": "useFlameThrowerInRooms", "value": 50},
                       {"key": "eatPizza", "value": 50}],
    "easy": [{"key": "triggerNoteBlocksWithItems", "value": 50}],
    "hard": [{"key": "haveLoveEffect", "value": 50}],
    "login": [{"key": "login", "value": 170}],
}
_DAILY_CACHE_KEY = "_daily_challenges_cache"
_daily_mem: dict = {}
_daily_last_try = 0.0


def _daily_window():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    start = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now < start:
        start -= timedelta(days=1)
    end = start + timedelta(days=1)
    fmt = "%Y %m/%d %H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


_DAILY_OVERRIDE_KEY = "_daily_override"   # editor "custom challenges" (leading "_" = never served raw)


async def daily_override():
    """The editor's custom daily challenges ({itemKeys}) or None (= use live Yeeps)."""
    from services.db import globals_col
    doc = await globals_col.find_one({"key": _DAILY_OVERRIDE_KEY}, {"_id": 0, "value": 1})
    v = (doc or {}).get("value") or {}
    return v if isinstance(v.get("itemKeys"), dict) and v["itemKeys"] else None


def reset_daily_cache():
    """Forget the in-memory set + retry throttle so the next request re-reads
    the override / refetches live (editor "refresh" + after saving)."""
    global _daily_last_try
    _daily_mem.clear()
    _daily_last_try = 0.0


async def _daily_challenges() -> dict:
    """{itemKeys, startTime, expireTime} for the current daily window. An editor
    override (custom challenges) wins over live Yeeps."""
    import time as _t
    global _daily_last_try
    start, end = _daily_window()
    try:
        ov = await daily_override()
        if ov:
            return {"itemKeys": ov["itemKeys"], "startTime": start, "expireTime": end, "source": "custom"}
    except Exception as e:
        print(f"[daily] override read failed: {e}")
    if _daily_mem.get("startTime") == start:
        return _daily_mem
    try:
        from services.db import globals_col
        doc = await globals_col.find_one({"key": _DAILY_CACHE_KEY}, {"_id": 0, "value": 1})
        v = (doc or {}).get("value") or {}
        if v.get("startTime") == start and v.get("itemKeys"):
            _daily_mem.clear(); _daily_mem.update(v)
            return _daily_mem
        # New day: refresh from live Yeeps (throttled to one attempt per 30 min).
        if _t.time() - _daily_last_try > 1800:
            _daily_last_try = _t.time()
            from services import yeeps_direct
            r = await yeeps_direct.invoke_standalone(
                "g2_fetchGlobalRotation", {"rotationKey": "challenges"}, account="cw_fetch_staffalias")
            if isinstance(r, dict) and r.get("itemKeys"):
                v = {"itemKeys": r["itemKeys"], "startTime": start, "expireTime": end, "source": "live"}
                await globals_col.update_one({"key": _DAILY_CACHE_KEY},
                                             {"$set": {"key": _DAILY_CACHE_KEY, "value": v}}, upsert=True)
                print(f"[daily] refreshed daily challenges from live Yeeps: "
                      f"{ {k: len(x) for k, x in r['itemKeys'].items()} }")
                _daily_mem.clear(); _daily_mem.update(v)
                return _daily_mem
    except Exception as e:
        print(f"[daily] live refresh failed: {type(e).__name__}: {e}")
    # Fallback: last cached set or the captured one, re-dated to today's window.
    keys = (_daily_mem.get("itemKeys") if _daily_mem else None) or _DAILY_FALLBACK_ITEMKEYS
    return {"itemKeys": keys, "startTime": start, "expireTime": end, "source": "fallback"}


# Active 2.x time-machine room (must not already be a room in freebuild_0).
TIME_MACHINE_ROOM_KEY = "fever_dream_1"   # fever dream served as the weekly throwback


async def handle_fetch_global_rotation(body: dict) -> dict:
    rotation_key = body.get("rotationKey", "")
    # itemKeys maps a subcategory to its items:
    #   {<subcategory>: [{"value": int, "key": str, "maxCount": int?}]}
    # Returning {} leaves every item uncategorised, so nothing shows up in
    # the build menu even though the account owns it. Everything goes under
    # the base techWeb category for now; the real per-item subcategory
    # mapping (techWeb_beach, techWeb_paint, ...) isn't in the captured data.
    item_keys = {}
    if rotation_key == "techWeb":
        item_keys = {"techWeb": [{"value": 0, "key": k} for k in await techweb_items()]}
    # 2.x: TimeMachineRoomManager reads itemKeys["timeMachine"][0].key as the
    # active time-machine room and caches it until expireTime. An empty list is
    # "Invalid time machine format", which also aborts loading the room map
    # ("Failed to get room map node ... NO_DATA_FOUND"). The key replaces the
    # map's "timeMachine" node, so it must NOT be a room already in that map
    # (ch2_old_starterHouse is -> "Duplicate room key" -> whole map rejected).
    expire = ""
    if rotation_key == "timeMachine":
        item_keys = {"timeMachine": [{"value": 0, "key": TIME_MACHINE_ROOM_KEY}]}
        expire = "2099 01/01 00:00:00"
    # Featured game mode (2.32: an empty list is "Invalid featured game mode format").
    # Real Yeeps: itemKeys {"featuredGameMode": [{"key": "minigame_captureTheFlag",
    # "value": 0}]}, one per day (17:00 UTC window). Override with globals
    # "featured_game_mode" (a minigame room key); else rotate daily through ours.
    if rotation_key == "featuredGameMode":
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        from services.db import globals_col
        _now = _dt.now(_tz.utc)
        _start = _now.replace(hour=17, minute=0, second=0, microsecond=0)
        if _now < _start:
            _start -= _td(days=1)
        _ov = await globals_col.find_one({"key": "featured_game_mode"}, {"_id": 0, "value": 1})
        _key = str((_ov or {}).get("value") or "")
        if not _key:
            _modes = ["minigame_captureTheFlag", "minigame_laserTag", "minigame_spleef", "minigame_paintWars",
                      "minigame_redVsBlue", "minigame_propTown", "minigame_battleArena", "minigame_toTheTop",
                      "minigame_copsAndRobbers", "minigame_impostor", "minigame_spikeArena", "minigame_hill"]
            _key = _modes[_start.toordinal() % len(_modes)]
        item_keys = {"featuredGameMode": [{"key": _key, "value": 0}]}
        result_start = _start.strftime("%Y %m/%d %H:%M:%S")
        expire = (_start + _td(days=1)).strftime("%Y %m/%d %H:%M:%S")

    result = {
        "rotationKey": rotation_key,
        "startTime": result_start if rotation_key == "featuredGameMode" else "1970 01/01 00:00:00",
        "expireTime": expire,
        "itemKeys": item_keys,
    }

    # Daily tab (ActiveChallengesDisplay): serve the REAL daily challenges with a
    # real daily window. Was empty itemKeys + a 1970 window -> "Challenges: Set
    # active challenges: 0" (empty daily tab).
    if rotation_key == "challenges":
        daily = await _daily_challenges()
        result["itemKeys"] = daily["itemKeys"]
        result["startTime"] = daily["startTime"]
        result["expireTime"] = daily["expireTime"]

    # rotationKey="challenges" responses carry an extra streakUpdate object
    # with login-streak tier data -- missing it entirely makes the client
    # fail to parse the response and crash on load.
    if rotation_key == "challenges":
        result["streakUpdate"] = {
            "loginStreakData": {
                "length": 0,
                "currentTier": "bronze",
                "nextTierThreshold": 1,
                "nextTier": "bronze",
            },
            "itemKeysToRemove": [],
            "itemKeysToAdd": [],
        }

    return result
