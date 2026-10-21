"""Texas Secretary of State early-voting turnout by county, every day of early voting.

Texas has no party registration and UF Election Lab publishes only a statewide Texas total, so
this is what gives Texas a county lean. The SOS reports, for all 254 counties, cumulative
in-person early voters and mail ballots received as of each early-voting date (aggregate
counts; the per-county voter rosters behind the "Voter Details Report" links are not used).

- 2026 (and later): the SOS's Civix app, goelect.txelections.civixapps.com. Its API returns a
  base64 JSON file per election and date.
- 2024 and 2022: the older earlyvoting.texas-election.com site, one HTML table per date. Built
  once into data/reference/tx_county_pace.csv and tx_pace.csv (python -m tracker.sources.tx_sos).

Both sites sit behind bot checks that turn away plain HTTP clients but let a real browser
through, so requests go through headless Chrome (common.chrome_get).

Rows are written in the shared pace / pace_county format: per-day increments by county and
method (in_person, mail), party "all".
"""
import base64
import csv
import json
import re
import sys
import time
from datetime import date, datetime

from ..common import ELECTION_DAYS, ROOT, chrome_get, save_raw

CIVIX = "https://goelect.txelections.civixapps.com/api-ivis-system/api/v1/getFile"
OLD = "https://earlyvoting.texas-election.com/Elections/getEVDetails.do"
OLD_IDS = {2024: 49664, 2022: 47009}
EDAY = {2022: date(2022, 11, 8), **ELECTION_DAYS}
NAME = "NOVEMBER GENERAL ELECTION"
WINDOW = 19  # days before Election Day to start from (mail-only days before in-person voting)


def _increments(cum, year):
    """{date: {county: (in_person, mail)}} cumulative -> pace_county and pace rows (increments)."""
    county_rows, state = [], {}
    prev = {}
    for d in sorted(cum):
        dout = (EDAY[year] - d).days
        for cty, (ip, mail) in cum[d].items():
            p_ip, p_mail = prev.get(cty, (0, 0))
            for method, n in (("in_person", ip - p_ip), ("mail", mail - p_mail)):
                if n:
                    county_rows.append(("TX", year, cty, dout, method, "all", n))
                    state[(dout, method)] = state.get((dout, method), 0) + n
            prev[cty] = (ip, mail)
    state_rows = [("TX", year, d, m, "all", "all", "all", n) for (d, m), n in sorted(state.items(), reverse=True)]
    return county_rows, state_rows


def _civix(type_, tries=3, **params):
    """One Civix API file, decoded. The API sometimes answers with a 502 page; retry those."""
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{CIVIX}?type={type_}" + (f"&{q}" if q else "")
    for attempt in range(tries):
        text = chrome_get(url, retries=1)  # a bot-check page raises at once: no point retrying it
        try:
            return json.loads(base64.b64decode(json.loads(text)["upload"]).decode("utf-8"))
        except (ValueError, KeyError):
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Texas SOS API kept failing for {url}: {text[:120]}")


def fetch_2026(year=2026, day=None):
    """Cumulative county counts for each early-voting date up to the SOS's last update."""
    info = _civix("EVR_ELECTION")
    el = next(e for e in info["elections"] if e["election_date"] == EDAY[year].strftime("%m/%d/%Y")
              and NAME in e["election_name"])
    updated = datetime.strptime(info["date_updated"], "%m/%d/%Y").date()
    cum, raw = {}, {}
    for ev in el["early_voting_dates"]:
        d = datetime.strptime(ev["date"], "%m/%d/%Y").date()
        if d > updated or (EDAY[year] - d).days > WINDOW:
            continue
        j = _civix("EVR_EARLYVOTING", electionId=el["id"], electionDate=ev["date"])
        if j.get("early_voting_date") != d.isoformat():
            continue
        raw[d.isoformat()] = j
        cum[d] = {c["name"]: (c["total_in_person_votes_for_election"] or 0, c["total_mail_votes_for_election"] or 0)
                  for c in j["turnout_by_county"]}
    if day:
        save_raw("tx_sos", "early_voting.json", json.dumps(raw).encode(), day)
    return _drop_partial(cum)


def _drop_partial(cum):
    """Counties post each day's roster by 11 a.m. the next day, so the newest dates are often
    only partly in (e.g. 628k in-person voters on day one, then +19k for day two). Once
    in-person voting has started, drop trailing dates that added under a quarter of the day
    before."""
    tot = {d: (sum(v[0] for v in c.values()), sum(v[1] for v in c.values())) for d, c in cum.items()}
    ds = sorted(cum)
    while len(ds) >= 3:
        inc = lambda i: sum(tot[ds[i]]) - sum(tot[ds[i - 1]])
        if tot[ds[-2]][0] - tot[ds[-3]][0] > 0 and inc(-1) < 0.25 * inc(-2):
            cum.pop(ds.pop())
        else:
            break
    return cum


def fetch_old(year):
    """Cumulative county counts per date from the older SOS site (2024, 2022)."""
    page = chrome_get(f"https://earlyvoting.texas-election.com/Elections/getElectionEVDates.do?idElection={OLD_IDS[year]}")
    dates = sorted(set(re.findall(r'value="(\d{4}-\d\d-\d\d) 00:00:00\.0"', page)))
    cum = {}
    for ds in dates:
        d = date.fromisoformat(ds)
        if not 0 <= (EDAY[year] - d).days <= WINDOW:
            continue
        h = chrome_get(f"{OLD}?idElection={OLD_IDS[year]}&selectedDate={ds}%2000:00:00.0")
        rows = {}
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.S):
            # county name is a <th>, the counts are <td>s
            cells = [re.sub(r"<[^>]+>|\s+", " ", c).strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)]
            if len(cells) >= 7 and cells[0] and cells[0] != "TOTAL" and cells[3].replace(",", "").isdigit():
                rows[cells[0]] = (int(cells[3].replace(",", "")), int(cells[5].replace(",", "") or 0))
        if rows:
            cum[d] = rows
    return cum


def collect(con, day: str) -> dict:
    cum = fetch_2026(day=day)
    if not cum:
        raise RuntimeError("Texas SOS returned no 2026 early-voting dates")
    county, state = _increments(cum, 2026)
    con.execute("DELETE FROM pace WHERE state='TX' AND year=2026")
    con.executemany("INSERT INTO pace VALUES (?,?,?,?,?,?,?,?)", state)
    con.execute("DELETE FROM pace_county WHERE state='TX' AND year=2026")
    con.executemany("INSERT INTO pace_county VALUES (?,?,?,?,?,?,?)", county)
    con.commit()
    last = max(cum)
    return {"rows": len(county), "ballots": sum(sum(v) for v in cum[last].values()), "through": last.isoformat()}


def build_reference():
    """data/reference/tx_county_pace.csv and tx_pace.csv for 2022 and 2024."""
    county, state = [], []
    for year in (2022, 2024):
        c, s = _increments(fetch_old(year), year)
        county += c
        state += s
        print(f"  TX {year}: {len(c)} county rows")
    ref = ROOT / "data" / "reference"
    for name, header, rows in (("tx_county_pace.csv", ["state", "year", "county", "days_out", "method", "party", "n"], county),
                               ("tx_pace.csv", ["state", "year", "days_out", "method", "party", "race", "age_group", "n"], state)):
        with open(ref / name, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        print(f"  data/reference/{name}: {len(rows)} rows")


if __name__ == "__main__":
    if "--build" in sys.argv:
        build_reference()
