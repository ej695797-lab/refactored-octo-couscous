"""Bake the 1.31 custom music disks + a Hackers disk into the LIVE (2.32) data.

Appends the custom PCM16 FSB5 banks to a COPY of sharedassets2.resource and
re-points the same-named AudioClips (CompressionFormat 0, mono 44100), the same
scheme the 1.31 build uses (131-notes/bake_disk.py). Also copies the 1.31 JamNJig
record label and paints the Buffering record label fully pink.

Keys/names are left unchanged (record ownership is keyed by name in the shared
big_patterns catalog), so the Buffering slot now PLAYS Hackers and looks pink but
its title stays "Buffering" until the owner okays a key rename.

Usage: bake_disks_live.py <disks_dir> <in data.unity3d> <out data.unity3d> \
                          <in sharedassets2.resource> <out sharedassets2.resource>
"""
import os
import shutil
import sys

import UnityPy
from PIL import Image

PINK = (255, 105, 180)  # hot pink, "fully pink"

# clip name -> fsb file in disks_dir (131 banks + freshly-baked Hackers on Buffering)
CLIPS = {
    "TheFinalHider": "TheFinalHider.fsb",
    "LushCircuit":   "LushCircuit.fsb",
    "Butterfly":     "Butterfly.fsb",
    "JamNJig":       "JamNJig.fsb",
    "Buffering":     "Hackers.fsb",   # Buffering slot now plays Hackers
}


def main(ddir, src, dst, res_in, res_out):
    shutil.copyfile(res_in, res_out)
    banks = {}
    size = os.path.getsize(res_out)
    with open(res_out, "ab") as res:
        for name, fn in CLIPS.items():
            fsb = open(os.path.join(ddir, fn), "rb").read()
            assert fsb[:4] == b"FSB5", fn
            pad = (-size) % 32
            res.write(b"\0" * pad)
            off = size + pad
            res.write(fsb)
            size = off + len(fsb)
            n = (len(fsb) - 96) // 2            # mono PCM16, 96-byte FSB header
            banks[name] = (off, len(fsb), n, n / 44100.0)
            print(f"append {name:14} <- {fn:18} {len(fsb):>10}B at {off:>12} ({n/44100:.1f}s)")

    env = UnityPy.load(src)
    done = set()
    jam_png = os.path.join(ddir, "jamNJig_131.png")
    for o in env.objects:
        if o.type.name == "AudioClip":
            t = o.read_typetree()
            b = banks.get(t["m_Name"])
            if not b:
                continue
            if "sharedassets2.resource" not in t["m_Resource"]["m_Source"]:
                print("  !! unexpected resource for", t["m_Name"], t["m_Resource"]["m_Source"])
                continue
            off, sz, n, length = b
            t.update(m_Channels=1, m_Frequency=44100, m_BitsPerSample=16,
                     m_Length=float(length), m_LoadInBackground=True, m_CompressionFormat=0)
            t["m_Resource"].update(m_Offset=off, m_Size=sz)
            o.save_typetree(t)
            done.add("clip:" + t["m_Name"])
        elif o.type.name == "Texture2D":
            try:
                nm = o.peek_name()
            except Exception:
                continue
            if nm == "recordArtTexture_jamNJig" and os.path.exists(jam_png):
                d = o.read()
                d.image = Image.open(jam_png).convert("RGB")
                d.save()
                done.add("art:jamNJig")
                print("  label recordArtTexture_jamNJig <- 131 cat art")
            elif nm == "recordArtTexture_buffering":
                d = o.read()
                w, h = d.image.size
                d.image = Image.new("RGB", (w, h), PINK)
                d.save()
                done.add("art:buffering(pink)")
                print(f"  label recordArtTexture_buffering <- fully pink {w}x{h}")

    missing = {"clip:" + c for c in CLIPS} - done
    print("done:", sorted(done))
    assert not missing, f"clips not found in data: {missing}"
    open(dst, "wb").write(env.file.save(packer="lz4"))
    print("saved", dst, os.path.getsize(dst), "B;  resource", os.path.getsize(res_out), "B")


if __name__ == "__main__":
    main(*sys.argv[1:6])
