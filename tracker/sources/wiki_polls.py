"""Senate and governor polls from the polling tables in Wikipedia's 2026 race articles.

Editors add new polls within hours, often before VoteHub does, and list some VoteHub never
carries. Only the general-election table is read: tables in party primary sections and in
collapsed "Hypothetical polling" boxes are skipped. A poll is kept only when VoteHub (or
manual_polls.csv) has nothing like it: same race, an end date within WINDOW days and a
pollster or sponsor sharing a distinctive word, or the same top-two numbers. When a poll has
LV and RV rows, the LV row is used. Tables that don't parse cleanly are skipped, not guessed.
"""
import datetime as dt
import hashlib
import json
import re
import time
import urllib.parse

from ..common import ROOT, STATES, fetch
from .parties import _wiki_text

SOURCE = "wikipedia"
DAYS = 60       # only polls that ended this recently (the race-average window)
WINDOW = 3      # end dates this close count as the same poll
KIND = {"us-senator": "Senate", "governor": "Governor"}
POP_RANK = {"lv": 0, "rv": 1, "a": 2, "v": 3}
GENERIC = {"university", "college", "research", "polling", "poll", "polls", "group", "insights", "strategies",
           "associates", "analytics", "news", "media", "market", "partners", "the", "data", "public", "opinion"}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
NOT_CANDIDATE = re.compile(r"poll source|date|sample|margin|other|undecided|lead|^$", re.I)


def _titles(ptype, st):
    name = STATES[st]
    if ptype == "governor":
        return [f"2026 {name} gubernatorial election"]
    return [f"2026 United States Senate election in {name}", f"2026 United States Senate special election in {name}"]


def _strip_markup(s):
    s = re.sub(r"<ref[^>]*/>", "", s)
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S)
    s = re.sub(r"<br\s*/?>", " ", s)
    s = re.sub(r"\{\{\s*(?:efn|Efn|efn-ua|refn|sfn)[^{}]*(?:\{\{[^{}]*\}\}[^{}]*)*\}\}", "", s)
    s = re.sub(r"\{\{\s*(?:nbsp|sdash|mdash|ndash)\s*\}\}", " ", s)
    s = re.sub(r"\{\{\s*nowrap\s*\|([^{}]*)\}\}", r"\1", s)
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", s)
    s = re.sub(r"<[^>]+>", "", s).replace("'''", "").replace("''", "").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


def _split_attr(cell):
    """'style=..|content' -> (attrs, content), splitting at the first '|' outside {{ }} and [[ ]]."""
    depth = 0
    for i, ch in enumerate(cell):
        if cell.startswith(("{{", "[["), i):
            depth += 1
        elif cell.startswith(("}}", "]]"), i):
            depth -= 1
        elif ch == "|" and depth == 0:
            return cell[:i], cell[i + 1:]
    return "", cell


def _cells(lines, mark):
    """Cells of one table row from its lines ('|' data or '!' header), joining continuation lines."""
    out = []
    for line in lines:
        if line.startswith(mark):
            out.extend(re.split(r"\|\||!!", line[1:]))
        elif out:
            out[-1] += "\n" + line
    return out


def _span(attrs, key):
    m = re.search(key + r'\s*=\s*"?(\d+)', attrs)
    return int(m.group(1)) if m else 1


def _tables(text):
    """General-election tables, outside collapsed boxes, as lists of row line-lists."""
    tables, section, hidden, cur = [], "", 0, None
    for line in text.splitlines():
        h = re.match(r"(=+)\s*(.+?)\s*=+\s*$", line)
        if h and len(h.group(1)) == 2:
            section = h.group(2).lower()
        if re.search(r"\{\{\s*(hidden begin|cot|collapse top)", line, re.I):
            hidden += 1
        if re.search(r"\{\{\s*(hidden end|cob|collapse bottom)", line, re.I):
            hidden = max(hidden - 1, 0)
        if line.startswith("{|"):
            cur = [[]] if "general election" in section and not hidden else None
            continue
        if cur is None:
            continue
        if line.startswith("|}"):
            tables.append(cur)
            cur = None
        elif line.startswith("|-"):
            cur.append([])
        else:
            cur[-1].append(line)
    return tables


def _end_date(s):
    """'October 19–26, 2026', 'September 28 – October 2, 2026', 'September 14, 2026' -> (start, end) ISO."""
    s = s.replace("–", "-").replace("—", "-")
    parts = [p.strip(" ,") for p in s.split("-")]
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?$", parts[-1]) or re.match(r"(\d{1,2}),\s*(\d{4})$", parts[-1])
    first = re.match(r"([A-Za-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?$", parts[0])
    if not m or not first:
        return None, None
    if len(m.groups()) == 2:  # "26, 2026": same month as the start
        mon, day, year = first.group(1), m.group(1), m.group(2)
    else:
        mon, day, year = m.groups()
    if mon.lower() not in MONTHS or not year:
        return None, None
    end = dt.date(int(year), MONTHS[mon.lower()], int(day))
    smon, sday, syear = first.group(1).lower(), int(first.group(2)), first.group(3)
    if smon not in MONTHS:
        return None, end.isoformat()
    start = dt.date(int(syear) if syear else end.year - (MONTHS[smon] > end.month), MONTHS[smon], sday)
    return start.isoformat(), end.isoformat()


def _pct(s):
    m = re.match(r"(\d+(?:\.\d+)?)\s*%", _strip_markup(s))
    return float(m.group(1)) if m else None


def parse(text):
    """Polls from an article: [{pollster, partisan, start, end, n, pop, answers: [(name, pct)]}]."""
    polls = []
    for rows in _tables(text):
        head = _cells(rows[0], "!") if rows and any(l.startswith("!") for l in rows[0]) else []
        if len(rows) > 1 and not head:
            head = _cells(rows[1], "!")
            rows = rows[1:]
        names = [_strip_markup(_split_attr(c)[1]) for c in head]
        if not names or "poll source" not in names[0].lower() or any("colspan" in _split_attr(c)[0] for c in head):
            continue
        try:
            i_date = next(i for i, n in enumerate(names) if "date" in n.lower())
            i_n = next(i for i, n in enumerate(names) if "sample" in n.lower())
        except StopIteration:
            continue
        cand = [(i, re.sub(r"\s*\([A-Z]{1,3}\)\s*$", "", n)) for i, n in enumerate(names) if not NOT_CANDIDATE.search(n)]
        if len(cand) < 2:
            continue
        spans = {}  # column -> (rows left, raw cell) for rowspan cells
        for row in rows[1:]:
            raw = _cells(row, "|")
            if not raw:
                continue
            full, k = [], 0
            for col in range(len(names)):
                if col in spans:
                    left, val = spans[col]
                    full.append(val)
                    spans[col] = (left - 1, val) if left > 1 else None
                    if spans[col] is None:
                        del spans[col]
                elif k < len(raw):
                    attrs, val = _split_attr(raw[k])
                    k += 1
                    if _span(attrs, "colspan") > 1:
                        full = None
                        break
                    if _span(attrs, "rowspan") > 1:
                        spans[col] = (_span(attrs, "rowspan") - 1, val)
                    full.append(val)
                else:
                    full.append("")
            if not full:
                continue
            start, end = _end_date(_strip_markup(full[i_date]))
            answers = [(name, _pct(full[i])) for i, name in cand]
            if not end or sum(v is not None for _, v in answers) < 2:
                continue
            src = _strip_markup(full[0])
            m = re.search(r"\s*\((R|D)\)\s*$", src)
            sm = re.match(r"([\d,]+)\s*\((\w+)\)", _strip_markup(full[i_n]))
            polls.append({"pollster": src[:m.start()] if m else src, "partisan": m.group(1) if m else None,
                          "start": start, "end": end, "n": int(sm.group(1).replace(",", "")) if sm else None,
                          "pop": sm.group(2).lower() if sm else None,
                          "answers": [(n, v) for n, v in answers if v is not None]})
    # one row per poll: prefer likely voters
    best = {}
    for p in polls:
        key = (p["pollster"], p["end"])
        if key not in best or POP_RANK.get(p["pop"], 9) < POP_RANK.get(best[key]["pop"], 9):
            best[key] = p
    return list(best.values())


def _words(*names):
    return {w for n in names for w in re.findall(r"[a-z]+", (n or "").lower()) if len(w) >= 3} - GENERIC


def _known(con, ptype, st):
    rows = con.execute("""SELECT p.poll_id, p.pollster, p.sponsors, p.end_date,
                                 (SELECT json_group_array(pct) FROM (SELECT pct FROM poll_answers a
                                   WHERE a.poll_id = p.poll_id ORDER BY pct DESC LIMIT 2))
                          FROM polls p WHERE p.source != ? AND p.poll_type = ? AND p.state = ?""",
                       (SOURCE, ptype, st)).fetchall()
    return [(_words(r[1], r[2]), r[3], sorted(json.loads(r[4]) or [], reverse=True)) for r in rows]


def _dup(p, known):
    end = dt.date.fromisoformat(p["end"])
    top = sorted((v for _, v in p["answers"]), reverse=True)[:2]
    for words, kend, ktop in known:
        if not kend or abs((dt.date.fromisoformat(kend) - end).days) > WINDOW:
            continue
        # same pollster, or the same top-two numbers once rounding is allowed for (50.5 vs 51)
        if words & _words(p["pollster"]) or (len(ktop) == 2 and len(top) == 2
                                              and all(abs(a - b) <= 0.6 for a, b in zip(top, ktop))):
            return True
    return False


def _articles(titles, chunk=5):
    """{title: wikitext} for every title that exists, a few pages per API call (fewer calls, less
    throttling). A page the batch didn't return gets one single-page retry."""
    out = {}
    for i in range(0, len(titles), chunk):
        part = titles[i:i + chunk]
        url = ("https://en.wikipedia.org/w/api.php?action=query&prop=revisions&rvprop=content&rvslots=main"
               "&format=json&formatversion=2&redirects=1&maxlag=5&titles=" + urllib.parse.quote("|".join(part)))
        try:
            d = json.loads(fetch(url, timeout=90) or b"{}")
        except Exception:
            d = {}
        back = {r["to"]: r["from"] for r in d.get("query", {}).get("redirects", [])}
        missing = set()
        for pg in d.get("query", {}).get("pages", []):
            title = back.get(pg["title"], pg["title"])
            if pg.get("missing"):
                missing.add(title)
            elif pg.get("revisions"):
                out[title] = pg["revisions"][0]["slots"]["main"]["content"]
        for t in part:
            if t not in out and t not in missing:
                text = _wiki_text(t, tries=4)
                if text:
                    out[t] = text
        time.sleep(1)  # be polite to the Wikipedia API
    return out


def load(con, day: str) -> dict:
    con.execute("DELETE FROM poll_answers WHERE poll_id IN (SELECT poll_id FROM polls WHERE source = ?)", (SOURCE,))
    con.execute("DELETE FROM polls WHERE source = ?", (SOURCE,))
    cutoff = (dt.date.fromisoformat(day) - dt.timedelta(days=DAYS)).isoformat()
    races = {("us-senator", k.split("-")[0]) for k in json.loads(
        (ROOT / "data" / "history" / "candidates.json").read_text(encoding="utf-8"))["senate"]}
    races |= set(con.execute("""SELECT DISTINCT poll_type, state FROM polls WHERE poll_type IN ('us-senator', 'governor')
                                AND state IS NOT NULL AND end_date >= ?""", (cutoff,)).fetchall())
    races = sorted(r for r in races if r[1] in STATES)
    texts = _articles([t for r in races for t in _titles(*r)])
    stats = {"wiki_added": 0, "wiki_already_known": 0, "wiki_articles": len(texts)}
    for ptype, st in races:
        found = [p for t in _titles(ptype, st) if t in texts
                 for p in parse(texts[t]) if cutoff <= p["end"] <= day]
        known = _known(con, ptype, st)
        for p in found:
            if _dup(p, known):
                stats["wiki_already_known"] += 1
                continue
            pid = "wiki-" + hashlib.sha1(f"{ptype}|{st}|{p['pollster']}|{p['end']}".encode()).hexdigest()[:12]
            con.execute("INSERT OR REPLACE INTO polls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pid, ptype, f"{st} {KIND[ptype]}", st, None, None, p["pollster"], "", p["start"], p["end"],
                         None, p["n"], p["pop"], 0, p["partisan"], None, SOURCE, None, day, day))
            con.execute("DELETE FROM poll_answers WHERE poll_id = ?", (pid,))
            con.executemany("INSERT OR REPLACE INTO poll_answers VALUES (?,?,?)", [(pid, n, v) for n, v in p["answers"]])
            known.append((_words(p["pollster"]), p["end"], sorted((v for _, v in p["answers"]), reverse=True)[:2]))
            stats["wiki_added"] += 1
    con.commit()
    return stats
