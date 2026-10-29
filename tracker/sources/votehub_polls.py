"""VoteHub polling API: every Senate, Governor, and House poll (plus generic ballot).

Docs: https://votehub.com/polls/api/  (free for researchers, journalists, civic use)
The API returns the full poll history in one call; we upsert so first_seen/last_seen
track when each poll appeared.
"""
import json
import re

from ..common import NAME_TO_ABBR, fetch, save_raw
from . import manual_polls

URL = "https://api.votehub.com/polls"
SOURCE = "votehub"
TYPES = {
    "us-senator": "Senate",
    "governor": "Governor",
    "us-representative": "House",
    "generic-ballot": "Generic Ballot",
}
CD_RE = re.compile(r"\b([A-Z]{2})-(\d{1,2}|AL)\b")


def _race(p):
    """Return (race, state, district) from VoteHub's subject/seat_name fields."""
    kind = TYPES[p["poll_type"]]
    if p["poll_type"] == "generic-ballot":
        return "US Generic Ballot", None, None
    text = " ".join(x for x in (p.get("seat_name"), p.get("subject")) if x)
    if p["poll_type"] == "us-representative":
        m = CD_RE.search(text)
        if m:
            return f"{m.group(1)}-{m.group(2).zfill(2)}", m.group(1), m.group(2).zfill(2)
        return f"House ({text})", None, None
    name = re.sub(r"^\d{4}\s+", "", p.get("subject") or "").strip()
    st = NAME_TO_ABBR.get(name.lower())
    # Some states hold two Senate races in a cycle; VoteHub marks them in the subject.
    if st is None:
        for full, ab in NAME_TO_ABBR.items():
            if name.lower().startswith(full):
                st = ab
                break
    return f"{st or name} {kind}", st, None


def _flag(p, day):
    s, e, c = p.get("start_date") or "", p.get("end_date") or "", p.get("created_at") or ""
    issues = []
    if s and e and s > e:
        issues.append("start after end")
    if e and (e > day or (c and e > c)):
        issues.append("end date after publish/today")
    return "; ".join(issues) or None


def collect(con, day: str) -> dict:
    body = fetch(URL)
    if body is None:
        raise RuntimeError("VoteHub API returned 404")
    save_raw(SOURCE, "polls.json", body, day)
    polls = json.loads(body)

    stats = {t: 0 for t in TYPES.values()}
    new = 0
    for p in polls:
        if p["poll_type"] not in TYPES:
            continue
        # Keep this cycle only (anything fielded since the 2024 election).
        if (p.get("end_date") or "") < "2024-11-06":
            continue
        race, st, dist = _race(p)
        exists = con.execute("SELECT 1 FROM polls WHERE poll_id=?", (p["id"],)).fetchone()
        new += exists is None
        con.execute(
            """INSERT INTO polls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(poll_id) DO UPDATE SET
                 poll_type=excluded.poll_type, race=excluded.race, state=excluded.state,
                 district=excluded.district, subject=excluded.subject, pollster=excluded.pollster,
                 sponsors=excluded.sponsors, start_date=excluded.start_date, end_date=excluded.end_date,
                 created_at=excluded.created_at, sample_size=excluded.sample_size,
                 population=excluded.population, internal=excluded.internal,
                 partisan=excluded.partisan, url=excluded.url,
                 quality_flag=excluded.quality_flag, last_seen=excluded.last_seen""",
            (p["id"], p["poll_type"], race, st, dist, p.get("subject"), p.get("pollster"),
             ", ".join(p.get("sponsors") or []), p.get("start_date"), p.get("end_date"),
             p.get("created_at"), p.get("sample_size"), p.get("population"),
             int(bool(p.get("internal"))), p.get("partisan"), p.get("url"), SOURCE, _flag(p, day), day, day),
        )
        con.execute("DELETE FROM poll_answers WHERE poll_id=?", (p["id"],))
        con.executemany(
            "INSERT OR REPLACE INTO poll_answers VALUES (?,?,?)",
            [(p["id"], a["choice"], a.get("pct")) for a in p.get("answers") or []],
        )
        stats[TYPES[p["poll_type"]]] += 1
    con.commit()
    stats["new_today"] = new
    stats.update(manual_polls.load(con, day))  # hand-entered polls VoteHub hasn't added yet
    return stats
