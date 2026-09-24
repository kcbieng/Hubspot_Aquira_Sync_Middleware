import pathlib

rows = [(len(p.read_text(encoding="utf-8", errors="ignore").splitlines()), p.as_posix()) for p in pathlib.Path("app").rglob("*.py")]
rows.sort(reverse=True)
print("TOTAL app/:", sum(r[0] for r in rows), "files:", len(rows))
for n, p in rows:
    print(f"{n:5d}  {p}")

trows = [(len(p.read_text(encoding="utf-8", errors="ignore").splitlines()), p.as_posix()) for p in pathlib.Path("tests").rglob("*.py")]
print("TOTAL tests/:", sum(r[0] for r in trows), "files:", len(trows))
