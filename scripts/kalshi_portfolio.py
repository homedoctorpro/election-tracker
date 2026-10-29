"""Your Kalshi balance, overall win/loss on settled markets, and open positions next to the
tracker's poll and early-vote reads.

Read-only: it only sends GET requests (balance, positions, market prices) and never places or
cancels orders. Run it yourself; it reads your API key on your machine, from KALSHI_KEY_ID and
KALSHI_KEY_FILE if set, else from Documents\\Tallysheet .env\\.env.local.

    python scripts\\kalshi_portfolio.py

Needs the `cryptography` package (pip install cryptography).
"""
import base64
import json
import os
import re
import sys
import time
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding

HOST = "https://api.elections.kalshi.com"
TRACKER = "https://homedoctorpro.github.io/election-tracker/data.json"


def sign(key, ts, method, path):
    """Kalshi request signature over timestamp + method + path (no query). Ed25519 keys (Kalshi's
    default since Oct 2026) sign the message directly; RSA keys use RSA-PSS with SHA-256."""
    msg = (ts + method + path.split("?")[0]).encode()
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return base64.b64encode(key.sign(msg)).decode()
    return base64.b64encode(key.sign(
        msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256())).decode()


def _signed_get(path, key_id, key):
    """GET with Kalshi's request signing."""
    ts = str(int(time.time() * 1000))
    sig = sign(key, ts, "GET", path)
    req = urllib.request.Request(HOST + path, headers={
        "KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def dollars(d, base):
    """Kalshi is migrating numeric fields, so read whichever form is present (same rules as
    Tallysheet's readDollars): `<base>_dollars` is a dollar string; a bare number is integer
    cents (revenue: 9852); a bare string is dollars (fee_cost: "1.723500")."""
    v = d.get(f"{base}_dollars")
    if v not in (None, ""):
        return float(v)
    v = d.get(base)
    if isinstance(v, (int, float)):
        return v / 100
    return float(v) if v not in (None, "") else 0.0


def count(d, base):
    """Contract counts: `<base>_fp` (fixed-point string) or a bare number."""
    v = d.get(f"{base}_fp")
    if v in (None, ""):
        v = d.get(base)
    return float(v) if v not in (None, "") else 0.0


TALLYSHEET_ENV = os.path.expanduser(r"~\Documents\Tallysheet .env\.env.local")


def _pem(text):
    """Rebuild a PEM pasted as one line, with literal \\n, or with an unwrapped body."""
    text = text.replace("\\n", "\n").strip().strip('"').strip("'")
    m = re.search(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", text, re.S)
    if not m:
        return None
    body = re.sub(r"\s+", "", m[2])
    lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {m[1]}-----\n{lines}\n-----END {m[1]}-----\n".encode()


def load_key():
    """API key id and private key: from the environment, else from Tallysheet's .env.local.
    The key can be inline (NEW_API_PRIVATE_KEY or KALSHI_PRIVATE_KEY, multi-line allowed) or a
    file (KALSHI_PRIVATE_KEY_FILE), so nothing needs setting per session."""
    env = {}
    if os.path.exists(TALLYSHEET_ENV):
        with open(TALLYSHEET_ENV, encoding="utf-8") as f:
            lines = iter(f.read().splitlines())
        for line in lines:
            k, sep, v = line.strip().partition("=")
            if not sep or k.startswith("#"):
                continue
            if "-----BEGIN" in v and "-----END" not in v:  # multi-line PEM value
                for more in lines:
                    v += "\n" + more
                    if "-----END" in more:
                        break
            env[k.strip()] = v.strip().strip('"').strip("'")
    key_id = os.environ.get("KALSHI_KEY_ID") or env.get("KALSHI_KEY_ID")
    if not key_id:
        sys.exit(f"No KALSHI_KEY_ID in the environment or {TALLYSHEET_ENV}.")
    for name, id_name in (("KALSHI_PRIVATE_KEY", "KALSHI_KEY_ID"), ("NEW_API_PRIVATE_KEY", "NEW_API_KEY_ID")):
        pem = _pem(os.environ.get(name) or env.get(name) or "")
        if pem:
            # a pasted key needs its own key's id; NEW_API_KEY_ID pairs with NEW_API_PRIVATE_KEY
            key_id = os.environ.get(id_name) or env.get(id_name) or key_id
            return key_id, serialization.load_pem_private_key(pem, password=None)
    key_file = (os.environ.get("KALSHI_KEY_FILE") or os.environ.get("KALSHI_PRIVATE_KEY_FILE")
                or env.get("KALSHI_PRIVATE_KEY_FILE"))
    if not key_file:
        sys.exit(f"No Kalshi private key found in the environment or {TALLYSHEET_ENV}.")
    key_file = os.path.expanduser(key_file)
    if not os.path.isabs(key_file):  # relative paths in the .env are relative to its folder
        key_file = os.path.join(os.path.dirname(TALLYSHEET_ENV), key_file)
    if not os.path.exists(key_file):
        sys.exit(f"Kalshi private key file not found: {key_file}\n"
                 "Save the key Kalshi gave you there (or set KALSHI_KEY_FILE to where it is).")
    with open(key_file, "rb") as f:
        return key_id, serialization.load_pem_private_key(f.read(), password=None)


def main():
    key_id, key = load_key()

    bal = _signed_get("/trade-api/v2/portfolio/balance", key_id, key)
    print(f"Balance: ${dollars(bal, 'balance'):,.2f}")

    # Overall win/loss across settled markets, by the same formula as Tallysheet
    # (Documents/tallysheet, tallysheet.vercel.app): revenue + $1 x min(yes, no) - yes cost
    # - no cost - fees. The min() term counts cash-outs: opposing yes/no pairs redeem $1 each
    # before settlement, and the settlement record leaves them out of revenue.
    wins = losses = 0
    net = 0.0
    cursor = ""
    while True:
        s = _signed_get("/trade-api/v2/portfolio/settlements?limit=1000" + (f"&cursor={cursor}" if cursor else ""),
                        key_id, key)
        for x in s.get("settlements", []):
            pnl = (dollars(x, "revenue") + min(count(x, "yes_count"), count(x, "no_count"))
                   - dollars(x, "yes_total_cost") - dollars(x, "no_total_cost") - dollars(x, "fee_cost"))
            net += pnl
            wins += pnl > 0
            losses += pnl < 0
        cursor = s.get("cursor")
        if not cursor:
            break
    print(f"Settled: {wins} won, {losses} lost, net ${net:+,.2f}")

    pos = _signed_get("/trade-api/v2/portfolio/positions?limit=200", key_id, key)
    positions = [p for p in pos.get("market_positions", []) if count(p, "position")]

    # the tracker's read for each race, keyed by the Democratic candidate's market ticker
    races = {}
    try:
        with urllib.request.urlopen(TRACKER, timeout=30) as r:
            for b in json.load(r)["battlegrounds"]:
                head = b.get("headline") or {}
                for race in b["races"]:
                    k = race.get("kalshi")
                    if k:
                        races[(b["state"], race["office"])] = (race, head, b["state_name"])
    except Exception as e:  # the portfolio still prints without the tracker
        print(f"(couldn't load the tracker: {e})")

    print(f"\n{len(positions)} open position(s)")
    for p in positions:
        t = p["ticker"]
        n = count(p, "position")  # contracts: positive = yes, negative = no
        m = json.load(urllib.request.urlopen(f"{HOST}/trade-api/v2/markets/{t}", timeout=30))["market"]
        bid, ask = dollars(m, "yes_bid"), dollars(m, "yes_ask")
        line = f"\n{t}: {'YES' if n > 0 else 'NO'} x{abs(n):g}  ({m.get('yes_sub_title')})  market {bid}/{ask}"
        line += f"  exposure ${dollars(p, 'market_exposure'):,.2f}"
        print(line)
        match = next((v for (st, off), v in races.items() if v[0]["kalshi"]["candidate"] == m.get("yes_sub_title")), None)
        if match:
            race, head, name = match
            print(f"  {name} {race['office']}: polls D-R {race['margin_d_minus_r']} "
                  f"({race['polls_averaged']} polls, latest {race['latest_poll_end']}); "
                  f"early vote vs 2024 {head.get('value')} ({head.get('basis')})")


if __name__ == "__main__":
    main()
