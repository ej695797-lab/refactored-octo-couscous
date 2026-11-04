"""
[240/biglive] Photon bot control for the editor's Photon tab (owner only, see
routers/editor_router.py _photon_ok).

The bot (yeeps-bot/bot.py) runs as its own transient user unit
"yeeps-photon-bot", so its memory isn't counted against the game server's 1G
limit and a server restart doesn't take it down. Commands go in through a FIFO
(the same text you'd type into the bot's window); its output goes to a log file
the tab shows.
"""

import asyncio
import json
import os
import re
import shlex
import stat
import sys
import time

BOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "yeeps-bot"))
STATE_DIR = os.path.expanduser("~/.local/state/yeeps/photon-bot-240")
FIFO = os.path.join(STATE_DIR, "in")
LOG = os.path.join(STATE_DIR, "out.log")
META = os.path.join(STATE_DIR, "meta.json")
VIEW = os.path.join(STATE_DIR, "view.json")                  # View tab: players, written by the bot
VIEW_BLOCKS = os.path.join(STATE_DIR, "view_blocks.json")    # View tab: the room's blocks
UNIT = "yeeps-photon-bot-240"
BOT_ID = 3
BOT_NAME = "BlazeBot"
_gen = 0                     # bumped on every start/stop: cancels queued commands


async def _run(*args, timeout: float = 20) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, "timed out"
    return p.returncode, out.decode(errors="replace")


async def running() -> bool:
    _, out = await _run("systemctl", "--user", "is-active", UNIT)
    return out.strip() in ("active", "activating", "deactivating")


def _meta() -> dict:
    try:
        return json.load(open(META))
    except Exception:
        return {}


def log_tail(max_bytes: int = 24000, max_lines: int = 120) -> str:
    """Last part of the bot's output, minus the menu and the actor dumps."""
    try:
        with open(LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - max_bytes))
            txt = f.read().decode(errors="replace")
    except OSError:
        return ""
    keep, in_menu = [], False
    for line in txt.replace("bot> ", "").splitlines():
        s = line.rstrip()
        if s.startswith("=") and "YEEPS BOT" in s:          # the bot's help menu
            in_menu = True
            continue
        if in_menu:
            in_menu = not (s.startswith("=") and set(s) == {"="})
            continue
        if not s or s.startswith("   #") and ": {" in s:
            continue
        keep.append(re.sub(r"<[^>]*>", "", s)[:300])        # names carry <color> tags
    return "\n".join(keep[-max_lines:])


async def status() -> dict:
    return {"running": await running(), **_meta()}


async def start(target: str, target_name: str, stay: bool, by: str) -> dict:
    global _gen
    _gen += 1
    if await running():
        await stop()
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        if not stat.S_ISFIFO(os.stat(FIFO).st_mode):
            os.unlink(FIFO)
            os.mkfifo(FIFO, 0o600)
    except FileNotFoundError:
        os.mkfifo(FIFO, 0o600)
    await _run("systemctl", "--user", "reset-failed", UNIT)
    # Empty the old output now (the unit's shell also does, a moment later), so
    # nothing reads the previous session's "bot is in the room" as this one's.
    open(LOG, "w").close()
    for fn in (VIEW, VIEW_BLOCKS):          # the View tab must not show the last room
        try:
            os.remove(fn)
        except OSError:
            pass
    q = shlex.quote
    run = (f"{q(sys.executable)} -u bot.py --bot-id {BOT_ID} --name {q(os.environ.get('YEEPS_BOT_NAME') or BOT_NAME)} run "
           f"--target {q(target)}{' --stay' if stay else ''} < {q(FIFO)} >> {q(LOG)} 2>&1")
    # The background sleep holds the FIFO's write end open so the bot never sees
    # end-of-input between commands. Photon's free plan caps at 20 players: when
    # this run's output says "Max CCU", wait 20 s and try again (up to ~20 min).
    # When the shell exits, systemd ends the unit and the sleep with it.
    script = (f": > {q(LOG)}; sleep infinity > {q(FIFO)} & i=0; "
              f"while :; do s=$(stat -c %s {q(LOG)}); {run}; "
              f"tail -c +$((s+1)) {q(LOG)} | grep -q 'Max CCU' || break; "
              f"i=$((i+1)); [ $i -ge 60 ] && break; "
              f"echo \"(Photon is full - 20 players max. Trying again in 20 s, attempt $i)\" >> {q(LOG)}; "
              f"sleep 20; done")
    _env2314 = os.path.expanduser("~/.config/yeeps/bot-2314.env")
    rc, out = await _run("systemd-run", "--user", f"--unit={UNIT}", "--collect",
                         f"--working-directory={BOT_DIR}",
                         f"--setenv=YEEPS_BOT_ENV={_env2314}",
                         "--setenv=YEEPS_TALK_PORT=8792",
                         f"--setenv=YEEPS_LOOK_ACCT={os.environ.get('YEEPS_LOOK_ACCT', '')}",
                         f"--setenv=YEEPS_VIEW_DIR={STATE_DIR}",
                         "-p", "MemoryMax=600M", "-p", "CPUQuota=100%",
                         "/bin/sh", "-c", script)
    if rc != 0:
        return {"ok": False, "error": out.strip()[:300] or "couldn't start the bot"}
    json.dump({"target": target, "targetName": target_name, "stay": bool(stay), "by": by,
               "startedAt": int(time.time())}, open(META, "w"))
    return {"ok": True}


def send(cmd: str) -> bool:
    """Type one line into the bot. False if it isn't listening."""
    cmd = " ".join(str(cmd).split())[:200]
    if not cmd:
        return False
    try:
        fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, (cmd + "\n").encode())
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def current_room() -> str:
    """Photon room the bot last joined (from its output), or ""."""
    m = None
    for m in re.finditer(r"joined (\S+) as actor", log_tail(max_bytes=200000, max_lines=100000)):
        pass
    return m.group(1) if m else ""


def view_state() -> dict:
    """The bot's latest View snapshot (players + room), or {} before it has one."""
    try:
        return json.load(open(VIEW))
    except Exception:
        return {}


def view_blocks() -> dict:
    try:
        return json.load(open(VIEW_BLOCKS))
    except Exception:
        return {}


def code_newer() -> bool:
    """bot.py changed since the running bot started (it needs a restart to use it)."""
    try:
        return os.path.getmtime(os.path.join(BOT_DIR, "bot.py")) > float(_meta().get("startedAt") or 0)
    except OSError:
        return False


def ready() -> bool:
    return "bot is in the room as" in log_tail(max_bytes=200000, max_lines=100000)


async def send_when_ready(cmd: str, timeout_s: float = 1200) -> None:
    """Send cmd once the bot (just started) is in its room. Dropped if the bot is
    restarted or stopped first. The timeout covers Photon-full retries."""
    gen, end = _gen, time.monotonic() + timeout_s
    while time.monotonic() < end and gen == _gen:
        if ready():
            await asyncio.sleep(2)               # let poses arrive so it can find the player
            if gen == _gen:
                send(cmd)
            return
        await asyncio.sleep(2)


async def stop(wait_s: float = 5) -> dict:
    """quit = the bot takes down whatever it built and leaves; force-stop after wait_s.
    wait_s is short (5s): a bot busy FOLLOWING a player isn't reading the FIFO, so a
    long graceful wait makes the editor Stop button look dead for up to a minute
    (owner 2026-10-22). 5s is ample for an idle bot's clean quit; otherwise force-stop."""
    global _gen
    _gen += 1
    if not await running():
        return {"ok": True, "was": False}
    send("quit")
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        await asyncio.sleep(1)
        if not await running():
            return {"ok": True, "was": True}
    await _run("systemctl", "--user", "stop", UNIT)
    return {"ok": True, "was": True, "forced": True}
