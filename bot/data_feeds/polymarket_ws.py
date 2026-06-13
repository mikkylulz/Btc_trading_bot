"""Polymarket CLOB market-data websocket: live books + last trades per token.

Feeds microstructure and execution. Trade prints are also what the paper engine
uses to decide whether a resting maker order would have filled (book trades
through the price), per Claude.md §6.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Callable, Dict, List, Optional

import websockets

from ..models import BookTop
from ..utils import utc_ts

log = logging.getLogger(__name__)

# trade callback signature: (token_id, price, size, ts)
TradeCallback = Callable[[str, float, float, float], None]


class OrderBookState:
    """Price->size ladders for one token."""

    def __init__(self, token_id: str):
        self.token_id = token_id
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.as_of: float = 0.0

    def apply_snapshot(self, bids: list, asks: list, ts: float) -> None:
        self.bids = {float(l["price"]): float(l["size"]) for l in bids}
        self.asks = {float(l["price"]): float(l["size"]) for l in asks}
        self.as_of = ts

    def apply_change(self, side: str, price: float, size: float, ts: float) -> None:
        ladder = self.bids if side.upper() == "BUY" else self.asks
        if size <= 0:
            ladder.pop(price, None)
        else:
            ladder[price] = size
        self.as_of = ts

    def top(self) -> BookTop:
        bb = max(self.bids) if self.bids else None
        ba = min(self.asks) if self.asks else None
        return BookTop(
            token_id=self.token_id,
            best_bid=bb,
            best_ask=ba,
            bid_size=self.bids.get(bb, 0.0) if bb is not None else 0.0,
            ask_size=self.asks.get(ba, 0.0) if ba is not None else 0.0,
            as_of=self.as_of,
        )

    def size_at_or_above_bid(self, price: float) -> float:
        """Visible bid size at or above `price` — queue-ahead estimate for a new bid."""
        return sum(s for p, s in self.bids.items() if p >= price)


class PolymarketBookFeed:
    """Maintains books for a dynamic set of token ids; emits trade prints."""

    name = "polymarket_ws"

    def __init__(self, ws_url: str, heartbeat_timeout_s: float = 30.0):
        self.ws_url = ws_url
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self._books: Dict[str, OrderBookState] = {}
        self._tokens: List[str] = []
        self._tokens_changed = asyncio.Event()
        self._trade_callbacks: List[TradeCallback] = []
        self._lock = threading.Lock()
        self._stop = asyncio.Event()
        self._last_msg_ts: Optional[float] = None

    # ── public API ────────────────────────────────────────────────────────────
    def set_tokens(self, token_ids: List[str]) -> None:
        with self._lock:
            new = sorted(set(token_ids))
            if new != self._tokens:
                self._tokens = new
                self._tokens_changed.set()

    def on_trade(self, cb: TradeCallback) -> None:
        self._trade_callbacks.append(cb)

    def get_top(self, token_id: str) -> Optional[BookTop]:
        with self._lock:
            st = self._books.get(token_id)
            return st.top() if st else None

    def get_book(self, token_id: str) -> Optional[OrderBookState]:
        with self._lock:
            return self._books.get(token_id)

    def is_stale(self, max_age_s: float, now: Optional[float] = None) -> bool:
        if self._last_msg_ts is None:
            return True
        return ((now if now is not None else utc_ts()) - self._last_msg_ts) > max_age_s

    # ── message handling (sync, unit-testable) ───────────────────────────────
    def handle_raw(self, raw) -> None:
        """One websocket frame: keepalive PONGs and non-JSON are ignored."""
        if raw in ("PONG", "PING", "pong", "ping"):
            self._last_msg_ts = utc_ts()
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        for m in (data if isinstance(data, list) else [data]):
            if isinstance(m, dict):
                self.handle_message(m)

    def handle_message(self, msg: dict) -> None:
        ts = utc_ts()
        self._last_msg_ts = ts
        ev = msg.get("event_type")
        token = msg.get("asset_id", "")
        if ev == "book":
            with self._lock:
                st = self._books.setdefault(token, OrderBookState(token))
                st.apply_snapshot(msg.get("bids", msg.get("buys", [])),
                                  msg.get("asks", msg.get("sells", [])), ts)
        elif ev == "price_change":
            with self._lock:
                st = self._books.setdefault(token, OrderBookState(token))
                for ch in msg.get("changes", []):
                    st.apply_change(ch.get("side", ""), float(ch.get("price", 0)),
                                    float(ch.get("size", 0)), ts)
        elif ev == "last_trade_price":
            try:
                price = float(msg.get("price", 0))
                size = float(msg.get("size", 0) or 0)
            except (TypeError, ValueError):
                return
            if price > 0:
                for cb in self._trade_callbacks:
                    try:
                        cb(token, price, size, ts)
                    except Exception:  # noqa: BLE001
                        log.exception("trade callback failed")

    # ── connection loop ───────────────────────────────────────────────────────
    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            with self._lock:
                tokens = list(self._tokens)
            if not tokens:
                self._tokens_changed.clear()
                try:
                    await asyncio.wait_for(self._tokens_changed.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                # The CLOB ws ignores protocol-level pings (it never PONGs them,
                # which made the websockets lib kill the connection every ~50s
                # with "keepalive ping timeout"). It wants an application-level
                # text "PING" every ~10s instead — so protocol pings are off.
                async with websockets.connect(self.ws_url, ping_interval=None,
                                              close_timeout=5) as ws:
                    await ws.send(json.dumps({"type": "market", "assets_ids": tokens}))
                    log.info("polymarket ws subscribed to %d tokens", len(tokens))
                    backoff = 1.0
                    self._tokens_changed.clear()
                    loop = asyncio.get_event_loop()
                    last_ping = 0.0
                    last_rx = loop.time()
                    while not self._stop.is_set():
                        if self._tokens_changed.is_set():
                            break  # token set changed: reconnect with new subscription
                        now_mono = loop.time()
                        if now_mono - last_ping >= 10.0:
                            await ws.send("PING")
                            last_ping = now_mono
                        if now_mono - last_rx > self.heartbeat_timeout_s:
                            log.warning("polymarket ws heartbeat timeout — reconnecting")
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue  # quiet book; loop to keep PINGs flowing
                        last_rx = loop.time()
                        self.handle_raw(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("polymarket ws error (%s) — reconnecting in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def stop(self) -> None:
        self._stop.set()
