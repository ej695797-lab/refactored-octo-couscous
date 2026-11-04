"""
Public endpoints the 1.31 player APK polls (no login): see services/remote.py.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from services import remote

router = APIRouter()

from config import PUBLIC_BASE_URL as SELF_BASE


@router.get("/client/feed")
async def client_feed():
    anns = [{"id": a["id"], "text": a["text"], "seconds": a["seconds"]}
            for a in await remote.active_announcements()]
    return JSONResponse(content={"announcements": anns,
                                 "disks": await remote.disk_manifest(SELF_BASE)},
                        headers={"Cache-Control": "no-store"})


@router.get("/client/disk/{clip}.b64")
async def client_disk_b64(clip: str):
    """Base64 text of the uploaded audio: the APK's stripped Unity build can only
    read web responses as text, then decodes OGG/WAV natively."""
    import base64
    d = await remote.get_disk(clip)
    if not d:
        return Response(status_code=404)
    return PlainTextResponse(base64.b64encode(bytes(d["data"])).decode(),
                             headers={"Cache-Control": "public, max-age=86400"})


@router.get("/client/screen/{slot}.b64")
async def client_screen_b64(slot: str):
    import base64
    d = await remote.get_screen(slot)
    if not d:
        return Response(status_code=404)
    return PlainTextResponse(base64.b64encode(bytes(d["data"])).decode(),
                             headers={"Cache-Control": "public, max-age=86400"})


@router.get("/client/disk/{clip}")
async def client_disk(clip: str):
    d = await remote.get_disk(clip)
    if not d:
        return Response(status_code=404)
    return Response(content=bytes(d["data"]),
                    media_type=remote.AUDIO_TYPES.get(d.get("ext"), "application/octet-stream"),
                    headers={"Cache-Control": "public, max-age=86400"})


def _clean(t: str) -> str:
    return " ".join(str(t or "").replace("\t", " ").split())


def _wrap(text: str, width: int = 38) -> str:
    """Line-wrap for the floating in-game text (newline encoded as |)."""
    out, line = [], ""
    for w in _clean(text).split(" "):
        if line and len(line) + 1 + len(w) > width:
            out.append(line); line = w
        else:
            line = (line + " " + w).strip()
    if line:
        out.append(line)
    return "|".join(out)


@router.get("/client/feed.txt")
async def client_feed_txt():
    """Same as /client/feed, one tab-separated record per line for the native
    client:  A<TAB>id<TAB>seconds<TAB>text   (| = newline in text)
             D<TAB>clip<TAB>type<TAB>url
             S<TAB>slot<TAB>WxHxFrames<TAB>url   (picture screens)
             F<TAB>accountID<TAB>1<TAB>-          (treat as friend)"""
    lines = []
    for a in await remote.active_announcements():
        lines.append(f"A\t{a['id']}\t{a['seconds']}\t{_wrap(a['text'])}")
    # News popup (editor News tab): shown once per launch to every player; a new id
    # whenever the text changes so everyone sees the edited version.
    try:
        from services.db import globals_col
        np_ = await globals_col.find_one({"key": "news_popup"}, {"_id": 0, "value": 1})
        v = (np_ or {}).get("value") or {}
        if v.get("enabled") and str(v.get("text") or "").strip():
            lines.append(f"A\tnews-{v.get('ts', 0)}\t{int(v.get('seconds') or 15)}\t{_wrap(str(v['text']))}")
    except Exception as e:
        print(f"[feed] news popup failed: {e}")
    for clip, d in (await remote.disk_manifest(SELF_BASE)).items():
        lines.append(f"D\t{clip}\t{d['type']}\t{d['url']}")
    # F lines: everyone online counts as a friend on the client (APK 131b6+),
    # unless globals "friends_everyone" is set false.
    try:
        from services.db import globals_col
        from services.kick import online_account_ids
        flag = await globals_col.find_one({"key": "friends_everyone"}, {"_id": 0, "value": 1})
        if (flag or {}).get("value", True):
            for aid in await online_account_ids():
                lines.append(f"F\t{aid}\t1\t-")
    except Exception as e:
        print(f"[feed] friends list failed: {e}")
    for sc in await remote.screen_manifest(SELF_BASE):
        lines.append(f"S\t{sc['slot']}\t{sc['w']}x{sc['h']}x{sc['frames']}\t{sc['url']}")
    # V line: a floating full-length video playing for everyone (131b8+).
    try:
        from services import video
        vline = await video.video_feed_line(SELF_BASE)
        if vline:
            lines.append(vline)
    except Exception as e:
        print(f"[feed] video line failed: {e}")
    return PlainTextResponse("\n".join(lines) + "\n", headers={"Cache-Control": "no-store"})


_vstate = {"body": "", "ts": 0.0}
_tvstate = {"body": "", "ts": 0.0}


@router.get("/client/tv.txt")
async def client_tv_state():
    """Yeeps TV channel for the 1x1 computer screens (services/tv.py). One T1 line
    or empty. Cached 2 s (every player polls)."""
    import time
    from services import tv
    now = time.time()
    if now - _tvstate["ts"] > 2.0:
        try:
            _tvstate["body"] = await tv.tv_state_line()
            _tvstate["ts"] = now
        except Exception as e:
            print(f"[tv] state line failed: {e}")
            return Response(status_code=503)
    return PlainTextResponse(_tvstate["body"] + "\n", headers={"Cache-Control": "no-store"})


@router.get("/client/video.txt")
async def client_video_state():
    """APK 131b9+ polls this every 3 s so Load/Stop land within seconds. One V2
    line, or empty when nothing is playing. Cached 1.5 s (every player polls)."""
    import time
    from services import video
    now = time.time()
    if now - _vstate["ts"] > 1.5:
        try:
            _vstate["body"] = await video.video_state_line(SELF_BASE)
            _vstate["ts"] = now
        except Exception as e:
            # Never answer "nothing playing" on a DB hiccup -- that would stop
            # every player's video. A non-200 makes the client keep its state.
            print(f"[video] state line failed: {e}")
            return Response(status_code=503)
    return PlainTextResponse(_vstate["body"] + "\n", headers={"Cache-Control": "no-store"})


@router.get("/client/video/{vid}/seg{idx}.b64")
async def client_video_segment(vid: str, idx: int):
    """Base64 of one BSCR frame-segment blob (same format as picture screens, so
    the APK reuses its screen decoder). Normally served by the Node relay straight
    from disk (tunnel rule ^/client/video/); this is only the fallback."""
    from services import video
    d = await video.get_segment(vid, idx)
    if not d:
        return Response(status_code=404)
    return Response(content=d, media_type="text/plain",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/client/video/{vid}/audio.b64")
async def client_video_audio(vid: str):
    """Base64 of the video's OGG soundtrack (stb_vorbis-decoded on the client)."""
    from services import video
    d = await video.get_audio(vid)
    if not d:
        return Response(status_code=404)
    return Response(content=d, media_type="text/plain",
                    headers={"Cache-Control": "public, max-age=86400"})


# ── 2.4.0 test builds: remote client logs ──────────────────────────────────
# The 240 test APK POSTs its Unity log, checkpoint lines and crash records here
# every few seconds, so it can be debugged without the headset on USB.
# Written to logs/devlog.log (trimmed to the last ~20 MB) + collection devlogs
# (TTL 4 days). Only accepted from 2.4.0 builds (X-Build-Tag "240b...").
import os as _os
from datetime import datetime as _dt, timezone as _tz

_DEVLOG_PATH = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "logs", "devlog.log")
_DEVLOG_MAX = 40 * 1024 * 1024
_devlog_ttl_ready = False


@router.post("/client/devlog")
async def client_devlog(request: Request):
    global _devlog_ttl_ready
    tag = request.headers.get("x-build-tag", "")
    if not tag.startswith(("240b", "big", "live", "play")):     # 2.4.0 test builds, big APK, live line (renamed from biglive 2026-10-17)
        return PlainTextResponse("no", status_code=403)
    body = (await request.body())[:300_000].decode("utf-8", "replace")
    # "Player Status Manager: Failed to parse some data <id>" fires for every friend on
    # every friends refresh (~35 lines/s across players, cause not found yet) and filled
    # the Mongo quota; keep one count line per upload instead of every line.
    if "Failed to parse some data" in body:
        _ls = body.splitlines()
        _kept = [l for l in _ls if "Player Status Manager: Failed to parse some data" not in l]
        if len(_kept) != len(_ls):
            _kept.append(f"[server] dropped {len(_ls) - len(_kept)} 'Player Status Manager: Failed to parse some data' lines")
            body = "\n".join(_kept)
    from services.ip_bans import client_ip
    ip = client_ip(request)
    now = _dt.now(_tz.utc)
    try:
        _os.makedirs(_os.path.dirname(_DEVLOG_PATH), exist_ok=True)
        with open(_DEVLOG_PATH, "a", encoding="utf-8") as f:
            for line in body.splitlines():
                f.write(f"{now:%m-%d} {ip} {tag.split('.')[0]} {line}\n")
        if _os.path.getsize(_DEVLOG_PATH) > _DEVLOG_MAX:
            with open(_DEVLOG_PATH, "rb") as f:
                f.seek(-_DEVLOG_MAX // 2, 2)
                keep = f.read()
            with open(_DEVLOG_PATH, "wb") as f:
                f.write(keep)
    except Exception as e:
        print(f"[devlog] file write failed: {e}")
    try:
        from services.db import db
        col = db["devlogs"]
        if not _devlog_ttl_ready:
            # 3-hour TTL backstop (logs are checked + deleted after ~1 h by ~/.config/yeeps/devlog_check.py) keeps devlogs from filling the shared 512 MB Atlas quota
            # (the daily cleanup caps it too). Tolerate an existing index with a
            # different expiry so a mismatch never blocks the insert or log-spams.
            try:
                await col.create_index("at", expireAfterSeconds=10800)
            except Exception:
                pass
            _devlog_ttl_ready = True
        await col.insert_one({"at": now, "tag": tag, "ip": ip, "text": body[:200_000]})
    except Exception as e:
        print(f"[devlog] db write failed: {e}")
    # One-shot remote commands for the test clients, queued by writing
    # logs/devlog_cmd.txt (one per line). "@<ip-prefix> <cmd>" targets one
    # headset (e.g. "@95.102 logcat 300"); untargeted lines go to the next
    # client that uploads. Commands (240b16+): ping, meminfo, logcat N, ls <dir>,
    # strdump <rva> <len>.
    cmd_path = _os.path.join(_os.path.dirname(_DEVLOG_PATH), "devlog_cmd.txt")
    try:
        if _os.path.exists(cmd_path):
            with open(cmd_path, encoding="utf-8") as f:
                lines = f.read()[:8000].splitlines()
            mine, keep = [], []
            for ln in lines:
                if not ln.strip():
                    continue
                if ln.startswith("@"):
                    target, _, cmd = ln[1:].partition(" ")
                    (mine if ip.startswith(target) else keep).append(cmd if ip.startswith(target) else ln)
                else:
                    mine.append(ln)
            if keep:
                with open(cmd_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(keep) + "\n")
            else:
                _os.remove(cmd_path)
            if mine:
                print(f"[devlog] sent commands to {tag.split('.')[0]} {ip}: {' | '.join(mine)[:200]}")
                return PlainTextResponse("\n".join(mine))
    except Exception as e:
        print(f"[devlog] command read failed: {e}")
    return PlainTextResponse("ok")


# ── 2.4.0 /online for the Discord bot ───────────────────────────────
# The one Discord app (and its /online command) runs on the 1.31 server. It asks
# this instance over localhost for the 2.4.0 list: GET 127.0.0.1:8240/bot/online
# with header X-Online-Token = ONLINE_FEED_TOKEN (240 env). Requests that came
# through the Cloudflare tunnel (CF-Connecting-IP set) are refused.
@router.get("/bot/online")
async def bot_online(request: Request):
    import hmac, os
    tok = os.environ.get("ONLINE_FEED_TOKEN", "")
    if (not tok or request.headers.get("cf-connecting-ip")
            or request.client.host not in ("127.0.0.1", "::1")
            or not hmac.compare_digest(request.headers.get("x-online-token", ""), tok)):
        return Response(status_code=404)
    from routers.discord_router import _do_online
    txt = await _do_online()
    return PlainTextResponse(txt.replace("online:**", "online on 2.4.0:**", 1)
                             if "online:**" in txt else "Nobody's online on 2.4.0 right now.",
                             headers={"Cache-Control": "no-store"})

# Cosmetic asset bundles the live APK (live41+) downloads at launch and swaps onto
# the matching cosmetics (native CosmeticTick). Files live in data/cosmetics/.
_COS_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "data", "cosmetics")


@router.get("/client/cosmetics/{name}")
async def client_cosmetic_bundle(name: str):
    import re as _re
    from fastapi.responses import FileResponse
    if not _re.fullmatch(r"[a-z0-9_]+\.bundle", name):
        raise HTTPException(status_code=404)
    path = _os.path.join(_COS_DIR, name)
    if not _os.path.isfile(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/octet-stream",
                        headers={"Cache-Control": "no-cache"})


# Dev trading-card artwork (owner 2026-10-18): full-card textures (back + front, the
# game's tradingCardTexture layout) as raw 512x512 RGB24 rows bottom-up, loaded by
# the live APK (CardArtTick, live55+) with Texture2D.LoadRawTextureData.
_CARD_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "data", "cards")


@router.get("/client/cards/{name}")
async def client_card_art(name: str):
    import re as _re
    from fastapi.responses import FileResponse
    if not _re.fullmatch(r"[a-z0-9_]+\.(rgb|png)", name):
        raise HTTPException(status_code=404)
    path = _os.path.join(_CARD_DIR, name)
    if not _os.path.isfile(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/octet-stream", headers={"Cache-Control": "no-cache"})


# ─── Map builders (live72+) ───────────────────────────────────────────────────
# The APK's creative guard keeps creative ON in a shared map only when a listed
# builder tagged it AND is in the room (services/creative_builders.py).
@router.get("/client/creative_builders")
async def client_creative_builders():
    from fastapi.responses import PlainTextResponse
    from services import creative_builders
    ids = sorted(await creative_builders.builder_ids())
    return PlainTextResponse("\n".join(ids) + "\n", headers={"Cache-Control": "no-cache", "Connection": "close"})


# ─── Single dancing skeletons (owner 2026-10-18, live59+) ─────────────────────
# The Photon bot (same machine) queues spawns for its Photon room with POST /bot/skel;
# every live59 client polls GET /client/skel?room=<photon room>&since=<id> about once a
# second and spawns the game's DancingSkeleton locally. In memory only (spawns older than
# 15 s are dropped). Lines: "<id> <x> <y> <z> <yaw deg> <ttl s>", plus "last <id>".
import collections as _skc
import time as _skt
_SKEL = {"id": 0, "rooms": _skc.defaultdict(lambda: _skc.deque(maxlen=200))}


@router.post("/bot/skel")
async def bot_skel(request: Request):
    from services.ip_bans import client_ip
    if request.headers.get("cf-connecting-ip") or (request.client and request.client.host not in ("127.0.0.1", "::1")):
        raise HTTPException(status_code=404)
    body = await request.json()
    room = str(body.get("room") or "")
    if not room:
        raise HTTPException(status_code=400, detail="room")
    out = []
    for s in body.get("spawns") or []:
        _SKEL["id"] += 1
        import re as _re
        slug = str(s.get("slug") or "")
        slug = slug if _re.fullmatch(r"[a-z0-9_]{1,40}", slug) else "-"
        e = (_SKEL["id"], float(s["x"]), float(s["y"]), float(s["z"]), float(s.get("yaw", 0)),
             float(s.get("ttl", 20)), _skt.time(), max(0, min(9, int(s.get("style", 0)))),
             int(s.get("seed", 0)) & 0xFFFFFFFF, slug)
        _SKEL["rooms"][room].append(e)
        out.append(e[0])
    return {"ok": True, "ids": out}


@router.get("/client/skel")
async def client_skel(room: str = "", since: int = 0):
    from fastapi.responses import PlainTextResponse
    q = _SKEL["rooms"].get(room)
    now = _skt.time()
    # polled ~once a second by every live59+ player: close the connection after each answer
    # so idle keep-alive sockets don't pile up (fd exhaustion 2026-10-18)
    _close = {"Connection": "close", "Cache-Control": "no-store"}
    if since < 0 or not q:
        return PlainTextResponse(f"last {_SKEL['id'] if since < 0 else since}\n", headers=_close)
    # 7th field = dance style (live62+: 0 salsa 1 fast 2 slow 3 spin 4 bounce; live61 ignores it)
    # 8th = seed (random dance), 9th = Mixamo dance slug (live65+; older builds ignore them)
    lines = [f"{i} {x:.2f} {y:.2f} {z:.2f} {yaw:.1f} {ttl:.0f} {st} {seed} {slug}" for i, x, y, z, yaw, ttl, t, st, seed, slug in q
             if i > since and now - t < 15]
    return PlainTextResponse("\n".join(lines + [f"last {max([since] + [e[0] for e in q])}"]) + "\n", headers=_close)



# Mixamo dances for the dancing skeleton (live65+, made by apk-work/fbx-work/mixamo_to_dance.py)
_DANCE_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "data", "dances")


@router.get("/client/dances/{name}")
async def client_dance(name: str):
    import re as _re
    from fastapi.responses import FileResponse
    if not _re.fullmatch(r"[a-z0-9_]+\.(bin|json)", name):
        raise HTTPException(status_code=404)
    path = _os.path.join(_DANCE_DIR, name)
    if not _os.path.isfile(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/octet-stream", headers={"Cache-Control": "no-cache"})
