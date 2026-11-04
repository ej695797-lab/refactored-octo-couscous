"""
Yeeps TV (owner 2026-11-01): every 1x1 computer block shows the same video
channel, with sound from the nearest computer.

How the owner adds videos: drop video files into the Windows folder
TV_DROP_DIR on the server host ("Videos\\Yeeps TV"). A background task converts each
new file once; deleting the file removes it from the channel. The channel plays
the ready videos in a random order (reshuffled every cycle), and every player
sees the same spot because the schedule is a pure function of the server clock.

Format: the same "BSCR" segments the floating video uses (services/video.py),
but small (TV_W x TV_H) and with a matching OGG audio chunk per segment so long
videos never need one huge audio decode on the headset. Files live in
<VIDEO_DIR>/<id>/seg<n>.b64 + aud<n>.b64 and are served by the Node relay.
Client poll: /client/tv.txt, one line or "":
    T1 id WxHxFPS nSeg segFrames frames startMs segBase audBase
startMs = server epoch ms when this video started (client time = now - startMs).
"""

import asyncio
import base64
import hashlib
import os
import random
import shutil
import subprocess
import time

from services.db import db, globals_col
from services.video import VIDEO_DIR, VIDEO_BASE_URL, _bscr_segment, _ffmpeg

tv_assets = db["tv_assets"]

TV_W, TV_H, TV_FPS = 128, 96, 12
TV_SEG_FRAMES = 120              # 10 s per segment (video + its audio chunk)
TV_AUDIO_AR = 22050
TV_MAX_SECONDS = 3 * 3600
TV_DROP_DIR = os.getenv("TV_DROP_DIR", os.path.expanduser("~/Videos/Yeeps TV"))
VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".gif")


def _source_key(path: str, st) -> str:
    return hashlib.sha1(f"{os.path.basename(path)}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]


def _convert(src: str, vid: str) -> dict:
    """Blocking. Streams ffmpeg output straight to segment files (low memory)."""
    exe = _ffmpeg()
    w, h, fps, segf = TV_W, TV_H, TV_FPS, TV_SEG_FRAMES
    out = os.path.join(VIDEO_DIR, vid)
    os.makedirs(out, exist_ok=True)
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,fps={fps},vflip")
    vcmd = ["nice", "-n", "15", exe, "-hide_banner", "-loglevel", "error", "-t", str(TV_MAX_SECONDS),
            "-i", src, "-an", "-vf", vf, "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    fb = w * h * 3
    proc = subprocess.Popen(vcmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    nseg, total, cur, buf = 0, 0, [], bytearray()
    while True:
        chunk = proc.stdout.read(fb * 12)
        if not chunk:
            break
        buf.extend(chunk)
        while len(buf) >= fb:
            cur.append(bytes(buf[:fb])); del buf[:fb]; total += 1
            if len(cur) >= segf:
                with open(os.path.join(out, f"seg{nseg}.b64"), "wb") as f:
                    f.write(base64.b64encode(_bscr_segment(cur, w, h, fps)))
                nseg += 1; cur = []
    proc.wait()
    if cur:
        with open(os.path.join(out, f"seg{nseg}.b64"), "wb") as f:
            f.write(base64.b64encode(_bscr_segment(cur, w, h, fps)))
        nseg += 1
    if total == 0:
        raise ValueError("no video frames decoded")

    # audio: one raw PCM pass, cut into exact per-segment chunks, each -> OGG
    acmd = ["nice", "-n", "15", exe, "-hide_banner", "-loglevel", "error", "-t", str(TV_MAX_SECONDS),
            "-i", src, "-vn", "-ac", "1", "-ar", str(TV_AUDIO_AR), "-f", "s16le", "-"]
    ap = subprocess.Popen(acmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    per = TV_AUDIO_AR * 2 * segf // fps          # bytes of s16 mono per segment
    has_audio = False
    for i in range(nseg):
        pcm = ap.stdout.read(per)
        if not pcm:
            pcm = b""
        if len(pcm) < per and i < nseg - 1:
            pcm += b"\0" * (per - len(pcm))
        if not pcm:
            continue
        enc = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(TV_AUDIO_AR),
                              "-ac", "1", "-i", "-", "-c:a", "libvorbis", "-q:a", "2", "-f", "ogg", "-"],
                             input=pcm, capture_output=True)
        if enc.stdout:
            has_audio = True
            with open(os.path.join(out, f"aud{i}.b64"), "wb") as f:
                f.write(base64.b64encode(enc.stdout))
    ap.stdout.close(); ap.wait()
    return {"w": w, "h": h, "fps": fps, "segFrames": segf, "nSegments": nseg, "frames": total,
            "durationMs": int(total * 1000 / fps), "audio": has_audio}


async def scan_drop_folder() -> None:
    """Sync tv_assets with the drop folder: convert new files, drop removed ones."""
    if not os.path.isdir(TV_DROP_DIR):
        try:
            os.makedirs(TV_DROP_DIR, exist_ok=True)
        except Exception:
            return
    present = {}
    for name in sorted(os.listdir(TV_DROP_DIR)):
        p = os.path.join(TV_DROP_DIR, name)
        if not name.lower().endswith(VIDEO_EXT) or not os.path.isfile(p):
            continue
        st = os.stat(p)
        if time.time() - st.st_mtime < 20:      # still being copied
            continue
        present[_source_key(p, st)] = (p, name)
    known = {d["src"]: d async for d in tv_assets.find({}, {"_id": 0})}
    for key, d in known.items():
        if key not in present:
            await tv_assets.delete_one({"src": key})
            shutil.rmtree(os.path.join(VIDEO_DIR, d["id"]), ignore_errors=True)
            print(f"[tv] removed {d.get('name')}")
    for key, (p, name) in present.items():
        if key in known:
            continue
        vid = hashlib.sha1(key.encode()).hexdigest()[:12]
        print(f"[tv] converting {name} ...")
        t0 = time.time()
        try:
            meta = await asyncio.to_thread(_convert, p, vid)
        except Exception as e:
            print(f"[tv] convert FAILED {name}: {e}")
            shutil.rmtree(os.path.join(VIDEO_DIR, vid), ignore_errors=True)
            await tv_assets.insert_one({"src": key, "id": vid, "name": name, "failed": str(e)[:200]})
            continue
        meta.update({"src": key, "id": vid, "name": os.path.splitext(name)[0][:80], "ts": int(time.time())})
        await tv_assets.insert_one(meta)
        print(f"[tv] ready {name}: {meta['durationMs'] // 1000}s, {meta['nSegments']} segs, {time.time() - t0:.0f}s to convert")


async def tv_watch_loop() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await scan_drop_folder()
        except Exception as e:
            print(f"[tv] scan error: {e}")
        await asyncio.sleep(60)


async def _schedule():
    """Current (meta, startMs) of the channel, or None. Random order per cycle,
    seeded by the cycle start, so it is the same for everyone."""
    items = [d async for d in tv_assets.find({"failed": {"$exists": False}}, {"_id": 0})]
    items = [d for d in items if d.get("durationMs", 0) > 0]
    if not items:
        return None
    items.sort(key=lambda d: d["id"])
    doc = await globals_col.find_one({"key": "tv"}, {"_id": 0, "value": 1}) or {}
    st = (doc.get("value") or {})
    now = int(time.time() * 1000)
    cyc = int(st.get("cycleStart") or 0)
    gap = 3000                                       # short pause between videos
    total = sum(d["durationMs"] + gap for d in items)
    if not cyc or now - cyc > 50 * total:
        cyc = now
    while True:
        order = items[:]
        random.Random(cyc).shuffle(order)
        t = now - cyc
        if t < total:
            break
        cyc += total
    if cyc != st.get("cycleStart"):
        await globals_col.update_one({"key": "tv"}, {"$set": {"key": "tv", "value": {"cycleStart": cyc}}}, upsert=True)
    start = cyc
    for d in order:
        if now < start + d["durationMs"] + gap:
            return (d, start) if now < start + d["durationMs"] else None
        start += d["durationMs"] + gap
    return None


async def tv_state_line() -> str:
    s = await _schedule()
    if not s:
        return ""
    m, start = s
    base = f"{VIDEO_BASE_URL}/client/video/{m['id']}/v.bin?s="
    return (f"T1\t{m['id']}\t{m['w']}x{m['h']}x{m['fps']}\t{m['nSegments']}\t{m['segFrames']}\t"
            f"{m['frames']}\t{start}\t{base}seg\t{base + 'aud' if m.get('audio') else '-'}")


async def tv_list() -> list:
    return [d async for d in tv_assets.find({}, {"_id": 0})]
