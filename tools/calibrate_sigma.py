"""Sigma calibration sweep against REAL Binance BTC price history.

Why this works: the backtester scores modelled P(Up)=Phi(z) against the actual
resolved outcome of each window. The synthetic order book only affects PnL/edge,
NOT calibration — so feeding real prices and sweeping sigma_scale tells us which
scale makes Phi(z) match reality. Lowest calibration_error wins (Claude.md §11
step 1: get sigma calibrated FIRST).

Usage:
    python -m tools.calibrate_sigma --window 15m --hours 12
"""

from __future__ import annotations

import argparse
import math
import time

import httpx

from bot.config import WINDOW_SECONDS, load_settings
from bot.utils import phi


def fetch_binance_1s(hours: float) -> list:
    """Real 1-second BTCUSDT closes for the last `hours`. Binance caps klines at
    1000/call (=1000s), so we page backwards from now."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(hours * 3600 * 1000)
    out = []
    with httpx.Client(timeout=30) as c:
        cur = start_ms
        pages = 0
        while cur < end_ms:
            batch = None
            for attempt in range(5):  # flaky network: retry with backoff
                try:
                    r = c.get("https://api.binance.com/api/v3/klines", params={
                        "symbol": "BTCUSDT", "interval": "1s",
                        "startTime": cur, "limit": 1000})
                    r.raise_for_status()
                    batch = r.json()
                    break
                except Exception as e:  # noqa: BLE001
                    print(f"  page at {cur} failed ({type(e).__name__}), "
                          f"retry {attempt+1}/5 ...")
                    time.sleep(2 * (attempt + 1))
            if not batch:
                print(f"  giving up page at {cur}, skipping ahead")
                cur += 1000 * 1000  # skip this 1000s page, keep going
                continue
            for k in batch:
                # kline: [openTime, open, high, low, close, vol, closeTime, ...]
                out.append((k[0] / 1000.0, float(k[4])))
            cur = batch[-1][0] + 1000
            pages += 1
            if pages % 10 == 0:
                print(f"  ... {len(out)} ticks so far")
            if len(batch) < 1000:
                break
    # de-dup + sort
    seen, dedup = set(), []
    for ts, px in sorted(out):
        if ts not in seen:
            seen.add(ts)
            dedup.append((ts, px))
    return dedup


def calibration_samples(prices, window, scale, lookback_s=1800.0,
                        step_s=15.0):
    """Direct calibration: for every non-overlapping window in the real series,
    sample Phi(z) at points inside the entry gate and pair each with the actual
    Up/Down outcome. Independent of any order book — pure model-vs-reality.

    Returns list of (modelled_p_up, up_won_bool)."""
    base = load_settings()
    win_s = WINDOW_SECONDS[window]
    lo_min, hi_min = base.entry_windows[window]
    ts_index = {round(t): i for i, (t, _) in enumerate(prices)}
    first_ts, last_ts = prices[0][0], prices[-1][0]

    def sigma_at(idx_now, price_now):
        start = prices[idx_now][0] - lookback_s
        seg = [p for t, p in prices[max(0, idx_now - int(lookback_s)):idx_now + 1]
               if t >= start]
        if len(seg) < base.sigma_min_samples:
            return None
        rets = [math.log(seg[i] / seg[i - 1]) for i in range(1, len(seg))
                if seg[i - 1] > 0]
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return price_now * math.sqrt(var * win_s) * scale

    samples = []
    wstart = math.ceil((first_ts + lookback_s) / win_s) * win_s
    while wstart + win_s <= last_ts:
        wclose = wstart + win_s
        i_open = ts_index.get(round(wstart))
        i_close = ts_index.get(round(wclose - 1))
        if i_open is None or i_close is None:
            wstart += win_s
            continue
        open_ref = prices[i_open][1]
        up_won = prices[i_close][1] >= open_ref      # ties -> Up
        t = wclose - hi_min * 60.0
        while t <= wclose - lo_min * 60.0:
            i_now = ts_index.get(round(t))
            if i_now is not None:
                sig = sigma_at(i_now, prices[i_now][1])
                if sig and sig > 0:
                    remaining = (wclose - t) / win_s
                    z = (prices[i_now][1] - open_ref) / (sig * math.sqrt(remaining))
                    samples.append((phi(z), up_won))
            t += step_s
        wstart += win_s
    return samples


def buckets_and_error(samples, nb=10):
    rows, werr, n = [], 0.0, 0
    for b in range(nb):
        lo, hi = b / nb, (b + 1) / nb
        s = [(p, w) for p, w in samples
             if lo <= p < hi or (b == nb - 1 and p >= hi)]
        if not s:
            continue
        mp = sum(p for p, _ in s) / len(s)
        rf = sum(1 for _, w in s if w) / len(s)
        rows.append((f"{lo:.1f}-{hi:.1f}", len(s), mp, rf))
        werr += abs(mp - rf) * len(s)
        n += len(s)
    return rows, (werr / n if n else 0.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="15m", choices=["5m", "15m", "1h"])
    ap.add_argument("--hours", type=float, default=12.0)
    ap.add_argument("--scales", default="0.5,0.7,0.85,1.0,1.2,1.5")
    args = ap.parse_args()

    print(f"fetching {args.hours}h of real Binance 1s BTC data ...")
    prices = fetch_binance_1s(args.hours)
    span_h = (prices[-1][0] - prices[0][0]) / 3600 if prices else 0
    print(f"  got {len(prices)} ticks over {span_h:.1f}h\n")

    scales = [float(x) for x in args.scales.split(",")]
    print(f"{'scale':>6} {'samples':>8} {'calib_err':>10}   (lower = better)")
    print("-" * 42)
    results = {}
    for sc in scales:
        smp = calibration_samples(prices, args.window, sc)
        _, err = buckets_and_error(smp)
        results[sc] = smp
        print(f"{sc:>6.2f} {len(smp):>8} {err:>10.4f}")

    best = min(scales, key=lambda sc: buckets_and_error(results[sc])[1])
    rows, err = buckets_and_error(results[best])
    print(f"\nBEST sigma_scale[{args.window}] = {best}  (calib_err {err:.4f})")
    print("calibration at best scale (modelled P_up vs realised Up freq):")
    for label, n, mp, rf in rows:
        flag = "" if abs(mp - rf) < 0.12 else "  <-- off"
        print(f"  {label}  n={n:>4}  modelled={mp:.3f}  realised={rf:.3f}{flag}")
    print("\nNote: samples within a window are autocorrelated and this is one "
          "regime/short span — treat as a first pass, re-run across more hours.")


if __name__ == "__main__":
    main()
