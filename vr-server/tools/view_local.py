"""
The View (live 3D look at the Photon bot's room) as a program on this PC, not on the
editor site (owner 2026-10-29: "i dont like it being on the editor site").

Serves the same page (static/editor_view.html) at http://127.0.0.1:8899 and talks to
the bot directly: its state files, its command pipe and its talk socket. Bound to
127.0.0.1 only, so nothing outside this PC can reach it, and there is no login.

Run:  yeeps-proxy-240/vr-server/tools/view_local.sh   (opens the browser by itself)
"""
import asyncio
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
sys.path.insert(0, SERVER)
os.chdir(SERVER)
# the live server's settings (database etc.), read the way systemd's EnvironmentFile does
for _ln in open(os.path.expanduser("~/.config/yeeps/vr-server-240.env"), encoding="utf-8"):
    _ln = _ln.strip()
    if _ln and not _ln.startswith("#") and "=" in _ln:
        _k, _v = _ln.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from fastapi import FastAPI, HTTPException, Request, WebSocket          # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402

from routers import editor_router as er                                 # noqa: E402
from services import photon_bot                                         # noqa: E402

PORT = int(os.getenv("VIEW_LOCAL_PORT", "8899"))
# the bot wears the owner's outfit (OwnerName) when started from here
os.environ["YEEPS_LOOK_ACCT"] = os.getenv("VIEW_LOOK_ACCT", "o_OWNER0000000001")
os.environ["YEEPS_BOT_NAME"] = os.getenv("VIEW_BOT_NAME", "OwnerName")
OWNER = "OwnerName (View app)"
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/")
async def root():
    return RedirectResponse("/editor/view")


@app.get("/editor/view", response_class=HTMLResponse)
async def page():
    html = open(os.path.join(SERVER, "static", "editor_view.html"), encoding="utf-8").read()
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


COS_DIR = os.path.join(SERVER, "static", "view-cos")      # tools/export_view_cosmetics.py


@app.get("/editor/view/a/cos/{name}")
async def cosmetic(name: str):
    from fastapi.responses import FileResponse, Response
    f = os.path.join(COS_DIR, name)
    if not re.fullmatch(r"[A-Za-z0-9_]+\.json", name) or not os.path.isfile(f):
        return Response(status_code=404)
    return FileResponse(f, media_type="application/json", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/editor/view/a/{path:path}")
async def asset(path: str):
    return await er.view_asset(path)


@app.get("/editor/api/photon/status")
async def status():
    st = await photon_bot.status()
    st["log"] = photon_bot.log_tail()
    return JSONResponse(st)


@app.get("/editor/api/photon/players")
async def players():
    return JSONResponse({"players": await er._online_players()})


@app.get("/editor/api/photon/view")
async def view():
    return await er.photon_view()


@app.get("/editor/api/photon/view/blocks")
async def view_blocks(rev: int = -1, sid: str = ""):
    return await er.photon_view_blocks(rev, sid)


@app.get("/editor/api/photon/view/patterns")
async def view_patterns():
    return await er.photon_view_patterns()


async def wear_owner_look():
    """The bot's account on live (players load a look from the account when someone joins):
    created like main.py's bot auth does if it isn't there yet, then given the owner's outfit,
    colors and name. The 1.31 copy (where the bot's Photon login made it) gets the same."""
    from handlers.login import DEFAULT_ACCOUNT, _server_time
    from services.db import accounts, client, globals_col
    aid = f"o_99000000000000{photon_bot.BOT_ID:02d}"
    src = await accounts.find_one({"accountID": os.environ["YEEPS_LOOK_ACCT"]},
                                  {"_id": 0, "skinColor": 1, "eyeColor": 1, "activeCosmetics": 1}) or {}
    look = dict(src)
    if isinstance(look.get("activeCosmetics"), list):
        look["activeCosmetics"] = [c for c in look["activeCosmetics"] if not str(c).startswith("roleBadge")]
    look["displayName"] = os.environ["YEEPS_BOT_NAME"]
    if not await accounts.find_one({"accountID": aid}, {"_id": 1}):
        acc = dict(DEFAULT_ACCOUNT)
        ov = await globals_col.find_one({"key": "default_account_override"}, {"_id": 0, "value": 1})
        if ov and isinstance(ov.get("value"), dict):
            acc.update(ov["value"])
        acc.update({"accountID": aid, "isBot": 1, "firstLogin": _server_time()})
        await accounts.insert_one(acc)
    await accounts.update_one({"accountID": aid}, {"$set": look})
    await client["Yeeps119"]["accounts"].update_one({"accountID": aid}, {"$set": look})


@app.post("/editor/api/photon/start")
async def start(request: Request):
    body = await request.json()
    target = str(body.get("target") or "").strip()
    online = {p["accountID"]: p for p in await er._online_players()}
    if target not in online:
        raise HTTPException(400, "that player isn't online right now")
    try:
        await wear_owner_look()
    except Exception as e:
        print(f"(couldn't put the owner's look on the bot: {e!r})")
    res = await photon_bot.start(target, online[target]["name"], bool(body.get("stay")), OWNER)
    if not res.get("ok"):
        raise HTTPException(500, res.get("error") or "couldn't start the bot")
    return JSONResponse({"ok": True})


@app.post("/editor/api/photon/stop")
async def stop():
    return JSONResponse(await photon_bot.stop())


@app.post("/editor/api/photon/cmd")
async def cmd(request: Request):
    body = await request.json()
    c = " ".join(str(body.get("cmd") or "").split())[:200]
    if not c or c.lower() in ("quit", "exit", "q"):
        raise HTTPException(400, "bad command")
    if await photon_bot.running() and photon_bot.code_newer():      # bot.py changed: restart it first
        m = photon_bot._meta()
        res = await photon_bot.start(m.get("target") or "", m.get("targetName") or "", bool(m.get("stay")), OWNER)
        if res.get("ok"):
            asyncio.get_running_loop().create_task(photon_bot.send_when_ready(c))
            return JSONResponse({"ok": True, "reloading": True})
    if not photon_bot.send(c):
        raise HTTPException(409, "the bot isn't running")
    return JSONResponse({"ok": True})


@app.post("/editor/api/photon/view/edit")
async def view_edit(request: Request):
    body = await request.json()
    op = body.get("op")
    try:
        x, y, z = (int(body.get(k)) for k in ("x", "y", "z"))
    except (TypeError, ValueError):
        raise HTTPException(400, "bad cell")
    if not all(0 <= v < 512 for v in (x, y, z)):
        raise HTTPException(400, "that spot is outside the room")
    if op == "put":
        pat, col = str(body.get("pattern") or ""), str(body.get("color") or "white")
        if not er._VIEW_PAT.match(pat) or not er._VIEW_COL.match(col):
            raise HTTPException(400, "pick a block and a color")
        try:
            fwd, up = int(body.get("fwd", 4)), int(body.get("up", 2))
        except (TypeError, ValueError):
            fwd, up = 4, 2
        if not (0 <= fwd < 6 and 0 <= up < 6) or fwd // 2 == up // 2:
            raise HTTPException(400, "bad facing")
        c = f"vput {pat} {x} {y} {z} {col} {fwd} {up}"
    elif op == "del":
        c = f"vdel {x} {y} {z}"
    else:
        raise HTTPException(400, "op must be put or del")
    if not photon_bot.send(c):
        raise HTTPException(409, "the bot isn't running - pick a player and press Watch")
    return JSONResponse({"ok": True})


@app.websocket("/editor/api/photon/talk")
async def talk(ws: WebSocket):
    import websockets
    origin = (ws.headers.get("origin") or "").rstrip("/")
    if origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
        await ws.close(code=4403)
        return
    await ws.accept()
    bot_ws = None
    for i in range(80):                       # the bot may still be starting / following into a room
        try:
            bot_ws = await websockets.connect(f"ws://127.0.0.1:{er.TALK_PORT}", max_size=1 << 20)
            break
        except Exception:
            if i % 10 == 0:
                await ws.send_text(json.dumps({"type": "status", "msg": "waiting for the bot..."}))
            await asyncio.sleep(0.5)
    if bot_ws is None:
        await ws.send_text(json.dumps({"type": "status", "msg": "The bot's talk didn't start. Click the map again."}))
        await ws.close()
        return

    async def up():
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                return
            if m.get("bytes"):
                await bot_ws.send(m["bytes"])
            elif m.get("text"):
                await bot_ws.send(m["text"][:500])

    async def down():
        async for m in bot_ws:
            if isinstance(m, (bytes, bytearray)):
                await ws.send_bytes(bytes(m))
            else:
                await ws.send_text(m)

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        for s in (bot_ws, ws):
            try:
                await s.close()
            except Exception:
                pass


if __name__ == "__main__":
    import uvicorn
    print(f"View: http://127.0.0.1:{PORT}  (this PC only - Ctrl+C to quit)")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
