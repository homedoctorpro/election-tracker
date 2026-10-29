"""House and Senate maps: every 2026 race with its candidates, poll average and 2024 Trump share.

Built once (committed to data/reference/):
  house_pres24.csv   2024 presidential result for each district on the lines used in 2026, from
                     The Downballot's calculations (they cover the nine states that redrew for 2026)
  map_paths.json     district and state outlines, projected (Albers USA) to SVG path strings
                     python -m tracker.race_map --build-pres
                     python -m tracker.race_map --build-shapes [--simplemaps <zip>]
Refreshed daily:
  candidates         the race tables in Wikipedia's "2026 United States House/Senate elections"
                     articles (general-election candidates once a state's primary is over);
                     the last good parse is kept in data/history/candidates.json if Wikipedia fails
"""
import argparse
import csv
import io
import json
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import zipfile
from pathlib import Path

from .common import ROOT, SENATE_2026, STATES, fetch

REF = ROOT / "data" / "reference"
PRES = REF / "house_pres24.csv"
PATHS = REF / "map_paths.json"
CANDS = ROOT / "data" / "history" / "candidates.json"  # committed by CI, so a failed fetch has a fallback
DOWNBALLOT = ("https://docs.google.com/spreadsheets/d/1eZfaFI-c-PFOoKx1-zZA2MP0_dxRq_LVK0re3BOQqy0"
              "/export?format=csv&gid=1491069057")
CENSUS_CD = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_cd119_20m.zip"
CENSUS_STATE = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_state_20m.zip"
# states whose 2026 House map differs from the 119th Congress lines in the Census file
REDRAWN = {"AL", "CA", "FL", "LA", "NC", "OH", "TN", "TX", "UT"}
# Senate going into the 2026 election: 53 R, 45 D plus 2 independents who caucus with the
# Democrats (King, Sanders), per the 2026 Senate elections article's infobox
SENATE_BEFORE = {"R": 53, "D": 47}
AT_LARGE = {"AK", "DE", "ND", "SD", "VT", "WY"}
FIPS = {"01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE",
        "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA",
        "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
        "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM",
        "36": "NY", "37": "NC", "38": "ND", "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
        "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
        "54": "WV", "55": "WI", "56": "WY"}
NAME_TO_ABBR = {v: k for k, v in STATES.items()}
# DFL: Minnesota's Democratic–Farmer–Labor Party, the state's Democratic Party
PARTY_WORDS = [("Democratic", "D"), ("DFL", "D"), ("Republican", "R"), ("Libertarian", "L"), ("Green", "G")]


def district_key(state, district):
    """'TX-09', or 'AK-AL' for an at-large seat, from any of '9', '09', 'AL', '00', None."""
    d = str(district or "").strip().upper()
    if state in AT_LARGE or d in ("", "AL", "00", "0"):
        return f"{state}-AL"
    return f"{state}-{d.zfill(2)}"


# ---- 2024 presidential by district -------------------------------------------------------

def build_pres():
    rows = list(csv.reader(io.StringIO(fetch(DOWNBALLOT, timeout=60).decode("utf-8"))))
    num = lambda s: int(s.replace(",", "")) if s.strip() else None
    out = []
    for r in rows[4:]:
        if not re.match(r"^[A-Z]{2}-(\d\d|AL)$", r[0]):
            continue
        st, d = r[0].split("-")
        out.append({"district": district_key(st, d), "incumbent": r[1], "inc_party": r[2].strip("()"),
                    "harris": num(r[4]), "trump": num(r[5]), "total": num(r[6])})
    assert len(out) == 435, f"expected 435 districts, got {len(out)}"
    with open(PRES, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    print(f"wrote {PRES} ({len(out)} districts)")


def load_pres():
    with open(PRES, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("harris", "trump", "total"):
            r[k] = int(r[k]) if r[k] else None
    return {r["district"]: r for r in rows}


def state_pres():
    """{state: (harris, trump, total)} for 2024, from results_state.csv."""
    out = {}
    with open(REF / "results_state.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["year"] == "2024" and r["race"] == "president":
                d, rep, o = int(r["dem"]), int(r["rep"]), int(r["other"] or 0)
                out[r["state"]] = (d, rep, d + rep + o)
    return out


# ---- shapes -----------------------------------------------------------------------------

def _project(shp, id_field, extra, out):
    """{id: projected geometry} via mapshaper's fixed Albers USA (AK and HI as insets)."""
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    subprocess.run([npx, "-y", "mapshaper@0.6.102", "-i", str(shp), *extra, "-proj", "albersusa",
                    "-o", str(out), "format=geojson", "precision=1"], check=True)
    return {f["properties"][id_field]: f["geometry"]
            for f in json.loads(Path(out).read_text(encoding="utf-8"))["features"] if f["geometry"]}


def _rings(g):
    return [r for poly in (g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]) for r in poly]


def _svg_paths(layers, width=975):
    """Projected geometries (meters) -> SVG path strings, all through one shared transform so
    layers built from different files line up. Returns (paths per layer, viewBox height)."""
    xs, ys = [], []
    for g in layers[-1].values():
        for r in _rings(g):
            xs += [p[0] for p in r]
            ys += [p[1] for p in r]
    x0, y1 = min(xs), max(ys)
    k = (width - 10) / (max(xs) - x0)
    tx = lambda p: f"{(p[0] - x0) * k + 5:.1f},{(y1 - p[1]) * k + 5:.1f}"
    out = [{key: "".join("M" + "L".join(tx(p) for p in r) + "Z" for r in _rings(g)) for key, g in layer.items()}
           for layer in layers]
    return out, round((max(ys) - min(ys)) * k + 10)


def build_shapes(simplemaps=None):
    """Census 119th-Congress lines everywhere except REDRAWN, which come from the SimpleMaps
    120th Congress basic file (CC BY 4.0). Without that file the redrawn states are left out."""
    tmp = Path(tempfile.mkdtemp())
    for z, url in (("cd", CENSUS_CD), ("st", CENSUS_STATE)):
        (tmp / f"{z}.zip").write_bytes(fetch(url, timeout=120))
        zipfile.ZipFile(tmp / f"{z}.zip").extractall(tmp / z)
    # filters avoid | and ^: npx is a .cmd on Windows, and cmd.exe would read them as operators
    keep = " ".join(k for k, v in FIPS.items() if v not in REDRAWN and v != "DC")
    simp = ["-simplify", "12%", "keep-shapes"]
    cd = _project(next((tmp / "cd").glob("*.shp")), "GEOID",
                  ["-filter", f"'{keep}'.split(' ').indexOf(GEOID.slice(0, 2)) > -1 && GEOID.slice(2) != 'ZZ'", *simp],
                  tmp / "cd.json")
    house = {district_key(FIPS[g[:2]], g[2:]): geom for g, geom in cd.items()}
    if simplemaps:
        zipfile.ZipFile(simplemaps).extractall(tmp / "sm")
        # the zip also has a pre-projected congress_albersusa.shp; we need the WGS84 one
        sm = _project(next(p for p in (tmp / "sm").rglob("*.shp") if "albers" not in p.name), "id",
                      ["-filter", f"'{' '.join(sorted(REDRAWN))}'.split(' ').indexOf(id.slice(0, 2)) > -1", *simp],
                      tmp / "sm.json")
        house.update({district_key(i[:2], i[2:]): geom for i, geom in sm.items()})
    st = _project(next((tmp / "st").glob("*.shp")), "STATEFP",
                  ["-filter", "+STATEFP < 60 && STATEFP != '11'", *simp], tmp / "st.json")
    states = {FIPS[fp]: geom for fp, geom in st.items()}
    (house, states), height = _svg_paths([house, states])
    missing = sorted(set(load_pres()) - set(house))
    PATHS.write_text(json.dumps({"w": 975, "h": height, "house": house, "states": states, "missing": missing,
                                 "redrawn_source": "simplemaps" if simplemaps else None},
                                separators=(",", ":")), encoding="utf-8")
    print(f"wrote {PATHS}: {len(house)} districts, {len(states)} states; missing {missing or 'none'}")
    shutil.rmtree(tmp, ignore_errors=True)


# ---- candidates (Wikipedia) ---------------------------------------------------------------

def _wikitext(title):
    url = ("https://en.wikipedia.org/w/api.php?action=parse&prop=wikitext&format=json&redirects=1"
           "&maxlag=5&page=" + urllib.parse.quote(title))
    return json.loads(fetch(url, timeout=90))["parse"]["wikitext"]["*"]


def _clean(s):
    s = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", s, flags=re.S)
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\{\{sortname\|([^|}]*)\|([^|}]*)[^}]*\}\}", r"\1 \2", s)
    s = re.sub(r"\{\{small\|([^}]*)\}\}", r"(\1)", s)
    s = re.sub(r"\{\{[^}]*\}\}", "", s)
    return re.sub(r"\s+", " ", s.replace("'''", "").replace("''", "")).strip()


def _candidates(block):
    out = []
    for m in re.finditer(r"\*\s*\{\{Party stripe\|[^}]*\}\}([^\n]*)", block):
        txt = _clean(m.group(1))
        pm = re.search(r"\(([^()]*)\)\s*$", txt)
        if not pm:
            continue
        label = pm.group(1)
        party = next((p for w, p in PARTY_WORDS if w.lower() in label.lower()), "I" if "independent" in label.lower() else "O")
        out.append({"name": txt[:pm.start()].strip(), "party": party, "label": label})
    return out


def _incumbent(cells):
    """(name, party letter) from the incumbent cells of a race row."""
    for i, c in enumerate(cells):
        if re.search(r"none.*new seat|vacant", c, re.I):
            return None, None
        if "sortname" in c or c.strip().startswith("[["):
            c = re.split(r"<br\s*/?>", c, flags=re.I)[0]  # drop "Redistricted from the ..." notes
            name = _clean(c.split("|", 1)[-1] if c.lstrip().startswith(("style", "{{Party shading")) else c)
            party = next((p for w, p in PARTY_WORDS if w in " ".join(cells[i:i + 3])), None)
            return name, party
    return None, None


def _rows(text, header_re):
    """Yield (header match, cell list, raw row text) for each wikitable row whose header matches."""
    for chunk in re.split(r"(?:\n\|-[^\n]*)+\n", text):
        m = re.match(header_re, chunk.strip())
        if m:
            yield m, [c.strip() for c in chunk.split("\n|")[1:]], chunk


def parse_house(text):
    out = {}
    # rowspan=2 rows are seats where redistricting put two incumbents; the first one is listed
    for m, cells, raw in _rows(text, r"!\s*(?:rowspan=\d+\s*\|\s*)?\{\{ushr\|([A-Z]{2})\|(\w+)\|X\}\}"):
        if re.search(r"\)\s*[\d.]+%", raw):
            continue  # a special election already decided, listed with its result
        cands = _candidates(raw)
        status = next((_clean(c.split("|")[-1]) for c in cells if re.search(r"Incumbent|New (?:member|representative)|resigned|died", c)), "")
        name, party = _incumbent(cells[1:])
        out[district_key(m.group(1), m.group(2))] = {"incumbent": name, "inc_party": party,
                                                     "status": status, "candidates": cands}
    return out


def parse_senate(text):
    out = {}
    hdr = r"!\s*\[\[2026 United States Senate (special )?election in ([^|\]]+)\|[^\]]*\]\]"
    for m, cells, raw in _rows(text, hdr):
        cands = _candidates(raw)
        if not cands:
            continue  # the summary tables earlier in the article name the race without candidates
        st = NAME_TO_ABBR.get(m.group(2).strip())
        if not st:
            continue
        status = next((_clean(c.split("|")[-1]) for c in cells if re.search(r"Incumbent|appointee|New senator", c, re.I)), "")
        name, party = _incumbent(cells[1:])
        out[st + ("-special" if m.group(1) else "")] = {"incumbent": name, "inc_party": party,
                                                        "status": status, "candidates": cands}
    return out


def candidates():
    """{'house': {...}, 'senate': {...}}, refreshed from Wikipedia or the last good copy."""
    try:
        got = {"house": parse_house(_wikitext("2026 United States House of Representatives elections")),
               "senate": parse_senate(_wikitext("2026 United States Senate elections"))}
        if len(got["house"]) >= 400 and len(got["senate"]) >= 30:
            CANDS.write_text(json.dumps(got, indent=1), encoding="utf-8")
            return got
        print(f"  race_map: Wikipedia parse looked short ({len(got['house'])} House, {len(got['senate'])} Senate); using last copy")
    except Exception as e:
        print(f"  race_map: Wikipedia candidates failed ({e}); using last copy")
    return json.loads(CANDS.read_text(encoding="utf-8")) if CANDS.exists() else {"house": {}, "senate": {}}


# ---- assemble --------------------------------------------------------------------------

def build(summary, ratings):
    """Data for the map tab. Polls themselves are looked up in data.json's poll list by the page."""
    cands = candidates()
    pres = load_pres()
    avg = {}
    for r in summary:
        if r["status"] != "ok":
            continue
        key = district_key(r["state"], r["district"]) if r["office"] == "House" else r["state"] if r["office"] == "Senate" else None
        if key:
            avg[key] = {k: r[k] for k in ("candidate_a", "party_a", "a_avg", "candidate_b", "party_b", "b_avg",
                                          "polls_averaged", "latest_poll_end", "margin_d_minus_r")}
    rate = {}
    for r in ratings:
        rate.setdefault(district_key(r["state"], r["district"]), {})[r["outlet"]] = r["rating"]
    house = {}
    for k, p in pres.items():
        c = cands["house"].get(k, {})
        house[k] = {"trump": round(100 * p["trump"] / p["total"], 1) if p["trump"] else None,
                    "harris": round(100 * p["harris"] / p["total"], 1) if p["harris"] else None,
                    # Trump minus Harris from raw votes, so it doesn't drift from rounding the shares
                    "margin": round(100 * (p["trump"] - p["harris"]) / p["total"], 1) if p["trump"] else None,
                    "incumbent": c["incumbent"] if c else p["incumbent"], "inc_party": c["inc_party"] if c else p["inc_party"],
                    "status": c.get("status", ""), "candidates": c.get("candidates", []),
                    "avg": avg.get(k), "ratings": rate.get(k)}
    sp = state_pres()
    senate = {}
    for st in sorted(SENATE_2026):
        h, t, tot = sp[st]
        for key, c in cands["senate"].items():
            if key.split("-")[0] != st:
                continue
            senate[key] = {"trump": round(100 * t / tot, 1), "harris": round(100 * h / tot, 1),
                           "margin": round(100 * (t - h) / tot, 1),
                           "special": key.endswith("-special"), **c,
                           # race_summary has one Senate row per state; it belongs to the special in FL/OH
                           "avg": avg.get(st) if key.endswith("-special") or f"{st}-special" not in cands["senate"] else None}
    # seats not up this year = the current split minus the incumbents whose seats are up
    up = {p: sum(1 for r in senate.values() if r.get("inc_party") == p) for p in SENATE_BEFORE}
    not_up = {p: SENATE_BEFORE[p] - up[p] for p in SENATE_BEFORE}
    if sum(not_up.values()) + len(senate) != 100:
        print(f"  race_map: Senate seats don't add to 100 ({not_up} not up, {len(senate)} races); bar left out")
        not_up = None
    return {"house": house, "senate": senate, "senate_not_up": not_up,
            "no_senate": sorted(set(STATES) - SENATE_2026 - {"DC"})}


def write_paths(site):
    if PATHS.exists():
        shutil.copy(PATHS, site / "map.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-pres", action="store_true")
    ap.add_argument("--build-shapes", action="store_true")
    ap.add_argument("--simplemaps", help="SimpleMaps congress basic zip (120th Congress lines)")
    ap.add_argument("--candidates", action="store_true", help="parse Wikipedia and print counts")
    a = ap.parse_args()
    if a.build_pres:
        build_pres()
    if a.build_shapes:
        build_shapes(a.simplemaps)
    if a.candidates:
        c = candidates()
        print(len(c["house"]), "House races,", len(c["senate"]), "Senate races")
