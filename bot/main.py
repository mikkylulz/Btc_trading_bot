"""Entry point: async scheduler loop, per-window cadence (Claude.md §12).

    python -m bot.main            # paper trading loop (default)
    python -m bot.main discover   # list active BTC markets + detected resolution feed
    python -m bot.main backtest   # run the backtester (synthetic GBM unless --csv)
    python -m bot.main status     # open positions + net-of-fee PnL
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys
from typing import Dict, Optional

from .backtester import (Backtester, generate_gbm_prices, load_prices_csv,
                         load_recorded_books)
from .config import Settings, load_settings
from .data_feeds import (ChainlinkFeed, PolymarketBookFeed, RTDSChainlinkFeed,
                         SpotWebsocketFeed)
from .execution.orders import OrderPlanner, taker_fee
from .execution.paper_engine import PaperEngine
from .execution.polymarket_client import LiveClient, PaperClient
from .alerts import Notifier
from .logging_db import BotStore, setup_logging
from .market_discovery import MarketDiscovery
from .recorder import DataRecorder
from .models import (Decision, FairValue, Fill, MarketInfo, Order, Outcome,
                     Position, ResolutionFeed, Side)
from .monitoring import Metrics
from .risk import RiskManager
from .signals import (MicrostructureSignal, MomentumSignal, VolatilityEstimator)
from .strategy import (FairValueEngine, SigmaUnavailableError,
                       StaleResolutionFeedError)
from .utils import utc_ts

log = logging.getLogger(__name__)


class TradingApp:
    def __init__(self, settings: Settings):
        self.s = settings
        self.store = BotStore(settings.db_path)
        self.metrics = Metrics(settings.prometheus_port,
                               enabled=settings.prometheus_enabled)
        self.notifier = Notifier(settings)

        self.spot = SpotWebsocketFeed(settings.binance_ws_url)
        if settings.chainlink_source == "direct":
            self.chainlink = ChainlinkFeed(
                base_url=settings.chainlink_api_base,
                api_key=settings.chainlink_stream_api_key,
                api_secret=settings.chainlink_stream_api_secret,
                feed_id=settings.chainlink_feed_id,
                poll_interval_s=settings.chainlink_poll_interval_s)
        else:
            self.chainlink = RTDSChainlinkFeed(settings.rtds_ws_url,
                                               settings.chainlink_symbol)
        self.books = PolymarketBookFeed(settings.clob_ws_url)

        vol = VolatilityEstimator(self.spot.buffer, settings.sigma_scale,
                                  min_samples=settings.sigma_min_samples)
        self.engine = FairValueEngine(
            settings, vol,
            momentum=MomentumSignal(self.spot.buffer),
            microstructure=MicrostructureSignal())
        self.planner = OrderPlanner(settings)
        self.discovery = MarketDiscovery(settings)
        self.risk = RiskManager(settings)
        self.recorder = (DataRecorder(settings.price_log_path, settings.book_log_path)
                         if settings.record_data else None)

        self.paper_engine = PaperEngine(queue_penalty=settings.paper_queue_penalty,
                                        non_fill_prob=settings.paper_non_fill_prob)
        self.books.on_trade(self.paper_engine.on_trade)
        self.paper_engine.on_fill(self._on_fill)

        if not settings.is_paper and self.risk.is_live_allowed():
            log.warning("LIVE MODE — real orders will be posted")
            self.client = LiveClient(settings)
            self.live = True
        else:
            if not settings.is_paper:
                log.error("live requested but gate failed — running PAPER")
            self.client = PaperClient(settings, self.paper_engine, self.books)
            self.live = False

        self.markets: Dict[str, MarketInfo] = {}
        self.orders: Dict[str, Order] = {}        # market_id -> CURRENT resting entry order
        # order_id -> Order for EVERY entry order placed (incl. ones we cancelled):
        # a "cancel" can race a fill (cancel returns ok but the order already
        # filled), so we must reconcile cancelled order ids too or the fill is
        # orphaned — untracked real exposure (the 2-fills-1-recorded bug 2026-06-17).
        self._order_ledger: Dict[str, Order] = {}
        self.exit_orders: Dict[str, Order] = {}   # market_id -> resting exit order
        self.positions: Dict[str, Position] = {}  # market_id -> position
        self.last_fv: Dict[str, FairValue] = {}
        self.exited_markets: set = set()          # no re-entry after an exit (one trade/window)
        self._last_reject: Dict[str, str] = {}    # market_id -> last reject reason (dedup)
        self._settle_warn_at: Dict[str, float] = {}  # market_id -> next warn ts (throttle)
        self._oracle_poll_at: Dict[str, float] = {}  # market_id -> next oracle poll ts
        self._next_pnl_poll: float = 0.0          # throttle the pnl_summary aggregate
        self._next_reconcile: float = 0.0         # throttle the live fill reconciler
        self._live_halted: bool = False           # live-validation circuit breaker tripped
        self._stop = asyncio.Event()
        self._reload_open_positions()

    # ── startup recovery ───────────────────────────────────────────────────────
    def _reload_open_positions(self) -> None:
        """Rebuild in-memory positions from the DB so a restart doesn't strand
        open trades as permanent zombies. Each is re-tracked; the eval loop then
        settles it at close (or voids it via the give-up path if it can never be
        graded). Markets still active get re-discovered with full token info."""
        from .config import WINDOW_SECONDS
        from .models import ResolutionFeed
        try:
            open_rows = self.store.open_trades()
        except Exception:  # noqa: BLE001
            log.exception("could not reload open positions")
            return
        recovered = 0
        for r in open_rows:
            mid, slug, window = r["market_id"], r["slug"], r["window"]
            tail = slug.rsplit("-", 1)[-1] if slug else ""
            wsec = WINDOW_SECONDS.get(window)
            if not tail.isdigit() or wsec is None:
                continue
            open_ts = int(tail)
            try:
                feed = ResolutionFeed(r["resolution_feed"])
            except (ValueError, KeyError):
                feed = ResolutionFeed.CHAINLINK
            market = MarketInfo(
                market_id=mid, slug=slug, question=slug, window=window,
                window_seconds=wsec, open_ts=open_ts, close_ts=open_ts + wsec,
                resolution_feed=feed, token_up="", token_down="",
                opening_reference=r["opening_reference"])
            pos = Position(
                market_id=mid, outcome=Outcome(r["outcome_side"]),
                contracts=r["contracts"], avg_price=r["executable_price"],
                fees_paid=r["fee_paid"] or 0.0, window=window,
                resolution_feed=r["resolution_feed"],
                opening_reference=r["opening_reference"] or 0.0,
                entry_feed_price=r["entry_feed_price"] or 0.0)
            self.markets[mid] = market
            self.positions[mid] = pos
            self.risk.restore_position(pos.contracts * pos.avg_price)
            recovered += 1
        if recovered:
            log.info("reloaded %d open position(s) from DB on startup", recovered)

    # ── resolution feed plumbing ──────────────────────────────────────────────
    def _res_feed(self, market: MarketInfo):
        # NEVER substitute spot for Chainlink here — staleness must halt instead.
        if market.resolution_feed == ResolutionFeed.CHAINLINK:
            return self.chainlink
        return self.spot  # 1h resolves on Binance; the spot WS *is* Binance BTC/USDT

    def _can_establish_opening(self, market: MarketInfo, now: float) -> bool:
        """A window that opened before our resolution-feed history started can
        never have its opening reference pinned — it is unpriceable and
        unsettleable, so don't track it (it would only spin until give-up)."""
        if market.opening_reference is not None or market.open_ts > now:
            return True  # already pinned, or the open is still ahead of us
        return self._res_feed(market).buffer.price_at_or_before(
            market.open_ts) is not None

    # ── fills ─────────────────────────────────────────────────────────────────
    def _register_order_fill(self, order: Order, now: float) -> bool:
        """Read one order's authoritative matched size and feed any new fill delta
        to _on_fill (the live equivalent of the paper callback). Returns True if a
        fill was registered. No-op in paper (matched is None -> the paper engine
        delivers fills via callback)."""
        matched = self.client.order_matched(order.order_id)
        if matched is None:
            return False  # transient API error / paper: don't guess, retry later
        delta = matched - order.filled
        if delta <= 1e-9:
            return False
        order.filled = matched  # before _on_fill so remaining/pop is correct
        self._on_fill(Fill(
            order_id=order.order_id, market_id=order.market_id,
            token_id=order.token_id, outcome=order.outcome,
            side=order.side, price=order.price, size=delta,
            fee=0.0, ts=now, is_maker=True))
        return True

    def _check_live_guard(self) -> None:
        """Live-validation circuit breaker. The paper edge is unproven live (maker
        adverse selection isn't modelled), so after the first N resolved LIVE
        trades, if cumulative net PnL <= floor OR win rate < min_wr, halt new live
        entries and alert. Derived from the DB each call -> persists across
        restarts. One-way trip: resume only after review (Claude.md / config)."""
        if self.s.is_paper or not self.s.live_guard_enabled or self._live_halted:
            return
        st = self.store.live_guard_stats(self.s.live_guard_min_trades)
        if st["n"] < self.s.live_guard_min_trades:
            return  # still gathering the validation sample
        if st["net_pnl"] > self.s.live_guard_min_pnl \
                and st["win_rate"] >= self.s.live_guard_min_wr:
            return  # first N live trades validated the edge
        self._live_halted = True
        msg = (f"LIVE GUARD TRIPPED: first {st['n']} live trades "
               f"net {st['net_pnl']:+.2f} USD, WR {st['win_rate']:.0%} "
               f"(floor {self.s.live_guard_min_pnl:+.2f} / min WR "
               f"{self.s.live_guard_min_wr:.0%}). Halting new live entries.")
        log.warning(msg)
        self.notifier.send("⛔ " + msg + "\nInvestigate before resuming.")

    def _reconcile_live_fills(self, now: float) -> None:
        """Periodic live fill reconciler. The strategy is blind to live fills
        otherwise: no Position is ever built, _evaluate never stops posting on a
        window, and exposure ladders far past the cap as the price moves (the
        wallet-drain bug, 2026-06-15). Paper mode is a no-op (callback).

        Reconciles EVERY placed order id (the ledger), not just the current
        resting one — a cancelled order can have filled before the cancel landed,
        and that fill must still be registered or it's orphaned (2026-06-17)."""
        for oid, order in list(self._order_ledger.items()):  # snapshot: _on_fill mutates
            self._register_order_fill(order, now)
            if order.remaining <= 1e-9:        # fully filled -> done reconciling it
                self._order_ledger.pop(oid, None)
        for _market_id, order in list(self.exit_orders.items()):
            self._register_order_fill(order, now)

    def _on_fill(self, fill: Fill) -> None:
        market = self.markets.get(fill.market_id)
        fv = self.last_fv.get(fill.market_id)
        if fill.side == Side.SELL:
            pos = self.positions.get(fill.market_id)
            if pos is None:
                log.warning("SELL fill with no position on %s — ignored",
                            fill.market_id)
                return
            # Clamp: never account for selling more than we own (a binary
            # position's loss can never exceed its cost).
            closed = min(fill.size, pos.contracts)
            pos.realized_exit_pnl += (fill.price - pos.avg_price) * closed - fill.fee
            pos.contracts -= closed
            self.risk.release_capital(closed * pos.avg_price)
            if pos.contracts <= 1e-9:
                self.positions.pop(fill.market_id, None)
                self.exited_markets.add(fill.market_id)
                self._cancel_tracked(self.exit_orders, fill.market_id)
                self._cancel_tracked(self.orders, fill.market_id)  # no re-entry via leftovers
                self.store.record_early_exit(fill.market_id, pos.realized_exit_pnl)
                self.risk.close_position()
                log.info("EXIT closed %s: net %+.2f", fill.market_id,
                         pos.realized_exit_pnl)
                slug = market.slug if market else fill.market_id
                self.notifier.send(
                    f"EXIT (prob-stop) {pos.outcome.value} {slug}\n"
                    f"  net {pos.realized_exit_pnl:+.2f} USD")
            return
        if fill.market_id in self.exited_markets:
            log.warning("BUY fill on already-exited %s — ignoring for tracking",
                        fill.market_id)
            return
        pos = self.positions.get(fill.market_id)
        if pos is None and market is not None and fv is not None:
            pos = Position(
                market_id=fill.market_id, outcome=fill.outcome,
                contracts=fill.size, avg_price=fill.price, fees_paid=fill.fee,
                fair_p_at_entry=fv.p_for(fill.outcome), z_at_entry=fv.z,
                window=market.window, resolution_feed=market.resolution_feed.value,
                opening_reference=fv.opening_reference,
                entry_feed_price=fv.feed_price)
            self.positions[fill.market_id] = pos
            self.risk.record_entry(market.window, fill.size * fill.price)
            fee_avoided = taker_fee(fill.size, self.s.fee_rate, fill.price)
            self.store.record_entry(market, fv, fill, fee_avoided,
                                    is_live=not self.s.is_paper)
            self.store.record_calibration(market, fv)
            log.info("FILL %s %s %.0f @ %.3f (fair %.3f)", market.slug,
                     fill.outcome.value, fill.size, fill.price,
                     fv.p_for(fill.outcome))
            self.notifier.send(
                f"ENTER {fill.outcome.value} {market.slug}\n"
                f"  {fill.size:.0f} @ {fill.price:.3f}  fair {fv.p_for(fill.outcome):.3f}"
                f"  ({market.window})")
        elif pos is not None:
            total = pos.contracts + fill.size
            pos.avg_price = (pos.avg_price * pos.contracts
                             + fill.price * fill.size) / total
            pos.contracts = total
            pos.fees_paid += fill.fee
            self.risk.record_addon(fill.size * fill.price)
            self.store.add_fill_to_trade(fill.market_id, fill.size, fill.price,
                                         fill.fee)
        # Drop the entry-order handle once it is fully filled.
        order = self.orders.get(fill.market_id)
        if order and order.order_id == fill.order_id and order.remaining <= 1e-9:
            self.orders.pop(fill.market_id, None)

    def _cancel_tracked(self, book: Dict[str, Order], market_id: str) -> None:
        # Only drop tracking once the cancel is CONFIRMED. If the cancel fails
        # (e.g. CLOB read timeout on a flaky link) the order may still be live;
        # forgetting it here lets the next cycle repost a duplicate -> double
        # exposure with one order untracked. Keep it tracked and retry instead.
        order = book.get(market_id)
        if order and self.client.cancel_order(order.order_id):
            book.pop(market_id, None)

    # ── discovery loop ────────────────────────────────────────────────────────
    async def discovery_loop(self) -> None:
        while not self._stop.is_set():
            try:
                found = await self.discovery.fetch_active_markets()
                now = utc_ts()
                for m in found:
                    if m.close_ts <= now or m.market_id in self.markets:
                        continue
                    if not self._can_establish_opening(m, now):
                        log.info("skipping %s: window opened before our feed "
                                 "history (no opening reference possible)", m.slug)
                        continue
                    self.markets[m.market_id] = m
                    log.info("tracking %s (%s, resolves on %s)", m.slug,
                             m.window, m.resolution_feed.value)
                tokens = []
                for m in self.markets.values():
                    tokens.extend([m.token_up, m.token_down])
                self.books.set_tokens(tokens)
            except Exception:  # noqa: BLE001
                log.exception("discovery failed")
            await asyncio.sleep(self.s.discovery_interval_s)

    # ── evaluation loop ───────────────────────────────────────────────────────
    async def eval_loop(self) -> None:
        while not self._stop.is_set():
            now = utc_ts()
            # Reconcile live fills BEFORE evaluating: a fill must register as a
            # Position so _evaluate stops posting on that window (one trade/window).
            if not self.s.is_paper and now >= self._next_reconcile:
                self._next_reconcile = now + self.s.reconcile_interval_s
                try:
                    self._reconcile_live_fills(now)
                except Exception:  # noqa: BLE001
                    log.exception("live fill reconcile failed")
                try:
                    self._check_live_guard()
                except Exception:  # noqa: BLE001
                    log.exception("live guard check failed")
            try:
                self._record_data(now)
            except Exception:  # noqa: BLE001
                log.exception("data recording failed (non-fatal)")
            for market in list(self.markets.values()):
                try:
                    if now >= market.close_ts:
                        await self._settle(market, now)
                    else:
                        self._evaluate(market, now)
                except Exception:  # noqa: BLE001
                    log.exception("eval failed for %s", market.slug)
            # Metrics must never take down the trading loop. A transient
            # sqlite "disk I/O error" (OneDrive/AV locking the file) once
            # crashed the whole bot from here — isolate it.
            try:
                self._update_metrics(now)
            except Exception:  # noqa: BLE001
                log.exception("metrics update failed (non-fatal)")
            await asyncio.sleep(self.s.eval_interval_s)

    def _evaluate(self, market: MarketInfo, now: float) -> None:
        up_top = self.books.get_top(market.token_up)
        down_top = self.books.get_top(market.token_down)
        try:
            fv = self.engine.fair_value(market, self._res_feed(market), now,
                                        up_top=up_top)
        except (StaleResolutionFeedError, SigmaUnavailableError) as e:
            self._cancel_tracked(self.orders, market.market_id)
            # Dedup: a stale feed rejects every tick; one row per state change
            # is signal, 400k identical rows is bloat.
            reason = type(e).__name__
            if self._last_reject.get(market.market_id) != reason:
                self._last_reject[market.market_id] = reason
                self.store.record_decision(Decision(
                    market_id=market.market_id, ts=now, action="reject",
                    reason=reason))
            return
        self._last_reject.pop(market.market_id, None)
        self.last_fv[market.market_id] = fv

        pos = self.positions.get(market.market_id)
        if pos is not None:
            self._manage_position(market, fv, pos, now)
            return

        if market.market_id in self.exited_markets:
            return  # one trade per window: no re-entry after an exit

        if self._live_halted:
            # circuit breaker tripped: no new live entries. Cancel any resting
            # entry order so we stop adding exposure (positions above still exit).
            if self.orders.get(market.market_id):
                self._cancel_tracked(self.orders, market.market_id)
            return

        # Register any fill on a resting order FIRST — a fill is a fill regardless
        # of book freshness or the entry window. This builds the Position (so we
        # stop posting) and closes the orphan-on-replace race before any
        # cancel/replace below. If it filled, route to _manage_position next cycle.
        order = self.orders.get(market.market_id)
        if order is not None and self._register_order_fill(order, now):
            return

        # Don't price a NEW entry (or reprice a resting one) off a STALE book:
        # during a Polymarket WS disconnect get_top returns the last-known book.
        # post_only already blocks a crossing fill, but this stops us acting on
        # stale prices at all. A resting order is left untouched (it just rides).
        if self.books.is_stale(self.s.max_book_age_s, now):
            return

        decision = self.planner.plan_entry(market, fv, up_top, down_top, now_ts=now)
        # Trend guard: don't bet against a strong recent BTC move (the drift-less
        # model overrates the reversal; the band trades only that side).
        if (decision.action == "post" and self.s.enable_trend_guard
                and decision.outcome is not None
                and self._fades_trend(market, decision.outcome, now)):
            decision.action, decision.reason = "reject", "fades_trend"
        order = self.orders.get(market.market_id)

        if not self.discovery.in_entry_window(market, now):
            if order:  # gate closed: stop chasing, let any resting order ride
                return
            return

        if order is not None:
            # Fill already registered above. Confirmed-cancel: only drop tracking
            # / repost when the cancel SUCCEEDS — a failed cancel (CLOB timeout on
            # a flaky link) leaves the order live; popping + reposting orphans it
            # and ladders fills past the cap (the wallet-drain bug). On a failed
            # cancel keep it tracked and retry next cycle.
            if decision.action != "post":
                if self.client.cancel_order(order.order_id):
                    self.orders.pop(market.market_id, None)
                    decision.action = "cancel"
                    decision.reason = "edge_gone:" + decision.reason
                    self.store.record_decision(decision)
            elif self.planner.should_replace(order.price, decision):
                if self.client.cancel_order(order.order_id):
                    self.orders.pop(market.market_id, None)
                    decision.action = "replace"
                    self._post(market, decision, now)
            return

        if decision.action != "post":
            self.store.record_decision(decision)
            return

        # Resting entry orders elsewhere can all fill at once — they count
        # toward the exposure caps too, not just filled positions.
        pending_usd = sum(o.price * o.remaining for o in self.orders.values())
        allowed, why = self.risk.can_enter(market.window, now,
                                           pending_usd=pending_usd,
                                           pending_count=len(self.orders))
        if not allowed:
            decision.action, decision.reason = "reject", why
            self.store.record_decision(decision)
            return

        # net-of-fee sizing: maker pays zero fee, price is the decision target
        size = self.risk.size_entry(decision.fair_p or 0.5,
                                    decision.target_price or 1.0,
                                    pending_usd=pending_usd)
        if size <= 0:
            decision.action, decision.reason = "reject", "size_zero"
            self.store.record_decision(decision)
            return
        decision.size = size
        self._post(market, decision, now)

    def _post(self, market: MarketInfo, decision: Decision, now: float) -> None:
        token = (market.token_up if decision.outcome == Outcome.UP
                 else market.token_down)
        top = self.books.get_top(token)
        try:
            order = self.client.place_maker_order(
                market.market_id, token, decision.outcome, Side.BUY,
                decision.target_price, decision.size or 0.0, top=top)
        except Exception as e:  # noqa: BLE001 — includes TakerOrderForbidden
            decision.action, decision.reason = "reject", f"order_rejected:{e}"
            self.store.record_decision(decision)
            return
        self.orders[market.market_id] = order
        self._order_ledger[order.order_id] = order  # reconcile even if later cancelled
        self.store.record_decision(decision)
        log.info("POST %s %s %.0f @ %.3f (fair %.3f, edge %.3f)", market.slug,
                 decision.outcome.value, order.size, order.price,
                 decision.fair_p or 0.0, decision.edge or 0.0)

    def _manage_position(self, market: MarketInfo, fv: FairValue,
                         pos: Position, now: float) -> None:
        p_ours = fv.p_for(pos.outcome)
        exit_order = self.exit_orders.get(market.market_id)
        if not self.risk.should_exit(p_ours):
            if exit_order is not None:  # probability recovered: stop selling
                self._cancel_tracked(self.exit_orders, market.market_id)
                self.store.record_decision(Decision(
                    market_id=market.market_id, ts=now, action="cancel",
                    reason=f"exit_recovered_{p_ours:.2f}", outcome=pos.outcome,
                    fair_p=p_ours))
            return
        token = (market.token_up if pos.outcome == Outcome.UP
                 else market.token_down)
        # Can't place a sell below Polymarket's 5-share maker floor — a partial
        # fill may have shrunk the position under it. A sub-floor sell would just
        # be rejected every tick; let the remainder ride to settlement (max loss
        # is the cost already at risk on a bounded binary).
        if pos.contracts < self.s.min_order_contracts:
            return
        top = self.books.get_top(token)
        if top is None or top.best_bid is None or top.best_ask is None:
            return
        # probability-space stop: ONE maker SELL just inside the ask, tracked so
        # we never stack duplicate exit orders (they double-fill and corrupt PnL)
        sell_px = max(top.best_bid + self.s.tick_size,
                      top.best_ask - self.s.tick_size)
        sell_px = round(sell_px / self.s.tick_size) * self.s.tick_size
        if exit_order is not None:
            if abs(exit_order.price - sell_px) <= self.s.tick_size + 1e-9:
                return  # already resting at (or within a tick of) the right price
            self._cancel_tracked(self.exit_orders, market.market_id)
        try:
            order = self.client.place_maker_order(
                market.market_id, token, pos.outcome, Side.SELL, sell_px,
                pos.contracts, top=top)
            self.exit_orders[market.market_id] = order
            self.store.record_decision(Decision(
                market_id=market.market_id, ts=now, action="exit",
                reason=f"p_collapsed_{p_ours:.2f}", outcome=pos.outcome,
                fair_p=p_ours, target_price=sell_px, size=pos.contracts))
        except Exception as e:  # noqa: BLE001
            log.warning("exit order failed for %s: %s", market.slug, e)

    # ── settlement ────────────────────────────────────────────────────────────
    def _record_data(self, now: float) -> None:
        """Append the resolution price + each tracked token's book top to CSV for
        offline backtesting. No-op unless record_data is set."""
        if self.recorder is None:
            return
        self.recorder.record_price(self.chainlink.as_of_timestamp or now,
                                   self.chainlink.last_value)
        for m in list(self.markets.values()):
            self.recorder.record_book(now, m.slug, "UP", m.token_up,
                                      self.books.get_top(m.token_up))
            self.recorder.record_book(now, m.slug, "DOWN", m.token_down,
                                      self.books.get_top(m.token_down))

    def _fades_trend(self, market: MarketInfo, outcome: Outcome, now: float) -> bool:
        """True if buying `outcome` bets AGAINST a strong recent BTC drift. The
        Phi(z) model assumes zero drift and overrates the reversal side, so in a
        sustained move the underdog-only band systematically fades and loses
        (live 2026-06-17). Measured on the resolution feed's own history."""
        feed = self._res_feed(market)
        now_px = feed.last_value
        past_px = feed.buffer.price_at_or_before(now - self.s.trend_lookback_s)
        if now_px is None or past_px is None or past_px <= 0:
            return False  # not enough history — don't gate
        drift = (now_px - past_px) / past_px
        if abs(drift) < self.s.trend_min_drift:
            return False  # no strong trend
        return ((outcome == Outcome.UP and drift < 0)
                or (outcome == Outcome.DOWN and drift > 0))

    def _local_grade(self, market: MarketInfo) -> Optional[bool]:
        """Grade off our own resolution-feed buffer (instant, same data the market
        settles on). Returns up_won, or None if we can't grade locally — no
        opening reference, or the feed has no tick AT/AFTER the close (a reconnect
        gap; grading off a pre-gap price mis-grades — the panama lesson)."""
        res_feed = self._res_feed(market)
        opening = market.opening_reference
        if opening is None:
            opening = res_feed.buffer.price_at_or_before(market.open_ts)
        if opening is None:
            return None
        as_of = res_feed.as_of_timestamp
        if as_of is None or as_of < market.close_ts:
            return None
        close_px = res_feed.buffer.price_at_or_before(market.close_ts)
        if close_px is None:
            return None
        return close_px >= opening if market.tie_rule_up else close_px > opening

    async def _oracle_resolution(self, market: MarketInfo, now: float) -> Optional[str]:
        """Polymarket's real resolved outcome, polled (throttled). Authoritative —
        it's what the market actually PAYS — so it's the PRIMARY grade for live
        trades (the local feed can disagree at the boundary) and the fallback for
        paper. Returns None until Polymarket's price has decisively resolved."""
        if now < self._oracle_poll_at.get(market.market_id, 0.0):
            return None
        self._oracle_poll_at[market.market_id] = now + self.s.oracle_poll_interval_s
        try:
            outcome = await self.discovery.fetch_resolution(market.slug)
        except Exception:  # noqa: BLE001
            log.exception("oracle resolution fetch failed for %s", market.slug)
            return None
        if outcome:
            log.info("oracle resolution for %s: %s (local feed couldn't grade)",
                     market.slug, outcome)
        return outcome

    async def _settle(self, market: MarketInfo, now: float) -> None:
        self._cancel_tracked(self.orders, market.market_id)
        self._cancel_tracked(self.exit_orders, market.market_id)
        # Final fill check on every order placed on this window before grading —
        # catches a fill that landed right at close (incl. on a 'cancelled' order)
        # so the position reflects true exposure, not an orphaned subset.
        for oid, order in list(self._order_ledger.items()):
            if order.market_id == market.market_id:
                self._register_order_fill(order, now)
                if order.remaining <= 1e-9:
                    self._order_ledger.pop(oid, None)
        has_pos = self.positions.get(market.market_id) is not None

        # Grading priority differs by mode:
        #  LIVE  — the oracle (Polymarket's real outcome) is what actually PAYS;
        #          the local feed can disagree at the boundary (it graded a loss
        #          as a win 2026-06-17). Prefer the oracle and WAIT for it rather
        #          than trust the local feed; fall back to local only as a last
        #          resort near the give-up timeout.
        #  PAPER — local grade is the simulated rule (instant, no API spam);
        #          oracle only as a fallback when local can't grade.
        up_won: Optional[bool] = None
        if not self.s.is_paper and has_pos:
            oracle = await self._oracle_resolution(market, now)
            if oracle is not None:
                up_won = (oracle == "UP")
            elif now - market.close_ts <= 300:
                if now >= self._settle_warn_at.get(market.market_id, 0.0):
                    self._settle_warn_at[market.market_id] = now + 30.0
                    log.warning("cannot settle %s yet (awaiting Polymarket oracle "
                                "resolution) — waiting", market.slug)
                return  # don't grade live off the local feed; wait for the oracle
            else:
                up_won = self._local_grade(market)
                if up_won is not None:
                    log.warning("settling %s off LOCAL feed (oracle never resolved "
                                "past give-up) — may differ from actual payout",
                                market.slug)
        else:
            up_won = self._local_grade(market)
            if up_won is None and has_pos:
                oracle = await self._oracle_resolution(market, now)
                if oracle is not None:
                    up_won = (oracle == "UP")

        if up_won is None:
            # neither local nor oracle could grade yet
            if not has_pos:
                log.info("dropping %s: nothing to grade (no position, feed/oracle "
                         "not resolved)", market.slug)
                self._drop_market(market.market_id)
                return
            if now >= self._settle_warn_at.get(market.market_id, 0.0):
                self._settle_warn_at[market.market_id] = now + 30.0
                log.warning("cannot settle %s yet (local feed not past close, "
                            "oracle not resolved) — waiting", market.slug)
            if now - market.close_ts > 300:
                self._void_position(market, "ungradeable_local_and_oracle", now)
            return

        outcome = "UP" if up_won else "DOWN"
        pos = self.positions.pop(market.market_id, None)
        if pos:
            # True position-level net: remaining contracts settle, plus any
            # PnL already realized by partial exit fills.
            won = (pos.outcome.value == outcome)
            payout = pos.contracts * (1.0 if won else 0.0)
            net = (payout - pos.contracts * pos.avg_price - pos.fees_paid
                   + pos.realized_exit_pnl)
            self.store.resolve_trades(market.market_id, outcome, net_override=net)
            self.risk.record_exit(pos.contracts * pos.avg_price)
            self.risk.record_result(market.window, won, ts=now)
            log.info("SETTLE %s -> %s (%s, net %+.2f)", market.slug, outcome,
                     "WIN" if won else "LOSS", net)
            run = self.store.pnl_summary()
            self.notifier.send(
                f"SETTLE {'WIN ✅' if won else 'LOSS ❌'} {pos.outcome.value} "
                f"{market.slug}\n  resolved {outcome}, net {net:+.2f} USD\n"
                f"  running: {run['net_pnl']:+.2f} USD over {run['resolved_trades']} "
                f"trades ({run['win_rate']*100:.0f}% WR)")
        else:
            self.store.resolve_trades(market.market_id, outcome)
        self.store.resolve_calibration(market.market_id, up_won)
        self._drop_market(market.market_id)

    def _void_position(self, market: MarketInfo, reason: str, now: float) -> None:
        """Close out a position that can never be graded locally: void the trade
        row (PnL 0, excluded from stats), release its capital, drop the market.
        Prevents the stuck-open-forever zombie state."""
        pos = self.positions.pop(market.market_id, None)
        n = self.store.void_open_trades(market.market_id, reason)
        if pos:
            self.risk.record_exit(pos.contracts * pos.avg_price)
        log.warning("VOID %s (%s) — %d trade(s) marked void, not graded",
                    market.slug, reason, n)
        self.notifier.send(f"VOID {market.slug}\n  reason: {reason} "
                           f"(could not grade locally; excluded from P&L)")
        self._drop_market(market.market_id)

    def _drop_market(self, market_id: str) -> None:
        self.markets.pop(market_id, None)
        self.last_fv.pop(market_id, None)
        self.exited_markets.discard(market_id)
        self._last_reject.pop(market_id, None)
        self._settle_warn_at.pop(market_id, None)
        self._oracle_poll_at.pop(market_id, None)
        self._order_ledger = {oid: o for oid, o in self._order_ledger.items()
                              if o.market_id != market_id}

    # ── metrics ───────────────────────────────────────────────────────────────
    def _update_metrics(self, now: float) -> None:
        for feed in (self.spot, self.chainlink):
            as_of = feed.as_of_timestamp
            self.metrics.set_feed_staleness(feed.name,
                                            now - as_of if as_of else math.inf)
        for window, h in self.risk.halts.items():
            self.metrics.set_halted(window, now < h.halted_until)
        # pnl_summary is a full table aggregate — every second is wasteful and
        # multiplies the chance of hitting a transient file lock. Once per 30s.
        if now >= self._next_pnl_poll:
            self._next_pnl_poll = now + 30.0
            s = self.store.pnl_summary()
            self.metrics.set_summary(len(self.positions),
                                     s["net_ev_per_trade"], 0.0, 0.0)

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def run(self) -> None:
        mode = "LIVE" if self.live else "PAPER"
        # LIVE_MODE is lock #1; effective mode is live only if all three locks pass.
        log.info("LIVE_MODE: %s | effective mode: %s",
                 self.s.live_mode, "live" if self.live else "paper")
        log.info("starting in %s mode", mode)
        self.notifier.send(
            f"Started in {mode} mode. Trading BTC up/down "
            f"({', '.join(self.s.market_slug_prefixes.values())}). "
            f"Alerts: entry, exit, settle. Max ${self.s.max_trade_usd:.0f}/trade.")
        tasks = [
            asyncio.create_task(self.spot.run(), name="spot_ws"),
            asyncio.create_task(self.chainlink.run(), name="chainlink"),
            asyncio.create_task(self.books.run(), name="polymarket_ws"),
            asyncio.create_task(self.discovery_loop(), name="discovery"),
            asyncio.create_task(self.eval_loop(), name="eval"),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()


# ── CLI ───────────────────────────────────────────────────────────────────────
async def cmd_discover(settings: Settings) -> None:
    found = await MarketDiscovery(settings).fetch_active_markets()
    if not found:
        print("no active markets matched the configured slug prefixes "
              f"({list(settings.market_slug_prefixes)})")
        return
    now = utc_ts()
    for m in sorted(found, key=lambda x: x.close_ts):
        mins = (m.close_ts - now) / 60.0
        print(f"{m.slug:<55} {m.window:>3}  resolves_on={m.resolution_feed.value:<9} "
              f"closes_in={mins:6.1f}m")


def cmd_backtest(settings: Settings, args) -> None:
    if args.csv:
        prices = load_prices_csv(args.csv)
        print(f"loaded {len(prices)} prices from {args.csv}")
    else:
        prices = generate_gbm_prices(args.hours * 3600)
        print(f"no --csv given: synthetic GBM, {args.hours}h @ 1s "
              "(pipeline smoke test, NOT an edge measurement)")
    recorded = None
    if args.books:
        recorded = load_recorded_books(args.books)
        nwin = sum(1 for w in recorded if any(recorded[w].values()))
        print(f"loaded recorded books for {nwin} windows from {args.books} "
              "-> REAL-edge mode (replaying actual books)")
    bt = Backtester(settings, window=args.window, taker_mode=args.taker,
                    recorded_books=recorded)
    print(bt.run(prices).summary())


def cmd_status(settings: Settings) -> None:
    store = BotStore(settings.db_path)
    s = store.pnl_summary()
    print(f"mode:              {'paper' if settings.is_paper else 'live'}")
    print(f"resolved trades:   {s['resolved_trades']}")
    print(f"open trades:       {s['open_trades']}")
    print(f"net PnL:           {s['net_pnl']:+.2f} USD")
    print(f"net EV per trade:  {s['net_ev_per_trade']:+.4f} USD  <- the KPI")
    print(f"win / loss:        {s['wins']}W / {s['losses']}L  "
          f"({s['win_rate']*100:.1f}% win rate, {s['exits']} early exits)")
    print(f"avg win / loss:    {s['avg_win']:+.2f} / {s['avg_loss']:+.2f} USD")
    print("  (note: WR is not the objective — breakeven WR = entry price + fees)")
    sides = store.side_breakdown()
    if sides:
        print("\nUP vs DOWN (resolved):")
        print(f"  {'side':<5}{'trades':>7}{'net USD':>10}{'EV/trade':>10}"
              f"{'win rate':>10}{'avg entry':>11}")
        for b in sides:
            print(f"  {b['side']:<5}{b['n']:>7}{b['net']:>+10.2f}{b['ev']:>+10.2f}"
                  f"{b['win_rate']*100:>9.0f}%{b['avg_entry']:>11.3f}")
        print("  (totals can be skewed by a few cheap tail wins — compare EV/trade)")
    print("\ncalibration (modelled P vs realised):")
    rows = store.calibration_report()
    if not rows:
        print("  (no resolved samples yet)")
    for b in rows:
        print(f"  {b['bucket']}  n={b['n']:>4}  modelled={b['mean_modelled_p']:.3f}"
              f"  realised={b['realised_freq']:.3f}")
    store.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="bot", description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run")
    sub.add_parser("discover")
    sub.add_parser("status")
    bt = sub.add_parser("backtest")
    bt.add_argument("--csv", help="resolution-feed price CSV (ts,price; 1s cadence)")
    bt.add_argument("--books", help="recorded book CSV (data/books.csv) for a "
                    "REAL-edge backtest; without it the book is synthetic")
    bt.add_argument("--window", default="15m", choices=["5m", "15m", "1h"])
    bt.add_argument("--hours", type=int, default=48, help="synthetic hours if no CSV")
    bt.add_argument("--taker", action="store_true",
                    help="fill as taker with real fees (net-vs-gross honesty check)")
    args = parser.parse_args(argv)

    setup_logging()
    settings = load_settings()
    cmd = args.cmd or "run"
    if cmd == "discover":
        asyncio.run(cmd_discover(settings))
    elif cmd == "backtest":
        cmd_backtest(settings, args)
    elif cmd == "status":
        cmd_status(settings)
    else:
        app = TradingApp(settings)
        try:
            asyncio.run(app.run())
        except KeyboardInterrupt:
            print("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
