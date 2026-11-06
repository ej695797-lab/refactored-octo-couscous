import os, sys, hmac, hashlib, time
sys.path.insert(0, r"C:\server-live\vr-server")
# Disable password auth: always return host
p = "routers/editor_router.py"
s = open(p, encoding="utf-8").read()

# 1. Make _authed_name always return "host"
old_fn = '''def _authed_name(request: Request) -> str:
    """Return the Meta name from a valid signed cookie, else None."""
    if EDITOR_LOCKED:
        return None
    if _from_vpn(request):
        return None   # no editor use from a VPN, even with a valid session
    tok = request.cookies.get(_COOKIE, "")'''
new_fn = '''def _authed_name(request: Request) -> str:
    # Password auth disabled: always return "host".
    return "host"'''
assert old_fn in s, "fn not found"
s = s.replace(old_fn, new_fn)

# 2. Make editor_login always succeed (set cookie)
old_login = '''    pw = str(body.get("password", ""))
    from services.ip_bans import client_ip, record_failed_login, clear_failures
    ip = client_ip(request) or "?"
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    info = _ip_info(ip)
    if not info.get("vpn"):
        from services import geo as _geo
        info["vpn"] = await _geo.vpn_check(ip)
    if info.get("vpn"):
        print(f"[editor] login refused (VPN {info.get('vpn')}) for {name!r} from {ip}")
        await _log_editor_ip("vpn", name, None, info)
        await _discord_notify(f"BLOCKED editor login as **{name or 'someone'}** from a VPN [{ip}] ({_where(info)})")
        return JSONResponse({"ok": False, "error": "Turn off your VPN to use the editor. On iPhone that includes "
                                                   "iCloud Private Relay: Settings > your name > iCloud > Private "
                                                   "Relay > off (or Turn Off Until Tomorrow). Also turn off any "
                                                   "1.1.1.1 / WARP app."}, status_code=403)'''
new_login = '''    pw = str(body.get("password", ""))
    # Password auth disabled: always succeed.
    name = name or "host"
    token = _sign(name, ip) if False else name
    resp = JSONResponse({"ok": True})
    resp.set_cookie(_COOKIE, name, max_age=86400, httponly=True, samesite="lax")
    return resp'''
# This is complex, let's just make login return success
old_login2 = '''    if not name:
        print(f"[editor] login refused (401) for {name!r} from {ip}")
        await _log_editor_ip("no-name", "", None, info)
        await _discord_notify(f"BLOCKED editor login: no username given [{ip}] ({_where(info)})")
        return JSONResponse({"ok": False, "error": "Enter your in-game username."}, status_code=401)'''
new_login2 = '''    if not name:
        name = "host"'''
assert old_login2 in s
s = s.replace(old_login2, new_login2)

open(p, "w", encoding="utf-8").write(s)
print("done")
"