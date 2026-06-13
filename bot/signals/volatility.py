"""Realised-vol estimator feeding sigma. LOAD-BEARING (Claude.md §3):
a wrong sigma miscalibrates every probability. Calibrated per window length via
the per-window `sigma_scale` multipliers (tune until the calibration plot is flat).

Estimates per-second log-return variance from the fast spot buffer (spot vol is a
good proxy for resolution-feed vol over minutes; the resolution feed itself ticks
too slowly for stable estimation), then scales to the window horizon:

    sigma_window(price units) = price * sqrt(var_per_sec * window_seconds) * scale
"""

from __future__ import annotations

import math
from typing import Dict, Optional

from ..data_feeds.base import RollingPriceBuffer


class VolatilityEstimator:
    def __init__(self, buffer: RollingPriceBuffer, sigma_scale: Dict[str, float],
                 min_samples: int = 30, lookback_s: float = 1800.0,
                 sample_interval_s: float = 1.0):
        self.buffer = buffer
        self.sigma_scale = sigma_scale
        self.min_samples = min_samples
        self.lookback_s = lookback_s
        self.sample_interval_s = sample_interval_s

    def var_per_second(self) -> Optional[float]:
        """Per-second variance of log returns, from ~1s-resampled buffer prices."""
        snap = self.buffer.snapshot()
        if len(snap) < self.min_samples:
            return None
        end_ts = snap[-1][0]
        start_ts = end_ts - self.lookback_s
        # Resample to a regular grid so trade-arrival clustering doesn't bias variance.
        samples = []
        t = max(start_ts, snap[0][0])
        i = 0
        while t <= end_ts:
            while i + 1 < len(snap) and snap[i + 1][0] <= t:
                i += 1
            samples.append(snap[i][1])
            t += self.sample_interval_s
        if len(samples) < self.min_samples:
            return None
        rets = [math.log(samples[k] / samples[k - 1]) for k in range(1, len(samples))
                if samples[k - 1] > 0]
        if len(rets) < self.min_samples - 1:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
        return var / self.sample_interval_s

    def sigma_for_window(self, window: str, window_seconds: int,
                         price: float) -> Optional[float]:
        """Price-unit sigma over the full window horizon, or None if not estimable."""
        var_s = self.var_per_second()
        if var_s is None or var_s <= 0 or price <= 0:
            return None
        scale = self.sigma_scale.get(window, 1.0)
        return price * math.sqrt(var_s * window_seconds) * scale
