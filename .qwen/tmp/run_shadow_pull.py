"""Stage the working tree's app/ into the container as a shadow package and run the
REAL patched pull against the live tenant — read-only, no HubSpot, no writes.

Answers the only question that matters before the first certified run: what does
_integrity say, and what would pruning do with it.
"""
import subprocess
import sys

LOCAL_APP = "app"
SHADOW = "/tmp/shadow"

steps = [
    ["docker", "exec", "hubquira-worker-1", "mkdir", "-p", f"{SHADOW}"],
    ["docker", "cp", LOCAL_APP, f"hubquira-worker-1:{SHADOW}/app"],
    ["docker", "cp", ".qwen/tmp/shadow_pull.py", f"hubquira-worker-1:{SHADOW}/shadow_pull.py"],
]
for cmd in steps:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("FAILED:", " ".join(cmd))
        print(r.stdout, r.stderr)
        sys.exit(1)
    print("ok:", " ".join(cmd))

run = subprocess.run(
    [
        "docker", "exec", "-w", SHADOW, "-e", f"PYTHONPATH={SHADOW}",
        "hubquira-worker-1", "python", "-u", "shadow_pull.py",
    ],
    capture_output=True,
    text=True,
)
print(run.stdout)
if run.stderr.strip():
    print("STDERR:", run.stderr[-3000:])
sys.exit(run.returncode)
