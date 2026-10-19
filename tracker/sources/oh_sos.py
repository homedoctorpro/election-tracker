"""Ohio Secretary of State absentee and early-voting data.

The SOS Absentee and Early Voting Data Dashboard (data.ohiosos.gov/portal/election-dashboards)
is a public Power BI report. Its model is queried directly through Power BI's public
(publish-to-web) query endpoint, which returns aggregate counts only. ohiosos.gov itself,
including the dashboard's raw-file download, sits behind a Cloudflare bot check, so it isn't
used here.

Counties report to SOS daily. The model holds every general election since Nov 2024 with
counts by received date, ballot type, county, age group and party. Ohio has no party
registration: "party" is the party ballot a voter pulled in a recent primary, and voters
who haven't voted in one count as unaffiliated.
"""
import gzip
import json
import urllib.request
from datetime import date, timedelta

from ..common import ELECTION_DAYS, UA

KEY = "69e1004e-3cb9-41b1-b23c-b3d80f4afdb4"   # the report's public resource key
MODEL = 826990
DATASET = "0e7f070f-deff-4aad-8dba-bb0efd13fc0c"
API = "https://wabi-us-gov-iowa-api.analysis.usgovcloudapi.net/public/reports/querydata?synchronous=true"
TABLE = "absentee v_detailed_grouped"
ELECTIONS = {2024: "2024 NOV GENERAL", 2026: "2026 NOV GENERAL"}

PARTY = {"Democratic": "dem", "Republican": "rep", "Unaffiliated": "una"}
# Ballots returned by mail, drop box or hand delivery are all absentee-by-mail ballots.
METHOD = {"EARLY IN PERSON": "in_person", "MAIL": "mail", "DROP BOX": "mail", "PERSONAL DELIVERY": "mail"}


def _col(c):
    return {"Column": {"Expression": {"SourceRef": {"Source": "a"}}, "Property": c}}


def _in(col, values):
    return {"Condition": {"In": {"Expressions": [_col(col)], "Values": [[{"Literal": {"Value": f"'{v}'"}}] for v in values]}}}


def query(cols, total, election, **filters):
    """Sum `total` grouped by `cols` for one election, with optional column=value filters.
    Returns a list of rows (cols..., sum). The endpoint truncates large results, so callers
    split big queries with filters."""
    sel = [{**_col(c), "Name": f"a.{c}"} for c in cols]
    sel.append({"Aggregation": {"Expression": _col(total), "Function": 0}, "Name": f"Sum(a.{total})"})
    q = {"Version": 2, "From": [{"Name": "a", "Entity": TABLE, "Type": 0}], "Select": sel,
         "Where": [_in("Election_Description", [election])] + [_in(c, [v]) for c, v in filters.items()]}
    body = {"version": "1.0.0", "modelId": MODEL, "cancelQueries": [], "queries": [{
        "QueryId": "", "ApplicationContext": {"DatasetId": DATASET},
        "Query": {"Commands": [{"SemanticQueryDataShapeCommand": {"Query": q, "Binding": {
            "Primary": {"Groupings": [{"Projections": list(range(len(sel)))}]},
            "DataReduction": {"DataVolume": 4, "Primary": {"Window": {"Count": 500000}}}, "Version": 1}}}]}}]}
    req = urllib.request.Request(API, data=json.dumps(body).encode(), headers={
        "X-PowerBI-ResourceKey": KEY, "Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    res = json.loads(raw)["results"][0]["result"]["data"]
    shape = res["dsr"]["DS"][0]
    if "odata.error" in shape or shape.get("IC") is False:
        raise RuntimeError(f"Ohio SOS query failed or was truncated: {json.dumps(shape)[:300]}")
    return _decode(shape, len(sel))


def _decode(shape, n):
    """Unpack Power BI's compressed result rows: `R` is a bitmask of values repeated from the
    previous row, `Ø` a bitmask of nulls, and dictionary-encoded columns index into ValueDicts."""
    dicts = shape.get("ValueDicts", {})
    out, prev, dict_of = [], [None] * n, [None] * n
    for row in shape["PH"][0].get("DM0", []):
        if "S" in row:
            dict_of = [s.get("DN") for s in row["S"]]
        vals, rep, nul = list(row.get("C", [])), row.get("R", 0), row.get("Ø", 0)
        cur = []
        for i in range(n):
            if rep >> i & 1:
                cur.append(prev[i])
            elif nul >> i & 1:
                cur.append(None)
            else:
                v = vals.pop(0)
                cur.append(dicts[dict_of[i]][v] if dict_of[i] and isinstance(v, int) else v)
        out.append(cur)
        prev = cur
    return out


def _day(ms):
    return None if ms is None else date(1970, 1, 1) + timedelta(milliseconds=ms)


def aggregate(year: int, cutoff: date | None = None):
    """Ballots received by (days_out, method, party, age group) statewide, and by (county,
    days_out, method, party). Returns (state_rows, county_rows) in pace / pace_county layout."""
    eday = ELECTION_DAYS[year]
    def keep(ms, cat, n):
        d = _day(ms)
        # a few county records carry mistyped years (e.g. 7726); keep the voting window only
        ok = n and d and eday - timedelta(days=60) <= d <= eday and (not cutoff or d <= cutoff)
        return (eday - d).days if ok else None

    state, county = {}, {}
    for cat in METHOD:
        for ms, party, age, n in query(["Data_Date", "Voter_Party_Bucketed", "Age_Group"],
                                       "BALLOTS_RECEIVED_INCL_EIP", ELECTIONS[year], Category=cat):
            if (dout := keep(ms, cat, n)) is not None:
                k = (dout, METHOD[cat], PARTY.get(party, "other"), age or "unknown")
                state[k] = state.get(k, 0) + int(n)
        for ms, party, cty, n in query(["Data_Date", "Voter_Party_Bucketed", "County_Name"],
                                       "BALLOTS_RECEIVED_INCL_EIP", ELECTIONS[year], Category=cat):
            if (dout := keep(ms, cat, n)) is not None:
                k = ((cty or "UNKNOWN").title(), dout, METHOD[cat], PARTY.get(party, "other"))
                county[k] = county.get(k, 0) + int(n)
    return ([("OH", year, d, m, p, "unknown", a, n) for (d, m, p, a), n in state.items()],
            [("OH", year, c, d, m, p, n) for (c, d, m, p), n in county.items()])


def _last_full_day(rows):
    """Counties report a day late, so the newest dates in the model are only partly in. Return
    the most recent days_out whose statewide count is at least a quarter of the median of the
    five days before it."""
    by = {}
    for r in rows:
        by[r[2]] = by.get(r[2], 0) + r[-1]
    days = sorted(by, reverse=True)   # oldest first
    for i in range(len(days) - 1, 4, -1):
        prior = sorted(by[d] for d in days[i - 5:i])
        if by[days[i]] >= 0.25 * prior[2]:
            return days[i]
    return days[-1] if days else None


def collect(con, day: str) -> dict:
    state, county = aggregate(2026, cutoff=date.fromisoformat(day) - timedelta(days=1))
    last = _last_full_day(state)
    if last is None:
        raise RuntimeError("Ohio SOS returned no 2026 ballots")
    state = [r for r in state if r[2] >= last]
    county = [r for r in county if r[3] >= last]
    con.execute("DELETE FROM pace WHERE state='OH' AND year=2026")
    con.executemany("INSERT INTO pace VALUES (?,?,?,?,?,?,?,?)", state)
    con.execute("DELETE FROM pace_county WHERE state='OH' AND year=2026")
    con.executemany("INSERT INTO pace_county VALUES (?,?,?,?,?,?,?)", county)
    con.commit()
    return {"rows": len(state), "ballots": sum(r[-1] for r in state),
            "through": (ELECTION_DAYS[2026] - timedelta(days=last)).isoformat()}
