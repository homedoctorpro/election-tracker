"""Your Kalshi balance, overall win/loss on settled markets, and open positions next to the
tracker's poll and early-vote reads.

Read-only: it only sends GET requests (balance, positions, market prices) and never places or
cancels orders. Run it yourself; it reads your API key from environment variables, so the key
stays on your machine.

Setup (PowerShell), once per session:
    $env:KALSHI_KEY_ID = "<your API key id>"
    $env:KALSHI_KEY_FILE = "C:\\path\\to\\kalshi-private-key.pem"
Then:
    python scripts\\kalshi_portfolio.py

Needs the `cryptography` package (pip install cryptography).
"""
import base64
import json
import os
import sys
import time
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

HOST = "https://api.elections.kalshi.com"
TRACKER = "https://homedoctorpro.github.io/election-tracker/data.json"


def _signed_get(path, key_id, key):
    """GET with Kalshi's request signing: RSA-PSS over timestamp + method + path."""
    ts = str(int(time.time() * 1000))
    sig = key.sign((ts + "GET" + path.split("?")[0]).encode(),
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                   hashes.SHA256())
    req = urllib.request.Request(HOST + path, headers={
        "KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _num(d, *keys):
    """First numeric value among `keys` (Kalshi reports some fields in cents, newer ones in dollars)."""
    for k in keys:
        if d.get(k) is not None:
            try:
                return float(d[k]) / (100 if not k.endswith(("_dollars", "_fp")) else 1)
            except (TypeError, ValueError):
                pass
    return None


def main():
    key_id, key_file = os.environ.get("KALSHI_KEY_ID"), os.environ.get("KALSHI_KEY_FILE")
    if not key_id or not key_file:
        sys.exit("Set KALSHI_KEY_ID and KALSHI_KEY_FILE first (see the top of this script).")
    with open(key_file, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)

    bal = _signed_get("/trade-api/v2/portfolio/balance", key_id, key)
    print(f"Balance: ${_num(bal, 'balance_dollars', 'balance') or 0:,.2f}")

    # overall win/loss across settled markets: payout minus what the contracts cost
    wins = losses = 0
    net = 0.0
    cursor = ""
    while True:
        s = _signed_get("/trade-api/v2/portfolio/settlements?limit=200" + (f"&cursor={cursor}" if cursor else ""),
                        key_id, key)
        for x in s.get("settlements", []):
            cost = (_num(x, "yes_total_cost_dollars", "yes_total_cost") or 0) + \
                   (_num(x, "no_total_cost_dollars", "no_total_cost") or 0)
            pnl = (_num(x, "revenue_dollars", "revenue") or 0) - cost
            net += pnl
            wins += pnl > 0
            losses += pnl < 0
        cursor = s.get("cursor")
        if not cursor:
            break
    print(f"Settled: {wins} won, {losses} lost, net ${net:+,.2f} (before fees)")

    pos = _signed_get("/trade-api/v2/portfolio/positions?limit=200", key_id, key)
    positions = [p for p in pos.get("market_positions", []) if _num(p, "position_fp", "position")]

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
        n = _num(p, "position_fp", "position") * (1 if p.get("position_fp") else 100)  # contracts; + yes, - no
        m = json.load(urllib.request.urlopen(f"{HOST}/trade-api/v2/markets/{t}", timeout=30))["market"]
        bid, ask = _num(m, "yes_bid_dollars"), _num(m, "yes_ask_dollars")
        line = f"\n{t}: {'YES' if n > 0 else 'NO'} x{abs(n):g}  ({m.get('yes_sub_title')})  market {bid}/{ask}"
        line += f"  exposure ${_num(p, 'market_exposure_dollars', 'market_exposure') or 0:,.2f}"
        print(line)
        match = next((v for (st, off), v in races.items() if v[0]["kalshi"]["candidate"] == m.get("yes_sub_title")), None)
        if match:
            race, head, name = match
            print(f"  {name} {race['office']}: polls D-R {race['margin_d_minus_r']} "
                  f"({race['polls_averaged']} polls, latest {race['latest_poll_end']}); "
                  f"early vote vs 2024 {head.get('value')} ({head.get('basis')})")


if __name__ == "__main__":
    main()
