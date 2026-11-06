import UnityPy, hashlib, sys
src, dst, seed = sys.argv[1:4]
env = UnityPy.load(src)
bf = env.file
old = [k for k in bf.files if not k.endswith(".resS")][0]
new = "CAB-" + hashlib.md5(seed.encode()).hexdigest()
raw = {}
# rewrite streamed-data paths inside the serialized file
sf = bf.files[old]
n = 0
for o in sf.objects.values():
    if o.type.name in ("Mesh", "Texture2D"):
        t = o.read_typetree()
        key = "m_StreamData"
        if key in t and t[key].get("path"):
            t[key]["path"] = t[key]["path"].replace(old, new); o.save_typetree(t); n += 1
sf.name = new
files = {}
for k, v in bf.files.items():
    nk = k.replace(old, new)
    if hasattr(v, "name"): v.name = nk
    files[nk] = v
bf.files = files
open(dst, "wb").write(bf.save(packer="lz4"))
print(old, "->", new, "stream paths fixed:", n)
