"""
Rough IP location, entirely local: DB-IP "IP to City Lite" (free, CC BY 4.0,
https://db-ip.com) read with maxminddb. Player IPs are never sent anywhere.

Used by services/device_auth.py: a headset may be tied to an account from a new
connection if it's within GEO_NEAR_KM of where that account usually plays.

DB file: GEO_DB_PATH (default ~/.local/share/yeeps/dbip-city-lite.mmdb). Refresh
monthly from https://download.db-ip.com/free/dbip-city-lite-YYYY-MM.mmdb.gz.
If the file or the library is missing, every lookup returns None (no relaxing).
"""

import math
import time
import os

GEO_DB_PATH = os.path.expanduser(os.getenv("GEO_DB_PATH", "~/.local/share/yeeps/dbip-city-lite.mmdb"))
GEO_NEAR_KM = float(os.getenv("GEO_NEAR_KM", "300"))

_reader = None
_failed = False


def _db():
    global _reader, _failed
    if _reader is None and not _failed:
        try:
            import maxminddb
            _reader = maxminddb.open_database(GEO_DB_PATH, maxminddb.MODE_MMAP)
        except Exception as e:
            _failed = True
            print(f"[geo] location database unavailable: {e}")
    return _reader


def locate(ip: str):
    """(lat, lon) of an IP, or None."""
    db = _db()
    if not db or not ip:
        return None
    try:
        rec = db.get(ip) or {}
        loc = rec.get("location") or {}
        lat, lon = loc.get("latitude"), loc.get("longitude")
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)
    except Exception:
        return None


def distance_km(a, b) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def nearest_km(ip: str, known_ips) -> float | None:
    """Distance (km) from ip to the closest of known_ips, or None if unknown."""
    here = locate(ip)
    if not here:
        return None
    best = None
    for k in known_ips:
        there = locate(k)
        if there:
            d = distance_km(here, there)
            best = d if best is None else min(best, d)
    return best


# Consumer VPN / relay egress ranges worth flagging (the Oct 9 editor intruder
# came through Cloudflare WARP).
_VPN_NETS = tuple(
    tuple(x.split("=", 1)) for x in os.getenv("VPN_NETS", "").split(",") if "=" in x
)  # "cidr=label,cidr=label"; ranges kept private


def vpn_hint(ip: str) -> str:
    """Name of a known VPN/relay the IP belongs to, or ""."""
    c = _VPN_CACHE.get(ip)
    if c and c[0] and c[1] > time.time():
        return c[0]   # flagged by the ip-api lookup (vpn_check) recently
    try:
        import ipaddress
        a = ipaddress.ip_address(ip)
        for net, label in _VPN_NETS:
            n = ipaddress.ip_network(net)
            if a.version == n.version and a in n:
                return label
    except Exception:
        pass
    return ""


def describe(ip: str) -> str:
    """'City, Region, Country' for an IP (best effort), or 'unknown location'."""
    db = _db()
    if not db or not ip:
        return "unknown location"
    try:
        rec = db.get(ip) or {}
        en = lambda d: ((d or {}).get("names") or {}).get("en", "")
        parts = [en(rec.get("city")), en((rec.get("subdivisions") or [{}])[0]), en(rec.get("country"))]
        out = ", ".join(p for p in parts if p)
        return out or "unknown location"
    except Exception:
        return "unknown location"


# Live VPN / proxy / hosting lookup (owner 2026-11-02: the hand-made list above
# missed native's AWS IPs and most raider IPs). ip-api.com free tier: no key,
# 45 lookups/min, so answers are cached (flagged 24h, clean 6h). If the lookup
# fails the IP is let through (the list above still applies).
_VPN_CACHE: dict = {}      # ip -> (label or "", expires)
_VPN_ASNS = {"AS54113": "Fastly relay (iCloud Private Relay / Mozilla VPN)",
             "AS13335": "Cloudflare WARP"}


async def vpn_check(ip: str) -> str:
    """vpn_hint plus a live ip-api.com lookup. Label of the VPN/proxy/host, or ""."""
    h = vpn_hint(ip)
    if h or not ip or ip == "?":
        return h
    c = _VPN_CACHE.get(ip)
    if c and c[1] > time.time():
        return c[0]
    try:
        import ipaddress
        if not ipaddress.ip_address(ip).is_global:
            return ""
        import httpx
        async with httpx.AsyncClient(timeout=4) as cl:
            r = await cl.get(f"http://ip-api.com/json/{ip}",
                             params={"fields": "status,proxy,hosting,as,isp"})
        d = r.json()
    except Exception:
        return ""
    if d.get("status") != "success":
        return ""
    asn = str(d.get("as") or "").split(" ")[0]
    isp = d.get("isp") or "unknown"
    if asn in _VPN_ASNS:
        label = _VPN_ASNS[asn]
    elif d.get("proxy"):
        label = f"VPN/proxy ({isp})"
    elif d.get("hosting"):
        label = f"hosting/datacenter ({isp})"
    else:
        label = ""
    _VPN_CACHE[ip] = (label, time.time() + (86400 if label else 21600))
    if len(_VPN_CACHE) > 5000:
        _VPN_CACHE.pop(next(iter(_VPN_CACHE)))
    return label
