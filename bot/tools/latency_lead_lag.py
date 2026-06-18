"""Phase-0 gate for the 15m-close latency play: does the Polymarket book LAG the
Chainlink resolving price in the final seconds?

Uses only recorded data (no creds):
  data/prices_chainlink.csv  -- RTDS Chainlink BTC/USD (the resolution price), ~1s
  data/books.csv             -- Polymarket best_bid/ask per window+side, ~1s
  data/outcomes.csv          -- real oracle outcome per window (where captured)

For each 15m window and each offset before close, ask:
  - has Chainlink already DECIDED the outcome (sign of price-vs-open) and does that
    early call match the FINAL outcome?  (signal reliability that early)
  - meanwhile, what does the book charge (best_ask) for that winning side?
    A high ask gap => the book hasn't repriced => exploitable IF you're faster.

Headline metric per offset = EV of "buy the Chainlink-implied side at the book ask":
  EV = P(early call == final) * 1  -  avg_ask  -  taker_fee(avg_ask)
Positive & material => a lag edge exists. ~0/negative => book tracks Chainlink => dead.

NOTE: prices_chainlink.csv is the RTDS RELAY (what MMs likely also see). This measures
the book's lag vs RTDS — the FLOOR of the opportunity. Direct Data Streams would only
be faster. If the book doesn't even lag RTDS, direct DS won't save it.  READ-ONLY.
"""
from __future__ import annotations

import bisect
import csv
import sys

WINDOW = 900           # 15m
FEE_RATE = 0.072
OFFSETS = [60, 30, 15, 10, 5, 2]   # seconds before close
PRICES = "data/prices_chainlink.csv"
BOOKS = "data/books.csv"
OUTCOMES = "data/outcomes.csv"


def _load_prices(path):
    ts, px = [], []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                ts.append(float(r["ts"])); px.append(float(r["price"]))
            except (TypeError, ValueError):
                pass
    pairs = sorted(zip(ts, px))
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _load_books(path, want_prefix="15m"):
    """{(slug,side): ([ts],[bid],[ask],[ask_size])} for 15m windows."""
    s = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if want_prefix not in r["slug"]:
                continue
            try:
                bid, ask = float(r["best_bid"]), float(r["best_ask"])
                asz = float(r["ask_size"])
            except (TypeError, ValueError):
                continue
            if not (0 < bid < 1 and 0 < ask <= 1 and ask >= bid):
                continue
            k = (r["slug"], r["side"])
            e = s.setdefault(k, ([], [], [], []))
            e[0].append(float(r["ts"])); e[1].append(bid)
            e[2].append(ask); e[3].append(asz)
    return s


def _load_outcomes(path):
    out = {}
    try:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                out[r["slug"]] = r["outcome"]
    except FileNotFoundError:
        pass
    return out


def _at(ts, vals, t):
    i = bisect.bisect_right(ts, t) - 1
    return vals[i] if i >= 0 else None


def _book_at(books, slug, side, t):
    """(book_ts, bid, ask, ask_size) at-or-before t, or None."""
    e = books.get((slug, side))
    if not e:
        return None
    i = bisect.bisect_right(e[0], t) - 1
    if i < 0:
        return None
    return e[0][i], e[1][i], e[2][i], e[3][i]


def _ask_at(books, slug, side, t):
    b = _book_at(books, slug, side, t)
    return b[2] if b else None


def main():
    pts, ppx = _load_prices(PRICES)
    books = _load_books(BOOKS)
    outcomes = _load_outcomes(OUTCOMES)
    slugs = sorted({k[0] for k in books})
    print(f"15m windows: {len(slugs)}  | chainlink ticks: {len(pts)}  | "
          f"oracle outcomes: {len(outcomes)}\n")

    # per offset: accumulate
    agg = {o: {"n": 0, "agree": 0, "ask_sum": 0.0, "ask_n": 0,
               "margin_bps_sum": 0.0} for o in OFFSETS}
    final_check = {"n": 0, "cl_close_eq_oracle": 0}

    for slug in slugs:
        try:
            open_ts = int(slug.split("-")[-1])
        except ValueError:
            continue
        close_ts = open_ts + WINDOW
        ptb = _at(pts, ppx, open_ts)          # price to beat (chainlink at open)
        if ptb is None:
            continue
        cl_close = _at(pts, ppx, close_ts)
        if cl_close is None:
            continue
        cl_final = "UP" if cl_close > ptb else "DOWN"
        final = outcomes.get(slug, cl_final)  # prefer oracle truth
        if slug in outcomes:
            final_check["n"] += 1
            final_check["cl_close_eq_oracle"] += (cl_final == final)

        for o in OFFSETS:
            t = close_ts - o
            clp = _at(pts, ppx, t)
            if clp is None:
                continue
            implied = "UP" if clp > ptb else "DOWN"
            ask = _ask_at(books, slug, implied, t)   # cost to BUY the implied side
            a = agg[o]
            a["n"] += 1
            a["agree"] += (implied == final)
            a["margin_bps_sum"] += abs(clp - ptb) / ptb * 1e4
            if ask is not None:
                a["ask_sum"] += ask; a["ask_n"] += 1

    if final_check["n"]:
        print(f"SANITY: Chainlink-at-close matches oracle on "
              f"{final_check['cl_close_eq_oracle']}/{final_check['n']} windows "
              f"({100*final_check['cl_close_eq_oracle']/final_check['n']:.0f}%) "
              f"-- validates price-to-beat + decode\n")

    print("offset | n   | early-call==final | avg margin | avg book ASK (winner-implied) | "
          "EV/contract (buy implied @ask)")
    print("-" * 104)
    for o in OFFSETS:
        a = agg[o]
        if not a["n"]:
            continue
        p_right = a["agree"] / a["n"]
        margin = a["margin_bps_sum"] / a["n"]
        ask = a["ask_sum"] / a["ask_n"] if a["ask_n"] else float("nan")
        fee = FEE_RATE * ask * (1 - ask) if a["ask_n"] else 0.0
        ev = p_right * 1.0 - ask - fee if a["ask_n"] else float("nan")
        print(f"T-{o:<4}| {a['n']:<4}| {p_right*100:6.1f}%           | "
              f"{margin:6.1f}bps  | {ask:.3f} (n={a['ask_n']})              | "
              f"{ev:+.3f}")
    print("\nRead: EV materially > 0 => book lags Chainlink => latency edge is REAL "
          "(direct DS only widens it).\nEV ~0 or < 0 => book tracks Chainlink => "
          "the play is dead; do NOT build the DS integration.")


def depth(stale_after_s: float = 3.0):
    """Execution-feasibility probe: is there fillable SIZE at the winning-side ask
    near close, and is the recorded book FRESH there? A high stale fraction means
    either the market pulls quotes near close (act earlier) or the link drops book
    updates (a VPS fixes it) — re-run this on a clean connection to disambiguate."""
    import statistics as st
    pts, ppx = _load_prices(PRICES)
    books = _load_books(BOOKS)
    slugs = sorted({k[0] for k in books})
    print(f"15m windows: {len(slugs)}  (stale threshold {stale_after_s:.0f}s)\n")
    print("offset | cov     | stale book      | median ask_size | median $ at ask | "
          "median spread")
    print("-" * 88)
    for o in OFFSETS:
        cov = stale = 0
        sizes, notionals, spreads = [], [], []
        for s in slugs:
            try:
                open_ts = int(s.split("-")[-1])
            except ValueError:
                continue
            close_ts = open_ts + WINDOW
            ptb = _at(pts, ppx, open_ts)
            clp = _at(pts, ppx, close_ts - o)
            if ptb is None or clp is None:
                continue
            implied = "UP" if clp > ptb else "DOWN"
            b = _book_at(books, s, implied, close_ts - o)
            if b is None:
                continue
            cov += 1
            bts, bid, ask, asz = b
            if (close_ts - o) - bts > stale_after_s:
                stale += 1
            sizes.append(asz); notionals.append(asz * ask); spreads.append(ask - bid)
        if not cov:
            continue
        print(f"T-{o:<4}| {cov:>3}/{len(slugs):<3} | {stale:>3} ({100*stale/cov:4.0f}%)     | "
              f"{st.median(sizes):>7.0f} sh     | ${st.median(notionals):>6.1f}        | "
              f"{st.median(spreads)*100:.1f}c")
    print("\nRead: ample size + LOW stale% => the +EV asks are fillable (edge real). "
          "\nHigh stale% => act earlier (T-30/T-60) and/or re-run on a VPS to tell "
          "quote-pull from link-drop.")


if __name__ == "__main__":
    if "--depth" in sys.argv:
        depth()
    else:
        main()
