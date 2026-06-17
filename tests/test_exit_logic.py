"""Regression tests for the exit-order spam / mis-graded exit PnL bugs found in
the first live paper run (June 2026):

- _manage_position posted a NEW sell every eval tick (up to 9 per market),
  duplicate sells double-filled, and record_early_exit overwrote the trade row
  with per-partial-fill PnL — five trades showed losses larger than the maximum
  possible loss for a binary contract.
- _settle could grade a window off a price from before a reconnect gap.
"""

import asyncio
import json

import pytest

from bot.main import TradingApp
from bot.models import (Fill, FairValue, MarketInfo, Outcome, ResolutionFeed,
                        Side)
from bot.risk import RiskManager

T0 = 1_750_000_000.0


@pytest.fixture
def app(settings):
    # enable_prob_stop=True so the exit-order machinery tests still exercise the
    # path (the stop is disabled by default in prod, but the machinery remains).
    s = settings.model_copy(update={"prometheus_enabled": False,
                                    "enable_prob_stop": True})
    app = TradingApp(s)

    async def _no_oracle(slug):  # unit tests never hit the network
        return None
    app.discovery.fetch_resolution = _no_oracle
    return app


def settle(app, market, now):
    return asyncio.run(app._settle(market, now))


def set_oracle(app, outcome):
    """Stub Polymarket's resolution (settlement now grades off the oracle)."""
    async def _o(slug):
        return outcome
    app.discovery.fetch_resolution = _o


def make_market(market_id="m1", close_ts=T0 + 600.0):
    return MarketInfo(
        market_id=market_id, slug=f"btc-updown-15m-{int(close_ts - 900)}",
        question="q", window="15m", window_seconds=900,
        open_ts=close_ts - 900, close_ts=close_ts,
        resolution_feed=ResolutionFeed.CHAINLINK,
        token_up="TU", token_down="TD", opening_reference=100_000.0)


def make_fv(p_up):
    return FairValue(p_up=p_up, z=0.0, sigma=100.0, realised_move=0.0,
                     remaining_frac=0.5, drift_adj=0.0, feed_price=100_000.0,
                     opening_reference=100_000.0, as_of=T0)


def seed_book(app, token, bid=0.30, ask=0.40, size=50):
    app.books.handle_message({
        "event_type": "book", "asset_id": token,
        "bids": [{"price": str(bid), "size": str(size)}],
        "asks": [{"price": str(ask), "size": str(size)}]})


def buy_into_position(app, market, size=10.0, price=0.50, p_up=0.80):
    app.markets[market.market_id] = market
    app.last_fv[market.market_id] = make_fv(p_up)
    app._on_fill(Fill(order_id="o-entry", market_id=market.market_id,
                      token_id=market.token_up, outcome=Outcome.UP,
                      side=Side.BUY, price=price, size=size, fee=0.0, ts=T0))
    return app.positions[market.market_id]


def open_trade_row(app, market_id):
    rows = app.store._conn.execute(
        "SELECT * FROM trades WHERE market_id = ?", (market_id,)).fetchall()
    assert len(rows) == 1
    return dict(rows[0])


# ── exit-order dedup ──────────────────────────────────────────────────────────

def test_exit_order_posted_once_not_spammed(app):
    market = make_market()
    pos = buy_into_position(app, market)
    seed_book(app, "TU")
    fv = make_fv(0.10)  # our UP side collapsed below exit_prob_floor (0.25)

    for _ in range(5):  # five eval ticks used to mean five stacked sell orders
        app._manage_position(market, fv, pos, T0 + 1)

    sells = [o for o in app.paper_engine.open_orders() if o.side == Side.SELL]
    assert len(sells) == 1
    assert market.market_id in app.exit_orders


def test_exit_order_replaced_when_book_moves(app):
    market = make_market()
    pos = buy_into_position(app, market)
    seed_book(app, "TU", bid=0.30, ask=0.40)
    fv = make_fv(0.10)
    app._manage_position(market, fv, pos, T0 + 1)
    first_px = app.exit_orders[market.market_id].price

    seed_book(app, "TU", bid=0.20, ask=0.28)  # book dropped: replace, don't stack
    app._manage_position(market, fv, pos, T0 + 2)
    sells = [o for o in app.paper_engine.open_orders() if o.side == Side.SELL]
    assert len(sells) == 1
    assert app.exit_orders[market.market_id].price != first_px


def test_exit_order_cancelled_when_probability_recovers(app):
    market = make_market()
    pos = buy_into_position(app, market)
    seed_book(app, "TU")
    app._manage_position(market, make_fv(0.10), pos, T0 + 1)
    assert market.market_id in app.exit_orders

    app._manage_position(market, make_fv(0.60), pos, T0 + 2)  # recovered
    assert market.market_id not in app.exit_orders
    assert app.paper_engine.open_orders() == []


# ── exit PnL accounting ───────────────────────────────────────────────────────

def test_oversized_sell_fill_clamped_to_position(app):
    """Loss on a binary can never exceed its cost. The pre-fix data had
    -$4.83 on 3.1 contracts bought at 0.86 (max possible loss $2.66)."""
    market = make_market()
    buy_into_position(app, market, size=3.0, price=0.86)
    app._on_fill(Fill(order_id="o-x", market_id=market.market_id,
                      token_id="TU", outcome=Outcome.UP, side=Side.SELL,
                      price=0.30, size=9.0, fee=0.0, ts=T0 + 5))  # 3x oversized
    row = open_trade_row(app, market.market_id)
    assert row["resolution_outcome"] == "EXIT"
    assert row["net_pnl"] == pytest.approx((0.30 - 0.86) * 3.0)
    assert row["net_pnl"] >= -(3.0 * 0.86) - 1e-9


def test_partial_exits_accumulate_and_resolve_once(app):
    market = make_market()
    buy_into_position(app, market, size=10.0, price=0.50)
    app._on_fill(Fill(order_id="o-x", market_id=market.market_id,
                      token_id="TU", outcome=Outcome.UP, side=Side.SELL,
                      price=0.40, size=4.0, fee=0.0, ts=T0 + 5))
    assert open_trade_row(app, market.market_id)["resolved"] == 0  # still open

    app._on_fill(Fill(order_id="o-x", market_id=market.market_id,
                      token_id="TU", outcome=Outcome.UP, side=Side.SELL,
                      price=0.45, size=6.0, fee=0.0, ts=T0 + 6))
    row = open_trade_row(app, market.market_id)
    assert row["resolved"] == 1
    assert row["resolution_outcome"] == "EXIT"
    # (0.40-0.50)*4 + (0.45-0.50)*6 = -0.40 - 0.30
    assert row["net_pnl"] == pytest.approx(-0.70)
    assert market.market_id in app.exited_markets
    assert market.market_id not in app.positions


def test_no_reentry_after_exit(app):
    market = make_market()
    buy_into_position(app, market, size=5.0, price=0.50)
    app._on_fill(Fill(order_id="o-x", market_id=market.market_id,
                      token_id="TU", outcome=Outcome.UP, side=Side.SELL,
                      price=0.40, size=5.0, fee=0.0, ts=T0 + 5))
    # a stray BUY fill after the exit must not silently rebuild a position
    app._on_fill(Fill(order_id="o-stray", market_id=market.market_id,
                      token_id="TU", outcome=Outcome.UP, side=Side.BUY,
                      price=0.35, size=5.0, fee=0.0, ts=T0 + 6))
    assert market.market_id not in app.positions


def test_addon_fill_updates_trade_row_and_risk(app):
    market = make_market()
    buy_into_position(app, market, size=10.0, price=0.50)
    cap_before = app.risk.capital_at_risk_usd
    app._on_fill(Fill(order_id="o-entry", market_id=market.market_id,
                      token_id="TU", outcome=Outcome.UP, side=Side.BUY,
                      price=0.60, size=10.0, fee=0.0, ts=T0 + 2))
    row = open_trade_row(app, market.market_id)
    assert row["contracts"] == pytest.approx(20.0)
    assert row["executable_price"] == pytest.approx(0.55)  # weighted avg
    assert app.risk.capital_at_risk_usd == pytest.approx(cap_before + 6.0)


# ── settlement ────────────────────────────────────────────────────────────────

def test_settle_waits_for_oracle(app):
    # settlement grades off the oracle in BOTH modes now; before it resolves,
    # don't grade off the local feed — wait.
    market = make_market(close_ts=T0 + 60)
    buy_into_position(app, market, size=10.0, price=0.50)
    settle(app, market, T0 + 70)               # oracle stub -> None
    assert market.market_id in app.markets     # not graded yet — waiting

    set_oracle(app, "UP")                       # Polymarket resolves UP
    settle(app, market, T0 + 95)                # past the 20s oracle-poll throttle
    assert market.market_id not in app.markets
    row = open_trade_row(app, market.market_id)
    assert row["resolution_outcome"] == "UP"
    assert row["net_pnl"] == pytest.approx(10.0 * (1 - 0.50))


def test_settle_with_partial_exit_uses_true_position_net(app):
    market = make_market(close_ts=T0 + 60)
    buy_into_position(app, market, size=10.0, price=0.50)
    app._on_fill(Fill(order_id="o-x", market_id=market.market_id,
                      token_id="TU", outcome=Outcome.UP, side=Side.SELL,
                      price=0.60, size=4.0, fee=0.0, ts=T0 + 10))  # +0.40 realized
    set_oracle(app, "UP")
    settle(app, market, T0 + 70)
    row = open_trade_row(app, market.market_id)
    # 6 remaining contracts win (6 * 0.50) plus 0.40 already realized
    assert row["net_pnl"] == pytest.approx(6 * 0.50 + 0.40)


def test_settle_drops_market_with_no_opening_reference(app):
    """A window that opened before our feed history can never be priced or
    graded — it must be dropped at close, not warn-spam until the give-up."""
    market = make_market(close_ts=T0 + 60)
    market.opening_reference = None
    app.markets[market.market_id] = market
    app.chainlink._update(100_050.0, T0 + 30)   # first tick AFTER the open
    app.chainlink._update(100_050.0, T0 + 61)
    settle(app, market, T0 + 65)
    assert market.market_id not in app.markets


def test_discovery_skips_window_opened_before_feed_history(app):
    now = T0 + 100
    already_open = make_market(market_id="m-old", close_ts=now + 200)  # open T0-600
    already_open.opening_reference = None
    future = make_market(market_id="m-new", close_ts=now + 1000)       # open T0+200
    future.opening_reference = None
    app.chainlink._update(100_000.0, now)  # feed history starts only now
    assert not app._can_establish_opening(already_open, now)
    assert app._can_establish_opening(future, now)   # open still ahead of us


# ── reject dedup ──────────────────────────────────────────────────────────────

def test_stale_feed_reject_logged_once_per_state(app):
    market = make_market()
    app.markets[market.market_id] = market
    for i in range(10):  # chainlink feed has no data: stale every tick
        app._evaluate(market, T0 + i)
    n = app.store._conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE action = 'reject'").fetchone()[0]
    assert n == 1


# ── store tagging ─────────────────────────────────────────────────────────────

def test_pnl_summary_excludes_tagged_rows(app):
    market = make_market()
    buy_into_position(app, market, size=10.0, price=0.50)
    set_oracle(app, "UP")
    settle(app, market, market.close_ts + 5)
    assert app.store.pnl_summary()["net_pnl"] == pytest.approx(5.0)

    app.store._conn.execute("UPDATE trades SET tag = 'pre_exit_fix_misgraded'")
    app.store._conn.commit()
    s = app.store.pnl_summary()
    assert s["resolved_trades"] == 0
    assert s["net_pnl"] == 0


def test_polymarket_ws_handle_raw_tolerates_keepalives(app):
    """PONG frames and junk must not raise (a raised frame used to trigger a
    full reconnect with backoff)."""
    app.books.handle_raw("PONG")
    app.books.handle_raw("not json at all {")
    app.books.handle_raw(json.dumps({
        "event_type": "book", "asset_id": "TU",
        "bids": [{"price": "0.30", "size": "50"}],
        "asks": [{"price": "0.40", "size": "50"}]}))
    top = app.books.get_top("TU")
    assert top is not None and top.best_bid == pytest.approx(0.30)


# ── zombie-trade fix: reload + void ─────────────────────────────────────────────

def test_reload_open_positions_from_db(settings):
    """A position open when the bot stopped must be rebuilt on restart, not
    stranded as a permanent open zombie."""
    from bot.logging_db import BotStore
    s = settings.model_copy(update={"prometheus_enabled": False})
    market = make_market(close_ts=T0 + 600)
    store = BotStore(s.db_path)
    fill = Fill(order_id="o", market_id=market.market_id, token_id=market.token_up,
                outcome=Outcome.UP, side=Side.BUY, price=0.50, size=10.0, fee=0.0, ts=T0)
    store.record_entry(market, make_fv(0.80), fill, fee_avoided=0.0)
    store.close()

    app = TradingApp(s)   # __init__ runs _reload_open_positions
    assert market.market_id in app.positions
    assert app.positions[market.market_id].contracts == 10.0
    assert app.positions[market.market_id].outcome == Outcome.UP
    assert market.market_id in app.markets
    assert app.risk.open_positions == 1
    assert app.risk.capital_at_risk_usd == pytest.approx(5.0)


def test_settle_voids_stranded_position(app):
    """If a position can never be graded (feed never caught up past close), it
    is voided after the give-up window — not left open forever."""
    market = make_market(close_ts=T0 + 60)
    buy_into_position(app, market, size=10.0, price=0.50)
    app.chainlink._update(100_050.0, T0 + 30)        # tick BEFORE close, never after
    settle(app, market, T0 + 60 + 301)               # past the 300s give-up window
    assert market.market_id not in app.positions
    assert market.market_id not in app.markets
    row = open_trade_row(app, market.market_id)
    assert row["resolved"] == 1
    assert row["resolution_outcome"] == "VOID"
    assert row["net_pnl"] == 0
    assert app.store.pnl_summary()["net_pnl"] == 0   # excluded from stats


def test_settle_uses_oracle_when_local_feed_cannot_grade(app):
    """Local feed missed the close (reconnect gap), but Polymarket resolved the
    market: grade off the oracle instead of voiding — the real outcome lands in
    P&L and the live-guard stats, not a void."""
    market = make_market(close_ts=T0 + 60)
    buy_into_position(app, market, size=10.0, price=0.50)  # UP position
    app.chainlink._update(100_050.0, T0 + 30)              # tick BEFORE close only

    async def _oracle_up(slug):
        return "UP"
    app.discovery.fetch_resolution = _oracle_up

    settle(app, market, T0 + 90)   # local can't grade -> oracle says UP
    assert market.market_id not in app.positions
    row = open_trade_row(app, market.market_id)
    assert row["resolved"] == 1
    assert row["resolution_outcome"] == "UP"
    assert row["net_pnl"] == pytest.approx(10.0 * (1 - 0.50))  # UP won
    assert (row["tag"] or "") == ""                            # graded, not voided
    assert app.store.pnl_summary()["net_pnl"] == pytest.approx(5.0)


def test_trend_guard_blocks_against_trend(app):
    """Downtrend: buying UP (the reversal/underdog) fades the trend -> blocked.
    Buying DOWN rides it -> allowed. This is the live-loss pattern (2026-06-17)."""
    market = make_market()                       # chainlink 15m
    app.chainlink._update(66000.0, T0)
    app.chainlink._update(65900.0, T0 + 1800)    # -0.15% over 30m (> 0.10% threshold)
    now = T0 + 1800
    assert app._fades_trend(market, Outcome.UP, now) is True     # fades the downtrend
    assert app._fades_trend(market, Outcome.DOWN, now) is False   # rides it


def test_trend_guard_allows_when_drift_is_noise(app):
    market = make_market()
    app.chainlink._update(66000.0, T0)
    app.chainlink._update(65996.0, T0 + 1800)    # -0.006%, below threshold = noise
    assert app._fades_trend(market, Outcome.UP, T0 + 1800) is False


def test_live_settles_off_oracle_not_local(app):
    """LIVE: the local feed would grade UP, but Polymarket resolved DOWN. Live
    settlement must trust the oracle (what actually pays), not the local feed —
    the bug that graded a real loss as a +$5.04 win on 2026-06-17."""
    app.s = app.s.model_copy(update={"live_mode": True})   # is_paper -> False
    market = make_market(close_ts=T0 + 60)
    buy_into_position(app, market, size=10.0, price=0.50)   # UP position
    app.chainlink._update(100_050.0, T0 + 61)              # local would say UP (>=open)

    async def _oracle_down(slug):
        return "DOWN"
    app.discovery.fetch_resolution = _oracle_down

    settle(app, market, T0 + 70)
    row = open_trade_row(app, market.market_id)
    assert row["resolution_outcome"] == "DOWN"             # oracle, NOT the local UP
    assert row["net_pnl"] == pytest.approx(-10.0 * 0.50)   # UP position lost


# ── risk: pending exposure ────────────────────────────────────────────────────

def test_pending_orders_consume_exposure_caps(settings):
    rm = RiskManager(settings)
    cap = settings.max_capital_at_risk_usd
    ok, reason = rm.can_enter("15m", T0, pending_usd=cap, pending_count=0)
    assert not ok and reason == "max_capital_at_risk"
    ok, reason = rm.can_enter(
        "15m", T0, pending_usd=0.0,
        pending_count=settings.max_concurrent_positions)
    assert not ok and reason == "max_concurrent_positions"
    ok, _ = rm.can_enter("15m", T0)
    assert ok


def test_pending_usd_shrinks_size_entry(settings):
    s = settings.model_copy(update={"bankroll_usd": 10_000.0,
                                    "max_trade_usd": 100.0,
                                    "min_order_contracts": 1.0,
                                    "max_capital_at_risk_usd": 50.0})
    rm = RiskManager(s)
    full = rm.size_entry(p_net=0.9, cost=0.5)
    crowded = rm.size_entry(p_net=0.9, cost=0.5, pending_usd=45.0)
    assert crowded < full
    assert crowded * 0.5 <= 5.0 + 1e-9
