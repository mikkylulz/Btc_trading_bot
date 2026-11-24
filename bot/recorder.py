"""Optional data recorder for offline backtesting.

Persists the resolution-feed price series and order-book tops to CSV so the
backtester can replay REAL recorded books — the only mode that measures actual
edge rather than just calibration (a synthetic book is generated from the fair
value, so it's circular). Disabled by default; enable with RECORD_DATA=true.

Each book row is labelled with its window `slug` and `side` (UP/DOWN) so the
backtester can align recorded tops to the right window/outcome. Rows are
appended and flushed per write (crash-safe; ~1/s, negligible overhead). Price
rows are deduped by feed timestamp. If an existing CSV's header doesn't match
(schema changed), the old file is archived to <path>.bak and a fresh one started.
"""

from __future__ import annotations

import csv
import logging
import os
import threading
from typing import Optional

from .models import BookTop

log = logging.getLogger(__name__)

_PRICE_HEADER = ["ts", "price"]
_BOOK_HEADER = ["ts", "slug", "side", "token_id",
                "best_bid", "best_ask", "bid_size", "ask_size"]
_OUTCOME_HEADER = ["slug", "outcome"]


class DataRecorder:
    def __init__(self, price_path: str, book_path: str, outcome_path: str):
        self._price_path = price_path
        self._book_path = book_path
        self._outcome_path = outcome_path
        self._lock = threading.Lock()
        self._last_price_ts: Optional[float] = None
        self._ensure(price_path, _PRICE_HEADER)
        self._ensure(book_path, _BOOK_HEADER)
        self._ensure(outcome_path, _OUTCOME_HEADER)

    def _ensure(self, path: str, header: list) -> None:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        if os.path.exists(path):
            try:
                with open(path, newline="") as f:
                    existing = next(csv.reader(f), None)
            except OSError:
                existing = None
            if existing == header:
                return  # compatible — append to it
            try:
                os.replace(path, path + ".bak")
                log.warning("recorder schema changed; archived %s -> %s.bak",
                            path, path)
            except OSError as e:
                log.warning("could not archive %s: %s", path, e)
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

    def record_outcome(self, slug: str, outcome: str) -> None:
        """The window's real (oracle) resolution — lets the backtester grade off
        Polymarket's result instead of the price-series close-vs-open proxy."""
        with self._lock:
            try:
                with open(self._outcome_path, "a", newline="") as f:
                    csv.writer(f).writerow([slug, outcome])
            except OSError as e:
                log.warning("outcome record failed: %s", e)

    def record_book(self, ts: float, slug: str, side: str, token_id: str,
                    top: Optional[BookTop]) -> None:
        if top is None:
            return
        with self._lock:
            try:
                with open(self._book_path, "a", newline="") as f:
                    csv.writer(f).writerow([
                        f"{ts:.3f}", slug, side, token_id,
                        "" if top.best_bid is None else f"{top.best_bid:.4f}",
                        "" if top.best_ask is None else f"{top.best_ask:.4f}",
                        f"{top.bid_size:.2f}", f"{top.ask_size:.2f}"])
            except OSError as e:
                log.warning("book record failed: %s", e)
