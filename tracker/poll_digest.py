"""The morning poll email: every Senate poll and every battleground House poll that appeared
since the last digest, each tagged with its pollster's Silver Bulletin grade and whether it was
partisan (VoteHub's flag: run or paid for by a party-aligned group).

- Battleground House = the races in UF's house_tossup_races.csv (data/latest/house_ratings.csv).
- Only current-matchup polls (or races too thinly polled to tell) are listed; primary and
  hypothetical matchups are left out, as are polls with a quality_flag.
- Grades come from the Silver Bulletin pollster ratings table (Datawrapper chart GiFps). The
  latest version is downloaded on each run and cached in data/raw/, never committed.
- "New" means first_seen after the last digest that was sent (data/raw/poll_digest_state.json);
  pass --mark-sent once the email has gone out.

python -m tracker.poll_digest [--since YYYY-MM-DD] [--mark-sent]
Writes data/latest/poll_digest.html and poll_digest.txt and prints the subject line.
"""
import argparse
import csv
import html
import json
import re
from collections import defaultdict
from datetime import date, timedelta

from .common import LATEST, RAW, fetch, today_et

SB_URL = "https://datawrapper.dwcdn.net/GiFps/{v}/dataset.csv"
SB_CACHE = RAW / "silver_pollster_ratings.csv"
STATE = RAW / "poll_digest_state.json"
DASHBOARD = "https://homedoctorpro.github.io/election-tracker/"
# VoteHub names that don't normalize onto the Silver Bulletin name
ALIASES = {
    "gqr": "greenberg quinlan rosner",
    "mason dixon": "mason dixon polling and strategy",
    "saint anselm": "saint anselm college",
    "gbao strategies": "gbao",
    "dccc targeting and analytics department": "dccc",
    "high point university survey research center": "high point university",
    "stetson university center for public opinion research": "stetson university",
    "university of georgia school of public and international affairs": "university of georgia",
    "university of southern california democracy and fair elections lab": "university of southern california",
    "the washington post/george mason university school of policy and government":
        "the washington post/george mason university schar school of policy and government",
}


def _norm(name):
    n = re.sub(r"[^a-z0-9/]+", " ", (name or "").lower().replace("&", "and")).strip()
    return ALIASES.get(n, n)


def _load_ratings():
    """{normalized pollster: (grade, bias)} from the newest Silver Bulletin table version."""
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    v = state.get("sb_version", 7)
    while True:  # Datawrapper bumps the version number on each republish
        body = fetch(SB_URL.format(v=v + 1), retries=1, timeout=20)
        if not body or not body.startswith(b"pollster"):
            break
        v += 1
    body = fetch(SB_URL.format(v=v), retries=2, timeout=30)
    if body and body.startswith(b"pollster"):
        RAW.mkdir(parents=True, exist_ok=True)
        SB_CACHE.write_bytes(body)
        state["sb_version"] = v
        STATE.write_text(json.dumps(state))
    out = {}
    if SB_CACHE.exists():
        for r in csv.DictReader(SB_CACHE.read_text(encoding="utf-8").splitlines(), delimiter="\t"):
            out[_norm(r["pollster"])] = (r["grade"].split("@@")[0], r["meanrevertedbias"].split("@@")[0])
    return out


def _rating(ratings, pollster, split=True):
    """Grade for a pollster; a joint poll ("A/B") falls back to its best-graded rated partner."""
    n = _norm(pollster)
    if n in ratings:
        return ratings[n]
    for variant in (n.replace("university", "college"), n.replace("college", "university"),
                    re.sub(r"^the ", "", n)):
        if variant in ratings:
            return ratings[variant]
    if not split or "/" not in pollster:
        return None
    parts = [g for g in (_rating(ratings, p, False) for p in pollster.split("/")) if g]
    return min(parts, key=lambda g: _grade_rank(g[0])) if parts else None


GRADES = ["A+", "A", "A-", "A/B", "B+", "B", "B-", "B/C", "C+", "C", "C-", "C/D", "D+", "D", "D-", "F"]


def _grade_rank(g):
    return GRADES.index(g) if g in GRADES else len(GRADES)


def _tier(grade):
    if grade is None:
        return "unrated"
    r = _grade_rank(grade)
    return "top" if r <= 3 else "solid" if r <= 7 else "weak"


def _party(s):
    m = re.search(r"\(([DRI])\)\s*$", s or "")
    return m.group(1) if m else None


def _name(s):
    return re.sub(r"\s*\([DRI]\)\s*$", "", s or "").split()[-1] if s else ""


def _margin(p):
    """'D+3' from the top two candidates, or 'Smith +3' when parties are unknown."""
    m = float(p["margin"] or 0)
    lead = _party(p["leader"]) or _name(p["leader"])
    return "Tied" if m == 0 else f"{lead}+{m:g}"


def _lead(s):
    """The average's margin as 'D+2.3', or 'I+3' / 'Osborn+3' in races with no D-R pairing."""
    if not s or s["status"] != "ok":
        return None
    if s["margin_d_minus_r"]:
        m = float(s["margin_d_minus_r"])
        return "Tied" if m == 0 else f"{'D' if m > 0 else 'R'}+{abs(m):g}"
    m = float(s["margin_a_minus_b"] or 0)
    return "Tied" if m == 0 else f"{s['party_a'] or s['candidate_a'].split()[-1]}+{m:g}"


def _closeness(s):
    return abs(float(s["margin_d_minus_r"] or s["margin_a_minus_b"] or 0))


def _avg(summary, race):
    s = summary.get(race)
    lead = _lead(s)
    if not lead:
        return None
    n = s["polls_averaged"]
    return f"avg {lead} ({n} poll{'' if n == '1' else 's'})"


def _short(pollster):
    """First firm of a joint poll, without a leading 'The', for the at-a-glance table."""
    first = pollster.split("/")[0]
    n = re.sub(r"^The ", "", pollster if len(first) <= 3 else first).strip()  # co/efficient
    return n if len(n) <= 22 else n[:21].rstrip() + "…"


def _side(p):
    return {"DEM": "D", "REP": "R"}.get(p["partisan"], p["partisan"])


def _read(name):
    with open(LATEST / name, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _date(d):
    x = date.fromisoformat(d)
    return f"{x:%b} {x.day}"


def build(since):
    ratings = _load_ratings()
    battleground = {r["race"] for r in _read("house_ratings.csv")}
    summary = {r["race"]: r for r in _read("race_summary.csv")}
    ratings_by_race = defaultdict(list)
    for r in _read("house_ratings.csv"):
        ratings_by_race[r["race"]].append(r["rating"])

    sections, latest = {}, {}
    for office, f in (("Senate", "polls_senate.csv"), ("Battleground House", "polls_house.csv")):
        races = defaultdict(list)
        for p in _read(f):
            if office == "Senate" and p["matchup"] == "current" and not p["quality_flag"]:
                if p["race"] not in latest or p["end_date"] > latest[p["race"]]["end_date"]:
                    latest[p["race"]] = p
            if p["first_seen"] <= since or p["quality_flag"] or p["matchup"] == "primary/hypothetical":
                continue
            if office != "Senate" and p["race"] not in battleground:
                continue
            g = _rating(ratings, p["pollster"])
            p["grade"], p["bias"] = g if g else (None, None)
            p["tier"] = _tier(p["grade"])
            races[p["race"]].append(p)
        for ps in races.values():
            ps.sort(key=lambda p: p["end_date"], reverse=True)
        # closest races first, by the current average (unaveraged races last)
        def key(race):
            s = summary.get(race)
            ok = bool(s and s["status"] == "ok")
            return (not ok, _closeness(s) if ok else 0, race)
        sections[office] = [(race, races[race]) for race in sorted(races, key=key)]

    for office, races in sections.items():
        for race, ps in races:
            if office != "Senate":
                rs = ratings_by_race[race]
                n = max((rs.count(x) for x in rs), default=0)
                tops = sorted({x for x in rs if rs.count(x) == n})
                ps[0]["_rating"] = (f"{' / '.join(tops)} at {n}{' each' if len(tops) > 1 else ''} "
                                    f"of {len(rs)} outlets") if rs else None
    for p in latest.values():
        g = _rating(ratings, p["pollster"])
        p["grade"], p["bias"] = g if g else (None, None)
        p["tier"] = _tier(p["grade"])
    return sections, summary, latest


def _tags(p):
    tags = []
    if p["grade"]:
        tags.append(f"{p['grade']} pollster")
        if p["tier"] == "weak":
            tags.append("low-rated")
    else:
        tags.append("unrated")
    if p["partisan"]:
        tags.append(f"PARTISAN ({_side(p)})" + (f" for {p['sponsors']}" if p["sponsors"] else ""))
    elif p["sponsors"]:
        tags.append(f"for {p['sponsors']}")
    if p["bias"] and p["grade"]:
        m = re.match(r"([DR])\+([\d.]+)", p["bias"])
        if m and float(m.group(2)) >= 1.5:
            tags.append(f"historical lean {p['bias']}")
    return tags


def _sample(p):
    n = f"{int(float(p['sample_size'])):,}" if p["sample_size"] else "n/a"
    return f"{n} {(p['population'] or '').upper()}".strip()


def _answers(p):
    parts = [a.strip() for a in (p["all_answers"] or "").split(";") if a.strip()][:3]
    return ", ".join(re.sub(r"(\d+)\.0\b", r"\1", a) for a in parts)


def render(sections, summary, latest, since):
    today = date.fromisoformat(today_et())
    counts = {o: sum(len(ps) for _, ps in races) for o, races in sections.items()}
    subject = (f"Polls {today:%a %b} {today.day}: {counts['Senate']} Senate, "
               f"{counts['Battleground House']} battleground House")
    css = {"top": "#1a7f37", "solid": "#555", "weak": "#b35900", "unrated": "#888"}
    intro = (f"New since {_date(since)}: {counts['Senate']} Senate, {counts['Battleground House']} "
             f"battleground House. Grade = Silver Bulletin; P(D)/P(R) = partisan.")
    txt = [intro, "", "SENATE AT A GLANCE (avg | new polls, or the last one in brackets)"]
    h = [f"<div style='font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:680px'>",
         f"<p style='color:#555;margin:0 0 10px'>{html.escape(intro)}</p>",
         "<table style='border-collapse:collapse;font-size:13px;width:100%'>"
         "<tr style='text-align:left;color:#666;border-bottom:1px solid #ccc'>"
         "<th style='padding:3px 6px'>Senate</th><th style='padding:3px 6px'>Avg</th>"
         "<th style='padding:3px 6px'>New polls <span style='font-weight:normal'>(grey: last poll)</span></th></tr>"]
    new = dict(sections["Senate"])
    glance = sorted((s for s in summary.values() if s["office"] == "Senate" and s["status"] == "ok"),
                    key=_closeness)
    glance += [{"race": r, "status": ""} for r in new if summary.get(r, {}).get("status") != "ok"]
    for s in glance:
        race, ps = s["race"], new.get(s["race"], [])
        old = [] if ps or race not in latest else [latest[race]]
        chips_t, chips_h = [], []
        for p in ps or old:
            grade = p["grade"] or "unrated"
            part = f" P({_side(p)})" if p["partisan"] else ""
            chips_t.append(f"{_margin(p)} {_short(p['pollster'])} {grade}{part}")
            chips_h.append(f"<b>{_margin(p)}</b> {html.escape(_short(p['pollster']))} "
                           f"<span style='color:{css[p['tier']]};font-weight:bold'>{grade}</span>"
                           + (f" <span style='color:#c00;font-weight:bold'>P({_side(p)})</span>" if part else ""))
        lead = _lead(s) or ""
        if old:
            chips_t = [f"[{_date(old[0]['end_date'])}: {chips_t[0]}]"]
            chips_h = [f"{_date(old[0]['end_date'])}: {chips_h[0]}"]
        txt.append(f"  {race.replace(' Senate', ''):<3} {lead:<7} {'; '.join(chips_t)}")
        shade = "" if ps else "color:#999;"
        h.append(f"<tr style='border-bottom:1px solid #eee;{shade}'>"
                 f"<td style='padding:3px 6px;font-weight:bold'>{race.replace(' Senate', '')}</td>"
                 f"<td style='padding:3px 6px;white-space:nowrap'>{lead}</td>"
                 f"<td style='padding:3px 6px'>{' · '.join(chips_h)}</td></tr>")
    h.append("</table>")
    txt.append("")
    for office, races in sections.items():
        txt += [f"== {office.upper()} ==", ""]
        h.append(f"<h2 style='font-size:17px;border-bottom:2px solid #222;padding-bottom:3px'>{office}</h2>")
        if not races:
            txt += ["No new polls.", ""]
            h.append("<p style='color:#888'>No new polls.</p>")
        for race, ps in races:
            avg = _avg(summary, race)
            extra = [x for x in (avg, ps[0].get("_rating")) if x]
            head = race + "".join(f" | {x}" for x in extra)
            txt.append(head)
            h.append(f"<p style='margin:14px 0 4px'><b>{html.escape(race)}</b>"
                     + (f" <span style='color:#666'>{html.escape(' · '.join(extra))}</span>" if extra else "") + "</p><ul style='margin:0;padding-left:20px'>")
            for p in ps:
                tags = _tags(p)
                line = (f"  {_margin(p)}  {p['pollster']} [{', '.join(tags)}], "
                        f"{_date(p['start_date'])}-{_date(p['end_date'])}, {_sample(p)}: {_answers(p)}")
                txt.append(line)
                tag_html = []
                for t in tags:
                    color = "#c00" if t.startswith("PARTISAN") else css[p["tier"]] if t.endswith("pollster") or t in ("unrated", "low-rated") else "#666"
                    weight = "bold" if t.startswith("PARTISAN") or t.endswith("pollster") else "normal"
                    tag_html.append(f"<span style='color:{color};font-weight:{weight}'>{html.escape(t)}</span>")
                pollster = html.escape(p["pollster"])
                if p["url"]:
                    pollster = f"<a href='{html.escape(p['url'])}' style='color:#0645ad'>{pollster}</a>"
                h.append(f"<li style='margin-bottom:5px'><b>{_margin(p)}</b> · {pollster} · {' · '.join(tag_html)}"
                         f"<br><span style='color:#555;font-size:13px'>{_date(p['start_date'])}–{_date(p['end_date'])}, "
                         f"{_sample(p)}: {html.escape(_answers(p))}</span></li>")
            txt.append("")
            h.append("</ul>")
    note = ("Poll averages are the tracker's plain average of up to 5 current-matchup polls from the "
            "last 60 days, not weighted for quality. House ratings are the most common of 10 outlets. "
            f"Dashboard: {DASHBOARD}")
    txt.append(note)
    h.append(f"<p style='color:#777;font-size:12px;margin-top:20px'>{html.escape(note)}</p></div>")
    return subject, "\n".join(txt), "\n".join(h)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="list polls first seen after this date (default: last digest sent)")
    ap.add_argument("--mark-sent", action="store_true", help="record today as the last digest sent")
    a = ap.parse_args(argv)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if a.mark_sent:
        state["last_sent"] = today_et()
        STATE.write_text(json.dumps(state))
        print(f"marked sent {state['last_sent']}")
        return 0
    yesterday = (date.fromisoformat(today_et()) - timedelta(days=1)).isoformat()
    since = a.since or state.get("last_sent") or yesterday
    # first_seen is a date: a digest sent this morning already covered polls first seen today
    # before it, so a second digest the same day starts from yesterday instead of showing nothing
    if since >= today_et():
        since = yesterday
    sections, summary, latest = build(since)
    subject, txt, body = render(sections, summary, latest, since)
    (LATEST / "poll_digest.txt").write_text(txt, encoding="utf-8")
    (LATEST / "poll_digest.html").write_text(body, encoding="utf-8")
    print(subject)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
