"""Risk: halt-streak isolation; paper_mode default true; live needs flag + record;
hard dollar cap dominates Kelly."""

import json

import pytest

from bot.risk import HaltState, RiskManager, kelly_fraction_binary

T0 = 1_750_000_000.0


def test_paper_mode_default_true(settings):
    assert settings.is_paper
    assert not RiskManager(settings).is_live_allowed()


def test_live_requires_confirm_flag(settings):
    s = settings.model_copy(update={"mode": "live"})
    assert not RiskManager(s).is_live_allowed()


def test_live_requires_validation_record(settings, tmp_path):
    s = settings.model_copy(update={"mode": "live", "live_confirm": "YES"})
    rm = RiskManager(s)
    assert not rm.is_live_allowed()          # no record on disk

    record = {"net_ev_per_trade": 0.012, "calibration_ok": True, "sample_size": 400}
    with open(s.validation_record_path, "w") as f:
        json.dump(record, f)
    assert RiskManager(s).is_live_allowed()


def test_live_rejects_negative_ev_record(settings):
    s = settings.model_copy(update={"mode": "live", "live_confirm": "YES"})
    with open(s.validation_record_path, "w") as f:
        json.dump({"net_ev_per_trade": -0.01, "calibration_ok": True,
                   "sample_size": 400}, f)
    assert not RiskManager(s).is_live_allowed()


def test_kelly_binary():
    assert kelly_fraction_binary(0.6, 0.5) == pytest.approx(0.2)
    assert kelly_fraction_binary(0.5, 0.5) == 0.0
    assert kelly_fraction_binary(0.4, 0.5) == 0.0    # never negative
    assert kelly_fraction_binary(0.6, 0.0) == 0.0
    assert kelly_fraction_binary(0.6, 1.0) == 0.0


def test_hard_dollar_cap_dominates_kelly(settings):
    s = settings.model_copy(update={"bankroll_usd": 10_000.0, "max_trade_usd": 5.0,
                                    "min_order_contracts": 1.0})
    rm = RiskManager(s)
    contracts = rm.size_entry(p_net=0.9, cost=0.5)
    assert contracts * 0.5 <= 5.0 + 1e-9     # capped at $5 despite huge Kelly size


def test_size_zero_below_min_contracts(settings):
    s = settings.model_copy(update={"max_trade_usd": 1.0, "min_order_contracts": 50.0})
    assert RiskManager(s).size_entry(p_net=0.9, cost=0.5) == 0.0


def test_exposure_limits(settings):
    rm = RiskManager(settings)
    for _ in range(settings.max_concurrent_positions):
        ok, _reason = rm.can_enter("15m", T0)
        assert ok
        rm.record_entry("15m", 1.0, T0)
    ok, reason = rm.can_enter("15m", T0)
    assert not ok and reason == "max_concurrent_positions"


def test_halt_streak_triggers_and_isolates(settings):
    rm = RiskManager(settings)
    for _ in range(settings.halt_streak_n):
        rm.record_result("15m", won=False, ts=T0)
    ok, reason = rm.can_enter("15m", T0 + 1)
    assert not ok and reason.startswith("halted_15m")
    # isolation: other market classes are unaffected
    ok_1h, _ = rm.can_enter("1h", T0 + 1)
    assert ok_1h


def test_halt_expires_and_old_losses_cannot_retrigger(settings):
    """The `since` filter: pre-halt losses must not re-trigger after expiry."""
    rm = RiskManager(settings)
    for _ in range(settings.halt_streak_n):
        rm.record_result("15m", won=False, ts=T0)
    h = rm.halts["15m"]
    after = h.halted_until + 1
    ok, _ = rm.can_enter("15m", after)
    assert ok
    # losses stamped BEFORE streak_reset_at are ignored
    for _ in range(settings.halt_streak_n):
        rm.record_result("15m", won=False, ts=T0 + 1)   # pre-reset timestamps
    ok, _ = rm.can_enter("15m", after)
    assert ok                                            # not re-halted
    # but fresh post-reset losses do count
    for _ in range(settings.halt_streak_n):
        rm.record_result("15m", won=False, ts=after)
    ok, _ = rm.can_enter("15m", after + 1)
    assert not ok


def test_probability_space_stop(settings):
    rm = RiskManager(settings)
    assert rm.should_exit(settings.exit_prob_floor - 0.01)
    assert not rm.should_exit(settings.exit_prob_floor + 0.01)
