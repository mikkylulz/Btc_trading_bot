"""Optional data recorder for offline backtesting (Claude.md §9).

Persists the resolution-feed price series and order-book tops to CSV so the
backtester can replay REAL recorded books — the only mode that measures actual
edge rather than just calibration (a synthetic book is generated from the fair
value, so it's circular). Disabled by default; enable with RECORD_DATA=true.

Rows are appended and flushed per write (crash-safe; the rate is ~1/s so the
overhead is negligible). Price rows are deduped by feed timestamp so a 1 Hz
eval loop doesn't write the same tick repeatedly.
"""

from __future__ import annotations

import csv
import logging
import os
import threading
from typing import Optional

from .models import BookTop

log = logging.getLogger(__name__)


class DataRecorder:
    def __init__(self, price_path: str, book_path: str):
        self._price_path = price_path
        self._book_path = book_path
        self._lock = threading.Lock()
        self._last_price_ts: Optional[float] = None
        for path, header in ((price_path, ["ts", "price"]),
                             (book_path, ["ts", "token_id", "best_bid",
                                          "best_ask", "bid_size", "ask_size"])):
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            if not os.path.exists(path):
                with open(path, "w", newline="") as f:
                    csv.writer(f).writerow(header)

    def record_price(self, ts: float, price: Optional[float]) -> None:
        if price is None or ts is None:
            return
        with self._lock:
            if self._last_price_ts is not None and ts <= self._last_price_ts:
                return  # dedup: same/older feed tick
            self._last_price_ts = ts
            try:
                with open(self._price_path, "a", newline="") as f:
                    csv.writer(f).writerow([f"{ts:.3f}", f"{price:.4f}"])
            except OSError as e:
                log.warning("price record failed: %s", e)

    def record_book(self, ts: float, token_id: str,
                    top: Optional[BookTop]) -> None:
        if top is None:
            return
        with self._lock:
            try:
                with open(self._book_path, "a", newline="") as f:
                    csv.writer(f).writerow([
                        f"{ts:.3f}", token_id,
                        "" if top.best_bid is None else f"{top.best_bid:.4f}",
                        "" if top.best_ask is None else f"{top.best_ask:.4f}",
                        f"{top.bid_size:.2f}", f"{top.ask_size:.2f}"])
            except OSError as e:
                log.warning("book record failed: %s", e)
