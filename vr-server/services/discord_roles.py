"""
Check whether a Discord user has a given role in the guild, so the web editor
can authorise anyone with the "data manager" role (not just the hardcoded
EDITOR_ADMINS accountIDs).

Uses the bot token (DISCORD_BOT_TOKEN) + guild id (DISCORD_GUILD_ID) already set
on the service. Fetching a SINGLE guild member by id (GET /guilds/{g}/members/
{id}) and the guild's roles does NOT need the privileged members intent, so no
extra Discord setup is required beyond the bot being in the server.
"""

import os
import time

import httpx

_API = "https://discord.com/api/v10"


def _token() -> str:
    return os.getenv("DISCORD_BOT_TOKEN", "").strip()


def _guild() -> str:
    return os.getenv("DISCORD_GUILD_ID", "").strip()


def _role_name() -> str:
    return os.getenv("EDITOR_ROLE_NAME", "data manager").strip().lower()


# Cache the role-id lookup (role names rarely change) for 10 minutes.
_role_cache = {"name": None, "id": None, "ts": 0.0}


async def _resolve_role_id() -> str | None:
    tok, guild = _token(), _guild()
    if not (tok and guild):
        return None
    want = _role_name()
    now = time.time()
    if _role_cache["name"] == want and _role_cache["id"] and now - _role_cache["ts"] < 600:
        return _role_cache["id"]
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{_API}/guilds/{guild}/roles",
                            headers={"Authorization": f"Bot {tok}"})
        if r.status_code != 200:
            return None
        for role in r.json():
            if str(role.get("name", "")).strip().lower() == want:
                _role_cache.update(name=want, id=role["id"], ts=now)
                return role["id"]
    except Exception:
        pass
    return None


# Generic role lookup (by name) — cache role id per lowercase name, 10 min.
_named_role_cache: dict = {}


async def _role_id_by_name(name: str) -> str | None:
    tok, guild = _token(), _guild()
    if not (tok and guild and name):
        return None
    want = name.strip().lstrip("@").strip().lower()   # "@None Verified" == "None Verified"
    now = time.time()
    hit = _named_role_cache.get(want)
    if hit and now - hit[1] < 600:
        return hit[0]
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{_API}/guilds/{guild}/roles",
                            headers={"Authorization": f"Bot {tok}"})
        if r.status_code != 200:
            return None
        for role in r.json():
            if str(role.get("name", "")).strip().lstrip("@").strip().lower() == want:
                _named_role_cache[want] = (role["id"], now)
                return role["id"]
    except Exception:
        pass
    return None


async def has_role(discord_id: str, role_name: str) -> bool:
    """True if this Discord user is in the guild and has the named role.
    False on any error / missing config (fail-open for callers that gate on it)."""
    tok, guild = _token(), _guild()
    if not (discord_id and tok and guild and role_name):
        return False
    rid = await _role_id_by_name(role_name)
    if not rid:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{_API}/guilds/{guild}/members/{discord_id}",
                            headers={"Authorization": f"Bot {tok}"})
        if r.status_code != 200:
            return False
        return rid in (r.json().get("roles") or [])
    except Exception:
        return False


async def has_editor_role(discord_id: str) -> bool:
    """True if this Discord user is in the guild and has the editor role."""
    tok, guild = _token(), _guild()
    if not (discord_id and tok and guild):
        return False
    rid = await _resolve_role_id()
    if not rid:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{_API}/guilds/{guild}/members/{discord_id}",
                            headers={"Authorization": f"Bot {tok}"})
        if r.status_code != 200:
            return False
        return rid in (r.json().get("roles") or [])
    except Exception:
        return False
