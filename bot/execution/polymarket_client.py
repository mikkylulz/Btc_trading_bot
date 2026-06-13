"""Execution clients. Paper and live share one interface so the strategy never
knows which it's talking to (Claude.md §6).

LiveClient wraps the official `py-clob-client` (imported lazily so paper mode and
tests never need it installed). It enforces maker-only: an order priced at or
through the opposite side is rejected unless allow_taker=true is set in config
with a documented reason.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import List, Optional

from ..config import Settings
from ..models import BookTop, Order, Outcome, Side
from ..utils import utc_ts
from .paper_engine import PaperEngine

log = logging.getLogger(__name__)


class TakerOrderForbidden(Exception):
    """Order would cross the spread and allow_taker is false (crypto default)."""


class ExecutionClient(ABC):
    @abstractmethod
    def place_maker_order(self, market_id: str, token_id: str, outcome: Outcome,
                          side: Side, price: float, size: float,
                          top: Optional[BookTop] = None) -> Order: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    def open_orders(self) -> List[Order]: ...


def _assert_not_crossing(side: Side, price: float, top: Optional[BookTop],
                         allow_taker: bool) -> None:
    if allow_taker or top is None:
        return
    if side == Side.BUY and top.best_ask is not None and price >= top.best_ask:
        raise TakerOrderForbidden(
            f"BUY {price} >= ask {top.best_ask}: would take; allow_taker=false")
    if side == Side.SELL and top.best_bid is not None and price <= top.best_bid:
        raise TakerOrderForbidden(
            f"SELL {price} <= bid {top.best_bid}: would take; allow_taker=false")


class PaperClient(ExecutionClient):
    def __init__(self, settings: Settings, engine: PaperEngine,
                 book_feed=None):
        self.s = settings
        self.engine = engine
        self.book_feed = book_feed

    def place_maker_order(self, market_id, token_id, outcome, side, price, size,
                          top=None):
        _assert_not_crossing(side, price, top, self.s.allow_taker)
        queue = 0.0
        if self.book_feed is not None:
            book = self.book_feed.get_book(token_id)
            if book is not None and side == Side.BUY:
                queue = book.size_at_or_above_bid(price)
        return self.engine.place_order(market_id, token_id, outcome, side, price,
                                       size, visible_queue_ahead=queue)

    def cancel_order(self, order_id: str) -> bool:
        return self.engine.cancel_order(order_id)

    def open_orders(self) -> List[Order]:
        return self.engine.open_orders()


class LiveClient(ExecutionClient):
    """Real CLOB orders via py-clob-client-v2.

    Why v2 and not the original py-clob-client: the old client's order builder
    rejects signature_type=3 (POLY_1271, the deposit-wallet setup confirmed
    working 2026-05-25) with "Invalid order inputs". v2 supports it — this is
    the same library the sister bots use for real fills. Install for live mode:
        pip install py-clob-client-v2

    Maker-first is preserved (Claude.md §1.2/§6): orders are GTC limit orders,
    and unless allow_taker=true they are submitted post_only=True, so the
    EXCHANGE rejects any order that would cross the spread — a second guard
    behind _assert_not_crossing."""

    def __init__(self, settings: Settings):
        self.s = settings
        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.clob_types import ApiCreds, OrderArgsV2, OrderType
            from py_clob_client_v2.constants import POLYGON
            from py_clob_client_v2.order_builder.constants import BUY, SELL
        except ImportError as e:
            raise RuntimeError(
                "py-clob-client-v2 is required for live mode: "
                "pip install py-clob-client-v2"
            ) from e
        self._OrderArgs = OrderArgsV2
        self._OrderType = OrderType
        self._BUY, self._SELL = BUY, SELL
        # signature_type from config (default 3 = POLY_1271 with the deposit
        # wallet as funder — confirmed working 2026-05-25).
        self.client = ClobClient(settings.clob_host,
                                 key=settings.polymarket_private_key,
                                 chain_id=POLYGON,
                                 signature_type=settings.polymarket_signature_type,
                                 funder=settings.polymarket_funder_address)
        # Prefer .env creds if present; otherwise derive them from the key.
        if settings.polymarket_api_key:
            self.client.set_api_creds(ApiCreds(
                api_key=settings.polymarket_api_key,
                api_secret=settings.polymarket_api_secret,
                api_passphrase=settings.polymarket_api_passphrase))
        else:
            self.client.set_api_creds(self.client.create_or_derive_api_key())

    def place_maker_order(self, market_id, token_id, outcome, side, price, size,
                          top=None):
        _assert_not_crossing(side, price, top, self.s.allow_taker)
        # Round defensively: price to the tick, size to 2dp so maker_amount
        # (size*price USDC) doesn't drift past 2 decimals and get rejected.
        px = round(price / self.s.tick_size) * self.s.tick_size
        args = self._OrderArgs(token_id=token_id, price=round(px, 4),
                               size=round(size, 2),
                               side=self._BUY if side == Side.BUY else self._SELL)
        signed = self.client.create_order(args)
        # post_only=True => exchange enforces maker (rejects a crossing order),
        # unless the operator has explicitly allowed taker on crypto.
        resp = self.client.post_order(signed, self._OrderType.GTC,
                                      post_only=not self.s.allow_taker)
        resp = resp or {}
        oid = resp.get("orderID") or resp.get("orderId") or resp.get("id") or ""
        if resp.get("success") is False or not oid:
            raise RuntimeError(f"order rejected by CLOB: {resp}")
        log.info("live order %s: %s %s %.2f @ %.3f", oid, side.value,
                 outcome.value, size, px)
        return Order(order_id=oid, market_id=market_id, token_id=token_id,
                     outcome=outcome, side=side, price=px, size=size,
                     placed_ts=utc_ts(), is_maker=True)

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel_orders([order_id])
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("cancel %s failed: %s", order_id, e)
            return False

    def open_orders(self) -> List[Order]:
        out: List[Order] = []
        try:
            for o in self.client.get_open_orders() or []:
                out.append(Order(
                    order_id=o.get("id", ""), market_id=o.get("market", ""),
                    token_id=o.get("asset_id", ""),
                    outcome=Outcome.UP,  # outcome mapping resolved by caller via token id
                    side=Side(o.get("side", "BUY")),
                    price=float(o.get("price", 0)),
                    size=float(o.get("original_size", 0)),
                    placed_ts=utc_ts(),
                    filled=float(o.get("size_matched", 0)),
                ))
        except Exception as e:  # noqa: BLE001
            log.warning("get_open_orders failed: %s", e)
        return out
