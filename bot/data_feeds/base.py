"""Feed base: every feed exposes last_value, as_of_timestamp, is_stale(max_age).

The strategy refuses to price a market whose resolution feed is stale (Claude.md §4).
"""

from __future__ import annotations

import bisect
import math
import threading
from collections import deque
from typing import Deque, Optional, Tuple

from ..utils import utc_ts


class RollingPriceBuffer:
    """Thread-safe rolling (timestamp, price) buffer with vol/lookback helpers."""

    def __init__(self, max_age_s: float = 7200.0):
        self.max_age_s = max_age_s
        self._buf: Deque[Tuple[float, float]] = deque()
        self._lock = threading.Lock()

    def append(self, ts: float, price: float) -> None:
        if price <= 0 or not math.isfinite(price):
            return
        with self._lock:
            if self._buf and ts < self._buf[-1][0]:
                ts = self._buf[-1][0]  # never go backwards in time
            self._buf.append((ts, price))
            cutoff = ts - self.max_age_s
            while self._buf and self._buf[0][0] < cutoff:
                self._buf.popleft()

    def snapshot(self) -> list:
        with self._lock:
            return list(self._buf)

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    def price_at_or_before(self, ts: float) -> Optional[float]:
        """Last price at or before `ts` — used for window-open references."""
        snap = self.snapshot()
        if not snap:
            return None
        times = [t for t, _ in snap]
        i = bisect.bisect_right(times, ts) - 1
        if i < 0:
            return None
        return snap[i][1]

    def price_seconds_ago(self, age_s: float, now: Optional[float] = None) -> Optional[float]:
        return self.price_at_or_before((now if now is not None else utc_ts()) - age_s)


class PriceFeed:
    """Base price feed. Subclasses call ._update(price, ts) as data arrives."""

    name = "feed"

    def __init__(self, buffer_age_s: float = 7200.0):
        self.buffer = RollingPriceBuffer(max_age_s=buffer_age_s)
        self._last_value: Optional[float] = None
        self._as_of: Optional[float] = None
        self._lock = threading.Lock()

    def _update(self, price: float, ts: Optional[float] = None) -> None:
        if price <= 0 or not math.isfinite(price):
            return
        ts = ts if ts is not None else utc_ts()
        with self._lock:
            self._last_value = price
            self._as_of = ts
        self.buffer.append(ts, price)

    @property
    def last_value(self) -> Optional[float]:
        with self._lock:
            return self._last_value

    @property
    def as_of_timestamp(self) -> Optional[float]:
        with self._lock:
            return self._as_of

    def is_stale(self, max_age_s: float, now: Optional[float] = None) -> bool:
        """True if there is no value or the newest value is older than max_age_s."""
        with self._lock:
            as_of = self._as_of
        if as_of is None:
            return True
        return ((now if now is not None else utc_ts()) - as_of) > max_age_s
