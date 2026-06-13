"""Chainlink BTC/USD via Polymarket's Real-Time Data Socket (RTDS).

Polymarket relays the same Chainlink data stream its 5m/15m markets resolve on
through `wss://ws-live-data.polymarket.com`, topic `crypto_prices_chainlink`,
no authentication required (docs.polymarket.com/market-data/websocket/rtds).
This is resolution truth by construction — it is the value the resolver sees.

Same hard rule as the direct feed (Claude.md §4): if this socket is down or
stale, 15m entries HALT. No fallback to spot, including the `crypto_prices`
(Binance) topic on this very socket.

Note: send PING every 5s per the docs; the server rate-limits aggressively
(HTTP 429) so reconnect backoff is long and patient.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import websockets

from .base import PriceFeed

log = logging.getLogger(__name__)

_HEADERS = {
    "Origin": "https://polymarket.com",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"),
}


class RTDSChainlinkFeed(PriceFeed):
    name = "chainlink_rtds"

    def __init__(self, ws_url: str = "wss://ws-live-data.polymarket.com",
                 symbol: str = "btc/usd", heartbeat_timeout_s: float = 30.0,
                 buffer_age_s: float = 7200.0):
        super().__init__(buffer_age_s=buffer_age_s)
        self.ws_url = ws_url
        self.symbol = symbol
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self._stop = asyncio.Event()

    @property
    def configured(self) -> bool:
        return bool(self.ws_url)

    def _subscribe_message(self) -> str:
        # The server-side symbol filter is broken (returns an AddSubscriptions
        # DB error, verified live 2026-06-10), so subscribe to the whole topic
        # and filter client-side in parse_message.
        return json.dumps({
            "action": "subscribe",
            "subscriptions": [{
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": "",
            }],
        })

    def parse_message(self, raw) -> Optional[tuple]:
        """Return (price, ts_seconds) from a crypto_prices_chainlink update."""
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None  # keepalive frames
        if not isinstance(msg, dict):
            return None
        body = msg.get("body")
        if isinstance(body, dict) and "error" in str(body.get("message", "")).lower():
            log.warning("RTDS subscription error frame: %s",
                        str(body.get("message"))[:200])
            return None
        if msg.get("topic") != "crypto_prices_chainlink":
            return None
        payload = msg.get("payload") or {}
        if payload.get("symbol") != self.symbol:
            return None
        try:
            # prefer the 18-decimal fixed-point value: resolution math compares
            # close vs open within dollars, so precision matters
            if payload.get("full_accuracy_value"):
                price = int(payload["full_accuracy_value"]) / 1e18
            else:
                price = float(payload["value"])
            ts = float(payload.get("timestamp", 0)) / 1000.0 or None
        except (KeyError, TypeError, ValueError):
            return None
        return (price, ts) if price > 0 else None

    async def run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                # ping_interval=5 sends protocol-level pings every 5s, which is
                # what the official RTDS client does (not text "PING" frames).
                async with websockets.connect(self.ws_url, open_timeout=10,
                                              additional_headers=_HEADERS,
                                              ping_interval=5, ping_timeout=10,
                                              close_timeout=5) as ws:
                    await ws.send(self._subscribe_message())
                    first = True
                    prev_tick: Optional[float] = None
                    ticks, max_gap = 0, 0.0
                    stats_at = asyncio.get_event_loop().time() + 300.0
                    while not self._stop.is_set():
                        # Reconnect only if NO frames at all arrive for the
                        # heartbeat window. Price staleness is judged by the
                        # engine via is_stale(max_feed_age), which halts
                        # entries — it must not be conflated with this.
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=self.heartbeat_timeout_s)
                        except asyncio.TimeoutError:
                            log.warning("RTDS silent for %ss — reconnecting",
                                        self.heartbeat_timeout_s)
                            break
                        parsed = self.parse_message(raw)
                        if parsed:
                            if first:
                                log.info("RTDS chainlink live: %s = %.2f",
                                         self.symbol, parsed[0])
                                first = False
                                backoff = 5.0  # reset only once data flows
                            self._update(parsed[0], parsed[1])
                            # Tick-gap diagnostics: this is how we find out
                            # whether staleness is sparse ticks or dropouts.
                            now_mono = asyncio.get_event_loop().time()
                            if prev_tick is not None:
                                gap = now_mono - prev_tick
                                max_gap = max(max_gap, gap)
                                if gap > 10.0:
                                    log.info("RTDS tick gap %.1fs on %s",
                                             gap, self.symbol)
                            prev_tick = now_mono
                            ticks += 1
                            if now_mono >= stats_at:
                                log.info("RTDS 5min stats: %d ticks, max gap %.1fs",
                                         ticks, max_gap)
                                ticks, max_gap = 0, 0.0
                                stats_at = now_mono + 300.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                is_429 = "429" in str(e)
                log.warning("RTDS chainlink error (%s) — reconnecting in %.0fs",
                            e, backoff)
                await asyncio.sleep(backoff)
                # rate limits demand patience; cap higher when we're being throttled
                backoff = min(backoff * 2, 600.0 if is_429 else 120.0)

    def stop(self) -> None:
        self._stop.set()

    def opening_reference(self, window_open_ts: float) -> Optional[float]:
        """Stream value at/just before the window open — the 'Price to Beat'."""
        return self.buffer.price_at_or_before(window_open_ts)
