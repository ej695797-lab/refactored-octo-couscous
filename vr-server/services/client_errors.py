"""
Client error tracking.

Every g2_logDebug upload carries the client's recent log ("HH:MM:SS.[Cat] msg",
newest first). Uploads overlap heavily, so each error line is de-duplicated per
account before it counts. Lines are grouped into signatures (numbers and
obfuscated IL2CPP names stripped) so the same bug from many players is one row.

Collections:
  client_error_sigs    one doc per signature: sig, sample, count, first/lastSeen,
                       recent samples (with stack lines), mute flag
  client_error_events  one small doc per new occurrence {sig, acct, v, at}
                       (TTL 7 days) -> windowed counts / unique players
  client_error_seen    de-dup cache of (account, raw line) (TTL 20 hours)

A spike (an error suddenly hitting several players) posts to the admin
webhook, at most once per signature every few hours. Muted signatures never
alert and are hidden on the dashboard by default.
"""

import asyncio
import hashlib
import re
from datetime import datetime, timedelta, timezone

import httpx
from pymongo.errors import BulkWriteError

from services.db import db

sigs = db["client_error_sigs"]
events = db["client_error_events"]
seen = db["client_error_seen"]

ERROR_CATS = {"Error", "Exception", "Assert"}
SPIKE_WINDOW = timedelta(minutes=30)
SPIKE_MIN_PLAYERS = 3
SPIKE_MIN_COUNT = 5
SPIKE_RATIO = 3.0
ALERT_COOLDOWN = timedelta(hours=6)
MAX_SAMPLES = 5

_LINE = re.compile(r"^(\d\d:\d\d:\d\d)\.\[([^\]]*)\]\s?(.*)$")
_OBF = re.compile(r"[\[\]]{6,}(?:\+[\[\]]{6,})*")
_NUM = re.compile(r"\d+")
_TAIL_ID = re.compile(r": \S{1,8}$")

_indexes_ready = False
_bg: set = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def signature(msg: str) -> str:
    s = _OBF.sub("<obf>", msg.strip())
    s = _NUM.sub("#", s)
    s = _TAIL_ID.sub(": <id>", s)
    return s[:180]


def sig_id(sig: str) -> str:
    return hashlib.sha1(sig.encode("utf-8", "replace")).hexdigest()[:16]


async def _ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    try:
        await events.create_index([("sig", 1), ("at", -1)])
        await events.create_index("at", expireAfterSeconds=7 * 86400)
        await seen.create_index("at", expireAfterSeconds=20 * 3600)
        await sigs.create_index([("lastSeen", -1)])
        _indexes_ready = True
    except Exception as e:
        print(f"[errors] index setup failed: {e}")


def parse_errors(logs: str) -> list[tuple[str, str, list[str]]]:
    """-> [(raw header line, message, stack lines)] for error-level entries."""
    out = []
    cur = None
    for line in (logs or "").split("\n"):
        m = _LINE.match(line)
        if m:
            cur = None
            if m.group(2) in ERROR_CATS and m.group(3).strip():
                cur = (line, m.group(3), [])
                out.append(cur)
        elif cur is not None and line.strip() and len(cur[2]) < 8:
            cur[2].append(line.strip()[:200])
    return out


async def ingest(account_id: str, logs: str, version: str = "",
                 alerts: bool = True, at: datetime | None = None) -> int:
    """Record new error occurrences from one log upload. Returns how many."""
    await _ensure_indexes()
    at = at or _now()
    acct = str(account_id or "?")
    parsed = parse_errors(logs)
    if not parsed:
        return 0

    # De-dup against earlier uploads from this account.
    docs, by_id = [], {}
    for raw, msg, stack in parsed:
        h = hashlib.sha1(f"{acct}|{raw}".encode("utf-8", "replace")).hexdigest()[:24]
        if h in by_id:
            continue
        by_id[h] = (msg, stack)
        docs.append({"_id": h, "at": at})
    fresh = set(by_id)
    try:
        await seen.insert_many(docs, ordered=False)
    except BulkWriteError as e:
        for w in e.details.get("writeErrors", []):
            if w.get("code") == 11000:
                fresh.discard(docs[w["index"]]["_id"])
    if not fresh:
        return 0

    grouped: dict[str, dict] = {}
    for h in fresh:
        msg, stack = by_id[h]
        sig = signature(msg)
        sid = sig_id(sig)
        g = grouped.setdefault(sid, {"sig": sig, "n": 0, "msg": msg, "stack": stack})
        g["n"] += 1

    ev = []
    for sid, g in grouped.items():
        sample = {"acct": acct, "at": at, "v": version, "msg": g["msg"][:400],
                  "stack": g["stack"]}
        await sigs.update_one(
            {"_id": sid},
            {"$setOnInsert": {"sig": g["sig"], "firstSeen": at, "muted": False},
             "$set": {"lastSeen": at, "sample": g["msg"][:400]},
             "$inc": {"count": g["n"]},
             "$push": {"samples": {"$each": [sample], "$slice": -MAX_SAMPLES}}},
            upsert=True)
        ev.extend({"sig": sid, "acct": acct, "v": version, "at": at} for _ in range(g["n"]))
    if ev:
        await events.insert_many(ev, ordered=False)

    if alerts:
        for sid in grouped:
            try:
                await _maybe_alert(sid)
            except Exception as e:
                print(f"[errors] alert check failed: {e}")
    return len(fresh)


def ingest_background(account_id: str, logs: str, version: str = "") -> None:
    async def run():
        try:
            await ingest(account_id, logs, version)
        except Exception as e:
            print(f"[errors] ingest failed: {e}")
    t = asyncio.create_task(run())
    _bg.add(t)
    t.add_done_callback(_bg.discard)


async def _window_stats(sid: str, start: datetime, end: datetime) -> tuple[int, int]:
    rows = await events.aggregate([
        {"$match": {"sig": sid, "at": {"$gte": start, "$lt": end}}},
        {"$group": {"_id": None, "n": {"$sum": 1}, "a": {"$addToSet": "$acct"}}},
    ]).to_list(1)
    if not rows:
        return 0, 0
    return rows[0]["n"], len(rows[0]["a"])


async def _maybe_alert(sid: str) -> None:
    doc = await sigs.find_one({"_id": sid}, {"sig": 1, "sample": 1, "muted": 1,
                                             "lastAlert": 1, "firstSeen": 1, "count": 1})
    if not doc or doc.get("muted"):
        return
    now = _now()
    last = doc.get("lastAlert")
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last and now - last < ALERT_COOLDOWN:
        return
    n, players = await _window_stats(sid, now - SPIKE_WINDOW, now)
    if players < SPIKE_MIN_PLAYERS or n < SPIKE_MIN_COUNT:
        return
    prev_n, _ = await _window_stats(sid, now - timedelta(hours=24), now - SPIKE_WINDOW)
    baseline = prev_n / 47.0  # average per 30 min over the previous ~day
    if n < SPIKE_RATIO * max(baseline, 1.0):
        return
    await sigs.update_one({"_id": sid}, {"$set": {"lastAlert": now}})
    first = doc.get("firstSeen")
    if first and first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    is_new = bool(first and now - first < timedelta(hours=24))
    text = (f"**Client error spike** ({'new error' if is_new else 'rising'})\n"
            f"{players} players, {n} times in the last 30 min "
            f"(usual: about {baseline:.1f} per 30 min)\n"
            f"```{(doc.get('sample') or doc.get('sig') or '')[:600]}```\n"
            f"Open the Errors tab in /editor to see details or mute it.")
    await _post_admin(text)


async def _post_admin(text: str) -> None:
    from config import ADMIN_DISCORD_WEBHOOK
    if not ADMIN_DISCORD_WEBHOOK:
        return
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            from config import labeled
            await c.post(ADMIN_DISCORD_WEBHOOK, json={"content": labeled(text)[:1900]})
    except Exception as e:
        print(f"[errors] webhook failed: {e}")


# ─── Dashboard queries ───────────────────────────────────────────────

WINDOWS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)}


async def summary(window: str = "24h", include_muted: bool = False) -> list[dict]:
    span = WINDOWS.get(window, WINDOWS["24h"])
    now = _now()
    cur = await events.aggregate([
        {"$match": {"at": {"$gte": now - span}}},
        {"$group": {"_id": "$sig", "n": {"$sum": 1}, "a": {"$addToSet": "$acct"},
                    "last": {"$max": "$at"}}},
    ]).to_list(None)
    prev = await events.aggregate([
        {"$match": {"at": {"$gte": now - 2 * span, "$lt": now - span}}},
        {"$group": {"_id": "$sig", "n": {"$sum": 1}}},
    ]).to_list(None)
    prev_n = {r["_id"]: r["n"] for r in prev}
    ids = [r["_id"] for r in cur]
    meta = {d["_id"]: d async for d in sigs.find(
        {"_id": {"$in": ids}}, {"samples": 0})}
    out = []
    for r in cur:
        m = meta.get(r["_id"], {})
        if m.get("muted") and not include_muted:
            continue
        out.append({
            "id": r["_id"], "sig": m.get("sig", ""), "sample": m.get("sample", ""),
            "count": r["n"], "players": len(r["a"]), "prevCount": prev_n.get(r["_id"], 0),
            "lastSeen": _iso(r["last"]), "firstSeen": _iso(m.get("firstSeen")),
            "total": m.get("count", 0), "muted": bool(m.get("muted")),
        })
    out.sort(key=lambda x: (x["players"], x["count"]), reverse=True)
    return out


async def detail(sid: str) -> dict | None:
    doc = await sigs.find_one({"_id": sid})
    if not doc:
        return None
    now = _now()
    hist = await events.aggregate([
        {"$match": {"sig": sid, "at": {"$gte": now - timedelta(hours=48)}}},
        {"$group": {"_id": {"$dateTrunc": {"date": "$at", "unit": "hour"}}, "n": {"$sum": 1}}},
    ]).to_list(None)
    hourly = {(_aware(h["_id"])).strftime("%Y-%m-%d %H"): h["n"] for h in hist}
    buckets = []
    for i in range(47, -1, -1):
        t = (now - timedelta(hours=i)).strftime("%Y-%m-%d %H")
        buckets.append({"hour": t, "n": hourly.get(t, 0)})
    accts = await events.aggregate([
        {"$match": {"sig": sid}},
        {"$group": {"_id": "$acct", "n": {"$sum": 1}, "last": {"$max": "$at"},
                    "v": {"$last": "$v"}}},
        {"$sort": {"last": -1}}, {"$limit": 50},
    ]).to_list(None)
    from services.db import accounts
    names = {a["accountID"]: a.get("displayName", "") async for a in accounts.find(
        {"accountID": {"$in": [a["_id"] for a in accts]}}, {"_id": 0, "accountID": 1, "displayName": 1})}
    return {
        "id": sid, "sig": doc.get("sig"), "muted": bool(doc.get("muted")),
        "total": doc.get("count", 0), "firstSeen": _iso(doc.get("firstSeen")),
        "lastSeen": _iso(doc.get("lastSeen")),
        "samples": [{**s, "at": _iso(s.get("at"))} for s in reversed(doc.get("samples", []))],
        "hourly": buckets,
        "players": [{"accountID": a["_id"], "name": names.get(a["_id"], ""), "count": a["n"],
                     "last": _iso(a["last"]), "v": a.get("v", "")} for a in accts],
    }


async def set_muted(sid: str, muted: bool) -> bool:
    r = await sigs.update_one({"_id": sid}, {"$set": {"muted": bool(muted)}})
    return r.matched_count > 0


def _aware(d):
    if isinstance(d, datetime) and d.tzinfo is None:
        return d.replace(tzinfo=timezone.utc)
    return d


def _iso(d) -> str:
    d = _aware(d)
    return d.strftime("%Y-%m-%d %H:%M:%S") if isinstance(d, datetime) else ""
