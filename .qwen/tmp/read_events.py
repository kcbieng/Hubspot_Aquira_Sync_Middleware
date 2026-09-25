"""Read-only dump of the stored 'Revenue pruning suppressed' events from app.db."""
import json
import sqlite3
import sys

db = sys.argv[1] if len(sys.argv) > 1 else "app.db"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("tables:", tables)

cand = [t for t in tables if "event" in t.lower()]
if not cand:
    sys.exit("no events table")
t = cand[0]
print("schema:", [r[1] for r in con.execute(f"PRAGMA table_info({t})")])

rows = list(con.execute(
    f"SELECT * FROM {t} WHERE message LIKE '%pruning suppressed%' ORDER BY id DESC LIMIT 5"
))
print(f"matched {len(rows)} pruning-suppressed event(s)\n")
for r in rows:
    d = dict(r)
    print("=" * 70)
    for k, v in d.items():
        if k == "data" and isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                pass
        print(f"  {k}: {json.dumps(v, indent=2)[:4000] if isinstance(v, (dict, list)) else v}")
