"""
Remote control for the 1.31 player APK (build 131b2+): announcements and
remotely replaced music-disk audio.

The APK polls GET /client/feed every ~30 s:
    {"announcements": [{"id", "text", "seconds"}],
     "disks": {"<AudioClip name>": {"url": ".../client/disk/<clip>", "type": "ogg"}}}
- announcements: shown once each (by id) as floating text in front of the player.
- disks: downloaded at launch; whenever a record's AudioClip (e.g. "TheFinalHider")
  plays, the downloaded clip plays instead. No new APK needed to change songs.

Storage: globals key "announcements" (list), collection "disk_audio".
Admin side: editor API (/editor/api/announce, /editor/api/disks/{record}).
"""

import time
import uuid

from services.db import db, globals_col

disk_audio = db["disk_audio"]

# The APK decodes these natively (stb_vorbis / 16-bit PCM WAV); MP3 is not supported.
AUDIO_TYPES = {"ogg": "audio/ogg", "wav": "audio/wav"}
MAX_ANNOUNCEMENTS = 20


def clip_name_for(record: str) -> str:
    """record_theFinalHider / theFinalHider / TheFinalHider -> TheFinalHider
    (the AudioClip name the game plays for that disk)."""
    r = (record or "").strip()
    if r.lower().startswith("record_"):
        r = r[len("record_"):]
    return r[:1].upper() + r[1:]


def _other_globals():
    """The OTHER server's globals (1.31 <-> live) so an announcement sent from either
    editor reaches every player (owner 2026-10-18: data managers' announcements from
    the 1.31 editor never showed on live). Live: CARRYOVER_DB; 1.31: ANNOUNCE_OTHER_DB
    (default Yeeps240)."""
    import os as _os
    from services.db import client as _mc
    other = _os.getenv("CARRYOVER_DB") or _os.getenv("ANNOUNCE_OTHER_DB", "Yeeps240")
    if not other or other == _os.getenv("DB_NAME", "Yeeps119"):
        return None
    return _mc[other]["globals"]


async def active_announcements() -> list:
    now = time.time()
    out, seen = [], set()
    cols = [globals_col]
    try:
        o = _other_globals()
        if o is not None:
            cols.append(o)
    except Exception as e:
        print(f"[announce] other server read failed: {e}")
    for col in cols:
        try:
            doc = await col.find_one({"key": "announcements"}, {"_id": 0, "value": 1}) or {}
        except Exception:
            continue
        for a in doc.get("value") or []:
            if float(a.get("until", 0)) > now and a.get("id") not in seen:
                seen.add(a.get("id")); out.append(a)
    return out


async def add_announcement(text: str, seconds: int = 10, active_minutes: int = 10,
                           by: str = "") -> dict:
    """Queue a message for every player. `seconds` = how long it stays on screen;
    `active_minutes` = how long players who launch later will still receive it."""
    a = {"id": uuid.uuid4().hex[:12], "text": text.strip()[:300],
         "seconds": max(3, min(int(seconds or 10), 60)),
         "until": time.time() + max(1, min(int(active_minutes or 10), 1440)) * 60,
         "by": by, "createdAt": int(time.time())}
    _own = await globals_col.find_one({"key": "announcements"}, {"_id": 0, "value": 1}) or {}
    keep = [x for x in (_own.get("value") or []) if float(x.get("until", 0)) > time.time()][-(MAX_ANNOUNCEMENTS - 1):]
    await globals_col.update_one({"key": "announcements"},
                                 {"$set": {"key": "announcements", "value": keep + [a]}},
                                 upsert=True)
    return a


async def clear_announcements() -> None:
    await globals_col.update_one({"key": "announcements"},
                                 {"$set": {"key": "announcements", "value": []}}, upsert=True)
    try:
        o = _other_globals()
        if o is not None:
            await o.update_one({"key": "announcements"}, {"$set": {"value": []}})
    except Exception as e:
        print(f"[announce] other server clear failed: {e}")


async def disk_manifest(base_url: str) -> dict:
    out = {}
    async for d in disk_audio.find({}, {"_id": 0, "clip": 1, "ext": 1, "ts": 1}):
        out[d["clip"]] = {"url": f"{base_url}/client/disk/{d['clip']}?v={d.get('ts', 0)}",
                          "type": d.get("ext", "ogg")}
    return out


async def set_disk(record: str, ext: str, data: bytes, by: str = "") -> str:
    clip = clip_name_for(record)
    await disk_audio.update_one(
        {"clip": clip},
        {"$set": {"clip": clip, "ext": ext, "data": data, "size": len(data),
                  "by": by, "ts": int(time.time())}},
        upsert=True)
    return clip


async def get_disk(clip: str):
    return await disk_audio.find_one({"clip": clip}, {"_id": 0})


async def delete_disk(record: str) -> bool:
    res = await disk_audio.delete_one({"clip": clip_name_for(record)})
    return res.deleted_count > 0


# ─── Picture screens (APK 131b3+) ─────────────────────────────────────
# Each screen block has a slot; the picture uploaded for a slot shows on every
# placed copy of that block. Uploads (PNG/JPG/GIF/WebP) are converted here into
# the small binary the APK decodes (see native-lib.cpp DecodeScreen):
#   "BSCR" u16 w, u16 h, u16 frames, u16 delayMs[frames], zlib(RGB24 frames,
#   bottom row first).
screen_images = db["screen_images"]

# slot -> (label, texture width, texture height). Sizes follow the picture area
# of each block (frame border excluded), so images aren't stretched.
SCREEN_SLOTS = {
    "screen": ("Screen (3x3, everyone, creative)", 256, 256),
    "blaze": ("BLAZE Screen (3x2, blaze role)", 288, 184),
}
SCREEN_RAW_BUDGET = 8 * 1024 * 1024     # decoded RGB bytes for all frames
SCREEN_MAX_FRAMES = 60


def convert_screen_image(data: bytes, slot: str) -> dict:
    """-> {"blob": bytes, "w", "h", "frames", "durationMs", "preview": png bytes}."""
    import io
    import struct
    import zlib
    from PIL import Image, ImageOps, ImageSequence

    _, W, H = SCREEN_SLOTS[slot]
    im = Image.open(io.BytesIO(data))
    frames, delays = [], []
    for fr in ImageSequence.Iterator(im):
        rgba = fr.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
        bg.alpha_composite(rgba)
        frames.append(ImageOps.pad(bg.convert("RGB"), (W, H), color=(0, 0, 0),
                                   method=Image.Resampling.LANCZOS))
        d = fr.info.get("duration", im.info.get("duration", 100)) or 100
        delays.append(max(20, int(d)))
        if len(frames) >= 600:
            break
    if not frames:
        raise ValueError("no frames in image")

    cap = max(1, min(SCREEN_MAX_FRAMES, SCREEN_RAW_BUDGET // (W * H * 3)))
    if len(frames) > cap:
        # keep `cap` evenly spaced frames; each kept frame lasts as long as the
        # frames it stands in for, so the animation keeps its speed
        idx = [int(i * len(frames) / cap) for i in range(cap)]
        bounds = idx[1:] + [len(frames)]
        delays = [sum(delays[a:b]) for a, b in zip(idx, bounds)]
        frames = [frames[i] for i in idx]
    delays = [min(d, 65535) for d in delays]

    raw = b"".join(f.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes() for f in frames)
    blob = (b"BSCR" + struct.pack("<HHH", W, H, len(frames))
            + struct.pack(f"<{len(delays)}H", *delays) + zlib.compress(raw, 9))
    pv = io.BytesIO()
    frames[0].save(pv, "PNG")
    return {"blob": blob, "w": W, "h": H, "frames": len(frames),
            "durationMs": sum(delays), "preview": pv.getvalue()}


async def set_screen(slot: str, data: bytes, by: str = "") -> dict:
    import asyncio
    conv = await asyncio.to_thread(convert_screen_image, data, slot)   # CPU-heavy for big GIFs
    doc = {"slot": slot, "data": conv["blob"], "preview": conv["preview"],
           "w": conv["w"], "h": conv["h"], "frames": conv["frames"],
           "durationMs": conv["durationMs"], "size": len(conv["blob"]),
           "by": by, "ts": int(time.time())}
    await screen_images.update_one({"slot": slot}, {"$set": doc}, upsert=True)
    return {k: v for k, v in doc.items() if k not in ("data", "preview")}


async def get_screen(slot: str, preview: bool = False):
    proj = {"_id": 0, "data": 0} if preview else {"_id": 0, "preview": 0}
    return await screen_images.find_one({"slot": slot}, proj)


async def delete_screen(slot: str) -> bool:
    res = await screen_images.delete_one({"slot": slot})
    return res.deleted_count > 0


async def screen_manifest(base_url: str) -> list:
    out = []
    async for d in screen_images.find({}, {"_id": 0, "data": 0, "preview": 0}):
        out.append({"slot": d["slot"], "w": d["w"], "h": d["h"], "frames": d["frames"],
                    "url": f"{base_url}/client/screen/{d['slot']}?v={d.get('ts', 0)}"})
    return out
