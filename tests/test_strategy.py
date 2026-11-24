"""Unit tests for the fair-value engine: Phi(z) math + edge cases."""

import math

import pytest

from bot.data_feeds.base import PriceFeed
from bot.models import MarketInfo, Outcome, ResolutionFeed
from bot.signals.volatility import VolatilityEstimator
from bot.strategy import (FairValueEngine, SigmaUnavailableError,
                          StaleResolutionFeedError)
from bot.utils import phi

T0 = 1_750_000_000.0


def make_market(window="15m", open_ts=T0, resolution=ResolutionFeed.CHAINLINK,
                opening_reference=100_000.0):
    secs = {"5m": 300, "15m": 900, "1h": 3600}[window]
    return MarketInfo(
        market_id="m1", slug="btc-test", question="q", window=window,
        window_seconds=secs, open_ts=open_ts, close_ts=open_ts + secs,
        resolution_feed=resolution, token_up="TU", token_down="TD",
        opening_reference=opening_reference)


def make_feed(price=100_050.0, ts=None):
    feed = PriceFeed()
    feed._update(price, ts if ts is not None else T0 + 450)
    return feed


class FixedVol(VolatilityEstimator):
    def __init__(self, sigma):
        self._sigma = sigma

    def sigma_for_window(self, window, window_seconds, price):
        return self._sigma


def make_engine(settings, sigma=100.0):
    return FairValueEngine(settings, FixedVol(sigma))


def test_phi_basics():
    assert phi(0.0) == pytest.approx(0.5)
    assert phi(10) == pytest.approx(1.0, abs=1e-9)
    assert phi(-10) == pytest.approx(0.0, abs=1e-9)
    assert phi(1.0) > phi(0.5) > phi(0.0)


def test_early_window_p_near_half(settings):
    """Early in the window remaining_time ~ 1 and a modest move barely registers."""
    eng = make_engine(settings, sigma=200.0)
    m = make_market()
    feed = make_feed(price=100_010.0, ts=T0 + 30)
    fv = eng.fair_value(m, feed, now_ts=T0 + 30)
    assert 0.45 < fv.p_up < 0.60


def test_late_window_move_dominates(settings):
    """remaining_time -> 0: the same move drives Phi(z) to the tail (the 'snipe')."""
    eng = make_engine(settings, sigma=200.0)
    m = make_market()
    feed = make_feed(price=100_100.0, ts=T0 + 880)
    fv = eng.fair_value(m, feed, now_ts=T0 + 880)
    assert fv.p_up > 0.95


def test_remaining_time_zero_tie_resolves_up(settings):
    """At expiry with zero move, ties resolve Up (close >= open, §1.1)."""
    eng = make_engine(settings)
    m = make_market(window="1h")
    feed = make_feed(price=100_000.0, ts=T0 + 3600)
    fv = eng.fair_value(m, feed, now_ts=T0 + 3600)
    assert fv.p_up == 1.0


def test_remaining_time_zero_down_move(settings):
    eng = make_engine(settings)
    m = make_market()
    feed = make_feed(price=99_900.0, ts=T0 + 900)
    fv = eng.fair_value(m, feed, now_ts=T0 + 901)
    assert fv.p_up == 0.0


def test_stale_resolution_feed_raises(settings):
    eng = make_engine(settings)
    m = make_market()
    feed = make_feed(price=100_050.0, ts=T0)  # last tick at window open
    with pytest.raises(StaleResolutionFeedError):
        eng.fair_value(m, feed, now_ts=T0 + 600)  # 600s stale > max_feed_age_s


def test_missing_opening_reference_refuses(settings):
    eng = make_engine(settings)
    m = make_market(opening_reference=None)
    feed = make_feed(price=100_050.0, ts=T0 + 450)  # buffer has nothing pre-open
    with pytest.raises(StaleResolutionFeedError):
        eng.fair_value(m, feed, now_ts=T0 + 450)


def test_sigma_unavailable_refuses(settings):
    eng = make_engine(settings, sigma=None)
    m = make_market()
    feed = make_feed(ts=T0 + 450)
    with pytest.raises(SigmaUnavailableError):
        eng.fair_value(m, feed, now_ts=T0 + 450)


def test_drift_cap_limits_signal_influence(settings):
    """A huge drift signal cannot move Phi(z) by more than drift_cap (§3)."""

    class HugeDrift:
        def drift(self, now_ts, sigma):
            return 1e9

    settings_d = settings.model_copy(update={"enable_momentum": True})
    eng = FairValueEngine(settings_d, FixedVol(200.0), momentum=HugeDrift())
    m = make_market()
    feed = make_feed(price=100_010.0, ts=T0 + 450)
    fv = eng.fair_value(m, feed, now_ts=T0 + 450)
    eng_base = make_engine(settings, sigma=200.0)
    base = eng_base.fair_value(m, make_feed(price=100_010.0, ts=T0 + 450),
                               now_ts=T0 + 450)
    assert abs(fv.p_up - base.p_up) <= settings.drift_cap + 1e-9


def test_p_for_outcome_symmetry(settings):
    eng = make_engine(settings, sigma=200.0)
    m = make_market()
    fv = eng.fair_value(m, make_feed(price=100_080.0, ts=T0 + 700), now_ts=T0 + 700)
    assert fv.p_for(Outcome.UP) + fv.p_for(Outcome.DOWN) == pytest.approx(1.0)
    assert math.isfinite(fv.z)
