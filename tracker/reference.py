"""Historical baselines for comparing 2026 against 2022 and 2024.

Build once (slow: downloads ~400 MB), commit the small CSVs in data/reference/:
    python -m tracker.reference --build
Every refresh loads them into the DB with load(con).

Sources
- EAC Election Administration and Voting Survey (EAVS) 2022 v1 and 2024 v2: official final
  counts by jurisdiction. Pre-Election-Day votes = F1d mail + F1f early in-person + F1g mail in
  all-mail jurisdictions. Negative values are EAC "not available" codes and count as missing.
- UF Election Lab 2024 US.csv: the final version plus every Internet Archive capture taken
  during the 2024 race, used for same-days-out comparisons where a capture exists.
- NC State Board of Elections absentee voter files for 2022 and 2024 (exact daily pace).
"""
import argparse
import csv
import gzip
import io
import re
import zipfile
from datetime import date, datetime

from .common import ELECTION_DAYS, ROOT, STATES, fetch
from .sources import nc_voter_file, pa_mail_file

REF = ROOT / "data" / "reference"
EAVS = {
    2022: "https://www.eac.gov/sites/default/files/2023-06/2022_EAVS_for_Public_Release_nolabel_V1_CSV.zip",
    2024: "https://www.eac.gov/sites/default/files/2026-02/2024_EAVS_for_Public_Release_nolabel_V2_csv.zip",
}
UF2024 = "https://election.lab.ufl.edu/data-downloads/earlyvote/2024/US.csv"
EDAY_2024 = ELECTION_DAYS[2024]
UF_COLS = ["voted_all", "voted_dem", "voted_rep", "voted_none", "accept_all", "inperson_all",
           "accept_dem", "accept_rep", "accept_none"]


def _write(name, header, rows):
    REF.mkdir(parents=True, exist_ok=True)
    with open(REF / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"  data/reference/{name}: {len(rows)} rows")


def _n(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x < 0 else x


def build_eavs():
    rows = []
    for year, url in EAVS.items():
        with zipfile.ZipFile(io.BytesIO(fetch(url, timeout=300))) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            data = list(csv.DictReader(io.TextIOWrapper(z.open(name), encoding="latin-1")))
        tot = {}
        for r in data:
            st = r["State_Abbr"].strip()
            if st not in STATES:
                continue
            t = tot.setdefault(st, {"F1a": 0, "F1d": 0, "F1f": 0, "F1g": 0})
            for k in t:
                v = _n(r.get(k))
                if v is not None:
                    t[k] += v
        for st, t in sorted(tot.items()):
            rows.append((st, year, int(t["F1a"]), int(t["F1d"]), int(t["F1f"]), int(t["F1g"])))
    _write("eavs_state.csv", ["state", "year", "total_voters", "mail", "early_in_person", "all_mail_jurisdictions"], rows)


def _uf_rows(body, snap):
    days_out = (EDAY_2024 - snap).days
    out = []
    for r in csv.DictReader(io.StringIO(body.decode("utf-8-sig", errors="replace"))):
        out.append([snap.isoformat(), days_out, r["state_abbv"].strip()] + [_n(r.get(c)) for c in UF_COLS])
    return out


def build_uf2024():
    rows = _uf_rows(fetch(UF2024), date(2024, 11, 6))  # current file = final; label it day after
    rows = [[*r[:1], -1, *r[2:]] for r in rows]          # days_out -1 marks "final"
    cdx = fetch("https://web.archive.org/cdx/search/cdx?url=" + UF2024.split("//")[1]
                + "&from=20240901&to=20241105&filter=statuscode:200&fl=timestamp&collapse=timestamp:8")
    for ts in cdx.decode().split():
        snap = datetime.strptime(ts[:8], "%Y%m%d").date()
        body = fetch(f"https://web.archive.org/web/{ts}id_/{UF2024}")
        if body and body.startswith(b"state"):
            rows += _uf_rows(body, snap)
    _write("uf2024_snapshots.csv", ["snapshot_date", "days_out", "state"] + UF_COLS, rows)


def build_from_archive(cycle: int):
    """Turn this tracker's own daily archive (data/archive/<cycle>/) into a baseline file,
    uf<cycle>_snapshots.csv, in the same format as the 2024 one. Run it after a cycle ends;
    the next cycle's comparisons can then match every state at the exact same days-out.

    days_out is measured from each state's own last_update date, so a state whose figures
    lagged a day is matched on the date its data actually ran through."""
    eday = ELECTION_DAYS[cycle]
    rows = []
    for folder in sorted((ROOT / "data" / "archive" / str(cycle)).iterdir()):
        f = folder / "US.csv.gz"
        if not f.exists():
            continue
        with gzip.open(f, "rt", encoding="utf-8-sig") as g:
            for r in csv.DictReader(g):
                try:
                    upd = datetime.strptime(r["last_update"].strip(), "%m/%d/%Y").date()
                except ValueError:
                    upd = date.fromisoformat(folder.name)
                days_out = (eday - upd).days
                rows.append([folder.name, days_out if days_out >= 0 else -1, r["state_abbv"].strip()]
                            + [_n(r.get(c)) for c in UF_COLS])
    _write(f"uf{cycle}_snapshots.csv", ["snapshot_date", "days_out", "state"] + UF_COLS, rows)


FEC_2022 = "https://www.fec.gov/documents/5676/federalelections2022.xlsx"
FEC_2024_PRES = "https://www.fec.gov/documents/5645/2024presgeresults.xlsx"


def build_results():
    """Official certified results by state from the FEC.
    2022: U.S. House vote summed statewide (every state has House races) and U.S. Senate where
    there was a race. 2024: President. Margin = (D - R) / all votes, in points."""
    import openpyxl  # only needed for this one-time build

    def sheet(url, name=None):
        wb = openpyxl.load_workbook(io.BytesIO(fetch(url, timeout=300)), read_only=True, data_only=True)
        return list((wb[name] if name else wb.worksheets[0]).iter_rows(values_only=True))

    rows = []
    for race, tab in (("house", "6. Table 5 House by Party"), ("senate", "5. Table 4 Senate by Party")):
        for r in sheet(FEC_2022, tab):
            st = (r[0] or "").strip() if isinstance(r[0], str) else ""
            if st not in STATES:
                continue
            d, rep, o = (r[4] or 0), (r[5] or 0), (r[6] or 0)  # general-election columns
            if d + rep + o:
                rows.append((st, 2022, race, d, rep, o))
    pres = sheet(FEC_2024_PRES)
    h = pres[0]
    i_d, i_r, i_t = h.index("HARRIS"), h.index("TRUMP"), h.index("TOTAL VOTES")
    for r in pres[1:]:
        if r[0] in STATES and r[i_t]:
            d, rep = r[i_d] or 0, r[i_r] or 0
            rows.append((r[0], 2024, "president", d, rep, r[i_t] - d - rep))
    _write("results_state.csv", ["state", "year", "race", "dem", "rep", "other"], rows)


def build_pa():
    rows = []
    for year in (2022, 2024):
        rows += pa_mail_file.aggregate(year)
    _write("pa_pace.csv", ["state", "year", "days_out", "method", "party", "race", "age_group", "n"], rows)


def build_nc():
    rows = []
    for year in (2022, 2024):
        print(f"  NC {year}: downloading voter file...")
        rows += nc_voter_file.aggregate(fetch(nc_voter_file.url(year), timeout=600), year)
    _write("nc_pace.csv", ["state", "year", "days_out", "method", "party", "race", "age_group", "n"], rows)


def load(con):
    """Load the committed reference CSVs into the DB (idempotent)."""
    def rows(name):
        p = REF / name
        if not p.exists():
            return []
        with open(p, encoding="utf-8") as f:
            return [[None if v == "" else v for v in r] for r in list(csv.reader(f))[1:]]

    con.execute("DELETE FROM ref_eavs")
    con.executemany("INSERT INTO ref_eavs VALUES (?,?,?,?,?,?)", rows("eavs_state.csv"))
    con.execute("DELETE FROM ref_uf_snapshots")
    for p in sorted(REF.glob("uf*_snapshots.csv")):
        year = int(p.name[2:6])
        con.executemany("INSERT OR REPLACE INTO ref_uf_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [[year, *r] for r in rows(p.name)])
    con.execute("DELETE FROM pace WHERE year < 2026")
    for p in sorted(REF.glob("*_pace.csv")):  # nc_pace.csv, pa_pace.csv, ...
        con.executemany("INSERT INTO pace VALUES (?,?,?,?,?,?,?,?)", rows(p.name))
    con.execute("DELETE FROM ref_results")
    con.executemany("INSERT INTO ref_results VALUES (?,?,?,?,?,?)", rows("results_state.csv"))
    con.commit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", choices=["all", "eavs", "uf2024", "nc", "pa", "results"])
    ap.add_argument("--from-archive", type=int, metavar="CYCLE",
                    help="build uf<CYCLE>_snapshots.csv from this tracker's own daily archive")
    a = ap.parse_args()
    if a.from_archive:
        build_from_archive(a.from_archive)
    if a.build in ("all", "eavs"):
        build_eavs()
    if a.build in ("all", "uf2024"):
        build_uf2024()
    if a.build in ("all", "nc"):
        build_nc()
    if a.build in ("all", "pa"):
        build_pa()
    if a.build in ("all", "results"):
        build_results()
