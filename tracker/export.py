"""Write the latest data to CSVs in data/latest/ for Excel / quick viewing."""
import csv

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
  SELECT poll_id, choice, pct,
         ROW_NUMBER() OVER (PARTITION BY poll_id ORDER BY pct DESC) rn
  FROM poll_answers
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


def tag_matchups(con, window_days=45):
    """VoteHub mixes primary and hypothetical-matchup polls in with general-election polls.
    Infer each race's current matchup as the two choices that appear most often in its
    polls from the last `window_days`, then flag every poll that includes both of them.
    Heuristic: races with no recent polling stay 'unknown'."""
    con.execute("CREATE TABLE IF NOT EXISTS poll_matchup (poll_id TEXT PRIMARY KEY, matchup TEXT)")
    con.execute("DELETE FROM poll_matchup")
    races = con.execute("""SELECT DISTINCT race FROM polls
                           WHERE poll_type IN ('us-senator','governor','us-representative')""").fetchall()
    for (race,) in races:
        recent = con.execute(f"""
            SELECT a.choice, COUNT(*) n FROM polls p JOIN poll_answers a USING (poll_id)
            WHERE p.race=? AND p.end_date >= DATE('now', '-{window_days} days') AND p.end_date <= DATE('now')
            GROUP BY a.choice ORDER BY n DESC, SUM(a.pct) DESC LIMIT 2""", (race,)).fetchall()
        pair = {r[0] for r in recent}
        for (pid,) in con.execute("SELECT poll_id FROM polls WHERE race=?", (race,)).fetchall():
            choices = {r[0] for r in con.execute("SELECT choice FROM poll_answers WHERE poll_id=?", (pid,))}
            tag = "unknown" if len(pair) < 2 else ("current" if pair <= choices else "primary/hypothetical")
            con.execute("INSERT INTO poll_matchup VALUES (?,?)", (pid, tag))
    con.commit()


def race_summary(con, last_n=5) -> int:
    """One row per race: the inferred matchup and a plain average of its last N polls.
    Margin is signed: candidate_a minus candidate_b (a = whoever leads the average)."""
    rows = []
    races = con.execute("""SELECT DISTINCT p.poll_type, p.race, p.state, p.district FROM polls p
                           JOIN poll_matchup m USING (poll_id) WHERE m.matchup='current'""").fetchall()
    for ptype, race, st, dist in races:
        polls = con.execute("""SELECT p.poll_id, p.end_date FROM polls p JOIN poll_matchup m USING (poll_id)
                               WHERE p.race=? AND m.matchup='current' AND p.quality_flag IS NULL
                               ORDER BY p.end_date DESC""",
                            (race,)).fetchall()
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
        rows.append((TYPE_LABEL[ptype], race, st, dist, len(polls), polls[0][1], len(recent),
                     a, round(a_avg, 1), b, round(b_avg, 1), round(a_avg - b_avg, 1)))
    rows.sort(key=lambda r: (r[0], abs(r[11])))
    LATEST.mkdir(parents=True, exist_ok=True)
    with open(LATEST / "race_summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["office", "race", "state", "district", "matchup_polls", "latest_poll_end",
                    f"polls_averaged", "candidate_a", "a_avg", "candidate_b", "b_avg", "margin_a_minus_b"])
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
    nc = compare.nc_pace(con)
    if nc:
        rows = [{"year": y, **v} for y, v in nc["same_day"].items()] +                [{"year": f"{y} final", **v} for y, v in nc["final"].items()]
        out["nc_same_day.csv"] = _write_dicts("nc_same_day.csv", rows)
    out["house_ratings.csv"] = _write(con, "house_ratings.csv", """
        SELECT race, state, district, outlet, rating FROM race_ratings
        WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM race_ratings) ORDER BY race, outlet""")
    return out
