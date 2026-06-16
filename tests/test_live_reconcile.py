"""Regression test for the live fill reconciler (2026-06-15 wallet-drain bug).

In live mode the bot had NO fill detection: _on_fill was wired only to the paper
engine, so a real CLOB fill never built a Position, _evaluate never stopped
posting on the window, and exposure laddered far past the $1 cap as the price
moved. _reconcile_live_fills reads each tracked order's authoritative matched
size and feeds the delta to _on_fill — the live equivalent of the callback.
"""

import pytest

from bot.main import TradingApp
from bot.models import (FairValue, MarketInfo, Order, Outcome, ResolutionFeed,
                        Side)

T0 = 1_750_000_000.0


@pytest.fixture
def app(settings):
    return TradingApp(settings.model_copy(update={"prometheus_enabled": False}))


def make_market(market_id="m1"):
    return MarketInfo(
        market_id=market_id, slug="btc-updown-5m-1750000000",
        question="q", window="5m", window_seconds=300,
        open_ts=T0, close_ts=T0 + 300,
        resolution_feed=ResolutionFeed.CHAINLINK,
        token_up="TU", token_down="TD", opening_reference=100_000.0)


def make_fv(p_up=0.6):
    return FairValue(p_up=p_up, z=0.0, sigma=100.0, realised_move=0.0,
                     remaining_frac=0.5, drift_adj=0.0, feed_price=100_000.0,
                     opening_reference=100_000.0, as_of=T0)


class FakeClient:
    """Returns scripted matched sizes per order_id; cancel is a no-op success."""
    def __init__(self, matched):
        self.matched = matched  # order_id -> matched contracts

    def order_matched(self, order_id):
        return self.matched.get(order_id)

    def cancel_order(self, order_id):
        return True


def track_entry(app, market, size=5.0, price=0.20):
    app.markets[market.market_id] = market
    app.last_fv[market.market_id] = make_fv()
    order = Order(order_id="o1", market_id=market.market_id,
                  token_id=market.token_up, outcome=Outcome.UP, side=Side.BUY,
                  price=price, size=size, placed_ts=T0)
    app.orders[market.market_id] = order
    return order


def test_partial_then_full_fill_builds_one_position(app):
    market = make_market()
    order = track_entry(app, market, size=5.0, price=0.20)
    app.client = FakeClient({"o1": 2.0})

    app._reconcile_live_fills(T0 + 1)
    pos = app.positions[market.market_id]
    assert pos.contracts == pytest.approx(2.0)
    assert order.filled == pytest.approx(2.0)
    assert app.risk.open_positions == 1  # not double-counted

    # remaining 3 shares fill: same order -> add-on, still ONE position
    app.client.matched["o1"] = 5.0
    app._reconcile_live_fills(T0 + 3)
    assert pos.contracts == pytest.approx(5.0)
    assert app.risk.open_positions == 1
    # fully filled -> the entry-order handle is dropped (no re-post)
    assert market.market_id not in app.orders


def test_no_double_count_on_repeated_reconcile(app):
    market = make_market()
    track_entry(app, market, size=5.0, price=0.20)
    app.client = FakeClient({"o1": 5.0})

    app._reconcile_live_fills(T0 + 1)
    app._reconcile_live_fills(T0 + 3)  # no new matched delta
    app._reconcile_live_fills(T0 + 5)
    assert app.positions[market.market_id].contracts == pytest.approx(5.0)
    assert app.risk.open_positions == 1


def test_api_error_creates_no_phantom_fill(app):
    market = make_market()
    order = track_entry(app, market, size=5.0, price=0.20)
    app.client = FakeClient({})  # order_matched -> None (transient error)

    app._reconcile_live_fills(T0 + 1)
    assert market.market_id not in app.positions
    assert order.filled == 0.0
    assert app.risk.open_positions == 0


class RaceClient:
    """A tracked order has FILLED, and a price move would trigger should_replace.
    cancel 'succeeds' — the orphan-on-replace re-ladder path. Records any repost."""
    def __init__(self, matched):
        self.matched = matched
        self.posted = []           # any place_maker_order call = a re-ladder repost

    def order_matched(self, order_id):
        return self.matched.get(order_id)

    def cancel_order(self, order_id):
        return True

    def place_maker_order(self, *a, **k):
        self.posted.append((a, k))
        raise AssertionError("re-posted on top of a filled order (re-ladder)")


def test_filled_order_is_not_replaced_on_price_move(app, monkeypatch):
    """The wallet-drain race: order fills, price moves, replace path would cancel
    + repost. The inline fill check must register the fill and bail instead."""
    from bot.models import Decision
    # 5m window, ~2 min left -> inside the entry gate
    market = MarketInfo(
        market_id="m1", slug="btc-updown-5m-1750000000", question="q",
        window="5m", window_seconds=300, open_ts=T0, close_ts=T0 + 120,
        resolution_feed=ResolutionFeed.CHAINLINK, token_up="TU", token_down="TD",
        opening_reference=100_000.0)
    order = track_entry(app, market, size=5.0, price=0.20)  # resting @ 0.20
    app.client = RaceClient({"o1": 5.0})                    # but it already filled

    # fair value + a fresh post decision at a DIFFERENT price -> should_replace
    monkeypatch.setattr(app.engine, "fair_value", lambda *a, **k: make_fv())
    monkeypatch.setattr(app.planner, "plan_entry", lambda *a, **k: Decision(
        market_id="m1", ts=T0, action="post", reason="edge", outcome=Outcome.UP,
        fair_p=0.6, target_price=0.30))
    monkeypatch.setattr(app.books, "get_top", lambda *a, **k: None)

    app._evaluate(market, T0)  # now = open+0 -> 2 min left

    assert app.client.posted == []                  # NO repost on top of the fill
    assert app.positions["m1"].contracts == pytest.approx(5.0)
    assert app.risk.open_positions == 1
    assert "m1" not in app.orders                   # fully-filled handle dropped


# ── live-validation circuit breaker ───────────────────────────────────────────
def _seed_live_trades(app, n, net_each, is_live=1, resolved=1):
    c = app.store._conn
    for i in range(n):
        c.execute(
            "INSERT INTO trades (market_id, outcome_side, executable_price,"
            " contracts, fee_paid, entry_ts, resolved, resolution_outcome,"
            " net_pnl, is_live) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"m{i}", "UP", 0.2, 5.0, 0.0, T0 + i, resolved, "UP",
             net_each, is_live))
    c.commit()


@pytest.fixture
def guarded(settings):
    s = settings.model_copy(update={"prometheus_enabled": False, "live_mode": True,
                                    "live_guard_min_trades": 20})
    app = TradingApp(s.model_copy(update={"live_mode": False}))  # build in paper
    app.s = s                                                    # then flip to live
    app._live_halted = False
    return app


def test_guard_trips_when_first_n_live_trades_lose(guarded):
    _seed_live_trades(guarded, 20, net_each=-1.0)   # 20 live losers
    guarded._check_live_guard()
    assert guarded._live_halted is True


def test_guard_passes_when_first_n_live_trades_win(guarded):
    _seed_live_trades(guarded, 20, net_each=+2.0)   # 20 live winners
    guarded._check_live_guard()
    assert guarded._live_halted is False


def test_guard_waits_for_enough_samples(guarded):
    _seed_live_trades(guarded, 5, net_each=-1.0)    # only 5 < min_trades
    guarded._check_live_guard()
    assert guarded._live_halted is False


def test_guard_ignores_paper_trades(guarded):
    _seed_live_trades(guarded, 20, net_each=-1.0, is_live=0)  # paper losers
    guarded._check_live_guard()
    assert guarded._live_halted is False


# ── book-staleness guard ──────────────────────────────────────────────────────
def test_no_entry_priced_off_a_stale_book(app, monkeypatch):
    """A Polymarket WS disconnect leaves get_top returning the last-known book;
    the bot must NOT price a new entry off it (but must once it's fresh again)."""
    from bot.models import Decision, Order, Side
    market = MarketInfo(
        market_id="m1", slug="btc-updown-5m-1750000000", question="q",
        window="5m", window_seconds=300, open_ts=T0, close_ts=T0 + 120,
        resolution_feed=ResolutionFeed.CHAINLINK, token_up="TU", token_down="TD",
        opening_reference=100_000.0)
    app.markets[market.market_id] = market
    monkeypatch.setattr(app.engine, "fair_value", lambda *a, **k: make_fv())
    monkeypatch.setattr(app.planner, "plan_entry", lambda *a, **k: Decision(
        market_id="m1", ts=T0, action="post", reason="edge", outcome=Outcome.UP,
        fair_p=0.6, target_price=0.40))
    posted = []

    class C:
        def place_maker_order(self, *a, **k):
            posted.append(a)
            return Order(order_id="o1", market_id="m1", token_id="TU",
                         outcome=Outcome.UP, side=Side.BUY, price=0.40,
                         size=5.0, placed_ts=T0)

        def cancel_order(self, oid):
            return True

        def order_matched(self, oid):
            return None
    app.client = C()

    app.books._last_msg_ts = T0 - 100.0   # book 100s old -> stale
    app._evaluate(market, T0)
    assert posted == []                   # no entry off a stale book
    assert "m1" not in app.orders

    app.books._last_msg_ts = T0           # book fresh again
    app._evaluate(market, T0)
    assert posted != []                   # now it posts (guard was the blocker)
