import os
"""
Client build identity + integrity gate (1.31.0-blaze and later).

The player APK's native lib (libBNMExplorer.so) computes a tag at startup:
    <buildId>.<fnv64 of libil2cpp.so>.<fnv64 of libBNMExplorer.so>
and puts it in the path of every API-gateway request as /c/<tag>/ (main.py's
middleware strips it and hands it to handlers as body["_clientBuildTag"]).
Login records it on the account (clientBuildTag / clientBuildStatus /
clientBuildSeenAt), so you can see who runs which APK.

classify(tag):
  "ok"        build id allowed AND the full tag (file hashes) allowed
  "outdated"  no tag (old/unofficial APK) or a build id that isn't current
              -> visible "update your APK" block (login, Photon, refresh)
  "modified"  current build id but native libs that don't match any known
              release -> covert deny at Photon auth (modder can't tell why)

Releasing a new APK: bump kBuildId in native-lib.cpp, compute the tag from the
release libs (apk-work/131-notes/expected_build_tag_*.txt) and set env
ALLOWED_CLIENT_BUILDS / ALLOWED_CLIENT_TAGS (comma lists). Keep the previous
release in the lists until everyone has updated. ENFORCE_CLIENT_BUILD=0 turns
blocking off (still records).
"""

import time as _time

from config import ALLOWED_CLIENT_BUILDS, ALLOWED_CLIENT_TAGS, ENFORCE_CLIENT_BUILD, REQUIRED_CLIENT_VERSION


def _bnum(build_id: str) -> int:
    """131b9 -> 9, 131b10 -> 10 (string max would sort 131b10 below 131b9)."""
    try:
        return int(str(build_id).rsplit("b", 1)[1])
    except Exception:
        return 0


def newest_build() -> str:
    """The newest allowlisted build id."""
    return max(ALLOWED_CLIENT_BUILDS, key=_bnum) if ALLOWED_CLIENT_BUILDS else ""


# First build with the safe dispenser code (131b8 refuses force-accepted blocks;
# 131b9 also drops the ItemDespawnZone hooks that crashed on held/loaded items).
# The Starter House's full sign/dispenser internal states are only served once
# every playable build is at least this.
GUARD_MIN_BUILD = "131b10"


async def only_guard_builds() -> bool:
    """True when every build that can currently play has the dispenser guard,
    i.e. it's safe to serve maps' full internal states (Starter House signs)."""
    if not ENFORCE_CLIENT_BUILD:
        return False
    req = await require_build()
    allowed = {r.strip() for r in req.split(",") if r.strip()} if req else set(ALLOWED_CLIENT_BUILDS)
    return bool(allowed) and all(_bnum(b) >= _bnum(GUARD_MIN_BUILD) for b in allowed)


# Live track (2.32.0) old-APK message, owner's wording 2026-10-28. Shown on the
# login popup and when an old build is refused at Photon.
LIVE_OUTDATED_MESSAGE = ('Outdated version. please redownload from the discord and no you are not banned '
                         'so dont say "im banned please fix this @ownername" i will time you out')

# Owner 2026-10-28: a live player with no Discord link can't load the game at all.
# Login fails (game shows its purple Fatal Error box) and the box's message comes
# from fetchGlobalFatalError, matched to the blocked login by IP / account.
LINK_REQUIRED_MESSAGE = ("Please make sure to do /link in the discord!\n\n"
                         "if you are not in the discord join by this link:\n"
                         "https://discord.gg/UfYxE9W9dX")
_link_blocked_recent: dict = {}      # ip or accountID -> time of the blocked login
_LINK_BLOCK_WINDOW = 30 * 60


def note_link_blocked(account_id: str, ip: str) -> None:
    now = _time.time()
    for k in (account_id, ip):
        if k:
            _link_blocked_recent[str(k)] = now
    if len(_link_blocked_recent) > 5000:
        for k, t in list(_link_blocked_recent.items()):
            if now - t > _LINK_BLOCK_WINDOW:
                _link_blocked_recent.pop(k, None)


def was_link_blocked(account_id: str = "", ip: str = "") -> bool:
    now = _time.time()
    return any(k and now - _link_blocked_recent.get(str(k), 0) < _LINK_BLOCK_WINDOW
               for k in (account_id, ip))


def clear_link_blocked(account_id: str = "", ip: str = "") -> None:
    for k in (account_id, ip):
        _link_blocked_recent.pop(str(k), None)


def outdated_message() -> str:
    """Shown to players on an old APK (also old builds of the same version,
    so name the build, not just the version)."""
    latest = newest_build()
    build = f" (build {latest})" if latest else ""
    url = os.getenv("LATEST_APK_URL", "131.your-domain.example/download")
    return (f"Outdated version. Update to the latest {REQUIRED_CLIENT_VERSION}{build} to keep playing."
            + (f" Download: {url}" if url else ""))


# ── "require newest build" testing toggle (globals key "requireBuild") ──
# While set to a build id, every client NOT on that build is treated as
# outdated -> blocked with the update message + kicked, so staff can clear the
# server during testing. Empty/absent = normal (all allowlisted builds play).
# Read from Mongo, cached 10 s so the login/Photon paths don't query every time.
_req_cache = {"val": "", "ts": 0.0}


async def require_build() -> str:
    now = _time.time()
    if now - _req_cache["ts"] < 10:
        return _req_cache["val"]
    val = _req_cache["val"]
    try:
        from services.db import globals_col
        d = await globals_col.find_one({"key": "requireBuild"}, {"_id": 0, "value": 1})
        val = str((d or {}).get("value") or "").strip()
    except Exception:
        pass
    _req_cache.update(val=val, ts=now)
    return val


def require_blocks(tag: str, req: str) -> bool:
    """True if a required build is set and this client's tag isn't it.
    `req` may list several builds, comma-separated (e.g. "131b12,131b14" while
    players move from one to the other)."""
    allowed = {r.strip() for r in (req or "").split(",") if r.strip()}
    return bool(allowed) and (tag or "").split(".", 1)[0] not in allowed


def classify(tag: str) -> str:
    tag = (tag or "").strip()
    if not tag:
        return "outdated"
    # Big / live-base / play clients run their own version track (biglive*,
    # big*, play*) and are gated by services.big_patterns, NOT by the 1.31
    # ALLOWED_CLIENT_BUILDS allowlist. Without this they'd always classify as
    # "outdated" (their build id isn't in that list) and the editor's online
    # list would label every live player "outdated" instead of their build.
    try:
        from services import big_patterns as _bp
        if _bp.is_big(tag) or _bp.is_livebase(tag):
            return "ok"
    except Exception:
        pass
    build_id = tag.split(".", 1)[0]
    if build_id not in ALLOWED_CLIENT_BUILDS:
        return "outdated"
    if ALLOWED_CLIENT_TAGS and tag not in ALLOWED_CLIENT_TAGS:
        return "modified"
    return "ok"


def current_status(doc: dict) -> str:
    """Build status of an account against the CURRENT allowlist. The stored
    clientBuildStatus is from the player's last login, so after a release it
    can still say "ok" for an APK that is no longer allowed; re-check the tag."""
    doc = doc or {}
    if "clientBuildTag" in doc:
        return classify(doc.get("clientBuildTag") or "")
    return doc.get("clientBuildStatus") or "unknown"


def enforced() -> bool:
    return ENFORCE_CLIENT_BUILD
