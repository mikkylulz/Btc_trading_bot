"""Chainlink Data Streams client (direct access) — resolution truth for 5m/15m.

Polls the Data Streams REST API (`/api/v1/reports/latest`) with the HMAC
authentication scheme and decodes the V3 report blob to the 18-decimal
benchmark price. Credentials come from the sponsored Polymarket-trader
programme (https://pm-ds-request.streams.chain.link/) or a normal Data Streams
subscription; the BTC/USD feed ID is shown on data.chain.link/streams/btc-usd.

HARD RULE: if this stream is unavailable or stale, the 5m/15m
path must HALT ENTRIES. There is no fallback to spot anywhere in this class or
its callers; a silent fallback to spot is the single most dangerous bug this
bot can have.

NOTE: the auth scheme and report schema below follow the Data Streams docs as
of early 2026 — verify against the welcome material that ships with your key
on first use (the log line on the first successful/failed poll will tell you).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Optional

import httpx

from .base import PriceFeed

log = logging.getLogger(__name__)


def hmac_headers(api_key: str, api_secret: str, method: str,
                 path_with_query: str, body: bytes = b"",
                 ts_ms: Optional[int] = None) -> dict:
    """Chainlink Data Streams HMAC auth headers.

    string-to-sign = "{METHOD} {path+query} {sha256(body)hex} {api_key} {ts_ms}"
    signature      = HMAC-SHA256(api_secret, string-to-sign), hex
    """
    ts = ts_ms if ts_ms is not None else int(time.time() * 1000)
    body_hash = hashlib.sha256(body).hexdigest()
    string_to_sign = f"{method} {path_with_query} {body_hash} {api_key} {ts}"
    sig = hmac.new(api_secret.encode(), string_to_sign.encode(),
                   hashlib.sha256).hexdigest()
    return {
        "Authorization": api_key,
        "X-Authorization-Timestamp": str(ts),
        "X-Authorization-Signature-SHA256": sig,
    }


def _word(data: bytes, i: int) -> bytes:
    return data[i * 32:(i + 1) * 32]


def decode_full_report(full_report_hex: str) -> Optional[float]:
    """Decode a Data Streams `fullReport` hex blob to the benchmark price.

    Outer ABI tuple: (bytes32[3] reportContext, bytes reportBlob, ...).
    V3 reportBlob words: feedId, validFrom, observationsTs, nativeFee, linkFee,
    expiresAt, benchmarkPrice (int192, 18 decimals), [bid, ask].
    """
    try:
        raw = bytes.fromhex(full_report_hex.removeprefix("0x"))
        blob_offset = int.from_bytes(_word(raw, 3), "big")  # word 3: offset to blob
        blob_len = int.from_bytes(raw[blob_offset:blob_offset + 32], "big")
        blob = raw[blob_offset + 32: blob_offset + 32 + blob_len]
        if len(blob) < 7 * 32:
            return None
        price_int = int.from_bytes(_word(blob, 6), "big", signed=True)
        price = price_int / 1e18
        return price if price > 0 else None
    except (ValueError, IndexError):
        return None


class ChainlinkFeed(PriceFeed):
    name = "chainlink"

    def __init__(self, base_url: str = "https://api.dataengine.chain.link",
                 api_key: str = "", api_secret: str = "", feed_id: str = "",
                 poll_interval_s: float = 1.0, buffer_age_s: float = 7200.0):
        super().__init__(buffer_age_s=buffer_age_s)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.feed_id = feed_id
        self.poll_interval_s = poll_interval_s
        self._stop = asyncio.Event()

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.api_secret
                    and self.feed_id)

    def parse_report(self, data: dict) -> Optional[tuple]:
        """Return (price, ts_seconds) from a /reports/latest payload."""
        report = data.get("report") or {}
        full = report.get("fullReport")
        if not full:
            return None
        price = decode_full_report(full)
        if price is None:
            return None
        ts = report.get("observationsTimestamp") or report.get("validFromTimestamp")
        return price, float(ts) if ts else None

    async def run(self) -> None:
        if not self.configured:
            # Not configured => feed stays permanently stale and 5m/15m halt.
            log.error("Chainlink Data Streams credentials not configured "
                      "(need API key+secret+feed ID) — chainlink-resolved "
                      "entries will be halted")
            return
        path = f"/api/v1/reports/latest?feedID={self.feed_id}"
        first = True
        async with httpx.AsyncClient(base_url=self.base_url, timeout=5.0) as client:
            while not self._stop.is_set():
                try:
                    headers = hmac_headers(self.api_key, self.api_secret,
                                           "GET", path)
                    resp = await client.get(path, headers=headers)
                    resp.raise_for_status()
                    parsed = self.parse_report(resp.json())
                    if parsed:
                        if first:
                            log.info("Chainlink Data Streams live: %s = %.2f",
                                     self.feed_id[:10], parsed[0])
                            first = False
                        self._update(parsed[0], parsed[1])
                    else:
                        log.warning("Chainlink report not decodable; feed left "
                                    "stale (verify report schema vs docs)")
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — stale, never falls back
                    log.warning("Chainlink poll failed (%s); staleness will halt "
                                "chainlink-resolved entries", e)
                await asyncio.sleep(self.poll_interval_s)

    def stop(self) -> None:
        self._stop.set()

    def opening_reference(self, window_open_ts: float) -> Optional[float]:
        """Stream value at/just before the window open — the 'Price to Beat'."""
        return self.buffer.price_at_or_before(window_open_ts)
