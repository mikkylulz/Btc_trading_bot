"""The fair-value engine.

    P(close >= open) = Phi(z)
    z = (drift_adj + realised_move) / (sigma * sqrt(remaining_time))

Feed handling is the subtle part: realised_move is computed against the RESOLUTION
feed for the market (Chainlink for 15m, Binance for 1h), never the fast spot feed.
A stale resolution feed raises StaleResolutionFeedError — there is no fallback.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from .config import Settings
from .data_feeds.base import PriceFeed
from .models import FairValue, MarketInfo, ResolutionFeed
from .signals.volatility import VolatilityEstimator
from .utils import clamp, phi

log = logging.getLogger(__name__)

# Below this fraction of the window we treat the outcome as determined by the
# realised move (and the tie rule) rather than evaluating Phi at an extreme z.
_REMAINING_FLOOR = 1e-6


class StaleResolutionFeedError(Exception):
    """Resolution feed is stale/missing — the market must not be priced."""


class SigmaUnavailableError(Exception):
    """No usable volatility estimate — the market must not be priced."""


class FairValueEngine:
    def __init__(self, settings: Settings, vol: VolatilityEstimator,
                 momentum=None, microstructure=None, funding=None, oi=None):
        self.s = settings
        self.vol = vol
        self.momentum = momentum
        self.microstructure = microstructure
        self.funding = funding
        self.oi = oi

    def resolution_feed_for(self, market: MarketInfo,
                            chainlink: PriceFeed, binance: PriceFeed) -> PriceFeed:
        if market.resolution_feed == ResolutionFeed.CHAINLINK:
            return chainlink
        return binance

    def fair_value(self, market: MarketInfo, res_feed: PriceFeed,
                   now_ts: float, up_top=None) -> FairValue:
        """Price one market now. Raises if the resolution feed or sigma is unusable."""
        if res_feed.is_stale(self.s.max_feed_age_s, now=now_ts):
            raise StaleResolutionFeedError(
                f"{market.resolution_feed.value} feed stale for {market.slug}; "
                "halting entries (no spot fallback)")

        feed_price = res_feed.last_value
        as_of = res_feed.as_of_timestamp or now_ts

        opening_ref = market.opening_reference
        if opening_ref is None:
            opening_ref = res_feed.buffer.price_at_or_before(market.open_ts)
            if opening_ref is not None:
                market.opening_reference = opening_ref  # pin it once observed
        if opening_ref is None or feed_price is None:
            raise StaleResolutionFeedError(
                f"no opening reference / price on {market.resolution_feed.value} "
                f"for {market.slug}")

        realised_move = feed_price - opening_ref
        remaining_frac = market.remaining_frac(now_ts)

        # Window over: outcome is determined by the move and the tie rule (1h ties -> Up).
        if remaining_frac <= _REMAINING_FLOOR:
            if realised_move > 0 or (realised_move == 0 and market.tie_rule_up):
                p = 1.0
            elif realised_move == 0:
                p = 0.0
            else:
                p = 0.0
            return FairValue(p_up=p, z=math.inf if p == 1.0 else -math.inf,
                             sigma=0.0, realised_move=realised_move,
                             remaining_frac=0.0, drift_adj=0.0,
                             feed_price=feed_price, opening_reference=opening_ref,
                             as_of=as_of)

        sigma = self.vol.sigma_for_window(market.window, market.window_seconds, feed_price)
        if sigma is None or sigma <= 0:
            raise SigmaUnavailableError(f"sigma unavailable for {market.slug}")

        denom = sigma * math.sqrt(remaining_frac)

        # Signal layer: bounded drift adjustments, then a hard probability-space clamp
        # so the sum can never move Phi(z) by more than drift_cap.
        drift = 0.0
        if self.s.enable_momentum and self.momentum is not None:
            drift += self.momentum.drift(now_ts, sigma)
        if self.s.enable_microstructure and self.microstructure is not None:
            drift += self.microstructure.drift(up_top, sigma)
        if self.s.enable_funding and self.funding is not None:
            drift += self.funding.drift()
        if self.s.enable_oi and self.oi is not None:
            drift += self.oi.drift()

        z_base = realised_move / denom
        p_base = phi(z_base)
        z = (drift + realised_move) / denom
        p = clamp(phi(z), p_base - self.s.drift_cap, p_base + self.s.drift_cap)
        p = clamp(p, 0.0, 1.0)

        return FairValue(p_up=p, z=z, sigma=sigma, realised_move=realised_move,
                         remaining_frac=remaining_frac, drift_adj=drift,
                         feed_price=feed_price, opening_reference=opening_ref,
                         as_of=as_of)
