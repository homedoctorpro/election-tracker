"""The shareable battleground cards, each a page (data inlined, so it also opens from disk) and a
1080x1350 PNG screenshot of it taken with headless Chrome:
  share        shift in the early-vote margin since 2024
  share-2022   the same since 2022
  share-trend  the margin by days before Election Day, 2026 against 2024 and 2022

The PNG is skipped, not failed, when no Chrome or Edge is installed. GitHub's Ubuntu runners
have google-chrome; set CHROME to point at another binary.
"""
import json
import subprocess
from pathlib import Path

from .common import find_chrome

TEMPLATE = Path(__file__).parent / "share.html"


CARDS = {"share": "2024", "share-2022": "2022", "share-both": "both", "share-trend": "trend"}


def build(site: Path, battlegrounds: list, generated: str, nv_firewall: dict | None = None) -> str:
    data = json.dumps({"generated": generated, "battlegrounds": battlegrounds, "nv_firewall": nv_firewall},
                      separators=(",", ":"))
    tpl = TEMPLATE.read_text(encoding="utf-8").replace("/*DATA*/null", data.replace("</", "<\\/"))
    exe = find_chrome()
    for name, mode in CARDS.items():
        page = site / f"{name}.html"
        page.write_text(tpl.replace("/*MODE*/'2024'", f"'{mode}'")
                        .replace("election-tracker/share.png", f"election-tracker/{name}.png"), encoding="utf-8")
        if exe:
            png = site / f"{name}.png"
            subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                            "--window-size=1080,1350", "--virtual-time-budget=8000", f"--screenshot={png.resolve()}",
                            page.resolve().as_uri()], check=True, timeout=120, capture_output=True)
    return ", ".join(CARDS) + ("" if exe else " (html only: no browser for the PNGs)")
