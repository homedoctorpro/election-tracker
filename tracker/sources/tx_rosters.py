"""Texas early voters by precinct, from the Secretary of State's daily statewide roster.

The SOS early-voting app (goelect.txelections.civixapps.com) publishes, for each early-voting
date, a statewide CSV of every voter who voted that day: county, name, voter ID, method and
precinct. Only counts by (county, precinct, method) are kept: the file is read in memory and
discarded, and no name or voter ID is ever written to disk or the database.

Each date's counts are saved to data/history/tx_precincts/<date>.csv (committed, so the series
survives a fresh CI database). A date is fetched again while it's one of the two newest, since
counties keep adding to a day's roster through the next morning; older dates are final.

Precinct lean (in tracker/compare.py) weighs each precinct by its 2024 presidential margin from
the Texas Legislative Council's VTD returns (data/reference/tx_vtd_pres24.csv).
"""
import base64
import csv
import io
import json
from collections import Counter
from datetime import date, datetime

from ..common import ELECTION_DAYS, ROOT, fetch
from .tx_sos import NAME, partial  # noqa: F401 (partial: re-exported for compare)

API = "https://goelect.txelections.civixapps.com/api-ivis-system/api/v1/getFile"
OUT = ROOT / "data" / "history" / "tx_precincts"
REFETCH = 2  # newest dates fetched again each run


def _get(params):
    body = fetch(f"{API}?" + "&".join(f"{k}={v}" for k, v in params.items()), timeout=300)
    if not body:
        raise RuntimeError(f"Texas SOS roster API returned nothing for {params}")
    return base64.b64decode(json.loads(body)["upload"])


def _counts(raw: bytes) -> Counter:
    """(county, precinct, method) -> voters, from one statewide roster CSV held in memory."""
    c = Counter()
    for r in csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))):
        method = "mail" if r["VOTING_METHOD"].upper().startswith("MAIL") else "in_person"
        c[(r["COUNTY"].strip().upper(), r["PRECINCT"].strip(), method)] += 1
    return c


def collect(con, day: str) -> dict:
    info = json.loads(_get({"type": "EVR_ELECTION"}))
    el = next(e for e in info["elections"] if e["election_date"] == ELECTION_DAYS[2026].strftime("%m/%d/%Y")
              and NAME in e["election_name"])
    updated = datetime.strptime(info["date_updated"], "%m/%d/%Y").date()
    dates = sorted(d for d in (datetime.strptime(e["date"], "%m/%d/%Y").date() for e in el["early_voting_dates"])
                   if d <= updated)
    OUT.mkdir(parents=True, exist_ok=True)
    fetched = []
    for d in dates:
        path = OUT / f"{d.isoformat()}.csv"
        if path.exists() and d not in dates[-REFETCH:]:
            continue
        counts = _counts(_get({"type": "EVR_STATEWIDE", "electionId": el["id"], "electionDate": d.strftime("%m/%d/%Y")}))
        if not counts:
            continue
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["county", "precinct", "method", "n"])
            w.writerows([*k, n] for k, n in sorted(counts.items()))
        fetched.append(d.isoformat())
    return {"dates": len(dates), "fetched": len(fetched), "through": dates[-1].isoformat() if dates else None}


def load():
    """{date: {(county, precinct, method): n}} from the saved daily counts."""
    out = {}
    for p in sorted(OUT.glob("*.csv")):
        with open(p, encoding="utf-8") as f:
            out[date.fromisoformat(p.stem)] = {(r["county"], r["precinct"], r["method"]): int(r["n"])
                                               for r in csv.DictReader(f)}
    return out

