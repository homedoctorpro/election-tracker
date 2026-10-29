"""Polls typed in by hand (data/reference/manual_polls.csv) for releases VoteHub hasn't added yet.

One CSV row per answer; rows sharing a poll_id are one poll. Once VoteHub carries the same poll
(same race and end date, pollster sharing a word like "Siena"), the manual copy is dropped so
it isn't counted twice. The CSV row can then be deleted.
"""
import csv
import re

from ..common import ROOT

CSV = ROOT / "data" / "reference" / "manual_polls.csv"
SOURCE = "manual"
KIND = {"us-senator": "Senate", "governor": "Governor"}


def _words(pollster):
    return {w for w in re.findall(r"[a-z]+", (pollster or "").lower()) if len(w) >= 4} - {"university", "college", "research"}


def _in_votehub(con, p):
    rows = con.execute("""SELECT pollster FROM polls WHERE source = 'votehub' AND poll_type = ? AND state = ?
                          AND COALESCE(district, '') = ? AND end_date = ?""",
                       (p["poll_type"], p["state"], p["district"], p["end_date"])).fetchall()
    return any(_words(r[0]) & _words(p["pollster"]) for r in rows)


def load(con, day: str) -> dict:
    con.execute("DELETE FROM poll_answers WHERE poll_id IN (SELECT poll_id FROM polls WHERE source = ?)", (SOURCE,))
    con.execute("DELETE FROM polls WHERE source = ?", (SOURCE,))
    polls = {}
    if CSV.exists():
        with open(CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                polls.setdefault(r["poll_id"], {**r, "answers": []})["answers"].append((r["choice"], float(r["pct"])))
    stats = {"manual": 0, "manual_now_in_votehub": 0}
    for pid, p in polls.items():
        if _in_votehub(con, p):
            stats["manual_now_in_votehub"] += 1
            continue
        dist = p["district"].zfill(2) if p["district"] else None
        race = f"{p['state']}-{dist}" if p["poll_type"] == "us-representative" else f"{p['state']} {KIND[p['poll_type']]}"
        con.execute("INSERT INTO polls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, p["poll_type"], race, p["state"], dist, None, p["pollster"], p["sponsors"],
                     p["start_date"], p["end_date"], None, int(p["sample_size"]) if p["sample_size"] else None,
                     p["population"], 0, None, p["url"], SOURCE, None, day, day))
        con.executemany("INSERT INTO poll_answers VALUES (?,?,?)", [(pid, c, v) for c, v in p["answers"]])
        stats["manual"] += 1
    con.commit()
    return stats
