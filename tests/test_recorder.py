"""DataRecorder: append price + book-top rows to CSV for offline backtesting."""

import csv

from bot.models import BookTop
from bot.recorder import DataRecorder


def _rows(path):
    with open(path, newline="") as f:
        return list(csv.reader(f))


def test_recorder_writes_headers_and_rows(tmp_path):
    pp, bp = str(tmp_path / "prices.csv"), str(tmp_path / "books.csv")
    r = DataRecorder(pp, bp)

    r.record_price(100.0, 66000.0)
    r.record_price(100.0, 66001.0)   # same feed ts -> deduped
    r.record_price(99.0, 65999.0)    # older ts -> deduped
    r.record_price(101.0, 66002.0)
    r.record_price(102.0, None)      # no price -> skipped

    r.record_book(101.0, "TU", BookTop(token_id="TU", best_bid=0.40,
                                       best_ask=0.45, bid_size=5, ask_size=7))
    r.record_book(101.0, "TD", None)  # no book -> skipped

    prices = _rows(pp)
    books = _rows(bp)
    assert prices[0] == ["ts", "price"]
    assert len(prices) == 3                  # header + 2 (dedups/None dropped)
    assert prices[1][1] == "66000.0000"
    assert books[0] == ["ts", "token_id", "best_bid", "best_ask",
                        "bid_size", "ask_size"]
    assert len(books) == 2                   # header + 1 (None skipped)
    assert books[1][1] == "TU" and books[1][2] == "0.4000"
