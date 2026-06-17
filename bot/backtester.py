"""Backtester (Claude.md §9) — the gatekeeper for live money. It must not flatter.

- Replays resolution-feed prices (CSV: ts,price — Chainlink history for 15m,
  Binance for 1h) and, where available, book data. Without recorded books it
  synthesises a book as fair + noise + spread: that validates MECHANICS and
  CALIBRATION, not edge — only recorded books can measure real edge. The report
  says which mode produced it.
- Maker fills only by default: an order fills only when the book trades through
  its price, with a queue/non-fill penalty. No instant mid fills, ever.
- EV and PnL are net of the modelled fee. A gross-positive / net-negative result
  is a FAIL and is surfaced as such.
"""

from __future__ import annotations

import csv
import logging
import math
import random
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .config import WINDOW_SECONDS, Settings
from .execution.orders import OrderPlanner, taker_fee
from .models import BookTop, FairValue, MarketInfo, Outcome, ResolutionFeed
from .utils import clamp, phi

log = logging.getLogger(__name__)


@dataclass
class SyntheticBookParams:
    spread: float = 0.04        # full spread on the contract
    noise_sd: float = 0.02      # market mispricing vs model, in probability units
    depth: float = 100.0        # contracts at top of book


@dataclass
class BacktestTrade:
    window_start: float
    outcome: Outcome
    entry_price: float
    modelled_p: float
    z: float
    fee: float
    fill_type: str              # maker | taker
    won: Optional[bool] = None

    @property
    def gross_pnl(self) -> float:
        return (1.0 if self.won else 0.0) - self.entry_price

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fee


@dataclass
class BacktestReport:
    mode: str
    windows: int = 0
    decisions: int = 0
    posts: int = 0
    trades: List[BacktestTrade] = field(default_factory=list)
    calibration_samples: List[Tuple[float, bool]] = field(default_factory=list)

    @property
    def fill_rate(self) -> float:
        return len(self.trades) / self.posts if self.posts else 0.0

    @property
    def gross_pnl(self) -> float:
        return sum(t.gross_pnl for t in self.trades)

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def net_ev_per_trade(self) -> float:
        return self.net_pnl / len(self.trades) if self.trades else 0.0

    @property
    def z_distribution(self) -> dict:
        zs = sorted(abs(t.z) for t in self.trades if math.isfinite(t.z))
        if not zs:
            return {}
        return {"min": zs[0], "median": zs[len(zs) // 2], "max": zs[-1]}

    def calibration_buckets(self, buckets: int = 10) -> List[dict]:
        out = []
        for b in range(buckets):
            lo, hi = b / buckets, (b + 1) / buckets
            s = [(p, r) for p, r in self.calibration_samples
                 if lo <= p < hi or (b == buckets - 1 and p == 1.0)]
            if s:
                out.append({"bucket": f"{lo:.1f}-{hi:.1f}", "n": len(s),
                            "mean_p": sum(p for p, _ in s) / len(s),
                            "realised": sum(1 for _, r in s if r) / len(s)})
        return out

    @property
    def calibration_error(self) -> float:
        """Sample-weighted mean |modelled - realised| across buckets."""
        bk = self.calibration_buckets()
        n = sum(b["n"] for b in bk)
        if not n:
            return 0.0
        return sum(abs(b["mean_p"] - b["realised"]) * b["n"] for b in bk) / n

    @property
    def verdict(self) -> str:
        if not self.trades:
            return "NO-TRADES"
        if self.gross_pnl > 0 and self.net_pnl <= 0:
            return ("FAIL: gross-positive but NET-OF-FEE NEGATIVE — fees eat the "
                    "edge; do not go live")
        if self.net_pnl <= 0:
            return "FAIL: net-of-fee PnL non-positive"
        return "PASS (net-of-fee positive; verify calibration + sample size)"

    def summary(self) -> str:
        lines = [
            f"mode:            {self.mode}",
            f"windows:         {self.windows}",
            f"decisions:       {self.decisions}",
            f"orders posted:   {self.posts}",
            f"fills (trades):  {len(self.trades)}  (fill rate {self.fill_rate:.1%})",
            f"gross PnL:       {self.gross_pnl:+.3f} /contract-units",
            f"net PnL:         {self.net_pnl:+.3f}",
            f"net EV/trade:    {self.net_ev_per_trade:+.4f}",
            f"calibration err: {self.calibration_error:.3f}",
            f"|z| at entry:    {self.z_distribution}",
            f"verdict:         {self.verdict}",
            "calibration:",
        ]
        for b in self.calibration_buckets():
            lines.append(f"  {b['bucket']}  n={b['n']:>4}  modelled={b['mean_p']:.3f}"
                         f"  realised={b['realised']:.3f}")
        return "\n".join(lines)


def load_prices_csv(path: str) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or row[0].lower() in ("ts", "timestamp"):
                continue
            try:
                out.append((float(row[0]), float(row[1])))
            except (ValueError, IndexError):
                continue
    out.sort(key=lambda x: x[0])
    return out


def load_recorded_books(path: str) -> dict:
    """Recorded book tops grouped by window-open-ts then side, each sorted by ts:
        {open_ts: {"UP": [(ts, BookTop), ...], "DOWN": [...]}}
    open_ts is parsed from the slug suffix (recurring series use
    '<prefix><open_unix_ts>'). Feeds the backtester REAL books per window."""
    out: dict = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            side = row.get("side")
            if side not in ("UP", "DOWN"):
                continue
            try:
                open_ts = int(str(row["slug"]).rsplit("-", 1)[-1])
                ts = float(row["ts"])
            except (ValueError, KeyError, AttributeError):
                continue
            bid = float(row["best_bid"]) if row.get("best_bid") else None
            ask = float(row["best_ask"]) if row.get("best_ask") else None
            top = BookTop(token_id=row.get("token_id", ""), best_bid=bid,
                          best_ask=ask,
                          bid_size=float(row["bid_size"] or 0) if row.get("bid_size") else 0.0,
                          ask_size=float(row["ask_size"] or 0) if row.get("ask_size") else 0.0,
                          as_of=ts)
            out.setdefault(open_ts, {}).setdefault(side, []).append((ts, top))
    for window in out.values():
        for lst in window.values():
            lst.sort(key=lambda x: x[0])
    return out


def generate_gbm_prices(n_seconds: int, start_price: float = 100_000.0,
                        vol_per_sec: float = 2e-4, seed: int = 7,
                        start_ts: float = 1_750_000_000.0) -> List[Tuple[float, float]]:
    """Synthetic 1s GBM series for smoke-testing the pipeline."""
    rng = random.Random(seed)
    out, p = [], start_price
    for i in range(n_seconds):
        p *= math.exp(rng.gauss(0.0, vol_per_sec))
        out.append((start_ts + i, p))
    return out


class Backtester:
    def __init__(self, settings: Settings, window: str = "15m",
                 synthetic: Optional[SyntheticBookParams] = None,
                 non_fill_prob: float = 0.3, eval_step_s: float = 5.0,
                 taker_mode: bool = False, sigma_lookback_s: float = 1800.0,
                 seed: int = 11, recorded_books: Optional[dict] = None):
        self.s = settings
        self.window = window
        self.window_seconds = WINDOW_SECONDS[window]
        self.synthetic = synthetic or SyntheticBookParams()
        self.non_fill_prob = non_fill_prob
        self.eval_step_s = eval_step_s
        self.taker_mode = taker_mode  # exists so net-vs-gross honesty is testable
        self.sigma_lookback_s = sigma_lookback_s
        self.rng = random.Random(seed)
        self.planner = OrderPlanner(settings)
        # {open_ts: {"UP"/"DOWN": [(ts, BookTop)]}} from the recorder. When a
        # window has recorded books, run() replays the REAL book (measures edge)
        # instead of synthesising one (which only validates calibration).
        self.recorded_books = recorded_books or {}

    def _recorded_top(self, window: dict, side: str, t: float):
        """The recorded top for `side` at-or-before time t (latest <= t), else None."""
        series = window.get(side) if window else None
        if not series:
            return None
        lo, hi = 0, len(series)
        while lo < hi:  # bisect_right on ts
            mid = (lo + hi) // 2
            if series[mid][0] <= t:
                lo = mid + 1
            else:
                hi = mid
        return series[lo - 1][1] if lo > 0 else None

    # ── walk-forward sigma from the same series, past data only ──────────────
    def _sigma(self, prices: List[Tuple[float, float]], idx_end: int,
               price_now: float) -> Optional[float]:
        start_ts = prices[idx_end][0] - self.sigma_lookback_s
        window = [p for t, p in prices[max(0, idx_end - int(self.sigma_lookback_s)):
                                       idx_end + 1] if t >= start_ts]
        if len(window) < self.s.sigma_min_samples:
            return None
        rets = [math.log(window[i] / window[i - 1]) for i in range(1, len(window))]
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        scale = self.s.sigma_scale.get(self.window, 1.0)
        return price_now * math.sqrt(var * self.window_seconds) * scale

    def _synthetic_top(self, true_p: float, mispricing: float) -> BookTop:
        tick = self.s.tick_size
        mid = clamp(true_p + mispricing, tick * 2, 1 - tick * 2)
        half = self.synthetic.spread / 2.0
        bid = max(tick, round((mid - half) / tick) * tick)
        ask = min(1 - tick, round((mid + half) / tick) * tick)
        if ask <= bid:
            ask = bid + tick
        return BookTop(token_id="UP", best_bid=bid, best_ask=ask,
                       bid_size=self.synthetic.depth, ask_size=self.synthetic.depth)

    def run(self, prices: List[Tuple[float, float]]) -> BacktestReport:
        report = BacktestReport(
            mode=("taker (test mode)" if self.taker_mode else
                  "maker-only, RECORDED books (real edge)" if self.recorded_books else
                  "maker-only, synthetic book (mechanics+calibration; NOT edge proof)"))
        if len(prices) < self.window_seconds * 2:
            log.warning("price series too short for window %s", self.window)
            return report

        first_ts = prices[0][0]
        # leave a sigma warm-up prefix
        t0 = first_ts + self.sigma_lookback_s
        t0 = math.ceil(t0 / self.window_seconds) * self.window_seconds
        last_ts = prices[-1][0]
        lo_min, hi_min = self.s.entry_windows[self.window]
        ts_index = {round(t): i for i, (t, _) in enumerate(prices)}

        wstart = t0
        while wstart + self.window_seconds <= last_ts:
            wclose = wstart + self.window_seconds
            i_open = ts_index.get(round(wstart))
            i_close = ts_index.get(round(wclose - 1))
            if i_open is None or i_close is None:
                wstart += self.window_seconds
                continue
            open_ref = prices[i_open][1]
            close_px = prices[i_close][1]
            up_won = close_px >= open_ref  # ties -> Up
            report.windows += 1

            market = MarketInfo(
                market_id=f"bt-{int(wstart)}", slug=f"bt-{self.window}-{int(wstart)}",
                question="backtest", window=self.window,
                window_seconds=self.window_seconds, open_ts=wstart, close_ts=wclose,
                resolution_feed=ResolutionFeed.CHAINLINK, token_up="UP",
                token_down="DOWN", opening_reference=open_ref)

            # one synthetic mispricing per window (book noise persists within a window)
            mis = self.rng.gauss(0.0, self.synthetic.noise_sd)
            rec = self.recorded_books.get(int(round(wstart)))  # real books, if recorded
            entered = False
            resting: Optional[tuple] = None   # (outcome, price, modelled_p, z)

            t = wclose - hi_min * 60.0
            t_end = wclose - self.eval_step_s  # resting orders fillable until close
            while t <= t_end:
                i_now = ts_index.get(round(t))
                if i_now is None:
                    t += self.eval_step_s
                    continue
                price_now = prices[i_now][1]
                sigma = self._sigma(prices, i_now, price_now)
                if sigma is None or sigma <= 0:
                    t += self.eval_step_s
                    continue
                remaining = (wclose - t) / self.window_seconds
                z = (price_now - open_ref) / (sigma * math.sqrt(remaining))
                p_up = phi(z)
                fv = FairValue(p_up=p_up, z=z, sigma=sigma,
                               realised_move=price_now - open_ref,
                               remaining_frac=remaining, drift_adj=0.0,
                               feed_price=price_now, opening_reference=open_ref,
                               as_of=t)
                if rec is not None:
                    # replay the REAL recorded book for this window/instant
                    up_top = self._recorded_top(rec, "UP", t)
                    down_top = self._recorded_top(rec, "DOWN", t)
                    if up_top is None or down_top is None:
                        t += self.eval_step_s
                        continue
                else:
                    up_top = self._synthetic_top(p_up, mis)
                    down_top = BookTop(token_id="DOWN",
                                       best_bid=round((1 - up_top.best_ask) / self.s.tick_size)
                                       * self.s.tick_size,
                                       best_ask=round((1 - up_top.best_bid) / self.s.tick_size)
                                       * self.s.tick_size,
                                       bid_size=up_top.ask_size, ask_size=up_top.bid_size)

                # maker fill check for a resting order: book trades through our bid
                if resting is not None and not entered:
                    outcome, bid_px, mp, mz = resting
                    top = up_top if outcome == Outcome.UP else down_top
                    if top.best_ask is not None and top.best_ask <= bid_px + 1e-9:
                        if self.rng.random() >= self.non_fill_prob:
                            report.trades.append(BacktestTrade(
                                window_start=wstart, outcome=outcome,
                                entry_price=bid_px, modelled_p=mp, z=mz,
                                fee=0.0, fill_type="maker"))
                            entered = True

                in_gate = lo_min <= (wclose - t) / 60.0 <= hi_min
                if not entered and resting is None and in_gate:
                    d = self.planner.plan_entry(market, fv, up_top, down_top, now_ts=t)
                    report.decisions += 1
                    if d.action == "post" and d.target_price is not None:
                        report.posts += 1
                        if self.taker_mode:
                            # cross at the ask and pay the real taker fee
                            top = up_top if d.outcome == Outcome.UP else down_top
                            ask = top.best_ask
                            fee = taker_fee(1.0, self.s.fee_rate, ask)
                            report.trades.append(BacktestTrade(
                                window_start=wstart, outcome=d.outcome,
                                entry_price=ask, modelled_p=d.fair_p or 0.5,
                                z=fv.z, fee=fee, fill_type="taker"))
                            entered = True
                        else:
                            resting = (d.outcome, d.target_price, d.fair_p or 0.5, fv.z)
                t += self.eval_step_s

            # settle
            for tr in report.trades:
                if tr.window_start == wstart and tr.won is None:
                    tr.won = up_won if tr.outcome == Outcome.UP else not up_won
                    report.calibration_samples.append(
                        (tr.modelled_p if tr.outcome == Outcome.UP
                         else 1 - tr.modelled_p, up_won))
            wstart += self.window_seconds

        return report
