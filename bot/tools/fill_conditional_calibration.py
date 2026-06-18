"""Fill-conditional calibration / adverse-selection waterfall.

Separates the two causes of the model's overconfidence gap using data already
in bot_data.sqlite — no recorder CSV needed:

  POST-time prob   : the model P(our side) when the maker order was first placed
                     (decisions table, action='post')
  FILL-time prob   : the model P(our side) captured at the fill (trades.fair_p_at_entry,
                     sourced from last_fv <=1s before fill)
  REALIZED         : did our side actually win (oracle-true outcome)

  adverse_selection = POST - FILL     (price/model drifted against us while resting)
  overconfidence    = FILL - REALIZED (model is wrong even at the moment of fill)

Restricted to oracle-true post-paper-fix trades (entry_ts >= 2026-06-17 09:40 UTC),
so grading is trustworthy. READ-ONLY.
"""
from __future__ import annotations

import bisect
import csv
import datetime as dt
import sqlite3
import sys

PAPER_FIX = dt.datetime(2026, 6, 17, 9, 40, 54, tzinfo=dt.timezone.utc).timestamp()
WINDOW_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}


def run(db_path: str = "bot_data.sqlite") -> None:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    trades = [dict(r) for r in c.execute(
        "select * from trades where resolved=1 and entry_ts>=? "
        "and resolution_outcome in ('UP','DOWN')", (PAPER_FIX,))]

    rows = []
    no_signal = 0
    for t in trades:
        # earliest 'post' decision on this window for our side, at/before the fill
        d = c.execute(
            "select fair_p, ts from decisions where market_id=? and action='post' "
            "and outcome=? and ts<=? order by ts asc limit 1",
            (t["market_id"], t["outcome_side"], t["entry_ts"] + 1.0)).fetchone()
        signal_p = d["fair_p"] if d and d["fair_p"] is not None else None
        if signal_p is None:
            no_signal += 1
        rows.append({
            "win": 1 if t["outcome_side"] == t["resolution_outcome"] else 0,
            "fill_p": t["fair_p_at_entry"],
            "signal_p": signal_p,
            "px": t["executable_price"] or 0.0,
            "window": t["window"],
        })

    def waterfall(name, rs):
        rs_s = [r for r in rs if r["signal_p"] is not None]
        if not rs:
            return
        n = len(rs)
        wr = sum(r["win"] for r in rs) / n
        fill = sum(r["fill_p"] for r in rs) / n
        line = (f"  {name:<10} n={n:<3}  "
                f"FILL_p={fill*100:5.1f}%  REALIZED={wr*100:5.1f}%  "
                f"overconf(fill-real)={ (fill-wr)*100:+5.1f}pt")
        if rs_s:
            sig = sum(r["signal_p"] for r in rs_s) / len(rs_s)
            fill_s = sum(r["fill_p"] for r in rs_s) / len(rs_s)
            line += (f"  | signalP={sig*100:5.1f}% (n={len(rs_s)})  "
                     f"adverse(sig-fill)={ (sig-fill_s)*100:+5.1f}pt")
        print(line)

    print(f"oracle-true post-fix trades: {len(rows)}  "
          f"(with a matched POST decision: {len(rows)-no_signal})\n")
    print("=== ADVERSE-SELECTION WATERFALL ===")
    waterfall("ALL", rows)
    for w in ("5m", "15m"):
        waterfall(w, [r for r in rows if r["window"] == w])

    print("\n=== FILL-TIME CALIBRATION (model P(our side) decile -> realized WR) ===")
    for lo in [i / 10 for i in range(10)]:
        rs = [r for r in rows if lo <= r["fill_p"] < lo + 0.1]
        if rs:
            wr = sum(r["win"] for r in rs) / len(rs)
            print(f"  fill_p {lo:.1f}-{lo+0.1:.1f}: n={len(rs):<3} "
                  f"realized WR={wr*100:5.1f}%  (overconf {(lo+0.05-wr)*100:+5.1f}pt)")


def _slug_meta(slug: str):
    """(window, open_ts, close_ts) from 'btc-updown-<win>-<open_ts>'."""
    parts = slug.split("-")
    win, open_ts = parts[-2], int(parts[-1])
    return win, open_ts, open_ts + WINDOW_SECONDS.get(win, 300)


def _load_books(path: str, slugs: set):
    """{(slug, side): ([ts...], [mid...])} for our trade slugs only (streamed,
    so the 148MB file never lands in memory). mid = (bid+ask)/2 of the real book."""
    series: dict = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row["slug"] not in slugs:
                continue
            try:
                bid, ask = float(row["best_bid"]), float(row["best_ask"])
            except (TypeError, ValueError):
                continue
            if not (0 < bid < 1 and 0 < ask < 1 and ask >= bid):
                continue
            series.setdefault((row["slug"], row["side"]), ([], [])
                              )[0].append(float(row["ts"]))
            series[(row["slug"], row["side"])][1].append((bid + ask) / 2.0)
    return series


def _at(series, key, t):
    """Real-book mid for `key` at-or-before time t, or None."""
    sk = series.get(key)
    if not sk:
        return None
    ts, mids = sk
    i = bisect.bisect_right(ts, t) - 1
    return mids[i] if i >= 0 else None


def run_live_book(db_path: str = "bot_data.sqlite",
                  books_path: str = "data/books.csv") -> None:
    """Live-book adverse-selection + market-vs-model calibration, using the REAL
    Polymarket book prints the recorder captured. READ-ONLY."""
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    trades = [dict(r) for r in c.execute(
        "select * from trades where resolved=1 and entry_ts>=? "
        "and resolution_outcome in ('UP','DOWN')", (PAPER_FIX,))]
    slugs = {t["slug"] for t in trades}
    print(f"loading real book prints for {len(slugs)} windows from {books_path} ...")
    series = _load_books(books_path, slugs)
    print(f"  windows with usable book series: "
          f"{len({k[0] for k in series})}\n")

    rows = []
    for t in trades:
        side = t["outcome_side"]
        key = (t["slug"], side)
        fill_ts = t["entry_ts"]
        _, _, close_ts = _slug_meta(t["slug"])
        m_fill = _at(series, key, fill_ts)
        m_60 = _at(series, key, fill_ts + 60.0)
        m_close = _at(series, key, close_ts)
        rows.append({
            "win": 1 if side == t["resolution_outcome"] else 0,
            "model_p": t["fair_p_at_entry"],
            "fill_px": t["executable_price"] or 0.0,
            "mkt_fill": m_fill, "mkt_60": m_60, "mkt_close": m_close,
            "window": t["window"],
        })

    def blk(name, rs):
        rs = [r for r in rs if r["mkt_fill"] is not None]
        if not rs:
            print(f"  {name:<6} (no book coverage)")
            return
        n = len(rs)
        wr = sum(r["win"] for r in rs) / n
        mdl = sum(r["model_p"] for r in rs) / n
        mkt = sum(r["mkt_fill"] for r in rs) / n
        brier_m = sum((r["model_p"] - r["win"]) ** 2 for r in rs) / n
        brier_k = sum((r["mkt_fill"] - r["win"]) ** 2 for r in rs) / n
        # adverse selection: market move AGAINST our long after we fill (mid falls)
        d60 = [r["mkt_fill"] - r["mkt_60"] for r in rs if r["mkt_60"] is not None]
        dcl = [r["mkt_fill"] - r["mkt_close"] for r in rs if r["mkt_close"] is not None]
        adv60 = sum(d60) / len(d60) * 100 if d60 else float("nan")
        advcl = sum(dcl) / len(dcl) * 100 if dcl else float("nan")
        print(f"  {name:<6} n={n:<3} realized={wr*100:5.1f}%  "
              f"model={mdl*100:5.1f}% (Brier {brier_m:.3f})  "
              f"MARKET={mkt*100:5.1f}% (Brier {brier_k:.3f})  "
              f"| post-fill mid drift +60s={adv60:+.1f}pt  to-close={advcl:+.1f}pt")

    print("=== MARKET-IMPLIED vs MODEL vs REALIZED (real book at fill) ===")
    print("  (lower Brier = better forecaster; positive drift = market moved "
          "AGAINST us after fill = adverse selection)")
    blk("ALL", rows)
    for w in ("5m", "15m"):
        blk(w, [r for r in rows if r["window"] == w])

    print("\n=== MARKET-IMPLIED calibration (market mid at fill decile -> realized) ===")
    cov = [r for r in rows if r["mkt_fill"] is not None]
    for lo in [i / 10 for i in range(10)]:
        rs = [r for r in cov if lo <= r["mkt_fill"] < lo + 0.1]
        if rs:
            wr = sum(r["win"] for r in rs) / len(rs)
            print(f"  market {lo:.1f}-{lo+0.1:.1f}: n={len(rs):<3} "
                  f"realized WR={wr*100:5.1f}%  (gap {(lo+0.05-wr)*100:+5.1f}pt)")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--live-book":
        run_live_book(*(args[1:] or []))
    else:
        run(args[0] if args else "bot_data.sqlite")
