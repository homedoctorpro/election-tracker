"""UF Election Lab (Michael McDonald) 2026 general early-vote statistics.

The lab compiles every state's official early/absentee reporting into one national file
plus per-state county and congressional-district files:
  https://election.lab.ufl.edu/early-vote/2026-early-voting/
License: CC BY-NC-ND 4.0 (attribution, non-commercial, no derivatives for redistribution).
"""
import csv
import io
import re
from collections import defaultdict
from datetime import datetime

from ..common import STATES, fetch, save_raw

BASE = "https://election.lab.ufl.edu/data-downloads/earlyvote/2026"
SOURCE = "uf_election_lab"
METRIC_RE = re.compile(r"^(request|accept|inperson|voted)_|^return_rate$")


def _num(v):
    if v is None:
        return None
    v = v.strip().replace(",", "")
    if v in ("", "NA", "N/A", "-"):
        return None
    if v.endswith("%"):
        v = v[:-1].strip()
    try:
        return float(v)
    except ValueError:
        return None


def _iso(d):
    try:
        return datetime.strptime(d.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


def _rows(body: bytes):
    return list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))


def _metrics(row):
    for k, v in row.items():
        if k and METRIC_RE.match(k):
            yield k, _num(v)


def collect(con, day: str) -> dict:
    stats = {"states": 0, "county_files": 0, "cd_files": 0, "values": 0, "ratings": 0}
    values = []

    # --- National file: one row per state ---
    body = fetch(f"{BASE}/US.csv")
    if body is None:
        raise RuntimeError("UF US.csv not found")
    save_raw(SOURCE, "US.csv", body, day)
    for r in _rows(body):
        st = r["state_abbv"].strip()
        con.execute(
            "INSERT OR REPLACE INTO ev_state_meta VALUES (?,?,?,?,?,?,?)",
            (day, st, r["state"], _iso(r["last_update"]), r["data_source"],
             None if r["notes"] == "NA" else r["notes"], _num(r["turn_2022"])),
        )
        values += [(day, st, "state", st, m, v) for m, v in _metrics(r)]
        stats["states"] += 1

    # --- Per-state county and congressional-district files (not every state has them) ---
    for st in STATES:
        body = fetch(f"{BASE}/{st}_county.csv")
        if body:
            save_raw(SOURCE, f"{st}_county.csv", body, day)
            stats["county_files"] += 1
            for r in _rows(body):
                county = (r.get("county") or r.get("County") or "").strip()
                if county:
                    values += [(day, st, "county", county, m, v) for m, v in _metrics(r)]

        body = fetch(f"{BASE}/{st}_cd.csv")
        if body:
            save_raw(SOURCE, f"{st}_cd.csv", body, day)
            stats["cd_files"] += 1
            cd_tot = defaultdict(float)
            for r in _rows(body):
                if not (r.get("cd") or "").strip():
                    continue
                cd = r["cd"].strip().zfill(2)
                county = (r.get("county") or "").strip()
                for m, v in _metrics(r):
                    if county:  # CD files split districts by county; keep the pieces too
                        values.append((day, st, "cd_county", f"{st}-{cd}|{county}", m, v))
                    if v is not None and m != "return_rate":
                        cd_tot[(cd, m)] += v
            values += [(day, st, "cd", f"{st}-{cd}", m, v) for (cd, m), v in cd_tot.items()]

    con.executemany("INSERT OR REPLACE INTO ev_values VALUES (?,?,?,?,?,?)", values)
    stats["values"] = len(values)

    # --- House race ratings the lab scrapes from ten outlets ---
    body = fetch(f"{BASE}/house_tossup_races.csv")
    if body:
        save_raw(SOURCE, "house_tossup_races.csv", body, day)
        outlets = ["cook", "ie", "sabato", "econ", "splitticket", "ddhq", "fpo", "fox", "silver", "rcp"]
        rows = []
        for r in _rows(body):
            for o in outlets:
                if o in r:
                    rows.append((day, r["key"], r["state"], r["cd"], o, (r[o] or "").replace("|", " ")))
        con.executemany("INSERT OR REPLACE INTO race_ratings VALUES (?,?,?,?,?,?)", rows)
        stats["ratings"] = len(rows)

    con.commit()
    return stats
