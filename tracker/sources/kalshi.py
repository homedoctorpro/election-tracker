"""Kalshi prices for the Democratic candidate in each tracked Senate and governor race.

Public market data from Kalshi's trade API (no account or key). Each race is one market:
"Will <Democratic candidate> win", priced in dollars from 0 to 1, which reads as the market's
probability. The midpoint of the best bid and ask is used; the last trade price when the book
is one-sided.

Tickers are listed by hand because Kalshi's series names aren't uniform (SENATENC, SENATEOHS
for Ohio's special election, GOVPARTYKS-27 for Kansas's January inauguration). If a race's
market is renamed, update MARKETS.
"""
from ..common import fetch_json

API = "https://api.elections.kalshi.com/trade-api/v2/markets/"
MARKETS = {
    ("NC", "Senate"): "SENATENC-26-D", ("GA", "Senate"): "SENATEGA-26-D", ("MI", "Senate"): "SENATEMI-26-D",
    ("ME", "Senate"): "SENATEME-26-D", ("IA", "Senate"): "SENATEIA-26-D", ("TX", "Senate"): "SENATETX-26-D",
    ("KS", "Senate"): "SENATEKS-26-D", ("FL", "Senate"): "SENATEFLS-26-D", ("OH", "Senate"): "SENATEOHS-26-D",
    ("AZ", "Governor"): "GOVPARTYAZ-26-D", ("GA", "Governor"): "GOVPARTYGA-26-D", ("MI", "Governor"): "GOVPARTYMI-26-D",
    ("NV", "Governor"): "GOVPARTYNV-26-D", ("PA", "Governor"): "GOVPARTYPA-26-D", ("WI", "Governor"): "GOVPARTYWI-26-D",
    ("IA", "Governor"): "GOVPARTYIA-26-D", ("OH", "Governor"): "GOVPARTYOH-26-D", ("ME", "Governor"): "GOVPARTYME-26-D",
    ("KS", "Governor"): "GOVPARTYKS-27-D", ("FL", "Governor"): "GOVPARTYFL-26-D", ("TX", "Governor"): "GOVPARTYTX-26-D",
}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def price(row):
    """Midpoint of bid and ask, or the last trade when the book is one-sided."""
    bid, ask, last = row
    return round((bid + ask) / 2, 3) if bid and ask else last


def collect(con, day: str) -> dict:
    rows, missing = [], []
    for (st, office), ticker in MARKETS.items():
        m = (fetch_json(API + ticker) or {}).get("market")
        if not m:
            missing.append(ticker)
            continue
        rows.append((day, st, office, ticker, m.get("yes_sub_title"), _f(m.get("yes_bid_dollars")),
                     _f(m.get("yes_ask_dollars")), _f(m.get("last_price_dollars")), _f(m.get("volume_fp")),
                     _f(m.get("volume_24h_fp"))))
    con.execute("DELETE FROM kalshi_prices WHERE snapshot_date=?", (day,))
    con.executemany("INSERT INTO kalshi_prices VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    if missing and not rows:
        raise RuntimeError(f"Kalshi returned none of the tracked markets: {missing}")
    return {"markets": len(rows), "missing": missing}
