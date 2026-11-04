"""Run the game server on the listening socket systemd hands over
(yeeps-vr.socket, 127.0.0.1:8000). systemd keeps that socket open while the
service restarts, so requests that arrive mid-restart wait a couple of seconds
instead of being refused (Cloudflare showed players a 502 "bad gateway" page,
which the game reported as "Failed to load game account").

uvicorn's own --fd flag labels the socket AF_UNIX, which breaks peer addresses
and therefore --proxy-headers (real player IPs); socket.socket(fileno=3)
auto-detects AF_INET.
"""
import os
import socket

import uvicorn

if __name__ == "__main__":
    if os.environ.get("TRACEMALLOC") == "1":     # memory-leak hunting (/editor/api/debug/mem)
        import tracemalloc
        tracemalloc.start(8)
    if os.environ.get("LISTEN_FDS") != "1":
        raise SystemExit("expected one socket from systemd (yeeps-vr.socket)")
    sock = socket.socket(fileno=3)
    config = uvicorn.Config("main:app", proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    uvicorn.Server(config).run(sockets=[sock])
