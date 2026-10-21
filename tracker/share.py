"""The shareable battleground card: site/share.html (data inlined, so it also opens from disk)
and site/share.png, a 1080x1350 screenshot of it taken with headless Chrome.

The PNG is skipped, not failed, when no Chrome or Edge is installed. GitHub's Ubuntu runners
have google-chrome; set CHROME to point at another binary.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

TEMPLATE = Path(__file__).parent / "share.html"
BROWSERS = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]


def _browser():
    for b in [os.environ.get("CHROME")] + BROWSERS:
        if b and (shutil.which(b) or Path(b).exists()):
            return shutil.which(b) or b
    return None


def build(site: Path, battlegrounds: list, generated: str) -> str:
    data = json.dumps({"generated": generated, "battlegrounds": battlegrounds}, separators=(",", ":"))
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*DATA*/null", data.replace("</", "<\\/"))
    page = site / "share.html"
    page.write_text(html, encoding="utf-8")
    exe = _browser()
    if not exe:
        return "share.html (no browser for share.png)"
    png = site / "share.png"
    subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                    "--window-size=1080,1350", "--virtual-time-budget=8000", f"--screenshot={png.resolve()}",
                    page.resolve().as_uri()], check=True, timeout=120, capture_output=True)
    return "share.html, share.png"
