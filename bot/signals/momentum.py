"""Short-horizon spot trend as a BOUNDED drift nudge (price units).

Not a vote — a tilt. The strategy layer additionally clamps the *total* drift so it
can never move Phi(z) by more than drift_cap (Claude.md §3). Disabled by default.
"""

from __future__ import annotations

from typing import Optional

from ..data_feeds.base import RollingPriceBuffer
from ..utils import clamp


class MomentumSignal:
    name = "momentum"

    def __init__(self, buffer: RollingPriceBuffer, lookback_s: float = 60.0,
                 gain: float = 0.3, max_sigma_frac: float = 0.25):
        self.buffer = buffer
        self.lookback_s = lookback_s
        self.gain = gain                 # fraction of the raw move carried as drift
        self.max_sigma_frac = max_sigma_frac

    def drift(self, now_ts: float, sigma_window: Optional[float]) -> float:
        if sigma_window is None or sigma_window <= 0:
            return 0.0
        snap_now = self.buffer.price_at_or_before(now_ts)
        snap_then = self.buffer.price_at_or_before(now_ts - self.lookback_s)
        if snap_now is None or snap_then is None:
            return 0.0
        raw = (snap_now - snap_then) * self.gain
        bound = sigma_window * self.max_sigma_frac
        return clamp(raw, -bound, bound)
