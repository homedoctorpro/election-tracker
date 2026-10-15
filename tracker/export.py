"""Write the latest data to CSVs in data/latest/ for Excel / quick viewing."""
import csv
from datetime import date, timedelta

from . import compare
from .common import LATEST, today_et


def _write(con, name, sql, params=()):
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    rows = cur.fetchall()
    LATEST.mkdir(parents=True, exist_ok=True)
    with open(LATEST / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    return len(rows)


POLLS_SQL = """
WITH ranked AS (
  SELECT a.poll_id, a.choice || COALESCE(' (' || cp.party || ')', '') AS choice, a.pct,
         ROW_NUMBER() OVER (PARTITION BY a.poll_id ORDER BY a.pct DESC) rn
  FROM poll_answers a JOIN polls pp USING (poll_id)
  LEFT JOIN candidate_party cp ON cp.poll_type = pp.poll_type AND cp.state = pp.state
       AND cp.district = COALESCE(pp.district, '') AND cp.choice = a.choice
),
summary AS (
  SELECT poll_id,
         MAX(CASE WHEN rn=1 THEN choice END) leader,
         MAX(CASE WHEN rn=1 THEN pct END)    leader_pct,
         MAX(CASE WHEN rn=2 THEN choice END) runner_up,
         MAX(CASE WHEN rn=2 THEN pct END)    runner_up_pct,
         GROUP_CONCAT(choice || ' ' || pct, '; ') all_answers
  FROM ranked GROUP BY poll_id
)
SELECT p.race, COALESCE(m.matchup,'n/a') AS matchup, p.state, p.district, p.start_date, p.end_date, p.pollster, p.sponsors,
       p.sample_size, p.population, p.internal, p.partisan,
       s.leader, s.leader_pct, s.runner_up, s.runner_up_pct,
       ROUND(s.leader_pct - s.runner_up_pct, 1) AS margin,
       s.all_answers, p.url, p.quality_flag, p.first_seen
FROM polls p JOIN summary s USING (poll_id)
LEFT JOIN poll_matchup m USING (poll_id)
WHERE p.poll_type = ?
ORDER BY p.end_date DESC, p.race
"""


def _write_dicts(name, rows):
    cols = list(dict.fromkeys(k for r in rows for k in r))
    LATEST.mkdir(parents=True, exist_ok=True)
    with open(LATEST / name, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


AVG_WINDOW_DAYS = 60  # only polls that ended this recently are averaged


def tag_matchups(con, window_days=AVG_WINDOW_DAYS):
    """VoteHub mixes primary and hypothetical-matchup polls in with general-election polls.
    Infer each race's current matchup as the two choices that appear most often in its
    polls from the last `window_days`, then flag every poll that includes both of them.
    Heuristic: races with no recent polling stay 'unknown'."""
    con.execute("CREATE TABLE IF NOT EXISTS poll_matchup (poll_id TEXT PRIMARY KEY, matchup TEXT)")
    con.execute("DELETE FROM poll_matchup")
    today = today_et()
    races = con.execute("""SELECT DISTINCT race FROM polls
                           WHERE poll_type IN ('us-senator','governor','us-representative')""").fetchall()
    for (race,) in races:
        recent = con.execute(f"""
            SELECT a.choice, COUNT(*) n FROM polls p JOIN poll_answers a USING (poll_id)
            WHERE p.race=? AND p.end_date >= DATE(?, '-{window_days} days') AND p.end_date <= ?
            GROUP BY a.choice ORDER BY n DESC, SUM(a.pct) DESC LIMIT 2""", (race, today, today)).fetchall()
        pair = {r[0] for r in recent}
        for (pid,) in con.execute("SELECT poll_id FROM polls WHERE race=?", (race,)).fetchall():
            choices = {r[0] for r in con.execute("SELECT choice FROM poll_answers WHERE poll_id=?", (pid,))}
            tag = "unknown" if len(pair) < 2 else ("current" if pair <= choices else "primary/hypothetical")
            con.execute("INSERT INTO poll_matchup VALUES (?,?)", (pid, tag))
    con.commit()


def race_summary(con, last_n=5, window_days=AVG_WINDOW_DAYS) -> int:
    """One row per 2026 race: the inferred matchup and a plain average of up to N polls that
    ended in the last `window_days`. Older polls are never averaged, so a stale poll can't flip
    a thinly polled race; races with nothing recent get status 'no recent polling'.
    Margin is signed: candidate_a minus candidate_b (a = whoever leads the average)."""
    rows = []
    today = date.fromisoformat(today_et())
    cutoff = (today - timedelta(days=window_days)).isoformat()
    party = {(t, st, d, ch): pty for t, st, d, ch, pty, _ in con.execute("SELECT * FROM candidate_party")}
    races = con.execute("""SELECT DISTINCT poll_type, race, state, district FROM polls
                           WHERE poll_type IN ('us-senator','governor','us-representative')
                             AND subject LIKE '2026%'""").fetchall()
    for ptype, race, st, dist in races:
        polls = con.execute("""SELECT p.poll_id, p.end_date FROM polls p JOIN poll_matchup m USING (poll_id)
                               WHERE p.race=? AND m.matchup='current' AND p.quality_flag IS NULL
                                 AND p.end_date >= ? AND p.end_date <= ?
                               ORDER BY p.end_date DESC""",
                            (race, cutoff, today.isoformat())).fetchall()
        if not polls:
            last = con.execute("""SELECT MAX(end_date), COUNT(*) FROM polls WHERE race=? AND subject LIKE '2026%'
                                  AND quality_flag IS NULL AND end_date <= ?""", (race, today.isoformat())).fetchone()
            rows.append((TYPE_LABEL[ptype], race, st, dist, "no recent polling", 0, last[0], 0,
                         "", "", "", "", "", "", "", "", ""))
            continue
        recent = [pid for pid, _ in polls[:last_n]]
        pcts = {}
        for pid in recent:
            for ch, pct in con.execute("SELECT choice, pct FROM poll_answers WHERE poll_id=?", (pid,)):
                pcts.setdefault(ch, []).append(pct)
        # the two candidates present in every recent poll, ranked by average
        both = sorted(((sum(v) / len(v), ch) for ch, v in pcts.items() if len(v) == len(recent)), reverse=True)[:2]
        if len(both) < 2:
            continue
        (a_avg, a), (b_avg, b) = both
        pa, pb = party.get((ptype, st, dist or "", a), ""), party.get((ptype, st, dist or "", b), "")
        margin = round(a_avg - b_avg, 1)
        # signed D-minus-R margin when it's a D vs R race (positive = D ahead)
        dr = margin if (pa, pb) == ("D", "R") else -margin if (pa, pb) == ("R", "D") else ""
        rows.append((TYPE_LABEL[ptype], race, st, dist, "ok", len(polls), polls[0][1], len(recent),
                     a, pa, round(a_avg, 1), b, pb, round(b_avg, 1), margin, pa, dr))
    # races with recent polling first (closest first), then the rest by most recent poll
    def order(r):
        if r[4] == "ok":
            return (r[0], 0, abs(r[14]))
        last = date.fromisoformat(r[6]).toordinal() if r[6] else 0
        return (r[0], 1, -last)  # most recently polled first
    rows.sort(key=order)
    LATEST.mkdir(parents=True, exist_ok=True)
    with open(LATEST / "race_summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["office", "race", "state", "district", "status", "matchup_polls_in_window", "latest_poll_end",
                    "polls_averaged", "candidate_a", "party_a", "a_avg", "candidate_b", "party_b", "b_avg",
                    "margin_a_minus_b", "leader_party", "margin_d_minus_r"])
        w.writerows(rows)
    return len(rows)


TYPE_LABEL = {"us-senator": "Senate", "governor": "Governor", "us-representative": "House"}


def export_all(con) -> dict:
    tag_matchups(con)
    out = {}
    out["early_vote_states.csv"] = _write(
        con, "early_vote_states.csv", "SELECT * FROM v_ev_state_latest ORDER BY total_voted DESC")
    out["early_vote_daily.csv"] = _write(
        con, "early_vote_daily.csv", "SELECT * FROM v_ev_state_daily ORDER BY state, snapshot_date")
    for lvl in ("county", "cd"):
        out[f"early_vote_{lvl}.csv"] = _write(con, f"early_vote_{lvl}.csv", """
            SELECT state, geo,
                   MAX(CASE WHEN metric='request_all'  THEN value END) requested,
                   MAX(CASE WHEN metric='accept_all'   THEN value END) mail_returned,
                   MAX(CASE WHEN metric='inperson_all' THEN value END) in_person,
                   MAX(CASE WHEN metric='voted_all'    THEN value END) total_voted,
                   MAX(CASE WHEN metric='voted_dem'    THEN value END) voted_dem,
                   MAX(CASE WHEN metric='voted_rep'    THEN value END) voted_rep
            FROM ev_values
            WHERE level=? AND snapshot_date=(SELECT MAX(snapshot_date) FROM ev_values)
            GROUP BY state, geo ORDER BY state, geo""", (lvl,))
    for t, name in [("us-senator", "polls_senate.csv"), ("governor", "polls_governor.csv"),
                    ("us-representative", "polls_house.csv"), ("generic-ballot", "polls_generic_ballot.csv")]:
        out[name] = _write(con, name, POLLS_SQL, (t,))
    out["race_summary.csv"] = race_summary(con)
    out["compare_states.csv"] = _write_dicts("compare_states.csv", compare.state_comparison(con, today_et())["states"])
    proj = []
    for r in compare.party_projection(con, today_et()):
        for y in ("2022", "2024"):
            proj.append({"state": r["state"], "baseline_year": y, "mail_ballots_2026": r["mail_ballots_2026"], **r[y]})
    out["party_projection.csv"] = _write_dicts("party_projection.csv", proj)
    nc = compare.nc_pace(con)
    if nc:
        rows = [{"year": y, **v} for y, v in nc["same_day"].items()] +                [{"year": f"{y} final", **v} for y, v in nc["final"].items()]
        out["nc_same_day.csv"] = _write_dicts("nc_same_day.csv", rows)
    out["house_ratings.csv"] = _write(con, "house_ratings.csv", """
        SELECT race, state, district, outlet, rating FROM race_ratings
        WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM race_ratings) ORDER BY race, outlet""")
    return out
