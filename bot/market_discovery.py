"""Gamma API market discovery (Claude.md §5).

- Exact slug-prefix validation only — NO fuzzy matching (fuzzy matching pulled
  stale markets in panama).
- Per market we record: window length, close timestamp, resolution feed
  (Chainlink vs Binance), tie rule, and the CLOB token ids for Up/Down.
- The opening reference ("Price to Beat") is pinned later from the resolution
  feed's own buffer at window open (strategy.py) — never from spot.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

import httpx

from .config import WINDOW_SECONDS, Settings
from .models import MarketInfo, ResolutionFeed
from .utils import parse_iso_utc, utc_ts

log = logging.getLogger(__name__)

# Default resolution feed per window per Claude.md §1.1. The market description is
# still checked and, if it names a feed explicitly, the description wins.
DEFAULT_FEED_BY_WINDOW = {
    "5m": ResolutionFeed.CHAINLINK,
    "15m": ResolutionFeed.CHAINLINK,
    "1h": ResolutionFeed.BINANCE,
}


def detect_window(slug: str, prefixes: dict) -> Optional[str]:
    """Exact prefix match against configured slug prefixes. Longest prefix wins."""
    best = None
    for prefix, window in prefixes.items():
        if slug.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, window)
    return best[1] if best else None


def detect_resolution_feed(description: str, window: str) -> ResolutionFeed:
    d = (description or "").lower()
    has_chainlink = "chainlink" in d
    has_binance = "binance" in d
    if has_chainlink and not has_binance:
        return ResolutionFeed.CHAINLINK
    if has_binance and not has_chainlink:
        return ResolutionFeed.BINANCE
    feed = DEFAULT_FEED_BY_WINDOW[window]
    if has_chainlink and has_binance:
        log.warning("description names both feeds; defaulting %s for window %s",
                    feed.value, window)
    return feed


def parse_market(m: dict, prefixes: dict) -> Optional[MarketInfo]:
    """Strictly parse one Gamma market dict; return None (and log why) if unusable."""
    slug = m.get("slug") or ""
    window = detect_window(slug, prefixes)
    if window is None:
        return None
    end_raw = m.get("endDate") or m.get("endDateIso")
    if not end_raw:
        log.debug("skip %s: no endDate", slug)
        return None
    try:
        close_ts = parse_iso_utc(end_raw).timestamp()
    except ValueError:
        log.warning("skip %s: bad endDate %r", slug, end_raw)
        return None

    raw_tokens = m.get("clobTokenIds")
    if isinstance(raw_tokens, str):
        try:
            raw_tokens = json.loads(raw_tokens)
        except json.JSONDecodeError:
            raw_tokens = None
    raw_outcomes = m.get("outcomes")
    if isinstance(raw_outcomes, str):
        try:
            raw_outcomes = json.loads(raw_outcomes)
        except json.JSONDecodeError:
            raw_outcomes = None
    if not raw_tokens or len(raw_tokens) != 2:
        log.debug("skip %s: missing clobTokenIds", slug)
        return None

    # Map token ids to outcomes explicitly; never assume ordering if outcomes present.
    token_up, token_down = raw_tokens[0], raw_tokens[1]
    if raw_outcomes and len(raw_outcomes) == 2:
        pairs = dict(zip([str(o).strip().lower() for o in raw_outcomes], raw_tokens))
        if "up" in pairs and "down" in pairs:
            token_up, token_down = pairs["up"], pairs["down"]
        else:
            log.warning("skip %s: outcomes %r are not Up/Down", slug, raw_outcomes)
            return None

    window_seconds = WINDOW_SECONDS[window]
    return MarketInfo(
        market_id=str(m.get("id") or m.get("conditionId") or slug),
        slug=slug,
        question=m.get("question") or slug,
        window=window,
        window_seconds=window_seconds,
        open_ts=close_ts - window_seconds,
        close_ts=close_ts,
        resolution_feed=detect_resolution_feed(m.get("description", ""), window),
        token_up=str(token_up),
        token_down=str(token_down),
        tie_rule_up=True,  # close >= open resolves Up (spec §1.1 / §13)
    )


def parse_resolution(m: dict) -> Optional[str]:
    """Polymarket's actual resolved outcome ('UP'/'DOWN') from a Gamma market
    dict, or None if not yet decisively resolved. The winning token settles to
    ~1.0 — we require a clear 1/0 split.

    We do NOT require the `closed` flag: Polymarket leaves it false for a while
    AFTER the price has decisively resolved (observed 2026-06-17: outcomePrices
    [0.0005, 0.9995] with closed=false). Settlement only consults this AFTER the
    window has closed (now >= close_ts), so a converged price IS the result; the
    tight 0.99/0.01 threshold guards against a mid-window odds reading."""
    prices = m.get("outcomePrices")
    outcomes = m.get("outcomes")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            prices = None
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = None
    if not prices or not outcomes or len(prices) != len(outcomes):
        return None
    try:
        labelled = {str(o).strip().lower(): float(p)
                    for o, p in zip(outcomes, prices)}
    except (TypeError, ValueError):
        return None
    if "up" not in labelled or "down" not in labelled:
        return None
    up_p, down_p = labelled["up"], labelled["down"]
    if up_p >= 0.99 and down_p <= 0.01:
        return "UP"
    if down_p >= 0.99 and up_p <= 0.01:
        return "DOWN"
    return None  # closed but not a decisive 1/0 split yet


class MarketDiscovery:
    def __init__(self, settings: Settings):
        self.s = settings

    async def fetch_resolution(self, slug: str) -> Optional[str]:
        """Settlement fallback: Polymarket's real resolved outcome for one market
        ('UP'/'DOWN'), or None if not yet resolved / unreachable. Grades off the
        same oracle the market settles on, so it's authoritative when our local
        feed missed the close (reconnect gap).

        Gamma's `/markets?slug=` defaults to OPEN markets only — once a window
        flips `closed=true` (which it does within seconds of resolving) that
        query returns an empty list, so the oracle was never actually consulted
        at settlement and grading silently fell back to the local feed
        (mis-graded 23% of near-tie 5m windows, found 2026-06-18). Query BOTH
        states: the still-open-but-converged window first, then the closed/
        archived one — take the first decisive resolution."""
        async with httpx.AsyncClient(base_url=self.s.gamma_host, timeout=10.0) as client:
            for params in ({"slug": slug}, {"slug": slug, "closed": "true"}):
                try:
                    resp = await client.get("/markets", params=params)
                    resp.raise_for_status()
                    batch = resp.json()
                except Exception as e:  # noqa: BLE001
                    log.warning("oracle resolution fetch failed for %s (%s): %r",
                                slug, params, e)
                    continue
                for m in batch if isinstance(batch, list) else []:
                    if m.get("slug") != slug:   # exact match only
                        continue
                    outcome = parse_resolution(m)
                    if outcome is not None:
                        return outcome
        return None

    def candidate_slugs(self, now_ts: float) -> List[str]:
        """Deterministic slugs for the in-progress window plus the next N, per
        series. The recurring series use '<prefix><window_open_unix_ts>'
        (verified live), so we construct exact slugs instead of crawling the
        catalog — exact-slug validation by construction."""
        slugs = []
        for prefix, window in self.s.market_slug_prefixes.items():
            secs = WINDOW_SECONDS[window]
            open0 = (int(now_ts) // secs) * secs
            for k in range(self.s.discovery_windows_ahead + 1):
                slugs.append(f"{prefix}{open0 + k * secs}")
        return slugs

    async def fetch_active_markets(self) -> List[MarketInfo]:
        """Fetch the BTC up/down markets for the current and upcoming windows."""
        out: List[MarketInfo] = []
        async with httpx.AsyncClient(base_url=self.s.gamma_host, timeout=10.0) as client:
            for slug in self.candidate_slugs(utc_ts()):
                try:
                    resp = await client.get("/markets", params={"slug": slug})
                    resp.raise_for_status()
                    batch = resp.json()
                except Exception as e:  # noqa: BLE001
                    # repr: httpx timeout exceptions stringify to ""
                    log.warning("gamma fetch failed for %s: %r", slug, e)
                    continue
                for m in batch if isinstance(batch, list) else []:
                    if m.get("slug") != slug:   # exact match only
                        continue
                    info = parse_market(m, self.s.market_slug_prefixes)
                    if info and all(x.market_id != info.market_id for x in out):
                        out.append(info)
        return out

    def in_entry_window(self, market: MarketInfo, now_ts: float) -> bool:
        """Entry gate: minutes remaining must be inside the configured per-window range."""
        lo, hi = self.s.entry_windows.get(market.window, (0.0, 0.0))
        mins_left = market.seconds_remaining(now_ts) / 60.0
        return lo <= mins_left <= hi
