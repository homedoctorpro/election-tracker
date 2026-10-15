"""Compare each state's 2026 early vote with 2022 (last midterm) and 2024 (last presidential).

Two kinds of comparison, kept apart because they answer different questions:
- vs FINAL pre-Election-Day vote (every state, EAVS): how far along 2026 is relative to the
  whole early vote last time. Early in the window this mostly reflects how fast a state mails
  ballots, so read it against the median state rather than on its own.
- SAME number of days before Election Day (exact): NC from voter files for all three cycles,
  and any state where an archived 2024 UF snapshot exists within a day of the state's days-out.

Days-out is measured from the date each source's data runs through, not from today.
"""
from datetime import date, timedelta

from .common import ELECTION_DAYS as EDAY


def days_out(d: str) -> int:
    return (EDAY[2026] - date.fromisoformat(d)).days


def _margin(d, r, o):
    """Dem minus Rep as a share of all party-coded ballots, in points."""
    tot = (d or 0) + (r or 0) + (o or 0)
    return round(100 * ((d or 0) - (r or 0)) / tot, 1) if tot else None


def state_comparison(con, day: str):
    eavs = {(s, y): dict(total=t, pre=(m or 0) + (e or 0) + (a or 0))
            for s, y, t, m, e, a in con.execute("SELECT * FROM ref_eavs")}
    uf24 = {}
    for r in con.execute("""SELECT days_out, state, voted_all, voted_dem, voted_rep, voted_none,
                                   accept_all, accept_dem, accept_rep, accept_none FROM ref_uf_snapshots
                            WHERE year = 2024"""):
        uf24.setdefault(r[1], {})[r[0]] = r[2:]
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
        row["mail_dr_margin_2024_final"] = final and _margin(*final[5:8])
        row["dr_margin_2024_final"] = final and _margin(*final[1:4])
        # exact same-days-out 2024 snapshot, if the archive has one within a day
        near = sorted((k for k in snaps if k >= 0 and abs(k - dout) <= 1), key=lambda k: abs(k - dout))
        if near and snaps[near[0]][0]:
            v = snaps[near[0]]
            row["same_day_2024"] = v[0]
            row["pct_of_same_day_2024"] = round(100 * tot / v[0], 1) if tot else None
            row["dr_margin_2024_same_day"] = _margin(*v[1:4])
        out.append(row)
    return {"states": out}


def nc_pace(con):
    """Cumulative NC early vote by days-out for 2022/2024/2026, with party/method/race/age mixes."""
    years = {}
    for year in (2022, 2024, 2026):
        rows = con.execute("SELECT days_out, method, party, race, age_group, n FROM pace "
                           "WHERE state='NC' AND year=? ORDER BY days_out DESC", (year,)).fetchall()
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
        "chart": {y: [[s["days_out"], s["total"]] for s in ser if s["days_out"] <= 50] for y, ser in years.items()},
    }
