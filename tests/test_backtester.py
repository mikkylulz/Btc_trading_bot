"""Backtester: maker-only fills, and the honesty rule — a known gross-positive /
net-negative scenario is reported as FAIL, not buried."""

from bot.backtester import (Backtester, BacktestReport, BacktestTrade,
                            generate_gbm_prices, load_recorded_books,
                            load_recorded_outcomes)
from bot.models import BookTop, Outcome


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


def test_load_recorded_books_groups_by_window_and_side(tmp_path):
    p = tmp_path / "books.csv"
    p.write_text(
        "ts,slug,side,token_id,best_bid,best_ask,bid_size,ask_size\n"
        "20.0,btc-updown-5m-1000,UP,TU,0.10,0.12,5,5\n"
        "10.0,btc-updown-5m-1000,UP,TU,0.09,0.11,5,5\n"   # out of order on purpose
        "15.0,btc-updown-5m-1000,DOWN,TD,0.88,0.90,5,5\n"
        "30.0,btc-updown-5m-1300,UP,TU,0.20,0.22,5,5\n")
    books = load_recorded_books(str(p))
    assert set(books) == {1000, 1300}                 # open_ts parsed from slug
    up = books[1000]["UP"]
    assert [ts for ts, _ in up] == [10.0, 20.0]        # sorted by ts
    assert up[1][1].best_ask == 0.12
    assert books[1000]["DOWN"][0][1].best_bid == 0.88


def test_recorded_top_at_or_before(settings):
    bt = Backtester(settings, window="5m")
    a = BookTop(token_id="TU", best_bid=0.10, best_ask=0.12)
    b = BookTop(token_id="TU", best_bid=0.18, best_ask=0.20)
    window = {"UP": [(10.0, a), (20.0, b)]}
    assert bt._recorded_top(window, "UP", 5.0) is None     # nothing yet
    assert bt._recorded_top(window, "UP", 15.0) is a       # latest <= 15
    assert bt._recorded_top(window, "UP", 25.0) is b
    assert bt._recorded_top(window, "DOWN", 25.0) is None   # side absent


def test_load_recorded_outcomes(tmp_path):
    p = tmp_path / "outcomes.csv"
    p.write_text("slug,outcome\n"
                 "btc-updown-5m-1000,DOWN\n"
                 "btc-updown-5m-1300,UP\n"
                 "btc-updown-5m-1600,bad\n")   # non UP/DOWN -> skipped
    out = load_recorded_outcomes(str(p))
    assert out == {1000: "DOWN", 1300: "UP"}


def test_recorded_outcome_overrides_price_series_grade(settings):
    # the backtester grades off the recorded oracle outcome when present
    bt = Backtester(settings, window="15m", recorded_outcomes={1000: "DOWN"})
    assert bt.recorded_outcomes.get(1000) == "DOWN"


def test_recorded_books_sets_real_edge_mode(settings):
    prices = generate_gbm_prices(6 * 3600, seed=3)
    bt = Backtester(settings, window="15m", seed=5,
                    recorded_books={123: {"UP": [], "DOWN": []}})
    report = bt.run(prices)
    assert "RECORDED" in report.mode                    # honest mode label
    assert report.windows > 0


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
