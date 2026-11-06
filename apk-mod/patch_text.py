#!/usr/bin/env python3
"""Replace serialized UI text strings inside data.unity3d MonoBehaviours
(TMP m_text etc.) by raw-bytes edit. il2cpp bundles have no MonoBehaviour
typetree, so we locate each length-prefixed string that contains a marker and
rewrite it in place (UnityPy rebuilds the serialized file, so the length may
change). Dry-run prints what it would do; --write applies + saves.

Usage: patch_text.py IN.unity3d OUT.unity3d [--write]
Edits (marker-substring -> replacement):
"""
import sys, struct, UnityPy

# (marker that identifies the target string, full replacement text)
EDITS = [
    # The whole Yeeps Terms of Service block -> owner placeholder.
    # TOS: pending owner decision (57 legal text blocks) -- not applied yet.
    # News-board review button label -> mod menu.
    ("Leave a review to help us grow", "open the built mod menu"),
]


def find_string(raw, marker_b):
    """Return (start_of_len_prefix, str_start, str_len) for the length-prefixed
    string that contains marker_b, or None. Unity string = <int32 LE len><bytes>."""
    i = raw.find(marker_b)
    if i < 0:
        return None
    TEXT = lambda b: b in (9, 10, 13) or 32 <= b <= 126
    # expand the printable run around the marker
    s = i
    while s > 0 and TEXT(raw[s - 1]):
        s -= 1
    e = i + len(marker_b)
    while e < len(raw) and TEXT(raw[e]):
        e += 1
    # the real string start is where the 4-byte length prefix validates
    for st in range(s, i + 1):
        if st < 4:
            continue
        L = struct.unpack_from("<i", raw, st - 4)[0]
        if L <= 0 or st + L > len(raw):
            continue
        if st <= i and (st + L) >= (i + len(marker_b)) and (st + L) <= e + 4:
            return (st - 4, st, L)
    return None


def main():
    src, out = sys.argv[1], sys.argv[2]
    write = "--write" in sys.argv
    env = UnityPy.load(src)
    patched = 0
    for o in env.objects:
        if o.type.name != "MonoBehaviour":
            continue
        raw = o.get_raw_data()
        changed = False
        for marker, repl in EDITS:
            mb = marker.encode()
            while True:
                hit = find_string(raw, mb)
                if not hit:
                    break
                lp, ss, L = hit
                old = raw[ss:ss + L]
                new = repl.encode()
                # rebuild: new length prefix + new bytes (UnityPy re-pads/aligns on save)
                raw = raw[:lp] + struct.pack("<i", len(new)) + new + raw[ss + L:]
                changed = True
                print(f"  [{o.path_id}] {marker!r}: {len(old)}B -> {len(new)}B  (old starts {old[:50]!r})")
                break  # one occurrence per object per marker is enough
        if changed:
            if write:
                o.set_raw_data(raw)
            patched += 1
    print(f"objects patched: {patched}  (write={write})")
    if write:
        with open(out, "wb") as f:
            f.write(env.file.save(packer="lz4"))
        print("saved", out)


if __name__ == "__main__":
    main()
