"""Discovery parsing (exact slugs, feed detection) and the trade/calibration store."""

import json

from bot.logging_db import BotStore
from bot.market_discovery import (detect_resolution_feed, detect_window,
                                  parse_market, parse_resolution)
from bot.models import Decision, Fill, Outcome, ResolutionFeed, Side
from tests.test_strategy import make_market, T0

PREFIXES = {"btc-up-or-down-5m": "5m", "btc-up-or-down-15m": "15m",
            "bitcoin-up-or-down-": "1h"}


def test_parse_resolution_decisive_outcomes():
    up = {"closed": True, "outcomes": '["Up", "Down"]',
          "outcomePrices": '["1", "0"]'}
    down = {"closed": True, "outcomes": ["Up", "Down"],
            "outcomePrices": ["0", "1"]}
    assert parse_resolution(up) == "UP"
    assert parse_resolution(down) == "DOWN"


def test_parse_resolution_decisive_even_when_not_closed():
    # Polymarket leaves closed=false for a while after the price resolves; a
    # decisive split is still the real outcome (settlement only reads this post-close).
    assert parse_resolution({"closed": False, "outcomes": '["Up","Down"]',
                             "outcomePrices": '["0.0005","0.9995"]'}) == "DOWN"


def test_parse_resolution_none_when_unresolved_or_ambiguous():
    assert parse_resolution({"closed": True, "outcomes": '["Up","Down"]',
                             "outcomePrices": '["0.62","0.38"]'}) is None  # mid
    assert parse_resolution({"closed": True}) is None                       # no prices
    assert parse_resolution({"closed": True, "outcomes": '["Yes","No"]',
                             "outcomePrices": '["1","0"]'}) is None  # not Up/Down


def test_detect_window_exact_prefix_only():
    assert detect_window("btc-up-or-down-15m-1230pm-et", PREFIXES) == "15m"
    assert detect_window("bitcoin-up-or-down-june-10-3pm-et", PREFIXES) == "1h"
    assert detect_window("btc-up-down-15m-fuzzy", PREFIXES) is None  # no fuzzy match
    assert detect_window("eth-up-or-down-15m", PREFIXES) is None


def test_detect_resolution_feed_description_wins():
    assert detect_resolution_feed("Resolves via the Chainlink BTC/USD stream",
                                  "1h") == ResolutionFeed.CHAINLINK
    assert detect_resolution_feed("Resolves on Binance BTCUSDT 1 hour candle",
                                  "15m") == ResolutionFeed.BINANCE
    # defaults per window when description is silent
    assert detect_resolution_feed("", "15m") == ResolutionFeed.CHAINLINK
    assert detect_resolution_feed("", "1h") == ResolutionFeed.BINANCE


def test_parse_market_maps_tokens_by_outcome():
    m = {
        "id": "123", "slug": "btc-up-or-down-15m-x",
        "question": "BTC Up or Down?",
        "endDate": "2026-06-10T15:15:00Z",
        "clobTokenIds": json.dumps(["tok_down", "tok_up"]),
        "outcomes": json.dumps(["Down", "Up"]),   # reversed order on purpose
        "description": "chainlink",
    }
    info = parse_market(m, PREFIXES)
    assert info is not None
    assert info.token_up == "tok_up"
    assert info.token_down == "tok_down"
    assert info.window == "15m"
    assert info.close_ts - info.open_ts == 900


def test_parse_market_rejects_incomplete():
    assert parse_market({"slug": "btc-up-or-down-15m-x"}, PREFIXES) is None
    assert parse_market({"slug": "other-market", "endDate": "2026-06-10T15:15:00Z"},
                        PREFIXES) is None


def test_store_roundtrip(settings):
    store = BotStore(settings.db_path)
    market = make_market()
    from tests.test_execution import _fv
    fv = _fv(0.80)

    store.record_decision(Decision(market_id="m1", ts=T0, action="reject",
                                   reason="edge_below_margin"))
    fill = Fill(order_id="o1", market_id="m1", token_id="TU", outcome=Outcome.UP,
                side=Side.BUY, price=0.58, size=10, fee=0.0, ts=T0, is_maker=True)
    store.record_entry(market, fv, fill, fee_avoided=0.17)
    store.record_calibration(market, fv)

    store.resolve_trades("m1", "UP")
    store.resolve_calibration("m1", up_won=True)

    s = store.pnl_summary()
    assert s["resolved_trades"] == 1
    # won: 10 * (1 - 0.58) = +4.20, zero maker fee
    assert abs(s["net_pnl"] - 4.20) < 1e-9

    cal = store.calibration_report()
    assert len(cal) == 1
    assert cal[0]["n"] == 1
    assert cal[0]["realised_freq"] == 1.0
    store.close()


def test_store_loss_settlement(settings):
    store = BotStore(settings.db_path)
    market = make_market()
    from tests.test_execution import _fv
    fill = Fill(order_id="o1", market_id="m1", token_id="TU", outcome=Outcome.UP,
                side=Side.BUY, price=0.58, size=10, fee=0.0, ts=T0)
    store.record_entry(market, _fv(0.80), fill, fee_avoided=0.17)
    store.resolve_trades("m1", "DOWN")
    s = store.pnl_summary()
    assert abs(s["net_pnl"] - (-5.80)) < 1e-9   # lost the full cost
    store.close()
