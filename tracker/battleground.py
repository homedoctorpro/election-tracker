"""One summary record per battleground state for the dashboard's Battlegrounds tab.

Pulls together what the other comparisons compute: 2026 early vote and party mix, pace against
the same point in 2022/2024 (or the final, where nothing better exists), the mail-ballot party
shift from party_projection, cumulative curves by days before Election Day for each cycle, and
the "county lean": the 2024 presidential margin of the counties the 2026 early vote is coming
from, compared with the counties the whole 2024 vote came from.

Each state carries a `tier` saying how solid its comparison is, so the page can show it:
  same_day   party mix compared at the same days-out (voter files or daily state reports)
  turnout    same-day turnout comparison, but no comparable party data (Ohio)
  final      party mix compared with the final 2024 mail mix only
  county     no party registration: county lean only (or turnout only, when the state has no
             county file)
"""
from .common import GOVERNOR_2026, SENATE_2026
from .compare import _margin, days_out, party_projection, state_comparison
from .reference import county_key

STATES = ["AZ", "GA", "MI", "NC", "NV", "PA", "WI", "IA", "OH", "ME", "KS", "FL", "TX"]


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
            pts.append([d, tot, _margin(dem, rep, tot - dem - rep)])
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


def _county_lean(con, st):
    """2024 presidential D-R margin of the counties this year's early vote comes from (weighted by
    each county's early votes so far), next to the statewide 2024 margin (weighted by each
    county's 2024 turnout). Early voting always leans toward large metro counties, so the gap is
    biased toward D in most states; read it against itself over time and across states."""
    res = {c: (d, r, t) for c, d, r, t in con.execute(
        "SELECT county, dem, rep, total FROM ref_county_results WHERE state=? AND year=2024", (st,))}
    if not res:
        return None
    ev = con.execute("""SELECT geo, value FROM ev_values WHERE state=? AND level='county' AND metric='voted_all'
                        AND snapshot_date=(SELECT MAX(snapshot_date) FROM ev_values)""", (st,)).fetchall()
    num = den = unmatched = 0
    for geo, v in ev:
        r = res.get(county_key(geo))
        if not v:
            continue
        if not r or not r[2]:
            unmatched += v
            continue
        num += v * (r[0] - r[1]) / r[2]
        den += v
    if not den or unmatched > 0.05 * (den + unmatched):
        return None
    d, r, t = (sum(x[i] for x in res.values()) for i in range(3))
    early, base = round(100 * num / den, 1), round(100 * (d - r) / t, 1)
    return {"early_2026": early, "turnout_2024": base, "shift": round(early - base, 1), "counties": len(ev),
            "ballots": den}


def battlegrounds(con, day, hist, race_summary):
    cmp = {r["state"]: r for r in state_comparison(con, day)["states"]}
    proj = {r["state"]: r for r in party_projection(con, day)}
    pace_states = {s for (s,) in con.execute("SELECT DISTINCT state FROM pace WHERE year=2026")}
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
        lean = _county_lean(con, st)
        same = [y for y, s in shifts.items() if s.get("basis") == "same day"]
        tier = ("same_day" if same else "turnout" if exact else "final" if shifts else "county")
        # Headline: the most reliable shift available, with the two margins it compares.
        # Same day: all early votes (mail and in person), since both years are counted through the
        # same day. Vs final: mail ballots only, because in-person voting starts later and the
        # final in-person mix would be compared with only a few days of it. County lean: all
        # early votes.
        head = None
        for y in ("2024", "2022"):
            pc = pace.get(y)
            if y in same and pc and pc["dr_base"] is not None and pc["dr_now"] is not None:
                head = {"value": round(pc["dr_now"] - pc["dr_base"], 1), "from": pc["dr_base"], "to": pc["dr_now"],
                        "year": y, "votes": "all", "basis": f"all early votes vs {y} same day"}
                break
        if not head and "2024" in shifts:
            s = shifts["2024"]
            head = {"value": round(s["mail_2026"] - s["mail_base"], 1), "from": s["mail_base"], "to": s["mail_2026"],
                    "year": "2024", "votes": "mail", "basis": "mail ballots vs 2024 final"}
        if not head and lean:
            head = {"value": lean["shift"], "from": lean["turnout_2024"], "to": lean["early_2026"],
                    "year": "2024", "votes": "all", "basis": "county lean of all early votes vs 2024 turnout"}
        races = []
        for r in race_summary:
            if r["state"] == st and not r["district"] and r["status"] == "ok" and r["office"] in ("Senate", "Governor"):
                races.append({k: r[k] for k in ("office", "candidate_a", "party_a", "a_avg", "candidate_b",
                                                 "party_b", "b_avg", "margin_d_minus_r", "polls_averaged",
                                                 "latest_poll_end")})
        races.sort(key=lambda r: (r["office"] != ("Senate" if st in SENATE_2026 else "Governor")))
        out.append({
            "state": st, "state_name": c["state_name"], "data_through": c["data_through"], "days_out": dout,
            "tier": tier, "exact_pace": exact, "headline": head, "races": races,
            "has_senate": st in SENATE_2026, "has_governor": st in GOVERNOR_2026,
            "ev": {"total": c["ev_2026"], "dr": c["dr_margin_2026"], "mail_dr": c.get("mail_dr_margin_2026")},
            "pct_of_final_2022": c.get("pct_of_final_2022"), "pct_of_final_2024": c.get("pct_of_final_2024"),
            "pace": pace, "shifts": shifts, "county_lean": lean,
            "curves": {str(y): v for y, v in curves.items()},
            "final_dr": {"2024": c.get("dr_margin_2024_final"), "2022": c.get("dr_margin_2022_final")},
        })
    return out
