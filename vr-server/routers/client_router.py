"""
Public endpoints the 1.31 player APK polls (no login): see services/remote.py.
"""

from fastapi import APIRouter, Request
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
             F<TAB>accountID<TAB>1<TAB>-          (treat as friend)
             C<TAB>creative<TAB>all<TAB>room,room   (rooms where creative is allowed)"""
    lines = []
    for a in await remote.active_announcements():
        lines.append(f"A\t{a['id']}\t{a['seconds']}\t{_wrap(a['text'])}")
    for clip, d in (await remote.disk_manifest(SELF_BASE)).items():
        lines.append(f"D\t{clip}\t{d['type']}\t{d['url']}")
    # F lines: everyone online counts as a friend on the client (APK 131b6+),
    # only when globals "friends_everyone" is set true (off by default, owner 2026-10-11).
    try:
        from services.db import globals_col
        from services.kick import online_account_ids
        flag = await globals_col.find_one({"key": "friends_everyone"}, {"_id": 0, "value": 1})
        if (flag or {}).get("value", False):
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
    try:
        lines.append(await _creative_line())
    except Exception as e:
        print(f"[feed] creative line failed: {e}")
    return PlainTextResponse("\n".join(lines) + "\n", headers={"Cache-Control": "no-store"})


_creative = {"line": "", "ts": 0.0}


async def _creative_line() -> str:
    """C line: the rooms where creative mode is allowed. APK 131b20+ turns the room's
    creativeMode back off anywhere else (private worlds are always the owner's
    choice), so a player can't switch Playground to creative to break it. While
    UNLOCK_ALL_MODES is on (building maps), every room is allowed."""
    import time
    from config import UNLOCK_ALL_MODES
    if UNLOCK_ALL_MODES:
        return "C\tcreative\t1\t-"
    if time.time() - _creative["ts"] > 60 or not _creative["line"]:
        from services.db import rooms
        keys = sorted({d["roomKey"] async for d in rooms.find(
            {"isCreative": {"$in": [1, True]}, "roomKey": {"$not": {"$regex": "^(p_|c_)"}}},
            {"_id": 0, "roomKey": 1})})
        _creative.update(line=f"C\tcreative\t0\t{','.join(keys) or '-'}", ts=time.time())
    return _creative["line"]


_vstate = {"body": "", "ts": 0.0}


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
