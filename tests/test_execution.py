"""Execution: maker order does not fill unless the book trades through; non-fill
is handled; crossing orders are forbidden when allow_taker=false."""

import random

import pytest

from bot.execution.paper_engine import PaperEngine
from bot.execution.polymarket_client import PaperClient, TakerOrderForbidden
from bot.models import BookTop, FairValue, MarketInfo, Outcome, ResolutionFeed, Side
from bot.execution.orders import OrderPlanner


def make_engine(**kw):
    return PaperEngine(queue_penalty=1.0, rng=random.Random(0), **kw)


def place(engine, price=0.60, size=10, queue=0.0):
    return engine.place_order("m1", "TOK", Outcome.UP, Side.BUY, price, size,
                              visible_queue_ahead=queue)


def test_no_fill_without_trade_through():
    eng = make_engine()
    fills = []
    eng.on_fill(fills.append)
    place(eng, price=0.60)
    eng.on_trade("TOK", 0.65, 100, ts=1.0)   # trades above our bid: no fill
    assert fills == []
    assert len(eng.open_orders()) == 1


def test_fill_on_trade_through():
    eng = make_engine()
    fills = []
    eng.on_fill(fills.append)
    place(eng, price=0.60, size=10)
    eng.on_trade("TOK", 0.59, 25, ts=1.0)    # trades through our bid
    assert len(fills) == 1
    assert fills[0].size == 10
    assert fills[0].price == 0.60
    assert fills[0].fee == 0.0               # maker pays zero
    assert eng.open_orders() == []


def test_queue_position_consumes_volume_first():
    eng = make_engine()
    fills = []
    eng.on_fill(fills.append)
    place(eng, price=0.60, size=10, queue=30)
    eng.on_trade("TOK", 0.60, 20, ts=1.0)    # queue 30 -> 10, no fill
    assert fills == []
    eng.on_trade("TOK", 0.60, 15, ts=2.0)    # queue absorbed, 5 left for us
    assert len(fills) == 1
    assert fills[0].size == 5
    eng.on_trade("TOK", 0.60, 100, ts=3.0)   # remaining 5 fill
    assert sum(f.size for f in fills) == 10


def test_non_fill_probability_one_never_fills():
    eng = PaperEngine(queue_penalty=1.0, non_fill_prob=1.0, rng=random.Random(0))
    fills = []
    eng.on_fill(fills.append)
    place(eng, price=0.60)
    eng.on_trade("TOK", 0.50, 1000, ts=1.0)
    assert fills == []                       # non-fills are normal, not an error


def test_cancel_removes_order():
    eng = make_engine()
    o = place(eng)
    assert eng.cancel_order(o.order_id)
    assert eng.open_orders() == []
    assert not eng.cancel_order("nope")


def test_crossing_order_forbidden_by_default(settings):
    client = PaperClient(settings, make_engine())
    top = BookTop(token_id="TOK", best_bid=0.55, best_ask=0.60)
    with pytest.raises(TakerOrderForbidden):
        client.place_maker_order("m1", "TOK", Outcome.UP, Side.BUY, 0.60, 10, top=top)


def test_crossing_allowed_only_with_explicit_flag(settings):
    s = settings.model_copy(update={"allow_taker": True})
    client = PaperClient(s, make_engine())
    top = BookTop(token_id="TOK", best_bid=0.55, best_ask=0.60)
    order = client.place_maker_order("m1", "TOK", Outcome.UP, Side.BUY, 0.60, 10,
                                     top=top)
    assert order.price == 0.60


def _fv(p_up=0.80):
    return FairValue(p_up=p_up, z=1.0, sigma=100.0, realised_move=50.0,
                     remaining_frac=0.3, drift_adj=0.0, feed_price=100_050.0,
                     opening_reference=100_000.0, as_of=0.0)


def _market():
    return MarketInfo(market_id="m1", slug="s", question="q", window="15m",
                      window_seconds=900, open_ts=0, close_ts=900,
                      resolution_feed=ResolutionFeed.CHAINLINK,
                      token_up="TU", token_down="TD")


def test_planner_posts_when_edge_clears_margin(settings):
    planner = OrderPlanner(settings)
    up = BookTop(token_id="TU", best_bid=0.55, best_ask=0.60, bid_size=50, ask_size=50)
    down = BookTop(token_id="TD", best_bid=0.40, best_ask=0.45, bid_size=50, ask_size=50)
    d = planner.plan_entry(_market(), _fv(0.80), up, down)
    assert d.action == "post"
    assert d.outcome == Outcome.UP
    assert d.target_price < 0.60             # never at/through the ask (maker)


def test_planner_rejects_thin_edge_with_reason(settings):
    planner = OrderPlanner(settings)
    up = BookTop(token_id="TU", best_bid=0.55, best_ask=0.60, bid_size=50, ask_size=50)
    down = BookTop(token_id="TD", best_bid=0.40, best_ask=0.45, bid_size=50, ask_size=50)
    d = planner.plan_entry(_market(), _fv(0.57), up, down)
    assert d.action == "reject"
    assert d.reason == "edge_below_margin"   # rejections carry reasons (§8)


def test_planner_rejects_missing_book(settings):
    planner = OrderPlanner(settings)
    d = planner.plan_entry(_market(), _fv(0.80), None, None)
    assert d.action == "reject"
    assert d.reason == "no_book"


def test_entry_band_skips_below_floor(settings):
    # band 0.31-0.52: a cheap ~0.10 entry must be rejected as outside the band
    s = settings.model_copy(update={"min_entry_price": 0.31, "max_entry_price": 0.52})
    planner = OrderPlanner(s)
    up = BookTop(token_id="TU", best_bid=0.09, best_ask=0.12, bid_size=50, ask_size=50)
    down = BookTop(token_id="TD", best_bid=0.85, best_ask=0.90, bid_size=50, ask_size=50)
    d = planner.plan_entry(_market(), _fv(0.80), up, down)
    assert d.action == "reject"
    assert d.reason == "outside_price_band"


def test_entry_band_allows_inside(settings):
    # same band: a ~0.40 entry with edge clears and posts
    s = settings.model_copy(update={"min_entry_price": 0.31, "max_entry_price": 0.52})
    planner = OrderPlanner(s)
    up = BookTop(token_id="TU", best_bid=0.40, best_ask=0.45, bid_size=50, ask_size=50)
    down = BookTop(token_id="TD", best_bid=0.55, best_ask=0.60, bid_size=50, ask_size=50)
    d = planner.plan_entry(_market(), _fv(0.80), up, down)
    assert d.action == "post"
    assert 0.31 <= d.target_price <= 0.52
