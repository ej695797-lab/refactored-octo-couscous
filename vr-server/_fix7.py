p = "handlers/login.py"
s = open(p, "rb").read()
old = b"    # Force-clear any admin block on every login so nobody gets stuck banned.\r\n    if account.get(\"adminBlocked\"):\r\n        try:\r\n            _exp = datetime.strptime(account[\"banExpireTimestamp\"], \"%Y %m/%d %H:%M:%S\").replace(tzinfo=timezone.utc)\r\n            if _exp <= datetime.now(timezone.utc):\r\n                account[\"adminBlocked\"] = 0\r\n                await accounts.update_one({\"accountID\": account_id},\r\n                    {\"$set\": {\"adminBlocked\": 0, \"banExpireTimestamp\": \"\", \"banReason\": \"\"}})\r\n        except Exception:\r\n            pass"
new = b"    # Force-clear any admin block on every login so nobody gets stuck banned.\r\n    if account.get(\"adminBlocked\"):\r\n        account[\"adminBlocked\"] = 0\r\n        account[\"banReason\"] = \"\"\r\n        account[\"banExpireTimestamp\"] = \"\"\r\n        account[\"blockReason\"] = \"\"\r\n        try:\r\n            await accounts.update_one({\"accountID\": account_id},\r\n                {\"$set\": {\"adminBlocked\": 0, \"banReason\": \"\", \"banExpireTimestamp\": \"\", \"blockReason\": \"\"}})\r\n        except Exception:\r\n            pass"
assert old in s, "old not found"
s = s.replace(old, new)
open(p, "wb").write(s)
print("done")