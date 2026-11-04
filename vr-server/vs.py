import asyncio, sys, json; sys.path.insert(0, ".")
from services import photon_bot as p
async def main():
    print("room", p.current_room(), "ready", p.ready())
    v = p.view_state()
    if asyncio.iscoroutine(v): v = await v
    s = json.dumps(v)[:1500]; print(s)
asyncio.run(main())
