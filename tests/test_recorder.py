"""DataRecorder: append price + book-top rows to CSV for offline backtesting."""

import csv

from bot.models import BookTop
from bot.recorder import DataRecorder


def _rows(path):
    with open(path, newline="") as f:
        return list(csv.reader(f))


def _paths(tmp_path):
    return (str(tmp_path / "prices.csv"), str(tmp_path / "books.csv"),
            str(tmp_path / "outcomes.csv"))


def test_recorder_writes_headers_and_rows(tmp_path):
    pp, bp, op = _paths(tmp_path)
    r = DataRecorder(pp, bp, op)

    r.record_price(100.0, 66000.0)
    r.record_price(100.0, 66001.0)   # same feed ts -> deduped
    r.record_price(99.0, 65999.0)    # older ts -> deduped
    r.record_price(101.0, 66002.0)
    r.record_price(102.0, None)      # no price -> skipped

    slug = "btc-updown-5m-1750000000"
    r.record_book(101.0, slug, "UP", "TU", BookTop(token_id="TU", best_bid=0.40,
                                                   best_ask=0.45, bid_size=5, ask_size=7))
    r.record_book(101.0, slug, "DOWN", "TD", None)  # no book -> skipped

    prices = _rows(pp)
    books = _rows(bp)
    assert prices[0] == ["ts", "price"]
    assert len(prices) == 3                  # header + 2 (dedups/None dropped)
    assert prices[1][1] == "66000.0000"
    assert books[0] == ["ts", "slug", "side", "token_id",
                        "best_bid", "best_ask", "bid_size", "ask_size"]
    assert len(books) == 2                   # header + 1 (None skipped)
    assert books[1][1] == slug and books[1][2] == "UP"
    assert books[1][3] == "TU" and books[1][4] == "0.4000"

    r.record_outcome(slug, "DOWN")
    outs = _rows(op)
    assert outs[0] == ["slug", "outcome"]
    assert outs[1] == [slug, "DOWN"]


def test_recorder_archives_on_schema_change(tmp_path):
    import os
    pp, bp, op = _paths(tmp_path)
    # pre-create books.csv with an OLD-schema header
    with open(bp, "w", newline="") as f:
        f.write("ts,token_id,best_bid,best_ask,bid_size,ask_size\n0,X,0.1,0.2,1,1\n")
    DataRecorder(pp, bp, op)                  # init should archive the incompatible file
    assert os.path.exists(bp + ".bak")        # old data preserved, not destroyed
    assert _rows(bp)[0][1] == "slug"          # fresh file has the new header
