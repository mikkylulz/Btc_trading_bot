"""Polymarket order-book imbalance as a bounded drift nudge.

Top-of-book imbalance on the contract itself can carry short-horizon information
about where the contract price is going. Disabled by default.
"""

from __future__ import annotations

from typing import Optional

from ..models import BookTop
from ..utils import clamp


class MicrostructureSignal:
    name = "microstructure"

    def __init__(self, gain: float = 0.1, max_sigma_frac: float = 0.15):
        self.gain = gain
        self.max_sigma_frac = max_sigma_frac

    def drift(self, up_top: Optional[BookTop], sigma_window: Optional[float]) -> float:
        if up_top is None or sigma_window is None or sigma_window <= 0:
            return 0.0
        total = up_top.bid_size + up_top.ask_size
        if total <= 0:
            return 0.0
        imbalance = (up_top.bid_size - up_top.ask_size) / total  # [-1, 1]
        bound = sigma_window * self.max_sigma_frac
        return clamp(imbalance * self.gain * sigma_window, -bound, bound)
