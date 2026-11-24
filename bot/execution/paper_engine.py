"""Paper engine with realistic maker semantics.

A resting order fills ONLY when the book trades through its price, after a
queue-position penalty is consumed. Instant fills at mid are forbidden — they
are why backtests lie. Wire `on_trade` to PolymarketBookFeed trade prints.
"""

from __future__ import annotations

import itertools
import logging
import random
import threading
from typing import Callable, Dict, List, Optional

from ..models import Fill, Order, Outcome, Side
from ..utils import utc_ts

log = logging.getLogger(__name__)

FillCallback = Callable[[Fill], None]


class PaperEngine:
    def __init__(self, queue_penalty: float = 1.5, non_fill_prob: float = 0.0,
                 rng: Optional[random.Random] = None):
        self.queue_penalty = queue_penalty
        self.non_fill_prob = non_fill_prob
        self.rng = rng or random.Random()
        self._orders: Dict[str, Order] = {}
        self._queue_left: Dict[str, float] = {}
        self._fill_callbacks: List[FillCallback] = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def on_fill(self, cb: FillCallback) -> None:
        self._fill_callbacks.append(cb)

    def place_order(self, market_id: str, token_id: str, outcome: Outcome,
                    side: Side, price: float, size: float,
                    visible_queue_ahead: float = 0.0) -> Order:
        order = Order(
            order_id=f"paper-{next(self._ids)}",
            market_id=market_id, token_id=token_id, outcome=outcome,
            side=side, price=price, size=size, placed_ts=utc_ts(),
            is_maker=True, queue_ahead=visible_queue_ahead * self.queue_penalty,
        )
        with self._lock:
            self._orders[order.order_id] = order
            self._queue_left[order.order_id] = order.queue_ahead
        log.debug("paper order %s: %s %s %.0f @ %.3f (queue %.0f)",
                  order.order_id, side.value, outcome.value, size, price,
                  order.queue_ahead)
        return order

    def cancel_order(self, order_id: str) -> bool:
        with self._lock:
            self._queue_left.pop(order_id, None)
            return self._orders.pop(order_id, None) is not None

    def open_orders(self) -> List[Order]:
        with self._lock:
            return list(self._orders.values())

    def on_trade(self, token_id: str, price: float, size: float, ts: float) -> None:
        """A trade printed on the book. Fill resting orders it trades through."""
        fills: List[Fill] = []
        with self._lock:
            for order in list(self._orders.values()):
                if order.token_id != token_id:
                    continue
                # Our BUY at p fills when the market trades at or below p
                # (someone sold down through our level); symmetric for SELL.
                trades_through = (price <= order.price + 1e-9 if order.side == Side.BUY
                                  else price >= order.price - 1e-9)
                if not trades_through:
                    continue
                if self.non_fill_prob > 0 and self.rng.random() < self.non_fill_prob:
                    continue
                # Queue ahead of us absorbs volume first.
                q = self._queue_left.get(order.order_id, 0.0)
                if q >= size:
                    self._queue_left[order.order_id] = q - size
                    continue
                available = size - q
                self._queue_left[order.order_id] = 0.0
                fill_size = min(available, order.remaining)
                if fill_size <= 0:
                    continue
                order.filled += fill_size
                fills.append(Fill(
                    order_id=order.order_id, market_id=order.market_id,
                    token_id=token_id, outcome=order.outcome, side=order.side,
                    price=order.price, size=fill_size, fee=0.0, ts=ts, is_maker=True,
                ))
                if order.remaining <= 1e-9:
                    self._orders.pop(order.order_id, None)
                    self._queue_left.pop(order.order_id, None)
        for f in fills:
            for cb in self._fill_callbacks:
                try:
                    cb(f)
                except Exception:  # noqa: BLE001
                    log.exception("fill callback failed")
