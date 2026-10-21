"""Shared helpers: HTTP, paths, dates, state lookups."""
import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
LATEST = DATA / "latest"
DB_PATH = DATA / "election.db"
ET = ZoneInfo("America/New_York")

# Federal general elections. Add the next cycle's date here when it starts.
ELECTION_DAYS = {
    2022: date(2022, 11, 8),
    2024: date(2024, 11, 5),
    2026: date(2026, 11, 3),
}

UA = "ElectionTracker/1.0 (personal research; daily refresh)"


def today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def now_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z")


def fetch(url: str, retries: int = 3, timeout: int = 60) -> bytes | None:
    """GET a URL. Returns None on 404, raises after retries on other errors."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"fetch failed for {url}: {last}")


def fetch_json(url: str):
    body = fetch(url)
    return None if body is None else json.loads(body)


CHROMES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
           r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]


def find_chrome() -> str | None:
    """A Chrome or Edge binary (CHROME env var first). GitHub's Ubuntu runners have google-chrome."""
    import os
    import shutil
    for b in [os.environ.get("CHROME")] + CHROMES:
        if b and (shutil.which(b) or Path(b).exists()):
            return shutil.which(b) or b
    return None


def chrome_get(url: str, retries: int = 3) -> str:
    """GET a page through headless Chrome and return the rendered DOM. For sites whose bot
    checks turn away plain HTTP clients but let a real browser through (Texas SOS). A JSON
    response comes back inside <pre>...</pre>."""
    import html
    import re
    import subprocess
    exe = find_chrome()
    if not exe:
        raise RuntimeError("no Chrome or Edge found (set CHROME)")
    last = ""
    for attempt in range(retries):
        r = subprocess.run([exe, "--headless=new", "--disable-gpu", "--virtual-time-budget=15000", "--dump-dom", url],
                           capture_output=True, timeout=120, text=True, encoding="utf-8", errors="replace")
        last = r.stdout
        # a Cloudflare challenge page is titled "Just a moment..."; normal pages may still load its script
        if "<title>Just a moment" not in last and len(last) > 200:
            m = re.search(r"<pre[^>]*>(.*)</pre>", last, re.S)
            return html.unescape(m.group(1)) if m else last
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"chrome_get blocked or empty for {url}: {last[:200]}")


def save_raw(source: str, name: str, body: bytes, day: str) -> Path:
    """Archive the untouched download so any day can be re-parsed later."""
    p = RAW / day / source / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return p


STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
NAME_TO_ABBR = {v.lower(): k for k, v in STATES.items()}

# Statewide races on the 2026 ballot, used to pick each state's 2022 baseline result.
# Senate includes the FL and OH specials.
SENATE_2026 = {"AL", "AK", "AR", "CO", "DE", "FL", "GA", "ID", "IL", "IA", "KS", "KY", "LA", "ME",
               "MA", "MI", "MN", "MS", "MT", "NE", "NH", "NJ", "NM", "NC", "OH", "OK", "OR", "RI",
               "SC", "SD", "TN", "TX", "VA", "WV", "WY"}
GOVERNOR_2026 = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "FL", "GA", "HI", "ID", "IL", "IA", "KS",
                 "ME", "MD", "MA", "MI", "MN", "NE", "NV", "NH", "NM", "NY", "OH", "OK", "OR", "PA",
                 "RI", "SC", "SD", "TN", "TX", "VT", "WI", "WY"}
