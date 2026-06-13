"""Fee model: fee = C * feeRate * p * (1-p) peaks at p=0.5; maker pays zero."""

import pytest

from bot.execution.orders import edge_margin, maker_fee, taker_fee


def test_taker_fee_peaks_at_half():
    rate = 0.072
    fees = {p: taker_fee(1.0, rate, p) for p in [0.1, 0.3, 0.5, 0.7, 0.9]}
    assert fees[0.5] == max(fees.values())
    assert fees[0.5] == pytest.approx(rate * 0.25)   # ~1.8% per contract at the peak
    assert fees[0.1] == pytest.approx(fees[0.9])     # symmetric


def test_taker_fee_scales_with_contracts():
    assert taker_fee(10, 0.072, 0.5) == pytest.approx(10 * taker_fee(1, 0.072, 0.5))


def test_maker_fee_is_zero():
    assert maker_fee(100, 0.5) == 0.0


def test_edge_margin_composition():
    assert edge_margin(0.01, 0.02, 0.05) == pytest.approx(0.08)
