"""Pennsylvania Department of State mail-ballot files on data.pa.gov (one row per mail voter).

The Socrata API aggregates server-side, so we only ever receive counts by return date and
party; no voter-level records are downloaded. Pennsylvania has no in-person early voting, so
mail ballots are the whole early vote.
"""
import json
import urllib.parse
from datetime import date, timedelta

from ..common import ELECTION_DAYS, fetch

DATASETS = {2022: "uhfm-zhus", 2024: "3q5t-ddp8", 2026: "nbwd-pfn4"}
# 2022's file has no ballot_status column; later files mark cancelled ballots.
HAS_STATUS = {2022: False, 2024: True, 2026: True}


def _party(p):
    # 2022 and 2026 files use D / R; the 2024 file uses DEM / REP
    return {"D": "dem", "DEM": "dem", "R": "rep", "REP": "rep"}.get((p or "").strip().upper(), "other")


def aggregate(year: int, cutoff: date | None = None):
    where = "ballotreturneddate IS NOT NULL"
    if HAS_STATUS[year]:
        where += " AND (ballot_status IS NULL OR ballot_status != 'Cancelled')"
    q = (f"SELECT ballotreturneddate, party, count(*) AS n WHERE {where} "
         "GROUP BY ballotreturneddate, party LIMIT 50000")
    url = f"https://data.pa.gov/resource/{DATASETS[year]}.json?" + urllib.parse.urlencode({"$query": q})
    eday = ELECTION_DAYS[year]
    counts = {}
    for r in json.loads(fetch(url, timeout=300)):
        try:
            d = date.fromisoformat(r["ballotreturneddate"][:10])
        except (KeyError, ValueError):
            continue
        if (cutoff and d > cutoff) or d > eday:
            continue
        k = ((eday - d).days, _party(r.get("party")))
        counts[k] = counts.get(k, 0) + int(r["n"])
    return [("PA", year, dout, "mail", party, "unknown", "unknown", n) for (dout, party), n in counts.items()]


def collect(con, day: str) -> dict:
    # The file is refreshed overnight and covers returns through the previous day.
    rows = aggregate(2026, cutoff=date.fromisoformat(day) - timedelta(days=1))
    con.execute("DELETE FROM pace WHERE state='PA' AND year=2026")
    con.executemany("INSERT INTO pace VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return {"rows": len(rows), "ballots": sum(r[-1] for r in rows)}
