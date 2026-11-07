p = "handlers/login.py"
s = open(p, encoding="utf-8").read()
old = "    # Force-clear any admin block on every login so nobody gets stuck banned.\n    if account.get(\"adminBlocked\"):\n        try:\n            _exp = datetime.strptime(account[\"banExpireTimestamp\"], \"%Y %m/%d %H:%M:%S\").replace(tzinfo=timezone.utc)\n            if _exp <= datetime.now(timezone.utc):\n                account[\"adminBlocked\"] = 0\n                await accounts.update_one({\"accountID\": account_id},\n                    {\"$set\": {\"adminBlocked\": 0, \"banExpireTimestamp\": \"\", \"banReason\": \"\"}})\n        except Exception:\n            pass"
new = "    # Force-clear any admin block on every login so nobody gets stuck banned.\n    if account.get(\"adminBlocked\"):\n        account[\"adminBlocked\"] = 0\n        account[\"banReason\"] = \"\"\n        account[\"banExpireTimestamp\"] = \"\"\n        account[\"blockReason\"] = \"\"\n        try:\n            await accounts.update_one({\"accountID\": account_id},\n                {\"$set\": {\"adminBlocked\": 0, \"banReason\": \"\", \"banExpireTimestamp\": \"\", \"blockReason\": \"\"}})\n        except Exception:\n            pass"
assert old in s, "old not found"
s = s.replace(old, new)
open(p, "w", encoding="utf-8").write(s)
print("done")