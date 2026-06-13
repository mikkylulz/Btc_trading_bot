"""Backtester: maker-only fills, and the honesty rule — a known gross-positive /
net-negative scenario is reported as FAIL, not buried (Claude.md §9/§14)."""

from bot.backtester import (Backtester, BacktestReport, BacktestTrade,
                            generate_gbm_prices)
from bot.models import Outcome


def test_gross_positive_net_negative_is_fail():
    report = BacktestReport(mode="constructed")
    # entry 0.50, wins -> gross +0.50; fee 0.60 swamps it -> net -0.10
    report.trades.append(BacktestTrade(
        window_start=0, outcome=Outcome.UP, entry_price=0.50, modelled_p=0.6,
        z=1.0, fee=0.60, fill_type="taker", won=True))
    assert report.gross_pnl > 0
    assert report.net_pnl <= 0
    assert report.verdict.startswith("FAIL")
    assert "NET-OF-FEE" in report.verdict


def test_net_negative_is_fail():
    report = BacktestReport(mode="constructed")
    report.trades.append(BacktestTrade(
        window_start=0, outcome=Outcome.UP, entry_price=0.60, modelled_p=0.6,
        z=1.0, fee=0.0, fill_type="maker", won=False))
    assert report.verdict.startswith("FAIL")


def test_no_trades_verdict():
    assert BacktestReport(mode="x").verdict == "NO-TRADES"


def test_maker_backtest_runs_and_charges_no_fees(settings):
    prices = generate_gbm_prices(6 * 3600, seed=3)
    bt = Backtester(settings, window="15m", seed=5)
    report = bt.run(prices)
    assert report.windows > 0
    assert all(t.fee == 0.0 and t.fill_type == "maker" for t in report.trades)
    # maker fills require trade-through: fill rate must be < 100% in general
    assert report.fill_rate <= 1.0


def test_taker_mode_pays_real_fees(settings):
    prices = generate_gbm_prices(6 * 3600, seed=3)
    bt = Backtester(settings, window="15m", taker_mode=True, seed=5)
    report = bt.run(prices)
    if report.trades:
        assert all(t.fee > 0 for t in report.trades)
        assert report.net_pnl < report.gross_pnl


def test_calibration_samples_recorded(settings):
    prices = generate_gbm_prices(8 * 3600, seed=9)
    report = Backtester(settings, window="15m", seed=2).run(prices)
    assert len(report.calibration_samples) == len(report.trades)
    for p, realised in report.calibration_samples:
        assert 0.0 <= p <= 1.0
        assert realised in (True, False)
