"""The daily post: for each state with a same-day 2024 baseline, how 2026 compares with 2024
overall (everything so far) and on the latest day alone, written as a short post for X.

Only states where 2024 is known day by day are scored, so both numbers compare like with
like: NC, PA and OH (voter files / SOS daily counts), IA (SOS daily reports), TX (county lean
from the SOS daily county turnout) and NV (the statewide ballot lead by day of early voting).
States compared only against 2024's final count are left out; early mail runs more
Democratic than the final count, so those would read too blue.

- Overall: 2026's D-R share of ballots so far, minus 2024's at the same days out.
- Latest day: the D-R share of just the ballots added on 2026's latest day, minus the same
  for 2024's ballots added between the same days out. A report that adds almost nothing
  (under 1% of the count) is folded into the step before it.
Nevada is in ballots, not points: its lead now against 2024's lead on the same day of early
voting, and the day's change in the lead against 2024's change that day.
"""
import json
from datetime import date, timedelta
from pathlib import Path

from . import compare
from .battleground import _county_ref, _lean_of, _pace_county_counts
from .common import ELECTION_DAYS as EDAY

URL = "homedoctorpro.github.io/election-tracker"
NO_NEW = "no new day"  # in place of a latest-day number the previous day's post already had
NAMES = {"NC": "North Carolina", "PA": "Pennsylvania", "IA": "Iowa", "OH": "Ohio", "TX": "Texas", "NV": "Nevada"}


def _dr(p):
    return 100 * (p.get("party:dem", 0) - p.get("party:rep", 0)) / p["total"] if p and p["total"] else None


def _at(series, d):
    """Cumulative point at d days out: the last point on or before it."""
    best = None
    for s in series:
        if s["days_out"] >= d:
            best = s
    return best


def _step(series):
    """The latest (earlier, later) pair of points in a cumulative series, folding a final report
    that added under 1% of the count into the step before it."""
    pts = list(series)
    while len(pts) >= 3 and pts[-1]["total"] - pts[-2]["total"] < .01 * pts[-1]["total"]:
        pts = pts[:-1]
    return (pts[-2], pts[-1]) if len(pts) >= 2 else None


def _day_dr(a, b):
    """D-R share of the ballots added between cumulative points a and b."""
    n = b["total"] - a["total"]
    if n <= 0:
        return None
    return 100 * ((b.get("party:dem", 0) - a.get("party:dem", 0)) - (b.get("party:rep", 0) - a.get("party:rep", 0))) / n


def _party_state(st, years):
    s26, s24 = years.get(2026), years.get(2024)
    if not s26 or not s24 or "party:dem" not in s26[-1]:
        return None
    step = _step(s26)
    now = step[1] if step else s26[-1]
    d = now["days_out"]
    row = {"state": st, "days_out": d, "overall": _dr(now) - _dr(_at(s24, d)), "now": _dr(now), "then": _dr(_at(s24, d))}
    if step:
        a24, b24 = _at(s24, step[0]["days_out"]), _at(s24, d)
        day26, day24 = _day_dr(*step), (_day_dr(a24, b24) if a24 and b24 else None)
        if day26 is not None and day24 is not None:
            row.update(day=day26 - day24, day_now=day26, day_then=day24, day_from=step[0]["days_out"])
    return row


def _texas(con):
    """County lean (2024 presidential margin of the counties votes come from), overall and for
    the latest day's votes alone, from the SOS daily county turnout."""
    ref = _county_ref(con, "TX")
    days = {y: [d for (d,) in con.execute("SELECT DISTINCT days_out FROM pace_county WHERE state='TX' AND year=? "
                                          "ORDER BY days_out DESC", (y,))] for y in (2024, 2026)}
    if not ref or len(days[2026]) < 2 or not days[2024]:
        return None
    d, prev = days[2026][-1], days[2026][-2]
    cnt = lambda y, dd: dict(_pace_county_counts(con, "TX", y, dd))
    lean = lambda c: _lean_of(ref, list(c.items()))[0]
    diff = lambda a, b: {k: v - a.get(k, 0) for k, v in b.items()}
    now26, prev26, now24, prev24 = cnt(2026, d), cnt(2026, prev), cnt(2024, d), cnt(2024, prev)
    if not now24:
        return None
    day26, day24 = lean(diff(prev26, now26)), lean(diff(prev24, now24))
    row = {"state": "TX", "days_out": d, "overall": lean(now26) - lean(now24), "now": lean(now26), "then": lean(now24),
           "lean": True}
    if day26 is not None and day24 is not None:
        row.update(day=day26 - day24, day_now=day26, day_then=day24, day_from=prev)
    return row


def _nevada(nv):
    if not nv or len(nv["days"].get(2026, [])) < 2:
        return None
    a = nv["days"][2026]
    if nv.get("partial") == a[-1][0]:
        a = a[:-1]  # today's day is still being counted; post the last finished one
    if len(a) < 2:
        return None
    b = dict(nv["days"].get(2024, []))
    day, lead = a[-1]
    if day not in b:
        return None
    row = {"state": "NV", "ev_day": day, "overall": lead - b[day], "now": lead, "then": b[day], "ballots": True}
    pday, plead = a[-2]
    if pday == day - 1 and pday in b:
        row.update(day=(lead - plead) - (b[day] - b[pday]), day_now=lead - plead, day_then=b[day] - b[pday])
    return row


def _fmt(v, ballots=False):
    if v is None:
        return "–"
    if ballots:
        a = abs(v)
        return ("D+" if v >= 0 else "R+") + (f"{a / 1e3:.1f}K" if a >= 1e3 else f"{a:.0f}")
    return ("D+" if v >= 0 else "R+") + f"{abs(v):.1f}"


def _dot(v, ballots=False):
    """Blue or red for which party the comparison with 2024 favors; white when it's a wash
    (under 1 point, or under 500 Nevada ballots)."""
    if v is None:
        return ""
    if abs(v) < (500 if ballots else 1):
        return "⚪"
    return "🔵" if v > 0 else "🔴"


def build(con, hist, nv_firewall, day=None, history=None):
    """{"rows": [...], "post": text, "verdicts": text}. Rows hold each state's overall and
    latest-day comparison. With `history` (a folder), each day's latest-day dates are saved
    there, and a state whose latest day is no newer than in the last earlier day's post is
    marked stale: its latest-day slot reads "no new day" instead of repeating old numbers."""
    rows = [r for r in (
        _party_state("NC", compare.pace_years(con, "NC")),
        _nevada(nv_firewall),
        _party_state("PA", compare.pace_years(con, "PA")),
        _party_state("IA", compare.iowa_years(con, hist)),
        _party_state("OH", compare.pace_years(con, "OH")),
        _texas(con),
    ) if r]
    for r in rows:
        r["name"] = NAMES[r["state"]]
        if "days_out" in r:
            r["date"] = (EDAY[2026] - timedelta(days=r["days_out"])).isoformat()
        else:
            r["date"] = (EDAY[2026] - timedelta(days=compare.NV_EV_DAYS_OUT - r["ev_day"] + 1)).isoformat()
    if history and day:
        _mark_stale(rows, Path(history), day)
    return {"rows": rows, "post": post(rows, day), "verdicts": verdicts(rows, day)}


def _mark_stale(rows, folder, day):
    """Compare each state's latest-day date with the most recent earlier day's post, then save
    today's dates (one small JSON per day, rewritten by each run that day)."""
    prev = sorted(p for p in folder.glob("*.json") if p.stem < day)
    seen = json.loads(prev[-1].read_text(encoding="utf-8")) if prev else {}
    for r in rows:
        r["stale"] = r["state"] in seen and r["date"] <= seen[r["state"]]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{day}.json").write_text(json.dumps({r["state"]: r["date"] for r in rows}, indent=0) + "\n",
                                        encoding="utf-8")


# Cut-offs for the verdict words: (good, very good). Points of D-R share, or Nevada ballots.
CUTS = {"points": (1, 5), "nv_overall": (2500, 10000), "nv_day": (500, 2000)}


def verdict(v, cuts):
    """'very good D' / 'good D' / 'neutral' / 'good R' / 'very good R' for a shift vs 2024."""
    if v is None:
        return "n/a"
    good, very = cuts
    if abs(v) < good:
        return "neutral"
    return ("very good " if abs(v) >= very else "good ") + ("D" if v > 0 else "R")


def verdicts(rows, day=None):
    """The second post: each state's overall and latest-day comparison with 2024 in words."""
    d = date.fromisoformat(day) if day else date.today()
    lines = [f"Early vote vs 2024 same day, {d.strftime('%b')} {d.day}", "(overall | latest day)"]
    for r in rows:
        nv = r.get("ballots", False)
        o = verdict(r["overall"], CUTS["nv_overall"] if nv else CUTS["points"])
        dd = NO_NEW if r.get("stale") else verdict(r.get("day"), CUTS["nv_day"] if nv else CUTS["points"])
        lines.append(f"{r['state']}: {o} | {dd}")
    foot = ["Neutral: under 1 pt. Very good: 5+ pts.", "Party reg., not votes."]
    text = "\n".join(lines + foot + [URL])
    while _xlen(text) > 280 and foot:
        foot.pop(0)
        text = "\n".join(lines + foot + [URL])
    return text


def post(rows, day=None):
    d = date.fromisoformat(day) if day else date.today()
    # a stale state's latest day was already posted, so it's left out of the latest-day tally
    fresh = lambda r, k: k == "overall" or not r.get("stale")
    better = lambda k: sum(1 for r in rows if fresh(r, k) and r.get(k) is not None and r[k] > 0)
    has = lambda k: sum(1 for r in rows if fresh(r, k) and r.get(k) is not None)
    lines = [f"Early vote vs 2024 same day, {d.strftime('%b')} {d.day}", "(overall | latest day)"]
    for r in rows:
        b = r.get("ballots", False)
        day_dot = _day_dot(r.get("day")) if b else _dot(r.get("day"))
        latest = NO_NEW if r.get("stale") else f"{day_dot}{_fmt(r.get('day'), b)}"
        lines.append(f"{r['state']} {_dot(r['overall'], b)}{_fmt(r['overall'], b)} | {latest}")
    latest = f"{better('day')}/{has('day')} latest" if has("day") else "no new days yet"
    lines.append(f"Ds better: {better('overall')}/{has('overall')} overall, {latest}")
    foot = [n for n in ("Party reg., not votes.",
                        "NV in ballots, TX county lean." if any(r["state"] in ("NV", "TX") for r in rows) else "") if n]
    text = "\n".join(lines + foot + [URL])
    # X counts a link as 23 characters and these emoji as 2; drop the notes if it runs long
    while _xlen(text) > 280 and foot:
        foot.pop()
        text = "\n".join(lines + foot + [URL])
    return text


def _day_dot(v):
    """_dot for Nevada's single-day change in ballots."""
    return _dot(v, True)


def _xlen(text):
    n = 0
    for line in text.split("\n"):
        n += 23 if line == URL else sum(2 if ord(ch) > 0x2000 else 1 for ch in line)
    return n + text.count("\n")
