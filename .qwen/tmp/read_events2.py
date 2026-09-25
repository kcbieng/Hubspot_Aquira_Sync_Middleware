"""Read-only: find the run(s) whose notice named failed reads, and the sync_run history."""
import json
import sqlite3

con = sqlite3.connect("file:app.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row

print("--- job_event: any message naming failed reads ---")
n = 0
for r in con.execute(
    "SELECT id, ts, level, message, payload_json FROM job_event "
    "WHERE message LIKE '%failed read%' ORDER BY id DESC LIMIT 12"
):
    n += 1
    print(f"[{r['id']}] {r['ts']} {r['level']}: {r['message'][:200]}")
    print(f"      payload: {str(r['payload_json'])[:2000]}")
if not n:
    print("  (none)")

print("\n--- job_event levels/messages, last 40 ---")
for r in con.execute("SELECT id, ts, job, level, substr(message,1,110) m FROM job_event ORDER BY id DESC LIMIT 40"):
    print(f"[{r['id']}] {r['ts']} {r['job']}/{r['level']}: {r['m']}")

print("\n--- sync_run, last 12 ---")
cols = [r[1] for r in con.execute("PRAGMA table_info(sync_run)")]
print("cols:", cols)
for r in con.execute("SELECT * FROM sync_run ORDER BY rowid DESC LIMIT 12"):
    d = {k: r[k] for k in r.keys()}
    print(json.dumps(d, default=str)[:800])

print("\n--- counts ---")
for t in ("job_event", "sync_run", "sync_run_item", "dead_letter"):
    print(t, con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
print("event id range:", con.execute("SELECT MIN(id), MAX(id) FROM job_event").fetchone()[:])
