"""Put a picture on a record's label in the 2.31.4 data (same as the 1.31 Jam N Jig
disk): record art is a 32x32 RGB24 atlas whose top-left 16x16 texels are the label,
so the atlas is scaled up to 256x256 and the picture goes in that quadrant.

Usage: record_art.py <in data.unity3d> <out data.unity3d> <recordArtTexture_name> <picture> [x0,y0,x1,y1]
"""
import sys

import UnityPy
from PIL import Image

src, dst, tex_name, pic_path = sys.argv[1:5]
crop = tuple(int(v) for v in sys.argv[5].split(",")) if len(sys.argv) > 5 else None
env = UnityPy.load(src)
done = False
for o in env.objects:
    if o.type.name != "Texture2D":
        continue
    try:
        if o.peek_name() != tex_name:
            continue
    except Exception:
        continue
    d = o.read()
    base = d.image.convert("RGB")
    k = 256 // base.width
    atlas = base.resize((base.width * k, base.height * k), Image.NEAREST)
    pic = Image.open(pic_path).convert("RGB")
    if crop:
        pic = pic.crop(crop)
    q = atlas.width // 2
    atlas.paste(pic.resize((q, q), Image.LANCZOS), (0, 0))
    d.image = atlas
    d.save()
    done = True
    print(f"{tex_name}: {base.width}x{base.height} -> {atlas.width}x{atlas.height}, label = picture")
assert done, f"{tex_name} not found"
open(dst, "wb").write(env.file.save(packer="lz4"))
print("saved", dst)
