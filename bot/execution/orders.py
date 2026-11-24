"""Maker-first order planning.

Trade trigger: enter only when
    fair_value - executable_maker_price > edge_margin
    edge_margin = fee_estimate + half_spread + model_error_buffer

Maker orders pay zero fee, so fee_estimate is zero on the maker path — but the
function models the real taker fee so any allow_taker path and all EV reporting
use the true net price. Crossing the spread is forbidden unless allow_taker=true.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..config import Settings
from ..models import BookTop, Decision, FairValue, MarketInfo, Outcome
from ..utils import clamp, utc_ts

log = logging.getLogger(__name__)


def taker_fee(contracts: float, fee_rate: float, price: float) -> float:
    """Polymarket crypto taker fee: fee = C * feeRate * p * (1-p). Peaks at p=0.5."""
    p = clamp(price, 0.0, 1.0)
    return contracts * fee_rate * p * (1.0 - p)


def maker_fee(contracts: float, price: float) -> float:
    """Makers pay zero (and may earn a rebate, which we conservatively ignore)."""
    return 0.0


def edge_margin(fee_per_contract: float, half_spread: float,
                model_error_buffer: float) -> float:
    return fee_per_contract + half_spread + model_error_buffer


class OrderPlanner:
    def __init__(self, settings: Settings):
        self.s = settings

    def plan_entry(self, market: MarketInfo, fv: FairValue,
                   up_top: Optional[BookTop], down_top: Optional[BookTop],
                   now_ts: Optional[float] = None) -> Decision:
        """Evaluate both outcomes' books; return the best actionable decision.

        Every path returns a Decision so rejections are logged with a reason (§8).
        """
        ts = now_ts if now_ts is not None else utc_ts()
        best: Optional[Decision] = None
        for outcome, top in ((Outcome.UP, up_top), (Outcome.DOWN, down_top)):
            d = self._plan_side(market, fv, outcome, top, ts)
            if d.action == "post" and (best is None or (d.edge or 0) > (best.edge or 0)):
                best = d
            elif best is None:
                best = d
        assert best is not None
        return best

    def _plan_side(self, market: MarketInfo, fv: FairValue, outcome: Outcome,
                   top: Optional[BookTop], ts: float) -> Decision:
        fair = fv.p_for(outcome)
        base = dict(market_id=market.market_id, ts=ts, outcome=outcome, fair_p=fair)

        if top is None or top.best_bid is None or top.best_ask is None:
            return Decision(action="reject", reason="no_book", **base)
        spread = top.best_ask - top.best_bid
        half_spread = spread / 2.0

        # Maker bid: join/improve best bid but never at/through the ask (that's taker).
        post_price = min(top.best_bid + self.s.tick_size, top.best_ask - self.s.tick_size)
        post_price = round(post_price / self.s.tick_size) * self.s.tick_size
        if post_price <= 0 or post_price >= 1:
            return Decision(action="reject", reason="degenerate_book",
                            book_price=top.best_ask, **base)

        margin = edge_margin(
            fee_per_contract=0.0,  # maker pays zero (taker path is disabled for crypto)
            half_spread=half_spread,
            model_error_buffer=self.s.model_error_buffer,
        )
        edge = fair - post_price - margin
        if edge <= 0:
            return Decision(action="reject", reason="edge_below_margin",
                            book_price=post_price, target_price=post_price,
                            edge=fair - post_price, edge_margin=margin, **base)

        # Don't pay more than fair minus the full margin even if the book allows it.
        target = min(post_price, round((fair - margin) / self.s.tick_size)
                     * self.s.tick_size)
        if target <= 0:
            return Decision(action="reject", reason="target_price_nonpositive",
                            edge=edge, edge_margin=margin, **base)
        # Entry-price band: only trade where the buy price falls in
        # [min_entry_price, max_entry_price]. Lets us restrict to a calibrated
        # band (e.g. 0.31-0.52) and skip cheaper/pricier entries regardless of edge.
        if target < self.s.min_entry_price or target > self.s.max_entry_price:
            return Decision(action="reject", reason="outside_price_band",
                            book_price=post_price, target_price=target,
                            edge=edge, edge_margin=margin, **base)
        return Decision(action="post", reason="edge_above_margin",
                        book_price=post_price, target_price=target,
                        edge=edge, edge_margin=margin, **base)

    def should_replace(self, order_price: float, decision: Decision) -> bool:
        """Cancel/replace if the fresh target moved by more than one tick."""
        if decision.action != "post" or decision.target_price is None:
            return False
        return abs(decision.target_price - order_price) > self.s.tick_size + 1e-9
