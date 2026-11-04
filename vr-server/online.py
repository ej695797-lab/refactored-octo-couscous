import asyncio, sys; sys.path.insert(0, ".")
from routers import editor_router as er
from services import photon_bot
async def main():
    ps = await er._online_players()
    for p in ps: print(p.get("accountID"), p.get("name"), p.get("roomKey") or p.get("room"), p.get("photonRoom"))
    print("bot running:", await photon_bot.running(), photon_bot._meta())
asyncio.run(main())
