"""Shared helpers: HTTP, paths, dates, state lookups."""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
LATEST = DATA / "latest"
DB_PATH = DATA / "election.db"
ET = ZoneInfo("America/New_York")

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
