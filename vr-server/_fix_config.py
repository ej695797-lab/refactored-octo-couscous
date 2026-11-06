p = "config.py"
s = open(p, encoding="utf-8").read()
old = "ENFORCE_CLIENT_BUILD = os.getenv(\"ENFORCE_CLIENT_BUILD\", \"1\") != \"0\""
new = "ENFORCE_CLIENT_BUILD = False"
assert old in s, "old not found"
s = s.replace(old, new)
open(p, "w", encoding="utf-8").write(s)
print("done")
"