"""Risk: fractional Kelly on net-of-fee edge, hard caps, halt-streak isolation,
paper/live gating (Claude.md §7).

- Hard dollar cap dominates Kelly.
- Halt-streak isolation is per market class (window label): on a loss streak the
  class halts, `streak_reset_at` is stored, and only losses with ts >= the `since`
  filter can re-trigger the halt after it expires (panama port).
- paper_mode defaults true. Live requires MODE=live, LIVE_CONFIRM=YES AND a
  paper-validation record on disk (§11).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import Settings
from .utils import safe_read_json, utc_ts

log = logging.getLogger(__name__)


def kelly_fraction_binary(p: float, cost: float) -> float:
    """Full-Kelly fraction of bankroll for a binary contract bought at `cost`
    with win probability `p` (payout 1). f* = (p - cost) / (1 - cost)."""
    if cost <= 0 or cost >= 1:
        return 0.0
    return max(0.0, (p - cost) / (1.0 - cost))


@dataclass
class HaltState:
    streak: int = 0
    halted_until: float = 0.0
    streak_reset_at: float = 0.0  # `since` filter: losses before this can't re-trigger


@dataclass
class RiskManager:
    settings: Settings
    open_positions: int = 0
    capital_at_risk_usd: float = 0.0
    entries_today: Dict[str, int] = field(default_factory=dict)  # window -> count
    _entries_day: str = ""
    halts: Dict[str, HaltState] = field(default_factory=dict)

    # ── live gating ───────────────────────────────────────────────────────────
    def is_live_allowed(self) -> bool:
        s = self.settings
        if s.is_paper:
            return False
        if s.live_confirm.strip().upper() != "YES":
            log.error("MODE=live but LIVE_CONFIRM != YES — staying paper")
            return False
        record = safe_read_json(s.validation_record_path, None)
        if not record or not record.get("net_ev_per_trade", 0) > 0 \
                or not record.get("calibration_ok") or not record.get("sample_size", 0):
            log.error("MODE=live but no valid paper-validation record at %s — "
                      "staying paper (Claude.md §11)", s.validation_record_path)
            return False
        return True

    # ── sizing ────────────────────────────────────────────────────────────────
    def size_entry(self, p_net: float, cost: float,
                   pending_usd: float = 0.0) -> float:
        """Contracts to buy. Fractional Kelly on the net-of-fee edge, then the hard
        dollar cap dominates. Returns 0 if any exposure limit binds.
        Resting unfilled entry orders (`pending_usd`) consume headroom too."""
        s = self.settings
        f = kelly_fraction_binary(p_net, cost) * s.kelly_fraction
        usd = min(f * s.bankroll_usd, s.max_trade_usd)
        if usd <= 0:
            return 0.0
        committed = self.capital_at_risk_usd + pending_usd
        if committed + usd > s.max_capital_at_risk_usd:
            usd = max(0.0, s.max_capital_at_risk_usd - committed)
        contracts = usd / cost if cost > 0 else 0.0
        return contracts if contracts >= s.min_order_contracts else 0.0

    # ── exposure gates ────────────────────────────────────────────────────────
    def can_enter(self, window: str, now_ts: Optional[float] = None,
                  pending_usd: float = 0.0, pending_count: int = 0) -> tuple:
        """(allowed, reason). Checks halt state and exposure limits.

        `pending_usd`/`pending_count` are resting unfilled entry orders: they
        count toward the caps because they can all fill at once."""
        now = now_ts if now_ts is not None else utc_ts()
        h = self.halts.get(window)
        if h and now < h.halted_until:
            return False, f"halted_{window}_until_{h.halted_until:.0f}"
        if self.open_positions + pending_count >= \
                self.settings.max_concurrent_positions:
            return False, "max_concurrent_positions"
        self._roll_day(now)
        if self.entries_today.get(window, 0) >= \
                self.settings.max_new_positions_per_window_per_day:
            return False, "max_new_positions_per_window_per_day"
        if self.capital_at_risk_usd + pending_usd >= \
                self.settings.max_capital_at_risk_usd:
            return False, "max_capital_at_risk"
        return True, "ok"

    def _roll_day(self, now_ts: float) -> None:
        from datetime import datetime, timezone
        day = datetime.fromtimestamp(now_ts, timezone.utc).strftime("%Y-%m-%d")
        if day != self._entries_day:
            self._entries_day = day
            self.entries_today = {}

    def record_entry(self, window: str, usd: float, now_ts: Optional[float] = None) -> None:
        self._roll_day(now_ts if now_ts is not None else utc_ts())
        self.entries_today[window] = self.entries_today.get(window, 0) + 1
        self.open_positions += 1
        self.capital_at_risk_usd += usd

    def record_addon(self, usd: float) -> None:
        """An add-on fill grew an existing position: more capital at risk, but
        not a new position or a new daily entry."""
        self.capital_at_risk_usd += usd

    def release_capital(self, usd: float) -> None:
        """A partial exit fill freed some capital without closing the position."""
        self.capital_at_risk_usd = max(0.0, self.capital_at_risk_usd - usd)

    def close_position(self) -> None:
        """The position is fully gone (capital already released per fill)."""
        self.open_positions = max(0, self.open_positions - 1)

    def record_exit(self, usd: float) -> None:
        self.close_position()
        self.release_capital(usd)

    # ── halt-streak isolation ─────────────────────────────────────────────────
    def record_result(self, window: str, won: bool, ts: Optional[float] = None) -> None:
        now = ts if ts is not None else utc_ts()
        h = self.halts.setdefault(window, HaltState())
        if won:
            h.streak = 0
            return
        # The `since` filter: losses from before the last reset cannot re-trigger.
        if now < h.streak_reset_at:
            return
        h.streak += 1
        if h.streak >= self.settings.halt_streak_n:
            h.halted_until = now + self.settings.halt_minutes * 60.0
            h.streak_reset_at = h.halted_until
            h.streak = 0
            log.warning("halt-streak: %s entries halted until %.0f", window,
                        h.halted_until)

    # ── probability-space stop (Claude.md §7) ─────────────────────────────────
    def should_exit(self, our_side_p: float) -> bool:
        """Exit if the modelled probability of our side has collapsed."""
        return our_side_p < self.settings.exit_prob_floor
