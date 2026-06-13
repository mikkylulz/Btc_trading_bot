"""Structured logging + SQLite persistence (Claude.md §8).

Three tables:
- decisions:   every trade decision, INCLUDING rejections with reason.
- trades:      entries with feed/fee/fill detail, resolved with outcome + net PnL.
- calibration: modelled P vs realised outcome — the most important table here;
               it is how we know whether Phi(z) is actually calibrated.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from typing import List, Optional

from .models import Decision, FairValue, Fill, MarketInfo
from .utils import utc_ts

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    market_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    outcome TEXT,
    fair_p REAL,
    book_price REAL,
    target_price REAL,
    edge REAL,
    edge_margin REAL,
    size REAL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    slug TEXT,
    window TEXT,
    resolution_feed TEXT,
    outcome_side TEXT,
    opening_reference REAL,
    entry_feed_price REAL,
    fair_p_at_entry REAL,
    z_at_entry REAL,
    executable_price REAL,
    contracts REAL,
    fee_paid REAL,
    fee_avoided REAL,
    fill_type TEXT,            -- maker | taker | none
    entry_ts REAL,
    resolved INTEGER DEFAULT 0,
    resolution_outcome TEXT,   -- UP | DOWN | EXIT
    net_pnl REAL,
    tag TEXT DEFAULT ''        -- non-empty = excluded from PnL (e.g. mis-graded pre-fix rows)
);
CREATE TABLE IF NOT EXISTS calibration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    market_id TEXT NOT NULL,
    window TEXT,
    modelled_p REAL NOT NULL,  -- modelled P of the side we measure (Up)
    realised INTEGER,          -- 1 if Up resolved, 0 otherwise, NULL if pending
    z REAL,
    remaining_frac REAL
);
"""


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


class BotStore:
    def __init__(self, db_path: str):
        d = os.path.dirname(db_path)
        if d:
            os.makedirs(d, exist_ok=True)
        # timeout: wait up to 30s for a held lock instead of raising. WAL +
        # NORMAL sync survives external file touches (OneDrive/AV on Desktop)
        # far better than the default rollback journal and is faster.
        self._conn = sqlite3.connect(db_path, check_same_thread=False,
                                     timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute("PRAGMA busy_timeout=30000")
            except sqlite3.OperationalError as e:
                log.warning("could not set WAL pragmas: %s", e)
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (idempotent)."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(trades)")}
        if "tag" not in cols:
            self._conn.execute("ALTER TABLE trades ADD COLUMN tag TEXT DEFAULT ''")
        cal_cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(calibration)")}
        if "tag" not in cal_cols:
            self._conn.execute(
                "ALTER TABLE calibration ADD COLUMN tag TEXT DEFAULT ''")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── decisions ─────────────────────────────────────────────────────────────
    def record_decision(self, d: Decision) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO decisions (ts, market_id, action, reason, outcome, fair_p,"
                " book_price, target_price, edge, edge_margin, size)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (d.ts, d.market_id, d.action, d.reason,
                 d.outcome.value if d.outcome else None, d.fair_p, d.book_price,
                 d.target_price, d.edge, d.edge_margin, d.size))
            self._conn.commit()

    # ── trades ────────────────────────────────────────────────────────────────
    def record_entry(self, market: MarketInfo, fv: FairValue, fill: Fill,
                     fee_avoided: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO trades (market_id, slug, window, resolution_feed,"
                " outcome_side, opening_reference, entry_feed_price, fair_p_at_entry,"
                " z_at_entry, executable_price, contracts, fee_paid, fee_avoided,"
                " fill_type, entry_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (market.market_id, market.slug, market.window,
                 market.resolution_feed.value, fill.outcome.value,
                 fv.opening_reference, fv.feed_price, fv.p_for(fill.outcome), fv.z,
                 fill.price, fill.size, fill.fee, fee_avoided,
                 "maker" if fill.is_maker else "taker", fill.ts))
            self._conn.commit()
            return int(cur.lastrowid)

    def resolve_trades(self, market_id: str, resolution_outcome: str,
                       net_override: Optional[float] = None) -> None:
        """Settle all open trades on a market: winner pays 1 per contract.

        `net_override` replaces the computed net when the caller knows the true
        position-level PnL (e.g. partial exits shrank the position, so the row's
        recorded contracts no longer reflect what settled)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, outcome_side, executable_price, contracts, fee_paid"
                " FROM trades WHERE market_id = ? AND resolved = 0",
                (market_id,)).fetchall()
            for r in rows:
                if net_override is not None:
                    net = net_override
                else:
                    won = r["outcome_side"] == resolution_outcome
                    payout = r["contracts"] * (1.0 if won else 0.0)
                    net = payout - r["contracts"] * r["executable_price"] - r["fee_paid"]
                self._conn.execute(
                    "UPDATE trades SET resolved = 1, resolution_outcome = ?,"
                    " net_pnl = ? WHERE id = ?",
                    (resolution_outcome, net, r["id"]))
            self._conn.commit()

    def add_fill_to_trade(self, market_id: str, size: float, price: float,
                          fee: float = 0.0) -> None:
        """An add-on fill grew the open trade: update contracts and the
        size-weighted average executable price so settlement math stays true."""
        with self._lock:
            r = self._conn.execute(
                "SELECT id, executable_price, contracts, fee_paid FROM trades"
                " WHERE market_id = ? AND resolved = 0 ORDER BY id DESC LIMIT 1",
                (market_id,)).fetchone()
            if r is None:
                return
            total = r["contracts"] + size
            avg = ((r["executable_price"] * r["contracts"] + price * size) / total
                   if total > 0 else r["executable_price"])
            self._conn.execute(
                "UPDATE trades SET contracts = ?, executable_price = ?,"
                " fee_paid = ? WHERE id = ?",
                (total, avg, r["fee_paid"] + fee, r["id"]))
            self._conn.commit()

    def record_early_exit(self, market_id: str, net_pnl: float) -> None:
        """Close open trades on a market via a pre-resolution exit (prob-space stop)."""
        with self._lock:
            self._conn.execute(
                "UPDATE trades SET resolved = 1, resolution_outcome = 'EXIT',"
                " net_pnl = ? WHERE market_id = ? AND resolved = 0",
                (net_pnl, market_id))
            self._conn.commit()

    # ── calibration ───────────────────────────────────────────────────────────
    def record_calibration(self, market: MarketInfo, fv: FairValue) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO calibration (ts, market_id, window, modelled_p, z,"
                " remaining_frac) VALUES (?,?,?,?,?,?)",
                (utc_ts(), market.market_id, market.window, fv.p_up,
                 None if fv.z in (float("inf"), float("-inf")) else fv.z,
                 fv.remaining_frac))
            self._conn.commit()
            return int(cur.lastrowid)

    def resolve_calibration(self, market_id: str, up_won: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE calibration SET realised = ? WHERE market_id = ?"
                " AND realised IS NULL", (1 if up_won else 0, market_id))
            self._conn.commit()

    def calibration_report(self, buckets: int = 10) -> List[dict]:
        """Bucketed modelled-P vs realised frequency. Flat diagonal = calibrated.
        Tagged rows (pre-fix era: settlement could grade off stale prices) are
        excluded — calibration measured against unreliable outcomes is noise."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT modelled_p, realised FROM calibration"
                " WHERE realised IS NOT NULL AND (tag IS NULL OR tag = '')"
            ).fetchall()
        out = []
        for b in range(buckets):
            lo, hi = b / buckets, (b + 1) / buckets
            sample = [r for r in rows if lo <= r["modelled_p"] < hi or
                      (b == buckets - 1 and r["modelled_p"] == 1.0)]
            if not sample:
                continue
            out.append({
                "bucket": f"{lo:.1f}-{hi:.1f}",
                "n": len(sample),
                "mean_modelled_p": sum(r["modelled_p"] for r in sample) / len(sample),
                "realised_freq": sum(r["realised"] for r in sample) / len(sample),
            })
        return out

    # ── reporting ─────────────────────────────────────────────────────────────
    def pnl_summary(self) -> dict:
        """Net PnL over resolved trades. Tagged rows (e.g. mis-graded pre-fix
        exits) are excluded — inflated stats must not leak into the summary.

        Win rate is reported but is NOT the objective (Claude.md §1.3): a binary's
        breakeven WR equals entry price plus fees, so a high WR at high entry
        prices can still be negative EV. Net-of-fee EV is the KPI."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(net_pnl), 0) AS net,"
                " COALESCE(AVG(net_pnl), 0) AS ev,"
                " COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,"
                " COALESCE(SUM(CASE WHEN net_pnl <= 0 THEN 1 ELSE 0 END), 0) AS losses,"
                " COALESCE(SUM(CASE WHEN resolution_outcome = 'EXIT' THEN 1 ELSE 0 END), 0) AS exits,"
                " COALESCE(AVG(CASE WHEN net_pnl > 0 THEN net_pnl END), 0) AS avg_win,"
                " COALESCE(AVG(CASE WHEN net_pnl <= 0 THEN net_pnl END), 0) AS avg_loss"
                " FROM trades WHERE resolved = 1 AND (tag IS NULL OR tag = '')"
            ).fetchone()
            open_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE resolved = 0").fetchone()
        n = row["n"]
        return {"resolved_trades": n, "net_pnl": row["net"],
                "net_ev_per_trade": row["ev"], "open_trades": open_row["n"],
                "wins": row["wins"], "losses": row["losses"], "exits": row["exits"],
                "win_rate": (row["wins"] / n) if n else 0.0,
                "avg_win": row["avg_win"], "avg_loss": row["avg_loss"]}

    def open_trades(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trades WHERE resolved = 0").fetchall()
        return [dict(r) for r in rows]
