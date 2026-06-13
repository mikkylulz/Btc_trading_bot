"""Shared dataclasses used across discovery, strategy, execution and risk."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ResolutionFeed(str, Enum):
    CHAINLINK = "chainlink"
    BINANCE = "binance"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Outcome(str, Enum):
    UP = "UP"
    DOWN = "DOWN"


@dataclass
class MarketInfo:
    """One discovered up/down market with everything the engine needs to price it."""

    market_id: str
    slug: str
    question: str
    window: str               # "5m" | "15m" | "1h"
    window_seconds: int
    open_ts: float            # unix seconds, window open
    close_ts: float           # unix seconds, resolution
    resolution_feed: ResolutionFeed
    token_up: str             # CLOB token id for the Up outcome
    token_down: str
    tie_rule_up: bool = True  # close >= open resolves Up (true for 1h Binance per spec)
    opening_reference: Optional[float] = None  # "Price to Beat"; from the resolution feed

    def seconds_remaining(self, now_ts: float) -> float:
        return self.close_ts - now_ts

    def remaining_frac(self, now_ts: float) -> float:
        return max(0.0, self.seconds_remaining(now_ts)) / self.window_seconds


@dataclass
class FairValue:
    """Output of the fair-value engine for one market at one instant."""

    p_up: float
    z: float
    sigma: float              # price-unit vol over the full window horizon
    realised_move: float      # feed_price - opening_reference
    remaining_frac: float
    drift_adj: float          # post-clamp drift actually applied (price units)
    feed_price: float
    opening_reference: float
    as_of: float              # unix ts of the resolution-feed value used

    def p_for(self, outcome: Outcome) -> float:
        return self.p_up if outcome == Outcome.UP else 1.0 - self.p_up


@dataclass
class BookTop:
    """Top-of-book for one token."""

    token_id: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    bid_size: float = 0.0
    ask_size: float = 0.0
    as_of: float = 0.0

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


@dataclass
class Order:
    order_id: str
    market_id: str
    token_id: str
    outcome: Outcome
    side: Side
    price: float
    size: float               # contracts
    placed_ts: float
    is_maker: bool = True
    filled: float = 0.0
    queue_ahead: float = 0.0  # paper-engine queue-position estimate at placement

    @property
    def remaining(self) -> float:
        return self.size - self.filled


@dataclass
class Fill:
    order_id: str
    market_id: str
    token_id: str
    outcome: Outcome
    side: Side
    price: float
    size: float
    fee: float
    ts: float
    is_maker: bool = True


@dataclass
class Position:
    market_id: str
    outcome: Outcome
    contracts: float
    avg_price: float
    fees_paid: float = 0.0
    fair_p_at_entry: float = 0.0
    z_at_entry: float = 0.0
    window: str = ""
    resolution_feed: str = ""
    opening_reference: float = 0.0
    entry_feed_price: float = 0.0
    realized_exit_pnl: float = 0.0  # accumulated net PnL from partial exit fills
    fills: list = field(default_factory=list)

    @property
    def cost(self) -> float:
        return self.contracts * self.avg_price + self.fees_paid


@dataclass
class Decision:
    """One trade decision — logged whether or not we act (rejections included)."""

    market_id: str
    ts: float
    action: str               # "post" | "replace" | "cancel" | "hold" | "reject" | "exit"
    reason: str
    outcome: Optional[Outcome] = None
    fair_p: Optional[float] = None
    book_price: Optional[float] = None
    target_price: Optional[float] = None
    edge: Optional[float] = None
    edge_margin: Optional[float] = None
    size: Optional[float] = None
