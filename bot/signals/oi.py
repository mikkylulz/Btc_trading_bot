"""Open-interest delta micro-nudge. Stub: returns 0 until a backtest earns it a
weight."""

from __future__ import annotations


class OpenInterestSignal:
    name = "oi"

    def drift(self, *args, **kwargs) -> float:
        return 0.0
