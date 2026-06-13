"""Low-latency spot websocket feed (Binance trade stream by default).

Used for sigma estimation and the momentum drift — NEVER as a resolution feed for
15m markets (Claude.md §1.1/§4). Auto-reconnect with exponential backoff plus a
staleness watchdog that forces a reconnect if messages stop arriving.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import websockets

from .base import PriceFeed

log = logging.getLogger(__name__)


class SpotWebsocketFeed(PriceFeed):
    name = "spot_ws"

    def __init__(self, ws_url: str, heartbeat_timeout_s: float = 15.0,
                 buffer_age_s: float = 7200.0):
        super().__init__(buffer_age_s=buffer_age_s)
        self.ws_url = ws_url
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self._stop = asyncio.Event()

    def parse_message(self, raw: str) -> Optional[tuple]:
        """Return (price, ts_seconds) or None. Handles Binance trade & Coinbase ticker."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return None
        # Binance trade stream: {"p": "104500.10", "T": 1718040000123, ...}
        if "p" in msg and ("T" in msg or "E" in msg):
            ts_ms = msg.get("T") or msg.get("E")
            return float(msg["p"]), float(ts_ms) / 1000.0
        # Coinbase ticker: {"type": "ticker", "price": "104500.10", "time": "..."}
        if msg.get("type") == "ticker" and "price" in msg:
            return float(msg["price"]), None
        return None

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.ws_url, ping_interval=20,
                                              ping_timeout=20, close_timeout=5) as ws:
                    log.info("spot ws connected: %s", self.ws_url)
                    backoff = 1.0
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.heartbeat_timeout_s)
                        parsed = self.parse_message(raw)
                        if parsed:
                            price, ts = parsed
                            self._update(price, ts)
            except asyncio.TimeoutError:
                log.warning("spot ws heartbeat timeout (%ss) — reconnecting",
                            self.heartbeat_timeout_s)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — any transport error => reconnect
                log.warning("spot ws error (%s) — reconnecting in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def stop(self) -> None:
        self._stop.set()
