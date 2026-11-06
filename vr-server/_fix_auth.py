import sys
sys.path.insert(0, r"C:\server-live\vr-server")
p = "routers/editor_router.py"
s = open(p, encoding="utf-8").read()
old = 'def _authed_name(request: Request) -> str:\n    """Return the Meta name from a valid signed cookie, else None."""\n    if EDITOR_LOCKED:\n        return None\n    if _from_vpn(request):\n        return None   # no editor use from a VPN, even with a valid session\n    tok = request.cookies.get(_COOKIE, "")'
new = 'def _authed_name(request: Request) -> str:\n    # Password auth disabled: always return "host".\n    return "host"'
assert old in s, "old not found"
s = s.replace(old, new)
open(p, "w", encoding="utf-8").write(s)
print("done")
