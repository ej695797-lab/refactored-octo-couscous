import os
import hmac
import hashlib
import time

MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://USER:PASSWORD@YOUR-CLUSTER/")
# 2.4.0 gets its OWN database (its own "folder"), isolated from 1.19.0/others.
# This codebase serves the 1.19.0 version -> its own database. The 2.4.0
# deployment sets DB_NAME=Yeeps240 (its own data), keeping the two separate.
DB_NAME = os.getenv("DB_NAME", "Yeeps119")

# Which game server this deployment is (e.g. "2.4.0" on the 2.4.0-blaze Render
# service). Empty on the 1.31 server = unchanged behaviour. When set, it prefixes
# every Discord webhook message so the two servers' posts can be told apart.
INSTANCE_LABEL = os.getenv("INSTANCE_LABEL", "").strip()


def labeled(text: str) -> str:
    return f"[{INSTANCE_LABEL}] {text}" if INSTANCE_LABEL else text
SERVER_PORT = int(os.getenv("PORT", "8000"))
# Off: the 1.19.0 database is maintained by hand/editor. Re-seeding on every boot
# overwrote it on each deploy. Set the SEED_DB env var to 1 to run it once.
SEED_DB = 0

# yeeps-proxy is the audited relay that actually talks to TrassGames' backend
# (AWS Cognito/Lambda). The editor's login-based import calls it instead of
# reimplementing that login flow here.
YEEPS_PROXY_URL = os.getenv("YEEPS_PROXY_URL", "https://proxy.your-domain.example")

# Public HTTPS address of THIS server (the APK and players fetch feed/disk/screen/video
# URLs from it). Self-hosted behind a Cloudflare Tunnel on your-domain.example since
# 2026-10-08 (Render free plan ran out of bandwidth).
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://api.your-domain.example").rstrip("/")

# The current, telemetry-free player build (manifest versionName "1.31.0-blaze").
# The client reports this in its presence/status (G2_PlayerStatuses) write and
# compares it against other players'/the room's version for the multiplayer
# join gate. Forcing the shared presence version to this value makes OLDER
# clients (which report "1.19.0" and still ship GameAnalytics telemetry) fail
# that check -> they can't join rooms. Bump via env REQUIRED_CLIENT_VERSION.
REQUIRED_CLIENT_VERSION = os.getenv("REQUIRED_CLIENT_VERSION", "2.32.0-yeeper920")
# True only on a 2.x deployment (the separate 2.4.0-blaze Render service sets
# REQUIRED_CLIENT_VERSION=2.4.0-blaze). Every 2.x-only behaviour is gated on this,
# so the 1.31 server is unaffected.
try:
    IS_V2 = int(REQUIRED_CLIENT_VERSION.split(".")[0]) >= 2
except ValueError:
    IS_V2 = False

# TESTING toggle (default OFF = production): when ON, serve every room with
# isModeLocked=0 so play/creative can be switched freely on all maps, and let
# anyone edit official maps in-game. OFF = each room keeps its real mode-lock
# (official/public maps can't be set to creative) and only map-makers/staff edit
# official maps. Set env UNLOCK_ALL_MODES=1 (and redeploy) to turn testing on.
UNLOCK_ALL_MODES = (os.getenv("UNLOCK_ALL_MODES", "0") == "1")

# Attestation build gate (Photon custom-auth). The 1.19-era clients send an
# attestation `summaryHash` on the Photon auth GET; the 1.31.0-blaze client sends
# NONE. So once the server moved to 1.31, ANY presented hash means an old-era
# build: the retired 1.19.0-blaze hashes get a clear "update" message, and any
# other hash (a modified clone such as clone_maker's dev-menu build) is denied
# covertly. A connection with no hash (1.31) passes.
#
# CLIENT_ATTEST_HASHES (env, comma list) re-opens specific hashes if a future
# build starts sending one. Empty by default for 1.31.
CURRENT_CLIENT_ATTEST_HASH = os.getenv("CLIENT_ATTEST_HASH", "")
ALLOWED_ATTEST_HASHES = set(
    h.strip().upper() for h in (os.getenv("CLIENT_ATTEST_HASHES") or "").split(",")
    if h.strip())
if CURRENT_CLIENT_ATTEST_HASH:
    ALLOWED_ATTEST_HASHES.add(CURRENT_CLIENT_ATTEST_HASH.upper())
# The 2.4.0 client's attestation hash isn't known yet, so on a 2.x deployment an
# unknown hash is only logged (the APK build-tag gate still blocks modified
# builds). Set ENFORCE_ATTEST_HASH=1 there once its hash is in CLIENT_ATTEST_HASHES.
ENFORCE_ATTEST_HASH = os.getenv("ENFORCE_ATTEST_HASH", "0" if IS_V2 else "1") != "0"

# The retired 1.19.0-blaze builds (distributed copy + owner's re-signed copy).
RETIRED_ATTEST_HASHES = {
    "18FD045CAC26F2BF4F3A1A2620606E4E5FDBBE9B",
    "1DAF28ECB96130AF2D4B1A27318E640B0099B3E8",
}

# Client build identity/integrity gate (services/build_gate.py). The 1.31 APK
# sends /c/<buildId>.<libil2cpp hash>.<libBNMExplorer hash>/ in its login path.
ALLOWED_CLIENT_BUILDS = set(filter(None, (os.getenv("ALLOWED_CLIENT_BUILDS") or "131b15")   # 131b15 = headset keys on API calls + Photon joins; older builds must update
                                   .replace(" ", "").split(",")))
ALLOWED_CLIENT_TAGS = set(filter(None, (os.getenv("ALLOWED_CLIENT_TAGS") or
                                        "131b6.5d63cf523d48ca70.471a1e62d887c1ee,"
                                        "131b7.5d63cf523d48ca70.70b77951983a3b75,"
                                        "131b8.5d63cf523d48ca70.2a95d7567387e028,"
                                        "131b9.5d63cf523d48ca70.9405fa0e645c79fb,"
                                        "131b10.5d63cf523d48ca70.c6acf83c2316f614,"
                                        "131b11.5d63cf523d48ca70.9df62344179e4601,"
                                        "131b12.5d63cf523d48ca70.92dfaba248cca21e,"
                                        "131b14.5d63cf523d48ca70.1d7427803d0d9cbc,"   # headset keys
                                        "131b15.5d63cf523d48ca70.0871af7f5891be23")
                                 .replace(" ", "").split(",")))
ENFORCE_CLIENT_BUILD = os.getenv("ENFORCE_CLIENT_BUILD", "1") != "0"

# COVERT bans: instead of a visible "You are banned." (which tells a cheater they
# were caught, and how), a covert deny at Photon auth returns a message that looks
# like an ordinary connection/server hiccup. The player just can't get in and
# assumes it's their internet or a flaky server — they can't tell they were
# detected, so they can't work around it (e.g. spoof the build hash). Used for the
# build gate (modified/old clients) and the hidden `shadowBanned` account flag.
COVERT_DENY_MESSAGE = os.getenv(
    "COVERT_DENY_MESSAGE",
    "Couldn't reach the server. Check your connection and try again.")
# Extra covert targets by id/ip without touching the DB (comma-separated).
SHADOW_BAN_IDS = set(filter(None, (os.getenv("SHADOW_BAN_IDS") or "").replace(" ", "").split(",")))
SHADOW_BAN_IPS = set(filter(None, (os.getenv("SHADOW_BAN_IPS") or "").replace(" ", "").split(",")))

# ─── Rotating client token (anti-clone) ──────────────────────────────
# A time-based code the REAL client computes from a shared secret and sends on
# connect; the server computes the same and checks it. It changes every
# CLIENT_TOKEN_ROTATE_SECONDS, so there is no static value for a clone to copy —
# a cloner would have to extract the live secret+algorithm from the build, not
# just screenshot a hash. Same HMAC-over-a-time-window trick as the editor
# password. clone_maker's current clone has NO token logic, so it sends nothing
# valid and is denied the moment enforcement is on.
#
# ROLLOUT SAFETY: ENFORCE_CLIENT_TOKEN defaults OFF. While off, the server only
# OBSERVES tokens (logs whether a valid one arrived) so you can confirm the
# token-client build is live for everyone BEFORE enforcing — otherwise every
# player on a build without the token would be locked out. Flip ENFORCE_CLIENT_
# TOKEN=1 only once the token build is universal.
CLIENT_TOKEN_SECRET = os.getenv("CLIENT_TOKEN_SECRET", "yeeps-client-token-secret-change-me")
CLIENT_TOKEN_ROTATE_SECONDS = int(os.getenv("CLIENT_TOKEN_ROTATE_SECONDS", "3600"))
ENFORCE_CLIENT_TOKEN = (os.getenv("ENFORCE_CLIENT_TOKEN", "0") == "1")


def client_token(window: int = None) -> str:
    """The 6-digit code valid for the given rotation window (default: now)."""
    if window is None:
        window = int(time.time() // CLIENT_TOKEN_ROTATE_SECONDS)
    mac = hmac.new(CLIENT_TOKEN_SECRET.encode(),
                   f"clienttoken|{window}".encode(), hashlib.sha256).hexdigest()
    return f"{int(mac, 16) % 1000000:06d}"


def valid_client_tokens() -> set:
    """Accept the current window plus ±1 for clock skew / hour boundaries."""
    w = int(time.time() // CLIENT_TOKEN_ROTATE_SECONDS)
    return {client_token(w - 1), client_token(w), client_token(w + 1)}

# Discord audit webhooks (override with env so they aren't pinned in the repo):
#   ADMIN  = /link activity + player/data edits made from the web editor
#   INGAME = things players do in-game (e.g. in-game map saves)
ADMIN_DISCORD_WEBHOOK = os.getenv(
    "ADMIN_DISCORD_WEBHOOK",
    "REDACTED_DISCORD_WEBHOOK")
INGAME_DISCORD_WEBHOOK = os.getenv(
    "INGAME_DISCORD_WEBHOOK",
    "REDACTED_DISCORD_WEBHOOK")

# The editor password auto-rotates to a new random 6-digit code every
# EDITOR_PW_ROTATE_SECONDS; each new code is posted to EDITOR_PW_WEBHOOK, and
# live editor sessions are booted at each rotation (must sign in again).
EDITOR_PW_ROTATE_SECONDS = int(os.getenv("EDITOR_PW_ROTATE_SECONDS", str(3 * 3600)))
EDITOR_PW_WEBHOOK = os.getenv(
    "EDITOR_PW_WEBHOOK",
    "REDACTED_DISCORD_WEBHOOK")

# Local Photon bot (yeeps-bot on the owner's PC). A Photon auth request carrying
# botKey=<BOT_KEY> is let in as bot account o_99000000000000<NN> (botId 1-9).
# Empty = bots disabled.
BOT_KEY = os.getenv("BOT_KEY", "")

# Map blocks owned by an account that isn't in the database (official players
# from copied maps, bot accounts, "testing") are given to this account: on every
# in-game save, on editor saves/imports, and by the editor's Maps-tab button.
DEFAULT_MAP_OWNER = os.getenv("DEFAULT_MAP_OWNER", "o_OWNER0000000001")   # OwnerName
