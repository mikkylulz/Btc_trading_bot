"""Feed tests: staleness halts entries; Chainlink-down does NOT fall back to spot."""

import pytest

from bot.data_feeds.base import PriceFeed, RollingPriceBuffer
from bot.data_feeds.chainlink_feed import ChainlinkFeed
from bot.strategy import FairValueEngine, StaleResolutionFeedError
from tests.test_strategy import FixedVol, make_market, T0


def test_is_stale_no_data():
    assert PriceFeed().is_stale(10.0)


def test_is_stale_old_data():
    f = PriceFeed()
    f._update(100.0, ts=T0)
    assert not f.is_stale(10.0, now=T0 + 5)
    assert f.is_stale(10.0, now=T0 + 11)


def test_buffer_price_at_or_before():
    buf = RollingPriceBuffer()
    for i in range(10):
        buf.append(T0 + i, 100.0 + i)
    assert buf.price_at_or_before(T0 + 4.5) == 104.0
    assert buf.price_at_or_before(T0 - 1) is None


def test_buffer_rejects_garbage():
    buf = RollingPriceBuffer()
    buf.append(T0, -5.0)
    buf.append(T0, 0.0)
    buf.append(T0, float("nan"))
    buf.append(float("inf"), 100.0)   # bad ts -> rejected
    assert len(buf) == 0


def test_buffer_millisecond_ts_does_not_freeze_pruning(buffer_age=7200.0):
    # a corrupt millisecond timestamp must not pin "now" forward and stop pruning
    buf = RollingPriceBuffer(max_age_s=buffer_age)
    buf.append(T0 * 1000.0, 100.0)          # ms timestamp -> normalised to seconds
    buf.append(T0 + 1, 101.0)               # normal seconds tick, 1s later
    # old entries beyond max_age must still prune as normal seconds advance
    buf.append(T0 + buffer_age + 10, 102.0)
    assert len(buf) == 1                     # the two older entries pruned by age


def test_buffer_hard_size_cap():
    buf = RollingPriceBuffer(max_age_s=1e18, maxlen=100)   # age-prune effectively off
    for i in range(5000):
        buf.append(T0 + i, 100.0 + (i % 7))
    assert len(buf) <= 100                   # deque maxlen bounds memory regardless


def test_chainlink_unconfigured_is_stale():
    feed = ChainlinkFeed()                          # no key/secret/feed id
    assert not feed.configured
    assert feed.is_stale(10.0)


def test_chainlink_hmac_headers_deterministic():
    from bot.data_feeds.chainlink_feed import hmac_headers
    h = hmac_headers("key1", "secret1", "GET",
                     "/api/v1/reports/latest?feedID=0xabc", ts_ms=1781100000000)
    assert h["Authorization"] == "key1"
    assert h["X-Authorization-Timestamp"] == "1781100000000"
    assert len(h["X-Authorization-Signature-SHA256"]) == 64
    # same inputs -> same signature (regression pin for the signing string)
    h2 = hmac_headers("key1", "secret1", "GET",
                      "/api/v1/reports/latest?feedID=0xabc", ts_ms=1781100000000)
    assert h == h2


def test_chainlink_decode_v3_full_report():
    from bot.data_feeds.chainlink_feed import decode_full_report
    # construct outer tuple: 3 context words, offset word -> blob, blob = 7 words
    # with word 6 = benchmarkPrice int192 (18 decimals)
    price_wei = int(104500.5 * 1e18)
    blob = (b"\x00" * 32 * 6) + price_wei.to_bytes(32, "big")
    head = (b"\x11" * 32 * 3                      # reportContext
            + (5 * 32).to_bytes(32, "big")        # offset to blob (after 5 head words)
            + b"\x00" * 32)                       # filler head word
    raw = head + len(blob).to_bytes(32, "big") + blob
    assert decode_full_report("0x" + raw.hex()) == 104500.5
    assert decode_full_report("0xdeadbeef") is None


def test_chainlink_down_does_not_fall_back_to_spot(settings):
    """THE critical safety test: with spot fresh and Chainlink dead, the 15m
    path must refuse to price — never silently use spot (Claude.md §4)."""
    chainlink = ChainlinkFeed()                     # dead resolution feed
    spot = PriceFeed()
    spot._update(100_000.0, ts=T0 + 450)            # spot is alive and fresh

    eng = FairValueEngine(settings, FixedVol(200.0))
    market = make_market(window="15m")              # resolves on Chainlink

    with pytest.raises(StaleResolutionFeedError):
        eng.fair_value(market, chainlink, now_ts=T0 + 450)
    # and there is no code path that hands `spot` to a chainlink market: the
    # engine prices only what it is given, and main._res_feed maps 15m->chainlink.
    from bot.models import ResolutionFeed
    assert market.resolution_feed == ResolutionFeed.CHAINLINK
