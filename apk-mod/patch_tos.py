#!/usr/bin/env python3
"""Replace the WHOLE Yeeps Terms of Service with the owner's placeholder
(owner 2026-10-17: change the WHOLE tos just to say "i dont know what to put here").

The TOS panel is ~28 TextMeshPro sections (x2 scenes). For each TOS MonoBehaviour
the section text (its longest length-prefixed string) is rewritten:
  intro "Please read carefully before agreeing..." -> PLACEHOLDER
  title "Terms of Service"                        -> kept (panel heading)
  every other section (headings + bodies)         -> ""  (blank)
Unity strings are <int32 len><bytes> padded to 4 bytes; the padding is rewritten
for the new length (UnityPy rebuilds the file, so sizes may change).

Usage: patch_tos.py IN.unity3d OUT.unity3d [--write]
"""
import struct
import sys

import UnityPy

PLACEHOLDER = "i dont know what to put here"
INTRO = b"Please read carefully before agreeing"
KEEP = {b"Terms of Service"}
MARKERS = [b"Trass Games", b"arbitrator", b"Severability", b"Dispute Resolution",
           b"Entire Agreement", b"Privacy Policy", b"Terms of Servic", b"ToS"]
# TOS section openings (from the panel); a string must start with one of these
# (or be a known heading) to be touched, so nothing outside the TOS changes.
TOS_HEADS = [
    b"16.1 Entire Agreement", b"All title, ownership", b"Some people may not be ready for VR",
    b"If you have any questions, complaints", b"Please read this section carefully",
    b"Please read carefully before agreeing", b"Be creative and original",
    b"This section only applies to the extent", b"You may provide feedback",
    b"1. Agreement to Terms and Privacy Policy", b"You are granted a limited",
    b"These Terms of Sale", b"You further agree not to engage", b"2. Privacy Policy",
    b"You may only use our Services", b"The Game is offered on an", b"Impersonate other players",
    b"Do not attempt to bypass rules", b"You agree and acknowledge that you assume",
    b"For your own safety, do not disclose", b"\xe2\x80\x9cChildren\xe2\x80\x9d are any person",
    b"Terms of Service", b"Damage, disable, impair any Services",
    b"For some individuals, the game", b"Trass Games may update, remove",
]


def pad(n):
    return (4 - n % 4) % 4


def longest_string(raw):
    """(len_prefix_offset, length) of the longest aligned printable string."""
    best = None
    for i in range(0, len(raw) - 4, 4):
        L = struct.unpack_from("<i", raw, i)[0]
        if 1 <= L <= 60000 and i + 4 + L <= len(raw):
            s = raw[i + 4:i + 4 + L]
            ok = sum(1 for c in s if c in (9, 10, 13) or c >= 32) / L
            if ok > 0.97 and (best is None or L > best[1]):
                best = (i, L)
    return best


def main():
    src, out = sys.argv[1], sys.argv[2]
    write = "--write" in sys.argv
    env = UnityPy.load(src)
    n_intro = n_blank = n_keep = 0
    for o in env.objects:
        if o.type.name != "MonoBehaviour":
            continue
        raw = o.get_raw_data()
        if not any(m in raw for m in MARKERS):
            continue
        hit = longest_string(raw)
        if not hit:
            continue
        lp, L = hit
        s = raw[lp + 4:lp + 4 + L]
        if not any(s.startswith(h) for h in TOS_HEADS):
            continue
        if s in KEEP:
            n_keep += 1
            continue
        new = PLACEHOLDER.encode() if s.startswith(INTRO) else b""
        old_end = lp + 4 + L + pad(L)
        if raw[lp + 4 + L:old_end] != b"\0" * pad(L):
            print(f"  [{o.assets_file.name}:{o.path_id}] unexpected padding, skipped")
            continue
        raw = raw[:lp] + struct.pack("<i", len(new)) + new + b"\0" * pad(len(new)) + raw[old_end:]
        if new:
            n_intro += 1
        else:
            n_blank += 1
        if write:
            o.set_raw_data(raw)
    print(f"intro->placeholder: {n_intro}  blanked: {n_blank}  title kept: {n_keep}  (write={write})")
    if write:
        with open(out, "wb") as f:
            f.write(env.file.save(packer="lz4"))
        print("saved", out)


if __name__ == "__main__":
    main()
