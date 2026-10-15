"""North Carolina State Board of Elections absentee/one-stop voter files.

One row per voter with the date each ballot came back, so we can rebuild the cumulative
count at any number of days before Election Day for 2022, 2024 and 2026 alike.
Only aggregate counts are kept; the voter-level file (names, addresses) is never stored.

https://s3.amazonaws.com/dl.ncsbe.gov/ENRS/<election>/absentee_<yyyymmdd>.zip
"""
import csv
import io
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta

from ..common import fetch

ELECTIONS = {
    2022: ("2022_11_08", date(2022, 11, 8)),
    2024: ("2024_11_05", date(2024, 11, 5)),
    2026: ("2026_11_03", date(2026, 11, 3)),
}


def url(year):
    folder, d = ELECTIONS[year]
    return f"https://s3.amazonaws.com/dl.ncsbe.gov/ENRS/{folder}/absentee_{d:%Y%m%d}.zip"


def _age_group(a):
    try:
        a = int(a)
    except (TypeError, ValueError):
        return "unknown"
    return "18-29" if a < 30 else "30-44" if a < 45 else "45-64" if a < 65 else "65+"


def _party(p):
    p = (p or "").strip().upper()
    return {"DEM": "dem", "REP": "rep", "UNA": "una"}.get(p, "other")


def _race(r, eth):
    r = (r or "").strip().upper()
    if (eth or "").strip().upper() == "HISPANIC or LATINO".upper():
        return "hispanic"
    return {"WHITE": "white", "BLACK or AFRICAN AMERICAN".upper(): "black"}.get(r, "other")


def _method(t):
    t = (t or "").strip().upper()
    return "in_person" if t in ("ONE-STOP", "EARLY VOTING") else "mail" if t == "MAIL" else "other"


def aggregate(zip_bytes: bytes, year: int, cutoff: date | None = None):
    """Count accepted ballots by (days_out, method, party, race, age). Returns list of tuples."""
    eday = ELECTIONS[year][1]
    counts = Counter()
    skipped = 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        with z.open(name) as f:
            for r in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace")):
                if not (r.get("ballot_rtn_status") or "").strip().upper().startswith("ACCEPTED"):
                    continue
                try:
                    d = datetime.strptime(r["ballot_rtn_dt"].strip(), "%m/%d/%Y").date()
                except ValueError:
                    continue
                if cutoff and d > cutoff:  # a handful of records carry future-dated typos
                    skipped += 1
                    continue
                days_out = (eday - d).days
                if days_out < 0:  # ballots logged after Election Day
                    continue
                counts[(days_out, _method(r["ballot_req_type"]), _party(r["voter_party_code"]),
                        _race(r["race"], r["ethnicity"]), _age_group(r["age"]))] += 1
    if skipped:
        print(f"  NC {year}: skipped {skipped} ballots with return dates after {cutoff}")
    return [("NC", year, *k, n) for k, n in counts.items()]


def collect(con, day: str) -> dict:
    """Daily: refresh the 2026 NC pace from today's file."""
    body = fetch(url(2026))
    if body is None:
        raise RuntimeError("NC 2026 absentee file not found")
    # The file is built around 5 AM and covers returns through the previous day.
    rows = aggregate(body, 2026, cutoff=date.fromisoformat(day) - timedelta(days=1))
    con.execute("DELETE FROM pace WHERE state='NC' AND year=2026")
    con.executemany("INSERT INTO pace VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return {"rows": len(rows), "ballots": sum(r[-1] for r in rows)}
