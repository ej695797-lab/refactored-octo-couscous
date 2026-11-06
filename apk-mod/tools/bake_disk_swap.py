"""Swap one existing record (music disk) in the LIVE data for a new song + label.

Same bake as bake_disks_live.py / 131-notes/bake_disk.py: audio -> ffmpeg mono PCM16
44100 (loudnorm I=-14) -> FSB5 v1 PCM bank appended (32-byte aligned) to a COPY of
sharedassets2.resource, then the record's AudioClip is re-pointed at it. The in-game
disk title is the AudioClip's m_Name, so a new name renames the disk; the ownership key
(record_<name>) does NOT change (renaming it broke Hackers: "record_hackers does not
exist"). The label texture keeps its name; its picture is replaced (a full record art
atlas of the same size, e.g. 32x32: label top-left, case colours in the other quadrants).

usage: bake_disk_swap.py CLIP NEW_CLIP_NAME TEXTURE AUDIO ART IN_DATA OUT_DATA IN_RES OUT_RES
"""
import os
import shutil
import struct
import subprocess
import sys

import UnityPy
import imageio_ffmpeg
from PIL import Image

AR, FREQ_IDX = 44100, 8


def fsb_from(audio):
    pcm = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", audio,
                          "-vn", "-ac", "1", "-ar", str(AR), "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    n = len(pcm) // 2
    pcm = pcm[:2 * n]
    v = (FREQ_IDX << 1) | (n << 34)                       # mono, data offset 0
    fsb = (b"FSB5" + struct.pack("<6I", 1, 1, 36, 0, len(pcm), 2) + b"\0" * 32
           + struct.pack("<Q", v) + b"\0" * 28 + pcm)
    assert len(fsb) == 96 + len(pcm)
    return fsb, n


def main(clip, new_name, tex_name, audio, art, src, dst, res_in, res_out):
    fsb, n = fsb_from(audio)
    shutil.copyfile(res_in, res_out)
    size = os.path.getsize(res_out)
    pad = (-size) % 32
    with open(res_out, "ab") as f:
        f.write(b"\0" * pad)
        off = size + pad
        f.write(fsb)
    print(f"audio: {n} samples ({n / AR:.1f}s), FSB {len(fsb)} B at {off}")

    pic = Image.open(art).convert("RGB")
    env = UnityPy.load(src)
    done = set()
    for o in env.objects:
        if o.type.name == "AudioClip":
            t = o.read_typetree()
            if t["m_Name"] != clip:
                continue
            assert "sharedassets2.resource" in t["m_Resource"]["m_Source"], t["m_Resource"]["m_Source"]
            t.update(m_Name=new_name, m_Channels=1, m_Frequency=AR, m_BitsPerSample=16,
                     m_Length=float(n / AR), m_LoadInBackground=True, m_CompressionFormat=0)
            t["m_Resource"].update(m_Offset=off, m_Size=len(fsb))
            o.save_typetree(t)
            done.add("clip")
        elif o.type.name == "Texture2D":
            try:
                if o.peek_name() != tex_name:
                    continue
            except Exception:
                continue
            d = o.read()
            w, h = d.image.size
            d.image = pic if pic.size == (w, h) else pic.resize((w, h), Image.NEAREST)
            d.save()
            done.add("art")
    print("updated:", sorted(done))
    assert done == {"clip", "art"}, f"missing: {({'clip', 'art'} - done)}"
    open(dst, "wb").write(env.file.save(packer="lz4"))
    print("saved", dst, os.path.getsize(dst), "B;  resource", os.path.getsize(res_out), "B")


if __name__ == "__main__":
    main(*sys.argv[1:10])
