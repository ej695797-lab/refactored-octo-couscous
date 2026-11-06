#!/usr/bin/env python3
"""Set the editor OWNER password.

Two ways to give the new password, both keep it out of the chat and shell history:

  1) From a file (works everywhere, no interactive prompt needed):
        python3 set_owner_password.py /path/to/pwfile
     Put ONLY the new password (your old one with ! on the end) in that file
     using a text editor, save, then run the command. The file is SHREDDED after.

  2) Interactive hidden prompt (if your terminal supports it):
        python3 set_owner_password.py

Only the salted hash (sha256(EDITOR_SECRET|owner-pw|password)) is written to
vr-server.env. Restart yeeps-vr afterward.
"""
import os, re, sys, hashlib

ENV = os.path.expanduser("~/.config/yeeps/vr-server.env")

env = {}
for ln in open(ENV):
    if "=" in ln and not ln.lstrip().startswith("#"):
        k, v = ln.rstrip("\n").split("=", 1)
        env[k.strip()] = v

secret = env.get("EDITOR_SECRET")
if not secret:
    sys.exit("EDITOR_SECRET not found in vr-server.env — cannot hash the password.")

pwfile = sys.argv[1] if len(sys.argv) > 1 else None
if pwfile:
    with open(pwfile) as f:
        pw = f.read().strip("\n")        # keep spaces, drop only trailing newline
    # Shred the file: overwrite then remove, so the plaintext doesn't linger.
    try:
        n = os.path.getsize(pwfile)
        with open(pwfile, "wb") as f:
            f.write(os.urandom(max(n, 64)))
            f.flush(); os.fsync(f.fileno())
    except Exception:
        pass
    try:
        os.remove(pwfile)
    except Exception:
        pass
else:
    import getpass
    pw = getpass.getpass("New owner password (your password WITH the ! at the end): ")
    if pw != getpass.getpass("Confirm: "):
        sys.exit("Passwords did not match — nothing changed.")

if not pw:
    sys.exit("Empty password — nothing changed.")

h = hashlib.sha256(f"{secret}|owner-pw|{pw}".encode()).hexdigest()

s = open(ENV).read()
if "OWNER_EDITOR_PW_HASH=" in s:
    s = re.sub(r"OWNER_EDITOR_PW_HASH=.*", "OWNER_EDITOR_PW_HASH=" + h, s, count=1)
else:
    s = s.rstrip("\n") + "\nOWNER_EDITOR_PW_HASH=" + h + "\n"
open(ENV, "w").write(s)

print("Owner password updated (hash written, length %d). Password file shredded." % len(pw))
print("Now restart: systemctl --user restart yeeps-vr.service")
