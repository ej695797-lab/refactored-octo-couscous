"""Patterns ported from live Yeeps into the big APK (2.4.0 + live content).

Only clients whose build tag starts with "big" know these keys, so only they
are given them (login adds them to the returned ownedPatterns; nothing is
stored on the account, so the same account on stock 2.4.0 is unaffected).
The list is written by apk-work/big/tools/import_240.py (report pattern_added).
"""
import json
import os
import os as _os

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "big_patterns.json")
_cache = {"mtime": None, "keys": []}


def is_big(tag: str) -> bool:
    # "play*" = the playyeeps line (clean stock 2.31.4 base on play.your-domain.example).
    # It uses the same live-2.31.4 server paths as biglive, so it's treated as a
    # big/live client everywhere (version-gate skip, 2.31.4 response formatting).
    # "live*" = the renamed live-2.31.4 line (was "biglive*"; "biglive" still
    # matches "big" for older installed clients, kept so they aren't kicked).
    return str(tag or "").startswith(("big", "live", "play"))


_VALID = os.path.join(os.path.dirname(_PATH), "big_valid_patterns.json")
_vcache = {"mtime": None, "keys": None}


def valid_keys():
    """Every pattern key the current big build has (None if unknown)."""
    try:
        m = os.path.getmtime(_VALID)
        if m != _vcache["mtime"]:
            _vcache.update(mtime=m, keys=set(json.load(open(_VALID))))
    except Exception:
        pass
    return _vcache["keys"]


def extra_owned() -> list:
    try:
        m = os.path.getmtime(_PATH)
        if m != _cache["mtime"]:
            _cache.update(mtime=m, keys=[k for k in json.load(open(_PATH)) if isinstance(k, str) and k])
    except Exception:
        pass
    return _cache["keys"]


_LIVE = os.path.join(os.path.dirname(_PATH), "live2314_patterns.json")
_lcache = {"mtime": 0, "keys": []}


def live_owned() -> list:
    """The full 2.31.4 pattern catalog (data/live2314_patterns.json), ripped
    from the live data's PatternManager. Live-base clients own all of these, so
    every item is available. Each key exists in the real client's pattern DB, so
    there are no "Owned pattern key X does not exist" rejections."""
    try:
        m = os.path.getmtime(_LIVE)
        if m != _lcache["mtime"]:
            _lcache.update(mtime=m, keys=[k for k in json.load(open(_LIVE)) if isinstance(k, str) and k])
    except Exception:
        pass
    return _lcache["keys"]


# Themes / lighting presets only the big APK has. Rooms that use them are sent
# to stock 2.4.0 clients with a theme/preset they know instead (an unknown key
# could break their room load).
import re as _re

BIG_THEMES = {"space": "researchFacility", "classic": "suburb"}
BIG_LIGHTING = {"hauntedHouse": "midnight", "pitchBlack": "midnight", "graveyard": "midnight",
                "halloween": "midnight", "toyStore": "night", "evilLab": "midnight", "cabin": "night"}
# Themes that exist only from a given biglive build onward. A live client on an
# older build doesn't have them, so they're downgraded to a theme it does have
# (an unknown themeKey can break the room load). value = (fallback, min build).
LIVE_THEME_MIN = {"classic": 7}
LIVE_THEME_FALLBACK = {"classic": "suburb"}
_RX = _re.compile(r'("(themeKey|customLightingKey)"\s*:\s*(?:\{\s*"S"\s*:\s*)?)"([A-Za-z0-9_]+)"')


def _biglive_ver(tag) -> int:
    # The live line was renamed "biglive<N>" -> "live<N>" (2026-10-17). Accept both,
    # and search (not match) so it also works on file names like "Yeeps-live27.apk".
    m = _re.search(r"(?:biglive|live)(\d+)", str(tag or ""))
    return int(m.group(1)) if m else 0


def _sub(m):
    table = BIG_THEMES if m.group(2) == "themeKey" else BIG_LIGHTING
    v = m.group(3)
    return f'{m.group(1)}"{table[v]}"' if v in table else m.group(0)


def downgrade_text(text: str) -> str:
    """JSON text for a stock 2.4.0 client: big-only theme/lighting keys replaced."""
    if "themeKey" not in text and "customLightingKey" not in text:
        return text
    return _RX.sub(_sub, text)


def downgrade_for_tag(text: str, tag: str) -> str:
    """Replace themeKeys the client's build doesn't have with one it does.
    Stock clients get the full BIG_THEMES/BIG_LIGHTING downgrade; big/live
    clients only get themes newer than their build downgraded (e.g. 'classic'
    for anything below biglive7)."""
    if "themeKey" not in text and "customLightingKey" not in text:
        return text
    if not is_big(tag):
        return _RX.sub(_sub, text)
    lv = _biglive_ver(tag)

    def sub(m):
        if m.group(2) != "themeKey":
            return m.group(0)
        v = m.group(3)
        if v in LIVE_THEME_MIN and lv < LIVE_THEME_MIN[v]:
            return f'{m.group(1)}"{LIVE_THEME_FALLBACK[v]}"'
        return m.group(0)
    return _RX.sub(sub, text)


# Only the login request carries the build tag (/c/<tag>/); DynamoDB and lambda
# requests carry just the headset key (/k/<key>/). Remember which build each
# headset logged in with so those requests can be told apart too.
import time as _time

_dev = {}          # key hash -> (expires, tag)


async def remember_device(device_key: str, tag: str) -> None:
    if not device_key or not tag:
        return
    from services.device_auth import key_hash
    from services.db import db
    h = key_hash(device_key)
    if _dev.get(h, (0, None))[1] == tag:
        _dev[h] = (_time.time() + 600, tag)
        return
    _dev[h] = (_time.time() + 600, tag)
    try:
        await db["device_builds"].update_one({"keyHash": h}, {"$set": {"keyHash": h, "tag": tag, "at": _time.time()}}, upsert=True)
    except Exception as e:
        print(f"[big] remember_device failed: {e}")


async def tag_for(state_tag: str, device_key: str) -> str:
    """The client's build tag: from the request if present, else the headset's last login."""
    if state_tag:
        return state_tag
    if not device_key:
        return ""
    from services.device_auth import key_hash
    h = key_hash(device_key)
    exp, tag = _dev.get(h, (0, None))
    if exp > _time.time():
        return tag or ""
    tag = ""
    try:
        from services.db import db
        d = await db["device_builds"].find_one({"keyHash": h}, {"_id": 0, "tag": 1})
        tag = (d or {}).get("tag") or ""
    except Exception:
        pass
    _dev[h] = (_time.time() + 600, tag)
    return tag


def new_pet_record() -> dict:
    """A fresh account.pets entry (same fields 2.4 stores for an adopted pet)."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y %m/%d %H:%M:%S")
    return {"cosmetics": [], "curTaskStartTime": now, "favoriteFood": "pie", "level": 1,
            "location_position": [0, 0, 0], "location_roomKey": "", "taskCompletedTimes": []}


# ---- testers only: big builds need the Discord role "Tester Role" ----------
TESTER_ROLE = os.getenv("BIG_TESTER_ROLE", "Tester Role")
_tester_cache = {}       # accountID -> (expires, bool)


async def is_tester(account_id: str, role_keys=None) -> bool:
    """True if the account may play a big build: its Discord account (linked with
    /link, stored on the 1.31 account with the same accountID) has TESTER_ROLE,
    or the account has the server-only 'blaze' role. Cached 5 minutes."""
    import time
    if "blaze" in (role_keys or []):
        return True
    hit = _tester_cache.get(account_id)
    if hit and hit[0] > time.time():
        return hit[1]
    ok = False
    try:
        from services.db import client
        src = os.getenv("CARRYOVER_DB", "Yeeps119") or "Yeeps119"
        acc = await client[src]["accounts"].find_one({"accountID": account_id}, {"discordID": 1, "_id": 0})
        did = str((acc or {}).get("discordID") or "")
        if did:
            from services.discord_roles import has_role
            ok = await has_role(did, TESTER_ROLE)
    except Exception as e:
        print(f"[big] tester check failed for {account_id}: {e}")
    _tester_cache[account_id] = (time.time() + 300, ok)
    return ok


def latest_big_build() -> str:
    """Build the newest-build gate forces STABLE bigN clients up to. This is the
    stable 2.4-base line (STABLE_BIG), kept separate from the download pointer:
    the public download (LATEST_BIG) may be a live-base build while stable
    players must not be force-updated onto it. Falls back to LATEST_BIG when no
    stable marker exists, ignoring any biglive value there."""
    try:
        name = open(os.path.expanduser("~/yeeps-public-240/download/STABLE_BIG")).read().strip()
        bid = name.replace("Yeeps-BIG-", "").replace(".apk", "")
        if bid:
            return bid
    except Exception:
        pass
    try:
        name = open(os.path.expanduser("~/yeeps-public-240/download/LATEST_BIG")).read().strip()
        bid = name.replace("Yeeps-BIG-", "").replace(".apk", "")
        # Ignore a live-base value here: the stable bigN gate must never force
        # stable players onto a live build (biglive<N>/live<N>).
        return "" if _biglive_ver(name) else bid
    except Exception:
        return ""


def is_livebase(tag: str) -> bool:
    """The experimental live-2.31.4-base track (tags 'biglive*' and 'play*');
    separate from the published 'bigN' tester line and exempt from the
    newest-build gate. 'play*' = the playyeeps line (clean stock 2.31.4 base
    on play.your-domain.example), which uses the same live-base response paths."""
    return str(tag or "").startswith(("biglive", "live", "play"))


def is_play(tag: str) -> bool:
    """The playyeeps line only ('play*'). Unlike biglive (which plays our local
    beta4 world), a play client PLAYS THE REAL LIVE YEEPS WORLD: world/data
    reads are forwarded to real Yeeps' AWS with guest Cognito creds
    (services/real_yeeps.py). Only the player identity (login / G2_Accounts) is
    served locally, to skip the Meta user-proof nonce a re-signed APK can't
    satisfy."""
    return str(tag or "").startswith("play")


def newest_livebase_build() -> int:
    """The newest published biglive build number (from download/LATEST_BIG), or
    0 if none. Used to hard-lock old biglive clients out until they update.
    Publishing a new bigliveN APK (LATEST_BIG) raises this automatically."""
    try:
        name = open(_os.path.expanduser("~/yeeps-public-240/download/LATEST_BIG")).read().strip()
        return _biglive_ver(name)   # searches for biglive<N>/live<N> anywhere in the name
    except Exception:
        return 0


def big_outdated(tag: str) -> bool:
    """A big client on anything but the newest published build must update.
    The live-base test track is exempt (it isn't published to the download)."""
    if is_livebase(tag):
        return False
    latest = latest_big_build()
    return bool(latest) and (tag or "").split(".", 1)[0] != latest
