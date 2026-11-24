"""Perp funding-rate micro-nudge. Stub: returns 0 until a backtest earns it a weight.
Wire to GATEIO_API_BASE funding endpoint when enabling."""

from __future__ import annotations


class FundingSignal:
    name = "funding"

    def drift(self, *args, **kwargs) -> float:
        return 0.0
