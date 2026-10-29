"""Place Kalshi limit orders that you choose. Previews by default; nothing is sent without --go.

You pick every order: ticker, side, contract count and the most you'll pay per contract. The
script only checks each one against the live order book, prints the cost, and with --go sends
it as a limit order after you type "yes". The key loads like kalshi_portfolio.py's (environment,
else Documents\\Tallysheet .env\\.env.local).

Preview (no key needed):
    python scripts\\kalshi_order.py KXHOUSERACE-IN05-26-D yes 10 0.19
Send:
    python scripts\\kalshi_order.py KXHOUSERACE-IN05-26-D yes 10 0.19 --go
Several orders at once: repeat the four fields, e.g.
    python scripts\\kalshi_order.py KXHOUSERACE-IN05-26-D yes 10 0.19 KXHOUSERACE-NE01-26-D yes 5 0.26

Max price is in dollars per contract (0.01 to 0.99). A limit order fills at that price or
better; whatever doesn't fill rests on the book until it fills or you cancel it on Kalshi.

Needs the `cryptography` package (pip install cryptography).
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

from kalshi_portfolio import HOST, dollars, load_key, sign


def _signed(method, path, key_id, key, body=None):
    """Signed request, using kalshi_portfolio's signer (Ed25519 or RSA key)."""
    ts = str(int(time.time() * 1000))
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(HOST + path, data=data, method=method, headers={
        "KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sign(key, ts, method, path),
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def parse(args):
    if not args or len(args) % 4:
        sys.exit(__doc__)
    orders = []
    for i in range(0, len(args), 4):
        ticker, side, n, price = args[i:i + 4]
        side = side.lower()
        if side not in ("yes", "no"):
            sys.exit(f"{ticker}: side must be yes or no, not {side!r}")
        n, price = int(n), float(price)
        if n < 1 or not 0.01 <= price <= 0.99:
            sys.exit(f"{ticker}: count must be >= 1 and max price between 0.01 and 0.99")
        orders.append((ticker.upper(), side, n, price))
    return orders


def main():
    args = sys.argv[1:]
    go = "--go" in args
    orders = parse([a for a in args if a != "--go"])

    total = 0.0
    for ticker, side, n, price in orders:
        m = json.load(urllib.request.urlopen(f"{HOST}/trade-api/v2/markets/{ticker}", timeout=30))["market"]
        ask = dollars(m, f"{side}_ask")
        status = m.get("status")
        print(f"{ticker} ({m.get('yes_sub_title')}): BUY {side.upper()} x{n} at up to ${price:.3f}"
              f"  | {side} ask now ${ask:.3f}  | market {status}"
              f"  | max cost ${n * price:,.2f}, pays ${n:,.2f} if right")
        if ask and price < ask:
            print("  below the current ask, so it will rest on the book rather than fill now")
        total += n * price
    print(f"\nTotal max cost: ${total:,.2f}")

    if not go:
        print("Preview only. Add --go to send these orders.")
        return
    if input('Type "yes" to send these orders: ').strip().lower() != "yes":
        print("Nothing sent.")
        return

    key_id, key = load_key()

    for ticker, side, n, price in orders:
        # V2 orders quote the YES book only: buying YES is a bid at the price, buying NO is
        # an ask (sell YES) at 1 - price.
        body = {"ticker": ticker, "client_order_id": str(uuid.uuid4()),
                "side": "bid" if side == "yes" else "ask", "count": f"{n}.00",
                "price": f"{price if side == 'yes' else 1 - price:.4f}",
                "time_in_force": "good_till_canceled", "self_trade_prevention_type": "taker_at_cross"}
        try:
            o = _signed("POST", "/trade-api/v2/portfolio/events/orders", key_id, key, body)
            print(f"{ticker}: order {o.get('order_id')} filled {o.get('fill_count')}, "
                  f"resting {o.get('remaining_count')}"
                  + (f" (avg ${o['average_fill_price']})" if o.get("average_fill_price") else ""))
        except urllib.error.HTTPError as e:
            print(f"{ticker}: rejected ({e.code}) {e.read().decode()[:300]}")


if __name__ == "__main__":
    main()
