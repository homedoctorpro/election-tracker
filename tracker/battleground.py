"""One summary record per battleground state for the dashboard's Battlegrounds tab.

Pulls together what the other comparisons compute: 2026 early vote and party mix, pace against
the same point in 2022/2024 (or the final, where nothing better exists), the mail-ballot party
shift from party_projection, cumulative curves by days before Election Day for each cycle, and
the "county lean": the 2024 presidential margin of the counties the 2026 early vote is coming
from, compared with where the 2022 or 2024 early vote came from (EAVS, by county). Daily 2026
series for the trend come from this tracker's own archive (data/archive/2026/).

Each state carries a `tier` saying how solid its comparison is, so the page can show it:
  same_day   party mix compared at the same days-out (voter files or daily state reports)
  turnout    same-day turnout comparison, but no comparable party data (Ohio)
  final      party mix compared with the final 2024 mail mix only
  county     no party registration: county lean only (or turnout only, when the state has no
             county file)
"""
import csv
import gzip
import io
import tarfile
from datetime import datetime
from pathlib import Path

from .common import GOVERNOR_2026, SENATE_2026
from .compare import _margin, days_out, party_projection, state_comparison
from .reference import county_key

STATES = ["AZ", "GA", "MI", "NC", "NV", "PA", "WI", "IA", "OH", "ME", "KS", "FL", "TX"]
PRIMARY_PARTY = {"OH"}  # "party" is the last primary ballot taken, not registration


def _pace_curves(con, st):
    """Cumulative ballots and D-R margin by days-out, per year, from a state's voter-file pace."""
    out = {}
    for year in (2022, 2024, 2026):
        rows = con.execute("SELECT days_out, party, SUM(n) FROM pace WHERE state=? AND year=? "
                           "GROUP BY days_out, party ORDER BY days_out DESC", (st, year)).fetchall()
        if not rows:
            continue
        by = {}
        for d, p, n in rows:
            by.setdefault(d, {})[p] = n
        cum, pts = {}, []
        for d in sorted(by, reverse=True):
            for p, n in by[d].items():
                cum[p] = cum.get(p, 0) + n
            tot = sum(cum.values())
            dem, rep = cum.get("dem", 0), cum.get("rep", 0)
            # no party data at all (Texas): no margin, rather than a fake 0
            pts.append([d, tot, _margin(dem, rep, tot - dem - rep) if "dem" in cum or "rep" in cum else None])
        out[year] = [p for p in pts if p[0] <= 60]
    return out


def _snapshot_curves(con, st, hist):
    """Curves from archived UF snapshots (and Iowa SOS reports) for past years, and this tracker's
    own daily history for 2026. Past years without party data get a null margin."""
    out = {}
    for year, d, tot, dem, rep, none in con.execute(
            "SELECT year, days_out, voted_all, voted_dem, voted_rep, voted_none FROM ref_uf_snapshots "
            "WHERE state=? AND days_out >= 0 AND days_out <= 60 ORDER BY year, days_out DESC", (st,)):
        if tot:
            out.setdefault(year, []).append([d, tot, _margin(dem, rep, none) if (dem or rep) else None])
    pts = {}
    for r in hist.get(st, []):
        if r["total_voted"] and r["last_update"]:
            d = days_out(r["last_update"])
            pts[d] = [d, r["total_voted"], _margin(r["voted_dem"], r["voted_rep"], r["voted_other"])
                      if (r["voted_dem"] or r["voted_rep"]) else None]
    if pts:
        out[2026] = [pts[d] for d in sorted(pts, reverse=True)]
    return out


def _at(curve, d, exact):
    """Cumulative total at d days out: the last point on or before it (voter files), or a point
    within a day of it (snapshots)."""
    if exact:
        best = None
        for p in curve:
            if p[0] >= d:
                best = p
        return best
    near = sorted((p for p in curve if abs(p[0] - d) <= 1), key=lambda p: abs(p[0] - d))
    return near[0] if near else None


def _county_ref(con, st):
    """Per-county 2024 presidential margin, keyed by county_key, plus fips."""
    return {c: (f, 100 * (d - r) / t) for c, f, d, r, t in con.execute(
        "SELECT county, fips, dem, rep, total FROM ref_county_pres WHERE state=? AND year=2024", (st,)) if t}


def _lean_of(ref, counts):
    """(2024 presidential margin weighted by `counts` [(county, votes)], votes matched)."""
    num = den = 0
    for c, v in counts:
        r = ref.get(county_key(c))
        if r and v:
            num += v * r[1]
            den += v
    return (num / den if den else None), den


def _pace_county_counts(con, st, year, dout):
    """Cumulative early votes by county through `dout` days out, from daily county data."""
    return con.execute("SELECT county, SUM(n) FROM pace_county WHERE state=? AND year=? AND days_out >= ? "
                       "GROUP BY county", (st, year, dout)).fetchall()


def _county_lean(con, st, ev, base_year, ref=None, same_day=None):
    """The 2024 presidential D-R margin of the counties this year's early votes come from
    (weighted by each county's early votes so far, `ev` = [(county name, votes)]), next to the
    same figure for `base_year`'s early vote by county. Both sides are early votes, so the
    usual pull of early voting toward metro counties mostly cancels out; what's left is where
    this year's early vote is coming from compared with last time.

    The base is `same_day` ([(county, votes)] at the same days out, where a state publishes
    daily county counts: Texas, Ohio 2024) or else the final early vote by county from EAVS,
    which is a full count against a partial 2026 one. Counties with no base count are left out
    of both sides. Where the state has no county EAVS data (Wisconsin), 2024 falls back to 2024
    turnout and 2022 has no lean."""
    ref = ref or _county_ref(con, st)
    if not ref:
        return None
    if same_day:
        base = {}
        for c, n in same_day:
            r = ref.get(county_key(c))
            if r:
                base[r[0]] = base.get(r[0], 0) + n
        basis = "same day"
    else:
        base = {f: e for f, e in con.execute(
            "SELECT fips, early FROM ref_county_early WHERE state=? AND year=?", (st, base_year))}
        basis = "early votes"
    if not any(base.values()):
        if base_year != 2024:
            return None
        base = {f: t for f, t in con.execute(
            "SELECT fips, total FROM ref_county_pres WHERE state=? AND year=2024", (st,))}
        basis = "turnout"
    num = den = skipped = 0
    used = set()
    for geo, v in ev:
        r = ref.get(county_key(geo))
        if not v:
            continue
        if not r or not base.get(r[0]):
            skipped += v
            continue
        num += v * r[1]
        den += v
        used.add(r[0])
    if not den or skipped > 0.1 * (den + skipped):
        return None
    bnum = bden = 0
    for f, m in ref.values():
        if base.get(f):
            bnum += base[f] * m
            bden += base[f]
    early, b = round(num / den, 1), round(bnum / bden, 1)
    return {"early_2026": early, "base": b, "shift": round(early - b, 1), "basis": basis,
            "counties": len(ev), "ballots": den}


ARCHIVE = Path(__file__).resolve().parent.parent / "data" / "archive" / "2026"


def _archive_days():
    """Each archived day's UF state rows and county files: [(folder, {state: row}, {state: [(county, voted)]})]."""
    out = []
    for folder in sorted(ARCHIVE.iterdir()) if ARCHIVE.exists() else []:
        f = folder / "US.csv.gz"
        if not f.exists():
            continue
        with gzip.open(f, "rt", encoding="utf-8-sig") as g:
            states = {r["state_abbv"].strip(): r for r in csv.DictReader(g)}
        counties = {}
        geo = folder / "uf_geo.tar.gz"
        if geo.exists():
            with tarfile.open(geo) as t:
                for m in t.getmembers():
                    if m.name.endswith("_county.csv") and m.name[:2] in STATES:
                        rows = csv.DictReader(io.TextIOWrapper(t.extractfile(m), encoding="utf-8-sig"))
                        counties[m.name[:2]] = [(r["county"], float(r.get("voted_all") or 0)) for r in rows]
        out.append((folder.name, states, counties))
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _daily_2026(con, days, st):
    """2026 by days-out from the archive: all-early D-R, mail D-R, and county lean (the 2024
    margin of the counties early votes came from). The latest archive day wins for a days-out."""
    ref = _county_ref(con, st)
    by = {}
    for folder, states, counties in days:
        r = states.get(st)
        if not r:
            continue
        try:
            d = days_out(datetime.strptime(r["last_update"].strip(), "%m/%d/%Y").date().isoformat())
        except ValueError:
            continue
        v = [_f(r.get(k)) for k in ("voted_dem", "voted_rep", "voted_none", "accept_dem", "accept_rep", "accept_none")]
        tot, mail = sum(x or 0 for x in v[:3]), sum(x or 0 for x in v[3:])
        lean = _county_lean(con, st, counties[st], 2024, ref) if st in counties and ref else None
        by[d] = {"all": _margin(*v[:3]), "mail": _margin(*v[3:]), "county": lean and lean["early_2026"],
                 "n_all": tot, "n_mail": mail, "n_county": lean["ballots"] if lean else 0}
    # skip days with too few ballots to read: under 1,000, or under 5% of the latest count (the
    # first days often hold only a few counties' returns)
    out = {}
    for k in ("all", "mail", "county"):
        last = by[min(by)][f"n_{k}"] if by else 0
        out[k] = [[d, by[d][k]] for d in sorted(by, reverse=True)
                  if by[d][k] is not None and by[d][f"n_{k}"] >= max(1000, .05 * last)]
    return out


def battlegrounds(con, day, hist, race_summary):
    cmp = {r["state"]: r for r in state_comparison(con, day)["states"]}
    proj = {r["state"]: r for r in party_projection(con, day)}
    pace_states = {s for (s,) in con.execute("SELECT DISTINCT state FROM pace WHERE year=2026")}
    days = _archive_days()
    out = []
    for st in STATES:
        c = cmp.get(st)
        if not c:
            continue
        p = proj.get(st, {})
        exact = st in pace_states
        curves = _pace_curves(con, st) if exact else _snapshot_curves(con, st, hist)
        now = curves.get(2026, [])
        dout = now[-1][0] if now else c["days_out"]
        cur = now[-1] if now else None
        pace = {}
        for year in (2018, 2022, 2024):
            prev = _at(curves.get(year, []), dout, exact)
            if cur and prev and prev[1]:
                pace[str(year)] = {"pct": round(100 * cur[1] / prev[1], 1), "base": prev[1],
                                   "dr_base": prev[2], "dr_now": cur[2], "days_out": prev[0]}
        shifts = {y: p[y] for y in ("2022", "2024") if p.get(y, {}).get("shift") is not None}
        ref = _county_ref(con, st)
        # 2026 county counts: the state's own daily county data where it exists (TX, OH), so the
        # base can be taken at the same days out; otherwise UF's latest county file
        pc_years = {y for (y,) in con.execute("SELECT DISTINCT year FROM pace_county WHERE state=?", (st,))}
        if 2026 in pc_years:
            ev = _pace_county_counts(con, st, 2026, dout)
        else:
            ev = con.execute("""SELECT geo, value FROM ev_values WHERE state=? AND level='county' AND metric='voted_all'
                                AND snapshot_date=(SELECT MAX(snapshot_date) FROM ev_values)""", (st,)).fetchall()
        lean = {}
        for y in (2024, 2022):
            sd = _pace_county_counts(con, st, y, dout) if 2026 in pc_years and y in pc_years else None
            v = _county_lean(con, st, ev, y, ref, same_day=sd)
            if v:
                lean[str(y)] = v
        same = [y for y, s in shifts.items() if s.get("basis") == "same day"]
        # Ohio's daily data carries the party ballot each voter last took in a primary. It isn't
        # registration, but it's the same definition in 2024 and 2026, so it's compared same-day.
        primary = st in PRIMARY_PARTY and any(v["dr_base"] is not None for v in pace.values())
        tier = ("same_day" if same or primary else "turnout" if exact else "final" if shifts else "county")
        # Headline per baseline year: the most reliable shift available, with the two margins it
        # compares. Same day: all early votes (mail and in person), since both years are counted
        # through the same day. Vs final (2024 only): mail ballots, because in-person voting starts
        # later and the final in-person mix would be compared with only a few days of it. County
        # lean: all early votes, against that year's early vote by county.
        heads = {}
        for y in ("2024", "2022"):
            pc, head = pace.get(y), None
            if tier == "same_day" and pc and pc["dr_base"] is not None and pc["dr_now"] is not None:
                head = {"value": round(pc["dr_now"] - pc["dr_base"], 1), "from": pc["dr_base"], "to": pc["dr_now"],
                        "year": y, "votes": "primary" if primary else "all", "kind": "same",
                        "basis": f"{'primary-ballot party of ' if primary else ''}all early votes vs {y} same day"}
            elif tier == "final" and shifts.get(y, {}).get("basis") == "final":
                # 2024 for every vs-final state; 2022 only where a 2022 final mail mix is on file (FL)
                s = shifts[y]
                head = {"value": round(s["mail_2026"] - s["mail_base"], 1), "from": s["mail_base"], "to": s["mail_2026"],
                        "year": y, "votes": "mail", "kind": "final", "basis": f"mail ballots vs {y} final"}
            elif (tier != "same_day" or primary) and y in lean:
                # (a primary-party state with no daily data for this year falls back to county lean)
                lv = lean[y]
                head = {"value": lv["shift"], "from": lv["base"], "to": lv["early_2026"], "year": y, "votes": "all",
                        "kind": "county", "same_day": lv["basis"] == "same day",
                        "basis": f"county lean of early votes vs {y} {lv['basis']}"}
            if head:
                heads[y] = head
        head = heads.get("2024")
        # trend: the headline's measure by days out, 2026 against earlier cycles
        kind = head and head["kind"]
        trend = None
        if kind == "same":
            trend = {"measure": "all", "series": {}, "final": {}}
            for y, cv in curves.items():
                top = max((q[1] for q in cv), default=0)
                pts = [[q[0], q[2]] for q in cv if q[2] is not None and q[1] >= 1000 and q[1] >= .02 * top]
                if pts and y >= 2022:
                    trend["series"][str(y)] = pts
                    if y < 2026:
                        trend["final"][str(y)] = pts[-1][1]
        elif kind == "county" and 2026 in pc_years:
            # daily county data: county lean on every day of each cycle, all at matching days out
            trend = {"measure": "county", "series": {}, "final": {}}
            for y in sorted(pc_years):
                pts = []
                for (d,) in con.execute("SELECT DISTINCT days_out FROM pace_county WHERE state=? AND year=? "
                                        "AND days_out <= 30 ORDER BY days_out DESC", (st, y)):
                    m, n = _lean_of(ref, _pace_county_counts(con, st, y, d))
                    if m is not None and n >= 1000:
                        pts.append([d, round(m, 1)])
                if pts:
                    trend["series"][str(y)] = pts
                    if y < 2026:
                        trend["final"][str(y)] = pts[-1][1]
            for y, v in lean.items():
                if y not in trend["series"]:
                    trend["final"][y] = v["base"]
        elif kind in ("final", "county"):
            d26 = _daily_2026(con, days, st)
            trend = {"measure": "mail" if kind == "final" else "county", "series": {}, "final": {}}
            if d26[trend["measure"]]:
                trend["series"]["2026"] = d26[trend["measure"]]
            if kind == "final":
                pts = [[d, _margin(a, b, n)] for d, a, b, n, t in con.execute(
                    "SELECT days_out, accept_dem, accept_rep, accept_none, accept_all FROM ref_uf_snapshots "
                    "WHERE state=? AND year=2024 AND days_out >= 0 ORDER BY days_out DESC", (st,)) if t and t >= 1000]
                if pts:
                    trend["series"]["2024"] = pts
                trend["final"]["2024"] = head["from"]
                if heads.get("2022", {}).get("kind") == "final":
                    trend["final"]["2022"] = heads["2022"]["from"]
            else:
                trend["final"] = {y: v["base"] for y, v in lean.items()}
        races = []
        for r in race_summary:
            if r["state"] == st and not r["district"] and r["status"] == "ok" and r["office"] in ("Senate", "Governor"):
                races.append({k: r[k] for k in ("office", "candidate_a", "party_a", "a_avg", "candidate_b",
                                                 "party_b", "b_avg", "margin_d_minus_r", "polls_averaged",
                                                 "latest_poll_end")})
        races.sort(key=lambda r: (r["office"] != ("Senate" if st in SENATE_2026 else "Governor")))
        out.append({
            "state": st, "state_name": c["state_name"], "data_through": c["data_through"], "days_out": dout,
            "tier": tier, "exact_pace": exact, "headline": head, "headlines": heads, "trend": trend, "races": races,
            "has_senate": st in SENATE_2026, "has_governor": st in GOVERNOR_2026,
            "ev": {"total": c["ev_2026"], "dr": c["dr_margin_2026"], "mail_dr": c.get("mail_dr_margin_2026")},
            "pct_of_final_2022": c.get("pct_of_final_2022"), "pct_of_final_2024": c.get("pct_of_final_2024"),
            "pace": pace, "shifts": shifts, "county_lean": lean,
            "curves": {str(y): v for y, v in curves.items()},
            "final_dr": {"2024": c.get("dr_margin_2024_final"), "2022": c.get("dr_margin_2022_final")},
        })
    return out
