"""Build the public dashboard: site/index.html + site/data.json.

State-level early-vote history is kept as one small CSV per day in data/history/ (committed
to git), so the trend survives even when the SQLite DB is rebuilt from scratch in CI.
"""
import csv
import json
import shutil
from pathlib import Path

from . import battleground, compare, daily, share
from .common import ROOT, now_et, today_et
from .sources import tx_rosters

HISTORY = ROOT / "data" / "history" / "ev_states"
# the fullest row seen for each (state, last_update) across every run, so a later run on the
# same date can't overwrite a source's end-of-day update; NV/FL day-by-day panels read this
BY_UPDATE = ROOT / "data" / "history" / "ev_by_update.csv"
NUMS = ("total_voted", "voted_dem", "voted_rep", "voted_other", "mail_returned", "in_person")
SITE = ROOT / "site"
TEMPLATE = Path(__file__).parent / "dashboard.html"


def _dicts(con, sql, params=()):
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def save_history(con):
    rows = _dicts(con, "SELECT * FROM v_ev_state_latest")
    if not rows:
        return
    HISTORY.mkdir(parents=True, exist_ok=True)
    with open(HISTORY / f"{rows[0]['snapshot_date']}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    merge_by_update(rows)


def merge_by_update(rows):
    """Fold rows into BY_UPDATE, keeping the one with the most ballots per (state, last_update)."""
    keep = {}
    if BY_UPDATE.exists():
        with open(BY_UPDATE, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                keep[(r["state"], r["last_update"])] = r
    for r in rows:
        if not r.get("last_update") or not r.get("total_voted"):
            continue
        k = (r["state"], r["last_update"])
        if k not in keep or float(r["total_voted"]) > float(keep[k]["total_voted"] or 0):
            keep[k] = {**r}
    if not keep:
        return
    fields = list(next(iter(keep.values())))
    with open(BY_UPDATE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(keep[k] for k in sorted(keep))


def load_by_update():
    """BY_UPDATE rows, numbers parsed, by state, oldest update first."""
    out = {}
    if BY_UPDATE.exists():
        with open(BY_UPDATE, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                for k in NUMS:
                    r[k] = float(r[k]) if r.get(k) else None
                out.setdefault(r["state"], []).append(r)
    return out


def load_history():
    hist = {}
    for p in sorted(HISTORY.glob("*.csv")):
        with open(p, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["total_voted"]:
                    hist.setdefault(r["state"], []).append([p.stem, float(r["total_voted"])])
    return hist


def load_history_rows():
    """Every saved daily state row, numbers parsed, by state."""
    hist = {}
    for p in sorted(HISTORY.glob("*.csv")):
        with open(p, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                for k in NUMS:
                    r[k] = float(r[k]) if r[k] else None
                hist.setdefault(r["state"], []).append(r)
    return hist


def build(con):
    save_history(con)
    recent_polls = _dicts(con, """
        SELECT p.poll_type, p.race, p.state, p.district, p.pollster, p.sponsors, p.start_date, p.end_date,
               p.sample_size, p.population, p.internal, p.partisan, p.url, p.quality_flag,
               COALESCE(m.matchup, 'n/a') matchup,
               (SELECT json_group_array(json_array(choice, pct, party)) FROM
                  (SELECT a.choice, a.pct,
                          CASE WHEN p.poll_type = 'generic-ballot'
                               THEN CASE a.choice WHEN 'Dem' THEN 'D' WHEN 'Rep' THEN 'R' END
                               ELSE cp.party END AS party
                   FROM poll_answers a
                   LEFT JOIN candidate_party cp ON cp.poll_type = p.poll_type AND cp.state = p.state
                        AND cp.district = COALESCE(p.district, '') AND cp.choice = a.choice
                   WHERE a.poll_id = p.poll_id ORDER BY a.pct DESC)) answers
        FROM polls p LEFT JOIN poll_matchup m USING (poll_id)
        WHERE p.end_date >= DATE('now', '-120 days')
        ORDER BY p.end_date DESC""")
    for p in recent_polls:
        p["answers"] = json.loads(p["answers"])

    summary = []
    with open(ROOT / "data" / "latest" / "race_summary.csv", encoding="utf-8") as f:
        summary = list(csv.DictReader(f))

    ratings = _dicts(con, """
        SELECT race, state, district, outlet, rating FROM race_ratings
        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM race_ratings)""")

    hist_rows, by_update = load_history_rows(), load_by_update()
    data = {
        "generated": now_et(),
        "ev_states": _dicts(con, "SELECT * FROM v_ev_state_latest ORDER BY total_voted DESC"),
        "ev_history": load_history(),
        "race_summary": summary,
        "polls": recent_polls,
        "ratings": ratings,
        "compare": compare.state_comparison(con, today_et()),
        # same-day panels on the "vs 2022 / 2024" tab, in display order
        "pace": {st: p for st, p in (("NC", compare.nc_pace(con)), ("PA", compare.state_pace(con, "PA")),
                                     ("IA", compare.iowa_pace(con, hist_rows)), ("OH", compare.state_pace(con, "OH")),
                                     ("TX", compare.state_pace(con, "TX"))) if p},
        "oh_counties": compare.county_same_day(con, "OH"),
        "nv_firewall": compare.nv_firewall(by_update, today_et()),
        "fl_lead": compare.fl_lead(by_update, today_et()),
        "tx_precincts": compare.tx_precinct_lean(tx_rosters.load()),
        "projection": compare.party_projection(con, today_et()),
        "battlegrounds": battleground.battlegrounds(con, today_et(), hist_rows, summary),
    }
    data["daily"] = daily.build(con, hist_rows, data["nv_firewall"], today_et(), ROOT / "data" / "history" / "daily_posts")
    SITE.mkdir(exist_ok=True)
    (SITE / "daily.txt").write_text(data["daily"]["post"] + "\n", encoding="utf-8")
    (SITE / "daily-verdicts.txt").write_text(data["daily"]["verdicts"] + "\n", encoding="utf-8")
    (SITE / "data.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    shutil.copy(TEMPLATE, SITE / "index.html")
    try:
        card = share.build(SITE, data["battlegrounds"], data["generated"], data["nv_firewall"])
    except Exception as e:  # the share image must never block the site build
        card = f"share card failed: {e}"
    return {"share": card,"polls": len(recent_polls), "states": len(data["ev_states"]), "history_days": len(list(HISTORY.glob("*.csv")))}
