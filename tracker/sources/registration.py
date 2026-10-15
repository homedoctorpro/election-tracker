"""Voter registration by party for states where we adjust mail-ballot shifts (NC, PA).

Used to separate "the mail electorate changed" from "the registered electorate changed": if
Democrats are a smaller share of mail ballots only because they're a smaller share of
registered voters, that isn't a change in Democratic turnout behavior.

NC: NCSBE voter_stats snapshots, published per election under ENRS/<election>/. For 2026 we
    take the most recent snapshot posted so far (the March primary until a newer one appears).
PA: Department of State weekly statistics workbook (currentvotestats.xlsx) for 2026; the
    certified November 2022 and 2024 PDFs for past cycles (built in tracker.reference).
"""
import csv
import io
import re
import zipfile
from datetime import datetime

from ..common import fetch

NC_BUCKET = "https://s3.amazonaws.com/dl.ncsbe.gov"
PA_CURRENT = ("https://www.pa.gov/content/dam/copapwp-pagov/en/dos/resources/voting-and-elections/"
              "voting-and-election-statistics/currentvotestats.xlsx")


def nc_snapshot(key: str):
    """Sum an NCSBE voter_stats zip to (as_of, dem, rep, other)."""
    body = fetch(f"{NC_BUCKET}/{key}", timeout=300)
    dem = rep = other = 0
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith((".txt", ".csv")))
        delim = "\t" if name.lower().endswith(".txt") else ","
        for r in csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8", errors="replace"), delimiter=delim):
            r = {(k or "").strip().lower(): v for k, v in r.items()}
            n = int(float(r.get("total_voters") or 0))
            p = (r.get("party_cd") or "").strip().upper()
            if p == "DEM":
                dem += n
            elif p == "REP":
                rep += n
            else:
                other += n
    as_of = datetime.strptime(re.search(r"voter_stats_(\d{8})", key).group(1), "%Y%m%d").date().isoformat()
    return as_of, dem, rep, other


def nc_latest_key(year: int):
    """Most recent voter_stats snapshot posted for the given year."""
    listing = fetch(f"{NC_BUCKET}?list-type=2&prefix=ENRS/{year}_").decode()
    keys = re.findall(r"<Key>([^<]*voter_stats_\d{8}\.zip)</Key>", listing)
    return max(keys, key=lambda k: re.search(r"(\d{8})", k).group(1)) if keys else None


def pa_current():
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(fetch(PA_CURRENT, timeout=120)), read_only=True, data_only=True)
    rows = list(wb["Reg Voter"].iter_rows(values_only=True))
    as_of = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", str(rows[0][0])).group(1)
    head = [str(c).strip() if c else "" for c in rows[1]]
    i_d, i_r, i_n, i_o = head.index("Dem"), head.index("Rep"), head.index("No Aff"), head.index("Other")
    dem = rep = other = 0
    for r in rows[2:]:
        if not r[0] or not isinstance(r[i_d], (int, float)) or str(r[0]).strip().lower().startswith("total"):
            continue
        dem += r[i_d]
        rep += r[i_r]
        other += (r[i_n] or 0) + (r[i_o] or 0)
    return datetime.strptime(as_of, "%m/%d/%Y").date().isoformat(), int(dem), int(rep), int(other)


def collect(con, day: str) -> dict:
    rows = []
    key = nc_latest_key(2026)
    if key:
        rows.append(("NC", 2026, *nc_snapshot(key)))
    rows.append(("PA", 2026, *pa_current()))
    con.execute("DELETE FROM registration WHERE year=2026")
    con.executemany("INSERT INTO registration VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    return {r[0]: f"as of {r[2]}" for r in rows}
