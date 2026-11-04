"""
Display-name visibility (owner's rule, 2026-10-10): a name must show at least
one visible character in game. Blank names ("", spaces, zero-width or other
invisible characters, or text hidden with rich-text tags like <size=0> or a
fully transparent colour) make a player impossible to pick out or name.

Rich-text tags are understood the way the game's TextMeshPro draws them:
<quad>/<sprite> draw a visible glyph; <size>, <alpha> and <color> with an
alpha byte can hide whatever follows them.
"""

import re
import unicodedata

# Characters that draw nothing but aren't in an "invisible" Unicode category.
_BLANK_CHARS = set("ᅟᅠㅤﾠ⠀᠎͏឴឵"
                   "⁡⁢⁣⁤﻿\U0001d159"
                   "\U0001d173\U0001d174\U0001d175\U0001d176\U0001d177\U0001d178\U0001d179\U0001d17a")
_INVISIBLE_CATEGORIES = {"Cc", "Cf", "Co", "Cn", "Cs", "Zs", "Zl", "Zp", "Mn", "Me"}
_TAG = re.compile(r"<[^<>]*>")


def _char_visible(c: str) -> bool:
    if c.isspace() or c in _BLANK_CHARS:
        return False
    return unicodedata.category(c) not in _INVISIBLE_CATEGORIES


def _alpha_of(value: str):
    """Alpha byte (0-255) of '#RRGGBBAA' / '#RGBA' / '#AA', else None (opaque)."""
    v = value.strip().strip('"').lstrip("#")
    try:
        if len(v) == 8:
            return int(v[6:8], 16)
        if len(v) == 4:
            return int(v[3] * 2, 16)
        if len(v) == 2:
            return int(v, 16)
    except ValueError:
        return None
    return None


def _size_hides(value: str) -> bool:
    m = re.match(r"\s*([+-]?[\d.]+)\s*(px|em|%)?", value or "")
    if not m:
        return False
    try:
        n = float(m.group(1))
    except ValueError:
        return False
    if value.strip().startswith(("+", "-")):
        return False                       # relative change, not an absolute size
    unit = m.group(2) or "px"
    return n < {"px": 3, "em": 0.2, "%": 20}[unit]


def visible_count(name) -> int:
    """How many characters (or <quad>/<sprite> glyphs) of the name actually show."""
    s = str(name or "")
    size_hidden, alpha_hidden, colors = False, False, []
    count, pos = 0, 0
    for m in _TAG.finditer(s):
        text = s[pos:m.start()]
        pos = m.end()
        hidden = size_hidden or alpha_hidden or (colors and colors[-1])
        if not hidden:
            count += sum(1 for c in text if _char_visible(c))
        tag = m.group(0)[1:-1].strip()
        low = tag.lower()
        if low.startswith("size="):
            size_hidden = _size_hides(tag[5:])
        elif low == "/size":
            size_hidden = False
        elif low.startswith("alpha="):
            a = _alpha_of(tag[6:])
            alpha_hidden = a is not None and a < 0x20
        elif low.startswith("color="):
            a = _alpha_of(tag[6:])
            colors.append(a is not None and a < 0x20)
        elif low == "/color":
            if colors:
                colors.pop()
        elif low.startswith(("quad", "sprite")):
            if not hidden:
                count += 1
    tail = s[pos:]
    if not (size_hidden or alpha_hidden or (colors and colors[-1])):
        count += sum(1 for c in tail if _char_visible(c))
    return count


def is_visible_name(name) -> bool:
    return visible_count(name) > 0


def fallback_name(oculus_id: str, account_id: str) -> str:
    """What a player with an invisible name is shown as instead."""
    if is_visible_name(oculus_id):
        return str(oculus_id)
    return "Yeep" + re.sub(r"\D", "", str(account_id or ""))[-4:]


# ---- unique names (owner's rule, 2026-10-27) ----
# No two players may share a name: same-named players are impossible to track.
# Names are compared as they read in game: color/size tags, accents and other
# combining marks, invisible characters and case are ignored, so "Nathan",
# "<color=red>nathan</color>" and "N̶a̶t̶h̶a̶n̶" all count as the same name.
# Checked across both versions' account lists (1.31 and live share accountIDs).
NAME_DBS = ("Yeeps119", "Yeeps240")


def name_key(name) -> str:
    s = unicodedata.normalize("NFKD", _TAG.sub("", str(name or "")))
    s = "".join(c for c in s if _char_visible(c) or c == " ")
    return " ".join(s.lower().split())


async def name_owner(name, account_id: str = ""):
    """accountID of another account already using this name, else None. Accounts
    linked to the same Discord as account_id (one person's duplicate docs) don't count."""
    key = name_key(name)
    if not key:
        return None
    from services.db import client
    mine = set()
    if account_id:
        for db in NAME_DBS:
            d = await client[db]["accounts"].find_one({"accountID": account_id}, {"_id": 0, "discordID": 1})
            if d and d.get("discordID"):
                mine.add(str(d["discordID"]))
    for db in NAME_DBS:
        async for a in client[db]["accounts"].find({"displayName": {"$type": "string", "$ne": ""}},
                                                   {"_id": 0, "accountID": 1, "displayName": 1, "discordID": 1}):
            if a.get("accountID") == account_id or name_key(a.get("displayName")) != key:
                continue
            if a.get("discordID") and str(a["discordID"]) in mine:
                continue
            return a.get("accountID")
    return None


def add_suffix(name: str, sfx: str, cap: int = 20) -> str:
    """name + sfx, cutting a plain name's visible characters (keeping accents and
    other marks on the letters they belong to) so it stays within cap."""
    if _TAG.sub("", name) != name or visible_count(name) + len(sfx) <= cap:
        return name + sfx
    keep, out = max(1, cap - len(sfx)), ""
    for c in name:
        if _char_visible(c):
            if keep == 0:
                break
            keep -= 1
        out += c
    return out + sfx


async def unique_name(name, account_id: str = "", cap: int = 20) -> str:
    """The name itself if free, else the name with the lowest free number added
    (Nathan -> Nathan2, Nathan3, ...)."""
    name = str(name or "")
    if not await name_owner(name, account_id):
        return name
    for n in range(2, 1000):
        sfx = str(n)
        cand = add_suffix(name, sfx, cap)
        if not await name_owner(cand, account_id):
            return cand
    return fallback_name("", account_id)
