"""Compare each state's 2026 early vote with 2022 (last midterm) and 2024 (last presidential).

Two kinds of comparison, kept apart because they answer different questions:
- vs FINAL pre-Election-Day vote (every state, EAVS): how far along 2026 is relative to the
  whole early vote last time. Early in the window this mostly reflects how fast a state mails
  ballots, so read it against the median state rather than on its own.
- SAME number of days before Election Day (exact): NC and PA from voter files for all three
  cycles, OH from the Secretary of State's daily county reports for 2024 and 2026, and any
  state where an archived 2024 UF snapshot exists within a day of the state's days-out.

Days-out is measured from the date each source's data runs through, not from today.
"""
from datetime import date, timedelta

from .common import ELECTION_DAYS as EDAY, GOVERNOR_2026, SENATE_2026


def days_out(d: str) -> int:
    return (EDAY[2026] - date.fromisoformat(d)).days


def _margin(d, r, o):
    """Dem minus Rep as a share of all party-coded ballots, in points."""
    tot = (d or 0) + (r or 0) + (o or 0)
    return round(100 * ((d or 0) - (r or 0)) / tot, 1) if tot else None


def state_comparison(con, day: str):
    eavs = {(s, y): dict(total=t, pre=(m or 0) + (e or 0) + (a or 0))
            for s, y, t, m, e, a in con.execute("SELECT * FROM ref_eavs")}
    uf = {}  # year -> state -> days_out -> counts
    for r in con.execute("""SELECT year, days_out, state, voted_all, voted_dem, voted_rep, voted_none,
                                   accept_all, accept_dem, accept_rep, accept_none FROM ref_uf_snapshots
                            ORDER BY snapshot_date"""):
        uf.setdefault(r[0], {}).setdefault(r[2], {})[r[1]] = r[3:]
    uf24 = uf.get(2024, {})
    mail26 = {}
    for st, m, v in con.execute("""SELECT state, metric, value FROM ev_values
            WHERE level='state' AND metric IN ('accept_dem','accept_rep','accept_none')
              AND snapshot_date=(SELECT MAX(snapshot_date) FROM ev_values)"""):
        mail26.setdefault(st, {})[m] = v

    out = []
    for st, name, last_update, tot, d, rp, o in con.execute(
            """SELECT state, state_name, last_update, total_voted, voted_dem, voted_rep, voted_other
               FROM v_ev_state_latest"""):
        dout = days_out(last_update or day)
        e22, e24 = eavs.get((st, 2022)), eavs.get((st, 2024))
        row = {"state": st, "state_name": name, "data_through": last_update, "days_out": dout,
               "ev_2026": tot,
               "final_pre_2022": e22 and e22["pre"], "final_pre_2024": e24 and e24["pre"],
               "early_share_2022": e22 and e22["total"] and round(100 * e22["pre"] / e22["total"], 1),
               "early_share_2024": e24 and e24["total"] and round(100 * e24["pre"] / e24["total"], 1),
               "pct_of_final_2022": round(100 * tot / e22["pre"], 1) if tot and e22 and e22["pre"] else None,
               "pct_of_final_2024": round(100 * tot / e24["pre"], 1) if tot and e24 and e24["pre"] else None,
               "dr_margin_2026": _margin(d, rp, o)}

        snaps = uf24.get(st, {})
        final = snaps.get(-1)
        m26 = mail26.get(st, {})
        row["mail_dr_margin_2026"] = _margin(m26.get("accept_dem"), m26.get("accept_rep"), m26.get("accept_none"))
        row["mail_party_ballots_2026"] = sum(m26.get(k) or 0 for k in ("accept_dem", "accept_rep", "accept_none")) or None
        row["mail_dr_margin_2024_final"] = final and _margin(*final[5:8])
        row["dr_margin_2024_final"] = final and _margin(*final[1:4])
        # exact same-days-out 2024 snapshot, if the archive has one within a day
        near = sorted((k for k in snaps if k >= 0 and abs(k - dout) <= 1), key=lambda k: abs(k - dout))
        if near and snaps[near[0]][0]:
            v = snaps[near[0]]
            row["same_day_2024"] = v[0]
            row["pct_of_same_day_2024"] = round(100 * tot / v[0], 1) if tot else None
            row["dr_margin_2024_same_day"] = _margin(*v[1:4])
        # mail mix at the same days-out in each earlier cycle with a daily series (Iowa SOS),
        # and the 2022 final where one exists; party_projection uses these
        for year, by_state in uf.items():
            s = by_state.get(st, {})
            if year == 2022 and s.get(-1):
                row["dr_margin_2022_final"] = _margin(*s[-1][1:4])
                row["mail_dr_margin_2022_final"] = _margin(*s[-1][5:8])
            near = sorted((k for k in s if k >= 0 and abs(k - dout) <= 1), key=lambda k: abs(k - dout))
            if near and s[near[0]][4]:
                row[f"mail_dr_margin_{year}_same_day"] = _margin(*s[near[0]][5:8])
                row[f"mail_same_day_{year}_days_out"] = near[0]
        out.append(row)
    return {"states": out}


def nc_pace(con):
    return state_pace(con, "NC")


def state_pace(con, st):
    """Cumulative early vote by days-out for each cycle a state has (NC: 2022/2024/2026, OH:
    2024/2026), with party/method/race/age mixes."""
    years = {}
    for year in (2022, 2024, 2026):
        rows = con.execute("SELECT days_out, method, party, race, age_group, n FROM pace "
                           "WHERE state=? AND year=? ORDER BY days_out DESC", (st, year)).fetchall()
        if not rows:
            continue
        by_day = {}
        for d, m, p, r, a, n in rows:
            by_day.setdefault(d, []).append((m, p, r, a, n))
        series, cum = [], {"total": 0}
        for d in sorted(by_day, reverse=True):
            for m, p, r, a, n in by_day[d]:
                for k in ("total", f"method:{m}", f"party:{p}", f"race:{r}", f"age:{a}"):
                    cum[k] = cum.get(k, 0) + n
            series.append({"days_out": d, **cum})
        years[year] = series
    if 2026 not in years:
        return None

    # 2026 data runs through its most recent return date; compare all years at that point.
    as_of = years[2026][-1]["days_out"]

    def at(series, d):
        best = {"days_out": d, "total": 0}
        for s in series:
            if s["days_out"] >= d:
                best = s
        return best

    return {
        "as_of_days_out": as_of,
        "as_of_date": (EDAY[2026] - timedelta(days=as_of)).isoformat(),
        "same_day": {y: at(s, as_of) for y, s in years.items()},
        "final": {y: s[-1] for y, s in years.items() if y != 2026},
        # D-R share of ballots so far; skip the first 1,000 ballots, which swing wildly
        "chart": {y: [[s["days_out"], round(100 * (s.get("party:dem", 0) - s.get("party:rep", 0)) / s["total"], 2)]
                      for s in ser if s["days_out"] <= 50 and s["total"] >= 1000] for y, ser in years.items()},
    }


def county_same_day(con, st, base_year=2024):
    """Each county's 2026 early vote and party mix against the same days-out in `base_year`."""
    last = con.execute("SELECT MIN(days_out) FROM pace_county WHERE state=? AND year=2026", (st,)).fetchone()[0]
    if last is None:
        return None
    by = {}
    for year in (base_year, 2026):
        for cty, p, n in con.execute("SELECT county, party, SUM(n) FROM pace_county WHERE state=? AND year=? "
                                     "AND days_out >= ? GROUP BY county, party", (st, year, last)):
            by.setdefault(cty, {}).setdefault(year, {})[p] = n
    out = []
    for cty, ys in sorted(by.items()):
        row = {"county": cty}
        for year in (base_year, 2026):
            v = ys.get(year, {})
            row[f"ballots_{year}"] = sum(v.values())
            row[f"dr_margin_{year}"] = _margin(v.get("dem"), v.get("rep"), sum(v.values()) - v.get("dem", 0) - v.get("rep", 0))
        b = row[f"ballots_{base_year}"]
        row["pct_of_same_day"] = round(100 * row["ballots_2026"] / b, 1) if b else None
        m0, m1 = row[f"dr_margin_{base_year}"], row["dr_margin_2026"]
        row["shift"] = round(m1 - m0, 1) if m0 is not None and m1 is not None else None
        out.append(row)
    return {"as_of_days_out": last, "as_of_date": (EDAY[2026] - timedelta(days=last)).isoformat(),
            "base_year": base_year, "counties": out}


def _result_2022(res, st):
    """2022 statewide margin for the same office as the state's top 2026 race: Senate if there's
    a 2026 Senate race, otherwise Governor. Falls back to the summed House vote when that office
    wasn't on the 2022 ballot or one party didn't run (some House totals are skewed by
    uncontested seats, so a statewide race is preferred)."""
    race = "senate" if st in SENATE_2026 else "governor" if st in GOVERNOR_2026 else None
    s = race and res.get((st, 2022, race))
    tot = s and sum(s)
    if s and s[0] > 0.1 * tot and s[1] > 0.1 * tot:
        return _margin(*s), race.title()
    h = res.get((st, 2022, "house"))
    return (_margin(*h), "House vote") if h else (None, None)


def _pace_counts(con, st, year, dout):
    """Mail ballots by party (dem, rep, other) from a state's voter file, through `dout` days out."""
    p = dict(con.execute("SELECT party, SUM(n) FROM pace WHERE state=? AND year=? AND method='mail' "
                         "AND days_out >= ? GROUP BY party", (st, year, dout)).fetchall())
    d, r = p.get("dem", 0), p.get("rep", 0)
    return d, r, sum(p.values()) - d - r


def _reg_shares(con, st, year):
    row = con.execute("SELECT as_of, dem, rep, other FROM registration WHERE state=? AND year=?", (st, year)).fetchone()
    if not row or not sum(row[1:]):
        return None, None
    t = sum(row[1:])
    return row[0], (row[1] / t, row[2] / t, row[3] / t)


def _reweighted_margin(counts, reg_now, reg_base):
    """Mail D-R margin if each party's ballots were scaled to the baseline year's registration
    shares: holds each party's propensity to vote by mail fixed and removes registration drift."""
    w = [c * b / n for c, n, b in zip(counts, reg_now, reg_base)]
    return _margin(*w)


def party_projection(con, day: str):
    """For each party-registration state and baseline year (2022, 2024): the actual statewide
    result that year, the change in mail-ballot party mix since then, and the result you'd get
    if that change carried over to the whole electorate.

    Shift = 2026 mail D-R margin minus the baseline year's mail D-R margin, both in party
    registration terms. Where a state publishes voter-level files (NC, PA) or daily reports
    (Iowa SOS, 2022 and 2024) both sides are taken at the same number of days before Election
    Day, as are states with an archived 2024 UF snapshot within a day; elsewhere the baseline
    is the final 2024 mail mix. This is arithmetic, not a forecast: registration isn't vote choice, and mail
    voters aren't the whole electorate."""
    res = {(s, y, r): (d, rp, o) for s, y, r, d, rp, o in con.execute("SELECT * FROM ref_results")}
    cmp = {r["state"]: r for r in state_comparison(con, day)["states"]}
    voter_file = {s for (s,) in con.execute("SELECT DISTINCT state FROM pace WHERE year=2026")}
    out = []
    for st, r in sorted(cmp.items()):
        if r.get("mail_dr_margin_2026") is None:
            continue
        row = {"state": st, "state_name": r["state_name"], "mail_ballots_2026": r.get("mail_party_ballots_2026"),
               "pct_of_final_2022": r.get("pct_of_final_2022")}
        for year in (2022, 2024):
            if year == 2022:
                result, race = _result_2022(res, st)
            else:
                p = res.get((st, 2024, "president"))
                result, race = (_margin(*p), "President") if p else (None, None)
            base = None
            if st in voter_file:
                last = con.execute("SELECT MIN(days_out) FROM pace WHERE state=? AND year=2026", (st,)).fetchone()[0]
                c26, cb = _pace_counts(con, st, 2026, last), _pace_counts(con, st, year, last)
                m26, mb = _margin(*c26), _margin(*cb)
                if mb is not None and m26 is not None:
                    base = {"mail_2026": m26, "mail_base": mb, "basis": "same day", "days_out": last, "n_2026": sum(c26)}
                    as_now, reg_now = _reg_shares(con, st, 2026)
                    as_base, reg_base = _reg_shares(con, st, year)
                    if reg_now and reg_base:
                        adj = _reweighted_margin(c26, reg_now, reg_base)
                        base.update(mail_2026_reg_adj=adj, reg_as_of=as_now, reg_base_as_of=as_base,
                                    reg_dr_2026=round(100 * (reg_now[0] - reg_now[1]), 1),
                                    reg_dr_base=round(100 * (reg_base[0] - reg_base[1]), 1))
                    # how far off a comparison against the final 2024 mix would have been
                    if year == 2024 and r.get("mail_dr_margin_2024_final") is not None:
                        base["shift_vs_final"] = round(r["mail_dr_margin_2026"] - r["mail_dr_margin_2024_final"], 1)
            elif r.get(f"mail_dr_margin_{year}_same_day") is not None:
                base = {"mail_2026": r["mail_dr_margin_2026"], "mail_base": r[f"mail_dr_margin_{year}_same_day"],
                        "basis": "same day", "days_out": r[f"mail_same_day_{year}_days_out"],
                        "n_2026": r.get("mail_party_ballots_2026")}
                if r.get(f"mail_dr_margin_{year}_final") is not None:
                    base["shift_vs_final"] = round(r["mail_dr_margin_2026"] - r[f"mail_dr_margin_{year}_final"], 1)
            elif r.get(f"mail_dr_margin_{year}_final") is not None:
                base = {"mail_2026": r["mail_dr_margin_2026"], "mail_base": r[f"mail_dr_margin_{year}_final"],
                        "basis": "final", "n_2026": r.get("mail_party_ballots_2026")}
            y = {"result": result, "race": race}
            if base:
                raw = round(base["mail_2026"] - base["mail_base"], 1)
                # where registration data exists, the projection uses the registration-adjusted shift
                adj = base.get("mail_2026_reg_adj")
                shift = round(adj - base["mail_base"], 1) if adj is not None else raw
                y.update(base, shift=shift, shift_raw=raw, reg_adjusted=adj is not None,
                         projected=None if result is None else round(result + shift, 1),
                         projected_raw=None if result is None else round(result + raw, 1))
            row[str(year)] = y
        out.append(row)
    return out
