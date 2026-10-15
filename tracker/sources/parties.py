"""Party for every candidate named in a poll.

VoteHub gives candidate names only. Parties come from:
  1. data/reference/party_overrides.csv: manual fixes, always win
  2. FEC candidate master file (Senate, House): matched on state, office, district and
     last name; https://www.fec.gov/files/bulk-downloads/2026/cn26.zip (no API key needed)
  3. Wikipedia's "2026 <State> <office> election" article (governors, and a fallback for
     Senate/House): infobox nominees, then the party section a candidate is listed under
Names that can't be matched unambiguously are left without a party rather than guessed.
"""
import csv
import io
import json
import re
import time
import unicodedata
import urllib.parse
import zipfile

from ..common import ROOT, STATES, fetch

FEC_CN = "https://www.fec.gov/files/bulk-downloads/2026/cn26.zip"
OVERRIDES = ROOT / "data" / "reference" / "party_overrides.csv"
OFFICE = {"us-senator": "S", "us-representative": "H", "governor": "G"}
SKIP = {"undecided", "other", "someone else", "dont know", "not sure", "would not vote", "none", "refused"}
SUFFIX = {"jr", "sr", "ii", "iii", "iv"}
WORD_TO_PARTY = [("democratic", "D"), ("democrat", "D"), ("republican", "R"), ("libertarian", "L"), ("taxpayers", "O"), ("constitution", "O"),
                 ("green", "G"), ("independent", "I"), ("forward", "I"), ("no party", "I")]


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", "", s.replace("-", "").replace(".", " ")).split()


def _last(tokens):
    t = [x for x in tokens if x not in SUFFIX]
    return t[-1] if t else ""


def _fec_party(code):
    code = (code or "").upper()
    return {"DEM": "D", "DFL": "D", "REP": "R", "LIB": "L", "GRE": "G"}.get(
        code, "I" if code in ("IND", "NPA", "NNE", "UN", "NOP", "IDP") else "O")


def fec_index():
    """{(office, state, district|''): [(last, first_tokens, party)]}"""
    idx = {}
    with zipfile.ZipFile(io.BytesIO(fetch(FEC_CN, timeout=120))) as z:
        for line in io.TextIOWrapper(z.open("cn.txt"), encoding="latin-1"):
            f = line.rstrip("\n").split("|")
            if len(f) < 7 or f[5] not in ("S", "H"):
                continue
            last, _, first = f[1].partition(",")
            key = (f[5], f[4], f[6].zfill(2) if f[5] == "H" else "")
            idx.setdefault(key, []).append((_last(_norm(last)), _norm(first), _fec_party(f[2]), f[3]))
    return idx


def _wiki_text(title, tries=4):
    url = ("https://en.wikipedia.org/w/api.php?action=parse&prop=wikitext&format=json&redirects=1"
           "&maxlag=5&page=" + urllib.parse.quote(title))
    for i in range(tries):
        try:
            d = json.loads(fetch(url, timeout=60) or b"{}")
        except Exception:
            d = {}
        if "parse" in d:
            return d["parse"]["wikitext"]["*"]
        if d.get("error", {}).get("code") == "missingtitle":
            return ""
        time.sleep(5 * (i + 1))  # throttled or lagged: back off and retry
    print(f"  parties: could not read Wikipedia article '{title}'")
    return ""


def wiki_parties(text):
    """{last_name: party} from an election article's infobox and party sections."""
    out = {}
    # infobox: | nomineeN = [[Name]] / | partyN = Democratic Party (United States)
    # (top-two / top-four states like CA and AK use candidateN instead of nomineeN)
    noms = dict(re.findall(r"\|\s*(?:nominee|candidate)(\d)\s*=\s*([^\n]+)", text))
    pars = dict(re.findall(r"\|\s*party(\d)\s*=\s*([^\n]+)", text))
    for n, name in noms.items():
        name = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", name)
        p = next((v for w, v in WORD_TO_PARTY if w in pars.get(n, "").lower()), None)
        if p:
            out.setdefault(_last(_norm(name)), p)
    # body: candidates linked in a "Candidates" subsection under a party heading
    # ("== Republican primary == / === Candidates ==="); endorsement boxes are skipped
    party, cand_level, depth = None, 0, 0  # cand_level: heading level of the open "Candidates" section
    for line in text.splitlines():
        if depth or re.match(r"\s*\{\{\s*(Endorsements box|Collapsible|Hidden|cot)", line, re.I):
            depth += line.count("{{") - line.count("}}")
            depth = max(depth, 0)
            continue
        h = re.match(r"(=+)\s*(.+?)\s*=+\s*$", line)
        if h:
            level, title = len(h.group(1)), h.group(2).lower()
            hp = next((v for w, v in WORD_TO_PARTY if w in title), None)
            if level == 2:
                party = hp  # each top-level section is one party's primary, or something else
            if cand_level and level <= cand_level:
                cand_level = 0  # left the Candidates section (e.g. into Endorsements or Polling)
            if "candidate" in title or "nominee" in title:
                cand_level = cand_level or level
            continue
        if party and cand_level:
            # candidate bullets start with the name, linked or not:
            # "* [[Jane Doe]], state senator ..." / "* Jane Doe, business owner ..."
            m = (re.match(r"\s*\*+\s*\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", line)
                 or re.match(r"\s*\*+\s*((?:[A-Z][\w.'-]*\s+){1,3}[A-Z][\w'-]+)\s*,", line))
            if m:
                out.setdefault(_last(_norm(m.group(1))), party)
    return out


def _close(a, b):
    """True if two last names differ by at most one edit (catches typos like Healy/Healey)."""
    if abs(len(a) - len(b)) > 1 or min(len(a), len(b)) < 4:
        return False
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return a[i + 1:] == b[i + 1:] or a[i:] == b[i + 1:] or a[i + 1:] == b[i:]


def _lookup(table, last):
    """Exact last-name match, else a unique one-edit match."""
    if last in table:
        return table[last]
    near = {v for k, v in table.items() if _close(k, last)}
    return near.pop() if len(near) == 1 else None


def _overrides():
    if not OVERRIDES.exists():
        return {}
    with open(OVERRIDES, encoding="utf-8") as f:
        return {(r["poll_type"], r["state"], r["choice"].strip().lower()): r["party"] for r in csv.DictReader(f)}


def collect(con, day: str) -> dict:
    cands = con.execute("""SELECT DISTINCT p.poll_type, p.state, COALESCE(p.district, ''), a.choice
                           FROM polls p JOIN poll_answers a USING (poll_id)
                           WHERE p.poll_type IN ('us-senator', 'us-representative', 'governor')
                             AND p.state IS NOT NULL""").fetchall()
    over, fec, wiki = _overrides(), fec_index(), {}
    office_word = {"us-senator": "United States Senate election in",
                   "us-representative": "United States House of Representatives elections in",
                   "governor": "gubernatorial election"}

    def wiki_for(ptype, st):
        key = (ptype, st)
        if key not in wiki:
            name = STATES[st]
            title = (f"2026 {name} gubernatorial election" if ptype == "governor"
                     else f"2026 {office_word[ptype]} {name}")
            wiki[key] = wiki_parties(_wiki_text(title))
            time.sleep(0.5)  # be polite to the Wikipedia API
        return wiki[key]

    rows, stats = [], {"matched": 0, "unmatched": 0}
    for ptype, st, dist, choice in cands:
        tokens = _norm(choice)
        if not tokens or " ".join(tokens) in SKIP:
            continue
        party, source = over.get((ptype, st, choice.strip().lower())), "override"
        if not party and ptype != "governor":
            pool = fec.get((OFFICE[ptype], st, dist if ptype == "us-representative" else ""), [])
            if not pool and ptype == "us-representative":
                pool = fec.get(("H", st, "00"), [])  # at-large seats are district 00 at the FEC
            hits = [c for c in pool if c[0] == _last(tokens)]
            if len({h[2] for h in hits}) > 1:  # same last name, different parties: use first name
                hits = [h for h in hits if tokens[0] in h[1]]
            if hits and len({h[2] for h in hits}) == 1:
                party, source = hits[0][2], "fec"
        if not party:
            party, source = _lookup(wiki_for(ptype, st), _last(tokens)), "wikipedia"
        if party:
            stats["matched"] += 1
            rows.append((ptype, st, dist, choice, party, source))
        else:
            stats["unmatched"] += 1
    con.execute("DELETE FROM candidate_party")
    con.executemany("INSERT OR REPLACE INTO candidate_party VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    return stats
