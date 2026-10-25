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
- Ohio Secretary of State absentee dashboard, Nov 2024 general (exact daily pace, statewide and
  by county; the dashboard has no party data before Nov 2024).
- Iowa Secretary of State "Absentee Ballot Statistics" by congressional district for the 2018,
  2022 and 2024 generals (sos.iowa.gov/elections/pdf/<year>/general/AbsenteeCongressional<year>.pdf).
  Each PDF holds every daily report of the cycle; ia_sos_absentee.csv sums the districts to
  statewide ballots received by party per report date, with in-person (Counter / In-Office,
  Satellite, In-Person) split out for 2022 and 2024 (the 2018 file has no receipt method).
  The site blocks scripted downloads, so the CSV was extracted by hand in a browser; party sums
  were checked against each report's Grand Total. Loaded over UF's Iowa rows (see load()).
"""
import argparse
import csv
import gzip
import io
import re
import zipfile
from datetime import date, datetime

from .common import ELECTION_DAYS, ROOT, STATES, fetch
from .sources import nc_voter_file, oh_sos, pa_mail_file, registration

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


COUNTY_2024 = ("https://raw.githubusercontent.com/tonmcg/US_County_Level_Election_Results_08-24/master/"
               "2024_US_County_Level_Presidential_Results.csv")


def county_key(name):
    """County name as matched between results and UF's county files ("St. Clair County" -> "stclair")."""
    s = name.lower().replace(" county", "").replace("saint", "st").replace("st.", "st")
    return re.sub(r"[^a-z]", "", s)


def build_county_results():
    """2024 presidential results by county (tonmcg/US_County_Level_Election_Results_08-24, compiled
    from state and county canvasses). Counties are stored under county_key()."""
    by_name = {v: k for k, v in STATES.items()}
    tot = {}  # AK house districts and DC wards share a key once numbers are stripped: sum them
    for r in csv.DictReader(io.StringIO(fetch(COUNTY_2024).decode("utf-8-sig"))):
        st = by_name.get(r["state_name"])
        if st:
            t = tot.setdefault((st, county_key(r["county_name"])), [r["county_fips"].zfill(5), 0, 0, 0])
            for i, k in enumerate(("votes_dem", "votes_gop", "total_votes"), 1):
                t[i] += int(float(r[k]))
    _write("county_results.csv", ["state", "year", "county", "fips", "dem", "rep", "total"],
           [(st, 2024, c, *v) for (st, c), v in tot.items()])


def build_eavs_county():
    """EAVS pre-Election-Day votes by county for 2022 and 2024: mail + early in person + mail in
    all-mail jurisdictions, with negative "not available" codes skipped (a county with none of
    the three reported is left blank). Jurisdictions are summed to their county by the first five
    digits of the ten-digit FIPS code; states whose codes aren't county-based (Wisconsin's
    municipalities) are left out."""
    rows = []
    for year, url in EAVS.items():
        with zipfile.ZipFile(io.BytesIO(fetch(url, timeout=300))) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            data = list(csv.DictReader(io.TextIOWrapper(z.open(name), encoding="latin-1")))
        by = {}
        for r in data:
            st, f = r["State_Abbr"].strip(), r["FIPSCode"].strip()
            if st not in STATES or len(f) != 10:
                continue
            parts = [_n(r.get(k)) for k in ("F1d", "F1f", "F1g")]
            c = by.setdefault((st, f[:5]), [None, 0])
            if any(p is not None for p in parts):
                c[0] = (c[0] or 0) + sum(p or 0 for p in parts)
            c[1] += _n(r.get("F1a")) or 0
        rows += [(st, year, f, None if e is None else int(e), int(t)) for (st, f), (e, t) in sorted(by.items())]
    _write("eavs_county.csv", ["state", "year", "fips", "early", "total"], rows)


PA_REG_PDFS = {
    2022: "voter-registration-statistics/2022%20Election%20VR%20Statistics.pdf",
    2024: "voter-registration-statistics/2024%20Election%20Nov..pdf",
}
PA_STATS = ("https://www.pa.gov/content/dam/copapwp-pagov/en/dos/resources/voting-and-elections/"
            "voting-and-election-statistics/")


def build_registration():
    """Registration by party at each past general election: NC voter_stats, PA certified PDFs."""
    from pypdf import PdfReader  # only needed for this one-time build
    rows = []
    for year, folder in ((2022, "2022_11_08"), (2024, "2024_11_05")):
        d = folder.replace("_", "")
        rows.append(("NC", year, *registration.nc_snapshot(f"ENRS/{folder}/voter_stats_{d}.zip")))
    for year, path in PA_REG_PDFS.items():
        text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(fetch(PA_STATS + path))).pages)
        # statewide row: "Total <Dem> <Rep> ... <All Parties>"
        nums = [int(x.replace(",", "")) for x in re.search(r"\nTotal ([\d, ]+)", text).group(1).split()]
        dem, rep, total = nums[0], nums[1], nums[-1]
        rows.append(("PA", year, f"{year}-11-0{8 if year == 2022 else 5}", dem, rep, total - dem - rep))
    _write("registration.csv", ["state", "year", "as_of", "dem", "rep", "other"], rows)


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


def build_oh():
    state, county = oh_sos.aggregate(2024)
    _write("oh_pace.csv", ["state", "year", "days_out", "method", "party", "race", "age_group", "n"], state)
    _write("oh_county_pace.csv", ["state", "year", "county", "days_out", "method", "party", "n"], county)


TLC_24G = ("https://data.capitol.texas.gov/dataset/35b16aee-0bb0-4866-b1ec-859f1f044241/resource/"
           "e1cd6332-6a7a-4c78-ad2a-852268f6c7a2/download/2024-general-vtds-election-data.zip")


def build_tx_vtd():
    """2024 presidential vote by Texas VTD (the Legislative Council's approximation of voting
    precincts), from its Comprehensive Election Datasets on data.capitol.texas.gov (CC BY)."""
    z = zipfile.ZipFile(io.BytesIO(fetch(TLC_24G, timeout=600)))
    tot = {}
    with z.open("2024_General_Election_Returns.csv") as f:
        for r in csv.DictReader(io.TextIOWrapper(f, encoding="latin-1")):
            if r["Office"] != "President":
                continue
            k = (r["County"].upper(), r["VTD"])
            t = tot.setdefault(k, [0, 0, 0])
            v = int(r["Votes"] or 0)
            t[0 if r["Party"] == "D" else 1 if r["Party"] == "R" else 2] += v
    _write("tx_vtd_pres24.csv", ["county", "vtd", "dem", "rep", "total"],
           [[c, v, d, rp, d + rp + o] for (c, v), (d, rp, o) in sorted(tot.items())])


IA_EDAY = {2018: date(2018, 11, 6), **ELECTION_DAYS}


def ia_snapshots(csv_rows):
    """Iowa SOS daily absentee reports as ref_uf_snapshots rows. They replace UF's 2024 Iowa rows,
    which count every absentee ballot as mail (in-person absentee included) and so can't be
    compared with 2026's mail-only figures. A report dated after Election Day is the final
    (days_out -1); "none" is No Party + Libertarian + Other, as in UF's files. Mail = all
    received minus in-person; 2018 has no in-person split, so its mail columns are empty."""
    last = {}
    for r in csv_rows:
        last[r[0]] = max(last.get(r[0], ""), r[1])
    out = []
    for year, d, dem, rep, np_, lib, oth, tot, i_d, i_r, i_o in csv_rows:
        days = (IA_EDAY[int(year)] - date.fromisoformat(d)).days
        if days < 0 and d != last[year]:
            continue  # only the last post-election report stands as the final
        year = int(year)
        dem, rep, tot = int(dem), int(rep), int(tot)
        none = int(np_) + int(lib) + int(oth)
        mail = [None] * 5
        if i_d is not None:
            i_d, i_r, i_o = int(i_d), int(i_r), int(i_o)
            inp = i_d + i_r + i_o
            mail = [tot - inp, inp, dem - i_d, rep - i_r, none - i_o]
        out.append([year, d, days if days >= 0 else -1, "IA", tot, dem, rep, none, *mail])
    return out


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
    ia = ia_snapshots(rows("ia_sos_absentee.csv"))
    for year in {r[0] for r in ia}:
        con.execute("DELETE FROM ref_uf_snapshots WHERE state='IA' AND year=?", (year,))
    con.executemany("INSERT INTO ref_uf_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ia)
    # Florida's 2022 final vote-by-mail and early-vote counts by party, from the Division of
    # Elections' archived status report (dos.fl.gov/media/706191/2022-ge-rptstatuscountsarchive-ev-vbm.pdf,
    # STATE TOTAL rows; "none" = No Party Affiliation + Other). UF has no 2022 files.
    con.executemany("INSERT OR REPLACE INTO ref_uf_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows("fl_dos_2022_final.csv"))
    con.execute("DELETE FROM pace WHERE year < 2026")
    for p in sorted(REF.glob("*_pace.csv")):  # nc_pace.csv, pa_pace.csv, oh_pace.csv, ...
        if not p.name.endswith("_county_pace.csv"):
            con.executemany("INSERT INTO pace VALUES (?,?,?,?,?,?,?,?)", rows(p.name))
    con.execute("DELETE FROM pace_county WHERE year < 2026")
    for p in sorted(REF.glob("*_county_pace.csv")):
        con.executemany("INSERT INTO pace_county VALUES (?,?,?,?,?,?,?)", rows(p.name))
    con.execute("DELETE FROM registration WHERE year < 2026")
    con.executemany("INSERT INTO registration VALUES (?,?,?,?,?,?)", rows("registration.csv"))
    con.execute("DELETE FROM ref_county_pres")
    con.executemany("INSERT INTO ref_county_pres VALUES (?,?,?,?,?,?,?)", rows("county_results.csv"))
    con.execute("DELETE FROM ref_county_early")
    con.executemany("INSERT INTO ref_county_early VALUES (?,?,?,?,?)", rows("eavs_county.csv"))
    con.execute("DELETE FROM ref_results")
    con.executemany("INSERT INTO ref_results VALUES (?,?,?,?,?,?)", rows("results_state.csv"))
    # hand-entered from each state's certified canvass (the FEC doesn't cover governors)
    con.executemany("INSERT INTO ref_results VALUES (?,?,?,?,?,?)",
                    [r[:6] for r in rows("governor_2022.csv")])
    con.commit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", choices=["all", "eavs", "uf2024", "nc", "pa", "oh", "results", "registration",
                                        "county_results", "eavs_county", "tx_vtd"])
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
    if a.build in ("all", "oh"):
        build_oh()
    if a.build in ("all", "registration"):
        build_registration()
    if a.build in ("all", "results"):
        build_results()
    if a.build in ("all", "county_results"):
        build_county_results()
    if a.build in ("all", "eavs_county"):
        build_eavs_county()
    if a.build in ("all", "tx_vtd"):
        build_tx_vtd()
