"""Permanent daily archive, committed to git, so future cycles can compare against 2026
at the exact same number of days before Election Day.

data/archive/<cycle>/<YYYY-MM-DD>/
    US.csv.gz                  UF Election Lab national file, exactly as downloaded
    house_tossup_races.csv.gz  UF race-ratings file
    uf_geo.tar.gz              every UF county and congressional-district file
    votehub_polls.json.gz      full VoteHub poll history as of that morning
    registration.csv.gz        NC and PA registered voters by party, latest snapshot
    pace.csv.gz                NC and PA accepted ballots by return day, method, party (and race,
                               age for NC); aggregate counts only, voter-level files are never stored
    manifest.json              what was saved and when

Re-running on the same day replaces that day's folder.
"""
import csv
import gzip
import io
import json
import shutil
import tarfile

from .common import RAW, ROOT, now_et

ARCHIVE = ROOT / "data" / "archive"
CYCLE = "2026"


def _gzip_bytes(data: bytes, dst):
    # mtime=0 keeps the output byte-identical for identical input, so git sees no change
    with open(dst, "wb") as f:
        with gzip.GzipFile(filename="", mode="wb", fileobj=f, compresslevel=9, mtime=0) as g:
            g.write(data)


def _gz(src, dst):
    _gzip_bytes(src.read_bytes(), dst)


def archive_day(con, day: str) -> dict:
    raw = RAW / day
    out = ARCHIVE / CYCLE / day
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    saved = []

    uf = raw / "uf_election_lab"
    for name in ("US.csv", "house_tossup_races.csv"):
        if (uf / name).exists():
            _gz(uf / name, out / f"{name}.gz")
            saved.append(f"{name}.gz")
    geo = sorted(p for p in uf.glob("*.csv") if p.name.endswith(("_county.csv", "_cd.csv"))) if uf.exists() else []
    if geo:
        with tarfile.open(out / "uf_geo.tar.gz", "w:gz") as t:
            for p in geo:
                t.add(p, arcname=p.name)
        saved.append(f"uf_geo.tar.gz ({len(geo)} files)")

    polls = raw / "votehub" / "polls.json"
    if polls.exists():
        _gz(polls, out / "votehub_polls.json.gz")
        saved.append("votehub_polls.json.gz")

    rows = con.execute("SELECT state, year, days_out, method, party, race, age_group, n FROM pace "
                       "WHERE year = ? ORDER BY 1,2,3,4,5,6,7", (int(CYCLE),)).fetchall()
    if rows:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["state", "year", "days_out", "method", "party", "race", "age_group", "n"])
        w.writerows(rows)
        _gzip_bytes(buf.getvalue().encode("utf-8"), out / "pace.csv.gz")
        saved.append("pace.csv.gz")

    reg = con.execute("SELECT state, year, as_of, dem, rep, other FROM registration WHERE year = ?",
                      (int(CYCLE),)).fetchall()
    if reg:
        buf = io.StringIO()
        csv.writer(buf).writerows([("state", "year", "as_of", "dem", "rep", "other"), *reg])
        _gzip_bytes(buf.getvalue().encode("utf-8"), out / "registration.csv.gz")
        saved.append("registration.csv.gz")

    (out / "manifest.json").write_text(json.dumps(
        {"day": day, "archived_at": now_et(), "files": saved}, indent=1), encoding="utf-8")
    size = sum(p.stat().st_size for p in out.iterdir())
    return {"files": len(saved), "kb": round(size / 1024)}
