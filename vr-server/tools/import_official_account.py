"""Copy a player's profile from an official-Yeeps account export (the JSON
{"data": {...}} a player can dump) onto their EXISTING account on this server.

    python3 tools/import_official_account.py <export.json> <o_accountID> [--apply]

Without --apply it only prints what would change. The account must already exist
(the player logs into the 1.31.0-blaze APK once). A copy of the account as it was
is saved to backups/ before writing.

Imported: name, eye/skin colour, worn cosmetics + saved outfits (items that don't
exist in 1.31 are dropped), bundles, coins/beans/session count (higher value
wins), stash/challenge history, toymaker request, creator pack / private rooms.
NOT imported: official bans/warnings, roles, credentials/session/hash fields,
spawn room (2.x rooms don't exist in 1.31), pets (this server doesn't store pet
data yet), community-world lists, favourites (they point at official item IDs).
"""
import json, os, re, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pymongo, config

SET = ("displayName", "eyeColor", "skinColor", "toymakerRequest", "firstLogin",
       "anniversaryPuzzle_isComplete", "hasCreatorPack", "hasUnlockedPrivateRooms",
       "lastCardChallengeRedeemedTime", "lastCardBeanPurchaseTime", "beanPurchasedCardCount",
       "lootQuotaProgress_lootQuotas_easy", "lootQuotaProgress_lootQuotas_medium",
       "lootQuotaProgress_lootQuotas_hard")
MAX = ("currency", "beans", "sessionCount")
UNION = ("ownedBundles", "redeemedStashIDs", "redeemedCardChallengeKeys")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        sys.exit(__doc__)
    path, aid = args
    apply = "--apply" in sys.argv
    src = json.load(open(path))
    src = src.get("data", src)
    db = pymongo.MongoClient(config.MONGO_URI)[config.DB_NAME]
    acc = db.accounts.find_one({"accountID": aid})
    if not acc:
        sys.exit(f"no account {aid} on this server - the player must log in once first")

    g = db.globals.find_one({"key": "default_account_override"}) or {}
    valid = set((g.get("value") or {}).get("ownedPatterns") or []) | set(acc.get("ownedPatterns") or [])
    base = lambda k: re.sub(r"\[.*\]$", "", k)          # "shirt[black]" -> "shirt"

    upd, dropped = {}, set()
    for k in SET:
        if k in src:
            upd[k] = src[k]
    if "displayName" in upd:
        upd["displayName_searchable"] = str(upd["displayName"]).lower()
    for k in MAX:
        if k in src:
            upd[k] = max(int(acc.get(k) or 0), int(src[k] or 0))
    for k in UNION:
        if k in src:
            have = list(acc.get(k) or [])
            upd[k] = have + [x for x in src[k] if x not in have]
    if "activeCosmetics" in src:
        upd["activeCosmetics"] = [c for c in src["activeCosmetics"] if base(c) in valid]
        dropped |= {c for c in src["activeCosmetics"] if base(c) not in valid}
    if "savedOutfits" in src:
        outfits = []
        for o in src["savedOutfits"]:
            keep = [c for c in o if base(c) in valid]
            dropped |= {c for c in o if base(c) not in valid}
            if keep:
                outfits.append(keep)
        upd["savedOutfits"] = outfits
    # Patterns: only ones that exist in 1.31 and that everyone may own (the
    # default set), so blaze-only blocks can never come in through an import.
    default = set((g.get("value") or {}).get("ownedPatterns") or [])
    have = list(acc.get("ownedPatterns") or [])
    extra = [p for p in src.get("ownedPatterns") or [] if p in default and p not in have]
    if extra:
        upd["ownedPatterns"] = have + extra
    upd["importedFrom"] = {"source": f"official {src.get('applicationVersion', '?')} export",
                           "name": src.get("displayName"), "at": datetime.now(timezone.utc)}

    print(f"account {aid} ({acc.get('displayName')!r}) <- export {src.get('displayName')!r}")
    for k, v in upd.items():
        old = acc.get(k)
        show = (f"{len(old) if isinstance(old, list) else old!r} -> {len(v)} items" if isinstance(v, list)
                else f"{str(old)[:40]!r} -> {str(v)[:60]!r}")
        print(f"  {k}: {show}")
    if dropped:
        print(f"  dropped (not in 1.31): {sorted(dropped)}")
    print(f"  NOT imported: ban ({src.get('banReason') or 'none'}), roles, pets ({len(src.get('pets') or {})}), "
          f"credentials, spawn room {src.get('spawnRoomKey')!r}")
    if not apply:
        print("dry run - add --apply to write")
        return
    bdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")
    os.makedirs(bdir, exist_ok=True)
    bfile = os.path.join(bdir, f"account_{aid}_before_import_{datetime.now():%Y%m%d_%H%M%S}.json")
    with open(bfile, "w") as f:
        json.dump(acc, f, default=str)
    db.accounts.update_one({"accountID": aid}, {"$set": upd})
    print(f"applied. backup of the old account: {bfile}")


if __name__ == "__main__":
    main()
