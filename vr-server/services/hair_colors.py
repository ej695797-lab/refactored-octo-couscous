"""
Limit hair colors to an allowed set (brown / black) server-side.

In 1.31 a hair color is a palette-key SUFFIX on the equipped cosmetic inside
activeCosmetics: "<cosmeticKey>[<colorKey>]", e.g. "hairBoy6[purple]" or
"hairGirl5[black]" (same palette names the paint system uses). There is no
hairColor account field. We rewrite the suffix on any HAIR cosmetic to an
allowed color, so a player's stored/served hair color is always brown or black.

SOFT restriction: the client's own picker still shows the full palette, and in a
live Photon room other players see the locally-picked color until the player
reloads (relog / room change) and the server-normalized value is re-read. A hard
lock (clamp at apply / filter the picker) needs an APK hook. Tune via env
ALLOWED_HAIR_COLORS / DEFAULT_HAIR_COLOR.
"""

import os
import re

ALLOWED_HAIR_COLORS = set(filter(None, (n.strip() for n in (
    os.getenv("ALLOWED_HAIR_COLORS") or "black,brown,brown_dark,brown_light"
).split(","))))
DEFAULT_HAIR_COLOR = os.getenv("DEFAULT_HAIR_COLOR", "brown")

_ENTRY_RE = re.compile(r"^([^\[]+)\[([^\]]*)\]$")


def _is_hair(key: str) -> bool:
    return "hair" in key.lower()


def clamp_entry(entry):
    """Rewrite a single activeCosmetics entry's hair color to an allowed one."""
    if not isinstance(entry, str):
        return entry
    m = _ENTRY_RE.match(entry)
    if not m:
        return entry                      # no [color] suffix -> default color, leave it
    key, color = m.group(1), m.group(2)
    if not _is_hair(key) or color in ALLOWED_HAIR_COLORS:
        return entry                      # not hair, or already allowed
    return f"{key}[{DEFAULT_HAIR_COLOR}]"


def clamp_active_cosmetics(cosmetics):
    """Return activeCosmetics with every hair cosmetic's color clamped to the
    allowed set. Non-hair cosmetics and non-list input are returned untouched."""
    if not isinstance(cosmetics, list):
        return cosmetics
    return [clamp_entry(c) for c in cosmetics]
