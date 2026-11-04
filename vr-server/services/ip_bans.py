"""
IP ban store with persistence + auto-ban on repeated editor-login failures.

- Bans are persisted in the `globals` collection (doc key "banned_ips", an array
  of IP strings) so they survive a Render redeploy (in-memory state resets, and a
  raider could otherwise just wait for the next deploy).
- An in-memory cache is loaded once per process; membership checks are then just
  a set lookup (no DB hit per request).
- Failed-login counts are in-memory only (per IP, within a rolling window). An IP
  that gets the editor password wrong MORE THAN _FAIL_LIMIT times is auto-banned.
"""

import time

from services.db import globals_col

_GLOBALS_KEY = "banned_ips"
_FAIL_LIMIT = 3            # ban after MORE THAN this many wrong passwords (i.e. the 4th)
_FAIL_WINDOW = 3600        # seconds; failures older than this don't count toward the streak

_cache = None             # set[str] | None  -- banned IPs, lazily loaded from DB
_fails: dict = {}         # ip -> [count, first_ts]


def client_ip(request) -> str:
    """Real client IP. The server sits behind a proxy, so request.client.host is
    the proxy. Behind Cloudflare (self-hosted tunnel) CF-Connecting-IP is set by
    Cloudflare and can't be spoofed -- X-Forwarded-For can (Cloudflare appends to a
    client-sent one, so its first hop is attacker-controlled). Fall back to the
    first X-Forwarded-For hop (Render), then the socket peer."""
    try:
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else ""
    except Exception:
        return ""


_cache_at = 0.0
_CACHE_TTL = 60            # re-read the list every minute so an unban made from the
                           # other server's editor (both share one unban button) applies


async def _load() -> set:
    global _cache, _cache_at
    if _cache is None or time.time() - _cache_at > _CACHE_TTL:
        _cache_at = time.time()
        try:
            doc = await globals_col.find_one({"key": _GLOBALS_KEY}, {"_id": 0, "ips": 1})
            _cache = set((doc or {}).get("ips") or [])
        except Exception:
            _cache = set()
    return _cache


async def is_banned(ip: str) -> bool:
    if not ip or ip == "?":
        return False
    return ip in await _load()


async def ban_ip(ip: str, reason: str = "") -> None:
    if not ip or ip == "?":
        return
    cache = await _load()
    cache.add(ip)
    try:
        await globals_col.update_one(
            {"key": _GLOBALS_KEY},
            {"$addToSet": {"ips": ip},
             "$set": {f"reasons.{ip.replace('.', '_')}": {"reason": reason, "at": int(time.time())}}},
            upsert=True,
        )
    except Exception as e:
        print(f"[ip_bans] persist failed for {ip}: {e}")


async def unban_ip(ip: str) -> bool:
    """Remove an IP from the ban list. Returns True if it was banned."""
    if not ip:
        return False
    cache = await _load()
    had = ip in cache
    cache.discard(ip)
    try:
        await globals_col.update_one(
            {"key": _GLOBALS_KEY},
            {"$pull": {"ips": ip},
             "$unset": {f"reasons.{ip.replace('.', '_')}": ""}})
    except Exception as e:
        print(f"[ip_bans] unban persist failed for {ip}: {e}")
    return had


async def list_bans() -> dict:
    """Current banned IPs + their reasons."""
    await _load()
    doc = await globals_col.find_one({"key": _GLOBALS_KEY}, {"_id": 0}) or {}
    return {"ips": sorted(doc.get("ips") or []), "reasons": doc.get("reasons") or {}}


async def record_failed_login(ip: str) -> bool:
    """Record one wrong-password attempt from `ip`. Returns True if this attempt
    trips the auto-ban (i.e. more than _FAIL_LIMIT failures in the window)."""
    if not ip or ip == "?":
        return False
    now = time.time()
    rec = _fails.get(ip)
    if rec is None or now - rec[1] > _FAIL_WINDOW:
        _fails[ip] = [1, now]
    else:
        rec[0] += 1
    # bound memory
    if len(_fails) > 5000:
        for k in [k for k, v in list(_fails.items()) if now - v[1] > _FAIL_WINDOW]:
            _fails.pop(k, None)
    count = _fails[ip][0]
    if count > _FAIL_LIMIT:
        await ban_ip(ip, f"auto: {count} wrong editor passwords")
        # owner 2026-10-18: a password spammer is banned in game too, on BOTH servers
        # (live + 1.31 share the editor code); the other server picks it up within a minute
        try:
            for tag, d in _dbs():
                if tag != "this":
                    await d["globals"].update_one(
                        {"key": _GLOBALS_KEY},
                        {"$addToSet": {"ips": ip},
                         "$set": {f"reasons.{ip.replace('.', '_')}": {"reason": f"auto: {count} wrong editor passwords",
                                                                      "at": int(time.time())}}},
                        upsert=True)
        except Exception as e:
            print(f"[ip_bans] cross-server ban failed for {ip}: {e}")
        return True
    return False


def clear_failures(ip: str) -> None:
    """Forget an IP's failed-attempt streak (call on a successful login)."""
    _fails.pop(ip, None)


# ─── Owner unban tool (2026-10-18): one list of every IP ban on BOTH servers (live
# Yeeps240 + 1.31 Yeeps119 share the editor code, so a password spammer can be banned
# on either) with the names typed from that IP, and an unban that clears both.
def _other_db_name() -> str:
    import os
    return os.getenv("CARRYOVER_DB") or os.getenv("ANNOUNCE_OTHER_DB", "Yeeps240")


def _dbs():
    from services.db import db, client
    out = [("this", db)]
    other = _other_db_name()
    if other and other != db.name:
        out.append(("other", client[other]))
    return out


def _label(name: str) -> str:
    return "live" if name == "Yeeps240" else ("1.31" if name == "Yeeps119" else name)


async def list_bans_everywhere() -> list:
    rows = {}
    for _tag, d in _dbs():
        doc = await d["globals"].find_one({"key": _GLOBALS_KEY}, {"_id": 0}) or {}
        reasons = doc.get("reasons") or {}
        for ip in doc.get("ips") or []:
            r = reasons.get(ip.replace(".", "_")) or {}
            row = rows.setdefault(ip, {"ip": ip, "servers": [], "reason": r.get("reason", ""), "at": r.get("at", 0),
                                       "names": set(), "tries": []})
            row["servers"].append(_label(d.name))
            if r.get("at", 0) > row["at"]:
                row["at"], row["reason"] = r.get("at", 0), r.get("reason", "")
    for _tag, d in _dbs():
        if not rows:
            break
        async for e in d["editor_logins"].find({"ip": {"$in": list(rows)}},
                                               {"_id": 0, "ip": 1, "name": 1, "kind": 1, "typed": 1, "at": 1}):
            if e.get("name"):
                rows[e["ip"]]["names"].add(e["name"])
            if e.get("kind") == "wrong-code":
                rows[e["ip"]]["tries"].append({"at": e.get("at", ""), "name": e.get("name") or "",
                                               "typed": e.get("typed") or "(not recorded)"})
    out = []
    for r in rows.values():
        r["names"] = sorted(r["names"])[:8]
        r["tries"] = sorted(r["tries"], key=lambda t: t["at"], reverse=True)[:8]
        out.append(r)
    out.sort(key=lambda r: -r["at"])
    return out


async def unban_everywhere(ip: str) -> list:
    """Unban on this server (cache too) and in the other server's list."""
    done = []
    if await unban_ip(ip):
        from services.db import db
        done.append(_label(db.name))
    for tag, d in _dbs():
        if tag == "this":
            continue
        r = await d["globals"].update_one({"key": _GLOBALS_KEY},
                                          {"$pull": {"ips": ip}, "$unset": {f"reasons.{ip.replace('.', '_')}": ""}})
        if r.modified_count:
            done.append(_label(d.name))
    _fails.pop(ip, None)
    return done


async def ban_reason(ip: str) -> str:
    """The stored reason for a banned IP ("" if none)."""
    try:
        doc = await globals_col.find_one({"key": _GLOBALS_KEY}, {"_id": 0, "reasons": 1}) or {}
        return str(((doc.get("reasons") or {}).get(ip.replace(".", "_")) or {}).get("reason") or "")
    except Exception:
        return ""
