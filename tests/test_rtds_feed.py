"""RTDS Chainlink feed: message parsing and the no-fallback guarantee."""

import json

from bot.data_feeds.rtds_chainlink import RTDSChainlinkFeed


def make_msg(topic="crypto_prices_chainlink", symbol="btc/usd",
             value=104500.5, ts_ms=1781100000000):
    return json.dumps({"topic": topic, "type": "update", "timestamp": ts_ms,
                       "payload": {"symbol": symbol, "timestamp": ts_ms,
                                   "value": value}})


def test_parses_documented_update_shape():
    feed = RTDSChainlinkFeed()
    parsed = feed.parse_message(make_msg())
    assert parsed == (104500.5, 1781100000.0)


def test_ignores_binance_topic_on_same_socket():
    """crypto_prices (Binance) on the same socket must NOT feed the resolution
    price — that would be the silent spot fallback the spec forbids."""
    feed = RTDSChainlinkFeed()
    assert feed.parse_message(make_msg(topic="crypto_prices",
                                       symbol="btcusdt")) is None


def test_ignores_other_symbols_and_garbage():
    feed = RTDSChainlinkFeed()
    assert feed.parse_message(make_msg(symbol="eth/usd")) is None
    assert feed.parse_message("PONG") is None
    assert feed.parse_message("{not json") is None
    assert feed.parse_message(json.dumps({"topic": "crypto_prices_chainlink",
                                          "payload": {}})) is None


def test_update_feeds_buffer_and_staleness():
    feed = RTDSChainlinkFeed()
    assert feed.is_stale(10.0)
    price, ts = feed.parse_message(make_msg())
    feed._update(price, ts)
    assert feed.last_value == 104500.5
    assert not feed.is_stale(10.0, now=ts + 5)
    assert feed.is_stale(10.0, now=ts + 11)
    assert feed.opening_reference(ts + 1) == 104500.5
    assert feed.opening_reference(ts - 1) is None


def test_subscribe_message_uses_empty_filter():
    """Server-side symbol filters are broken (live DB error, 2026-06-10):
    subscribe to the whole topic and filter client-side."""
    feed = RTDSChainlinkFeed(symbol="btc/usd")
    msg = json.loads(feed._subscribe_message())
    sub = msg["subscriptions"][0]
    assert msg["action"] == "subscribe"
    assert sub["topic"] == "crypto_prices_chainlink"
    assert sub["filters"] == ""


def test_prefers_full_accuracy_value():
    """Live frames carry an 18-decimal fixed-point price; resolution math
    compares close vs open within dollars, so use it over the rounded float."""
    feed = RTDSChainlinkFeed()
    raw = json.dumps({
        "connection_id": "x", "topic": "crypto_prices_chainlink",
        "type": "update", "timestamp": 1781118711021,
        "payload": {"full_accuracy_value": "61768703096330325000000",
                    "symbol": "btc/usd", "timestamp": 1781118710000,
                    "value": 61768.7},
    })
    price, ts = feed.parse_message(raw)
    assert price == 61768.703096330325
    assert ts == 1781118710.0


def test_subscription_error_frame_is_ignored():
    feed = RTDSChainlinkFeed()
    raw = json.dumps({"body": {"message": "leger AddSubscriptions error: rpc"}})
    assert feed.parse_message(raw) is None
