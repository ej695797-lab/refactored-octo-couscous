"""
Floating video for the player APK (build 131b8+): a full-length video + audio
shown on a world-space panel in front of every player, looping until stopped
from the editor's Announce tab.

Pipeline (all server-side, bundled static ffmpeg via imageio-ffmpeg):
  upload (mp4/webm/mov/gif) -> ffmpeg ->
    * frames: scaled+padded to WxH, FPS, rgb24, packed into "BSCR" segment blobs
      (the SAME format the picture-screen code already decodes on the client, so
      no new native decoder is needed — see services/remote.convert_screen_image
      and native-lib.cpp DecodeScreen). Each segment holds SEG_FRAMES frames.
    * audio: mono OGG/Vorbis (stb_vorbis-decodable, like the remote music disks).

Storage:
  video_assets   one meta doc per video {id,w,h,fps,frames,segFrames,nSegments,
                 durationMs,audioFmt,audioSize,name,by,ts,size}
  files          <VIDEO_DIR>/<id>/seg<n>.b64 + audio.b64 (base64 text, written
                 once, streamed to players by the Node relay -- see VIDEO_DIR)

Only ONE video is "active" at a time (globals key "video"); the client feed adds
a V line while something is active. Stop clears it and deletes the asset.

The frame display is keyed to AudioSource.time on the client, so audio and video
stay in sync and loop together without the server streaming a clock.
"""

import io
import os
import struct
import subprocess
import time
import uuid
import zlib

from services.db import db, globals_col

video_assets = db["video_assets"]
video_segments = db["video_segments"]
video_audio = db["video_audio"]

# ── tunables (kept modest: this streams to a VR headset over wifi) ──────
VID_W, VID_H = 256, 144          # 16:9 panel, letterboxed
VID_FPS = 10
MAX_SECONDS = 120                # hard cap on video length
SEG_FRAMES = 60                  # frames per downloaded segment (~1-3 MB zlib)
AUDIO_AR = 44100                 # mono OGG sample rate
MAX_UPLOAD = 64 * 1024 * 1024    # 64 MB source file cap


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _bscr_segment(frames_rgb: list, w: int, h: int, fps: int) -> bytes:
    """Pack a list of raw rgb24 frame buffers into one "BSCR" blob, identical to
    services.remote.convert_screen_image's output so the client reuses DecodeScreen.
    Rows are already bottom-first (ffmpeg vflip), delays are fixed at 1000/fps."""
    n = len(frames_rgb)
    delay = max(1, min(65535, round(1000 / fps)))
    raw = b"".join(frames_rgb)
    return (b"BSCR" + struct.pack("<HHH", w, h, n)
            + struct.pack(f"<{n}H", *([delay] * n)) + zlib.compress(raw, 1))   # level 1: ~4x faster, +13% size


def convert_video(data: bytes, name: str = "", by: str = "") -> dict:
    """Blocking (ffmpeg + zlib) — call via asyncio.to_thread. Returns the meta
    dict plus the raw pieces to store: {"meta", "segments": [bytes], "audio",
    "audioFmt"}. Streams frames so peak memory stays ~one segment."""
    import tempfile
    exe = _ffmpeg()
    w, h, fps = VID_W, VID_H, VID_FPS
    with tempfile.NamedTemporaryFile(suffix=".src", delete=False) as tf:
        tf.write(data)
        src = tf.name
    try:
        # 1) video -> raw rgb24 frames on stdout, scaled+letterboxed, vertically
        #    flipped (BSCR wants bottom row first), capped to MAX_SECONDS.
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,fps={fps},vflip")
        vcmd = [exe, "-hide_banner", "-loglevel", "error", "-t", str(MAX_SECONDS),
                "-i", src, "-an", "-vf", vf, "-pix_fmt", "rgb24",
                "-f", "rawvideo", "-"]
        frame_bytes = w * h * 3
        segments, buf, cur, total = [], bytearray(), [], 0
        proc = subprocess.Popen(vcmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        while True:
            chunk = proc.stdout.read(frame_bytes * 8)
            if not chunk:
                break
            buf.extend(chunk)
            while len(buf) >= frame_bytes:
                cur.append(bytes(buf[:frame_bytes]))
                del buf[:frame_bytes]
                total += 1
                if len(cur) >= SEG_FRAMES:
                    segments.append(_bscr_segment(cur, w, h, fps)); cur = []
                if total >= MAX_SECONDS * fps:
                    break
            if total >= MAX_SECONDS * fps:
                break
        proc.stdout.close()
        err = proc.stderr.read().decode("utf-8", "ignore")
        proc.wait()
        if cur:
            segments.append(_bscr_segment(cur, w, h, fps))
        if total == 0:
            raise ValueError(f"no video frames decoded ({err[:200] or 'unknown'})")

        # 2) audio -> mono OGG/Vorbis (small; stb_vorbis decodes it on the client)
        acmd = [exe, "-hide_banner", "-loglevel", "error", "-t", str(MAX_SECONDS),
                "-i", src, "-vn", "-ac", "1", "-ar", str(AUDIO_AR),
                "-c:a", "libvorbis", "-q:a", "3", "-f", "ogg", "-"]
        ar = subprocess.run(acmd, capture_output=True)
        audio = ar.stdout or b""
        audio_fmt = "ogg" if audio else ""

        meta = {
            "w": w, "h": h, "fps": fps, "frames": total,
            "segFrames": SEG_FRAMES, "nSegments": len(segments),
            "durationMs": int(total * 1000 / fps),
            "audioFmt": audio_fmt, "audioSize": len(audio),
            "name": (name or "video")[:80], "by": by,
            "size": sum(len(s) for s in segments) + len(audio),
        }
        return {"meta": meta, "segments": segments, "audio": audio, "audioFmt": audio_fmt}
    finally:
        try:
            os.unlink(src)
        except Exception:
            pass


# Self-hosted (2026-10-08): segments/audio are written ONCE as base64 text files
# (<VIDEO_DIR>/<id>/seg<n>.b64, audio.b64) and streamed to players by the Node
# relay (tunnel routes api.your-domain.example/client/video/* to it). Serving them from
# this FastAPI process (Mongo blob -> base64 per request) blew its memory up and
# froze the game server under ~30 segment downloads. Only the meta stays in Mongo.
VIDEO_DIR = os.path.expanduser(os.getenv("PUBLIC_VIDEO_DIR", "~/yeeps-public/video"))
# Host the APK fetches the chunks from: proxy.your-domain.example is tunnelled wholly to
# the Node relay (the APK accepts any host). Empty -> same host as the API.
VIDEO_BASE_URL = os.getenv("VIDEO_BASE_URL", os.getenv("YEEPS_PROXY_URL", "https://proxy.your-domain.example")).rstrip("/")


def _write_files(vid: str, segments, audio) -> None:
    import base64
    d = os.path.join(VIDEO_DIR, vid)
    os.makedirs(d, exist_ok=True)
    for i, seg in enumerate(segments):
        with open(os.path.join(d, f"seg{i}.b64"), "wb") as f:
            f.write(base64.b64encode(seg))
    if audio:
        with open(os.path.join(d, "audio.b64"), "wb") as f:
            f.write(base64.b64encode(audio))


async def store_video(conv: dict) -> dict:
    """Persist a converted video and make it the active video. Returns the stored
    meta (with id). Deletes any previously stored video first (one at a time)."""
    import asyncio
    await _delete_all_assets()
    vid = uuid.uuid4().hex[:12]
    meta = dict(conv["meta"]); meta["id"] = vid; meta["ts"] = int(time.time())
    await asyncio.to_thread(_write_files, vid, conv["segments"], conv["audio"] if conv["audio"] else b"")
    await video_assets.insert_one(dict(meta))
    meta.pop("_id", None)
    return meta


async def _delete_all_assets() -> None:
    import shutil
    await video_assets.delete_many({})
    await video_segments.delete_many({})     # legacy Mongo blobs (pre self-hosting)
    await video_audio.delete_many({})
    if os.path.isdir(VIDEO_DIR):
        # Yeeps TV videos (services/tv.py) share this folder: keep theirs
        tv_ids = {d["id"] async for d in db["tv_assets"].find({}, {"_id": 0, "id": 1})}
        for name in os.listdir(VIDEO_DIR):
            if name not in tv_ids:
                shutil.rmtree(os.path.join(VIDEO_DIR, name), ignore_errors=True)


async def video_gen() -> int:
    """Generation counter, bumped by every Load and Stop. An upload records the
    generation it started from and is DROPPED if it changed meanwhile -- so a
    slow conversion that finishes after you press Stop can't bring the video back."""
    doc = await globals_col.find_one({"key": "video"}, {"_id": 0, "gen": 1})
    return int((doc or {}).get("gen") or 0)


class StaleVideo(Exception):
    pass


async def set_active(vid: str, loop: bool = True) -> None:
    await globals_col.update_one({"key": "video"},
                                 {"$set": {"key": "video",
                                           "value": {"id": vid, "loop": bool(loop),
                                                     "ts": int(time.time())}},
                                  "$inc": {"gen": 1}},
                                 upsert=True)


async def stop_video() -> bool:
    """Clear the active video and delete its stored assets. Returns True if one
    was playing. Bumps the generation so in-flight uploads are discarded."""
    doc = await globals_col.find_one({"key": "video"}, {"_id": 0, "value": 1})
    was = bool((doc or {}).get("value"))
    await globals_col.update_one({"key": "video"}, {"$set": {"value": None}, "$inc": {"gen": 1}},
                                 upsert=True)
    await _delete_all_assets()
    return was


async def store_and_play(conv: dict, loop: bool, expected_gen) -> dict:
    """Store a converted video and make it active, unless a Load/Stop happened
    since `expected_gen` was read (then raise StaleVideo and store nothing)."""
    if expected_gen is not None and int(expected_gen) != await video_gen():
        raise StaleVideo("stopped or replaced while converting")
    meta = await store_video(conv)
    await set_active(meta["id"], loop)
    return meta


def parse_packed(data: bytes) -> dict:
    """Browser-converted upload (editor does decode/scale/compress, so the server
    does no CPU work):  "BVID" u32 headerLen  header-JSON  seg0 seg1 ... audio
    header = {name, w, h, fps, frames, segFrames, segs:[len...], audioFmt, audioLen, loop, gen}"""
    import json
    if data[:4] != b"BVID":
        raise ValueError("not a packed video")
    hl = struct.unpack_from("<I", data, 4)[0]
    hdr = json.loads(bytes(data[8:8 + hl]).decode("utf-8"))
    off = 8 + hl
    mv = memoryview(data)
    w, h, fps = int(hdr["w"]), int(hdr["h"]), int(hdr["fps"])
    if not (0 < w <= 1024 and 0 < h <= 1024 and 0 < fps <= 30):
        raise ValueError("bad size/fps")
    segs, frames = [], 0
    for L in hdr["segs"]:
        s = mv[off:off + int(L)]; off += int(L)
        if bytes(s[:4]) != b"BSCR" or struct.unpack_from("<HH", s, 4) != (w, h):
            raise ValueError("bad segment")
        frames += struct.unpack_from("<H", s, 8)[0]
        segs.append(s)
    if not segs or frames > MAX_SECONDS * fps + fps:
        raise ValueError("no frames / too long")
    alen = int(hdr.get("audioLen") or 0)
    audio = mv[off:off + alen] if alen else b""
    afmt = str(hdr.get("audioFmt") or "") if len(audio) else ""
    if afmt == "wav" and bytes(audio[:4]) != b"RIFF":
        audio, afmt = b"", ""
    meta = {"w": w, "h": h, "fps": fps, "frames": frames,
            "segFrames": int(hdr.get("segFrames") or SEG_FRAMES), "nSegments": len(segs),
            "durationMs": int(frames * 1000 / fps), "audioFmt": afmt, "audioSize": len(audio),
            "name": str(hdr.get("name") or "video")[:80],
            "size": sum(len(s) for s in segs) + len(audio)}
    return {"meta": meta, "segments": segs, "audio": audio, "audioFmt": afmt,
            "loop": bool(hdr.get("loop", True)), "gen": hdr.get("gen")}


async def active_video() -> dict | None:
    doc = await globals_col.find_one({"key": "video"}, {"_id": 0, "value": 1})
    v = (doc or {}).get("value")
    if not v or not v.get("id"):
        return None
    meta = await video_assets.find_one({"id": v["id"]}, {"_id": 0})
    if not meta:
        return None
    meta["loop"] = bool(v.get("loop", True))
    # play-once (owner 2026-10-18 "make the videos stop at the end"): once the video's
    # length (+ 30 s for slow downloads; live61+ hides it itself at the end) has passed it counts as
    # stopped, so every client's 3 s poll gets "" and removes the panel
    if not meta["loop"] and v.get("ts") and \
            time.time() > int(v["ts"]) + int(meta.get("durationMs") or 0) / 1000 + 30:
        return None
    return meta


def _file_b64(vid: str, name: str):
    p = os.path.join(VIDEO_DIR, os.path.basename(vid), name)
    if os.path.isfile(p):
        with open(p, "rb") as f:
            return f.read()
    return None


async def get_segment(vid: str, idx: int):
    """Base64 bytes of one segment (fallback path; normally Node serves the file)."""
    return _file_b64(vid, f"seg{int(idx)}.b64")


async def get_audio(vid: str):
    return _file_b64(vid, "audio.b64")


async def video_state_line(base_url: str) -> str:
    """/client/video.txt for APK 131b9+, polled every 3 s. One V2 line or "":
        V2 id WxHxFPS nSeg segFrames frames loop audioUrl segBase
    (frames/segFrames let the client start after segment 0 and stream the rest)."""
    m = await active_video()
    if not m:
        return ""
    base_url = VIDEO_BASE_URL or base_url
    # The client appends "<n>.b64" -> .../v.bin?s=seg<n>.b64 . The .bin path makes
    # Cloudflare edge-cache each chunk (see the Node relay route in server.js).
    seg_base = f"{base_url}/client/video/{m['id']}/v.bin?s=seg"
    audio = (f"{base_url}/client/video/{m['id']}/a.bin?v={m['ts']}"
             if m.get("audioFmt") else "-")
    return (f"V2\t{m['id']}\t{m['w']}x{m['h']}x{m['fps']}\t{m['nSegments']}\t"
            f"{m.get('segFrames') or SEG_FRAMES}\t{m['frames']}\t"
            f"{1 if m.get('loop', True) else 0}\t{audio}\t{seg_base}")


async def video_feed_line(base_url: str) -> str | None:
    """The feed.txt 'V' line for the active video, or None.
        V <id> WxHxFPS nSegments loop audioUrl segBaseUrl
    segBaseUrl is completed by the client as <segBaseUrl><n>.b64 ."""
    m = await active_video()
    if not m:
        return None
    base_url = VIDEO_BASE_URL or base_url
    # The client appends "<n>.b64" -> .../v.bin?s=seg<n>.b64 . The .bin path makes
    # Cloudflare edge-cache each chunk (see the Node relay route in server.js).
    seg_base = f"{base_url}/client/video/{m['id']}/v.bin?s=seg"
    audio = (f"{base_url}/client/video/{m['id']}/a.bin?v={m['ts']}"
             if m.get("audioFmt") else "-")
    return (f"V\t{m['id']}\t{m['w']}x{m['h']}x{m['fps']}\t{m['nSegments']}\t"
            f"{1 if m.get('loop', True) else 0}\t{audio}\t{seg_base}")
