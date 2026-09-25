"""Read-only: which settings rows exist locally, and do they carry Aquira credentials?
Values are masked — only length / shape is printed, never the secret."""
import sqlite3

con = sqlite3.connect("file:app.db?mode=ro", uri=True)
rows = list(con.execute("SELECT key, value FROM app_settings ORDER BY key"))
print(f"{len(rows)} app_settings row(s)")
for k, v in rows:
    s = "" if v is None else str(v)
    if any(t in k.lower() for t in ("password", "token", "secret", "key")):
        print(f"  {k} = <set: len={len(s)} prefix={s[:7]!r}>")
    else:
        print(f"  {k} = {s[:120]!r}")
