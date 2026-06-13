"""Config + env loading.

Secrets come from .env (see .env.example). Strategy/risk knobs have safe defaults here
and can be overridden via env (JSON for dict fields) — see Claude.md §10 for tuning order.
"""

from __future__ import annotations

from typing import Dict, Tuple

from pydantic_settings import BaseSettings, SettingsConfigDict

# Window lengths in seconds, keyed by the window label used everywhere in the bot.
WINDOW_SECONDS: Dict[str, int] = {"5m": 300, "15m": 900, "1h": 3600}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ─── Mode ────────────────────────────────────────────────────────────────
    mode: str = "paper"                 # paper | live
    allow_taker: bool = False           # crypto stays false; maker-first (Claude.md §1.2)
    live_confirm: str = ""              # must be "YES" (plus validation record) to go live

    # ─── Polymarket auth / hosts ─────────────────────────────────────────────
    polymarket_private_key: str = ""
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polymarket_funder_address: str = ""
    # 3 = POLY_1271 (proxy/deposit wallet as funder) — the setup confirmed
    # working live 2026-05-25. 1 = email/Magic proxy. Verify for YOUR wallet.
    polymarket_signature_type: int = 3
    clob_host: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    gamma_host: str = "https://gamma-api.polymarket.com"

    # ─── Feeds ───────────────────────────────────────────────────────────────
    binance_ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    coinbase_ws_url: str = ""
    # "rtds" (default): Chainlink prices relayed by Polymarket's public RTDS
    # socket — the exact values the resolver sees, no credentials needed.
    # "direct": your own Chainlink Data Streams credentials (lower latency;
    # sponsored key for 15m traders: https://pm-ds-request.streams.chain.link/).
    chainlink_source: str = "rtds"
    rtds_ws_url: str = "wss://ws-live-data.polymarket.com"
    chainlink_symbol: str = "btc/usd"
    chainlink_api_base: str = "https://api.dataengine.chain.link"
    chainlink_stream_api_key: str = ""
    chainlink_stream_api_secret: str = ""
    chainlink_feed_id: str = ""         # BTC/USD feed ID from data.chain.link/streams/btc-usd
    chainlink_poll_interval_s: float = 1.0
    max_feed_age_s: float = 10.0        # resolution feed older than this => refuse to price
    gateio_api_base: str = "https://api.gateio.ws"

    # ─── Discovery ───────────────────────────────────────────────────────────
    # Exact slug-prefix -> window mapping. No fuzzy matching (panama lesson).
    # Slug format (verified live 2026-06-10): "<prefix><window_open_unix_ts>",
    # e.g. btc-updown-15m-1781102700 closes at open_ts + 900. No 1h series was
    # found under btc-updown-1h-*; add its prefix here if/when it exists.
    market_slug_prefixes: Dict[str, str] = {
        "btc-updown-5m-": "5m",
        "btc-updown-15m-": "15m",
        "btc-updown-1h-": "1h",
    }
    discovery_interval_s: float = 30.0
    discovery_windows_ahead: int = 3    # current window + N upcoming per series

    # ─── Strategy (Claude.md §2, §3, §6) ─────────────────────────────────────
    # Polymarket crypto taker fee: fee = contracts * fee_rate * p * (1-p).
    # 0.072 makes the peak (p=0.5) ~1.8% per contract, the "~1.80% scale" in the spec.
    fee_rate: float = 0.072
    model_error_buffer: float = 0.05    # EV margin for sigma mis-estimation (0.04–0.06)
    drift_cap: float = 0.05             # max |ΔΦ(z)| the signal layer may cause
    tick_size: float = 0.01
    min_order_contracts: float = 5.0
    # minutes-remaining entry gate per window: (min, max)
    entry_windows: Dict[str, Tuple[float, float]] = {
        "5m": (1.0, 3.5),
        "15m": (3.0, 10.0),
        "1h": (10.0, 45.0),
    }
    # per-window multiplier on the estimated sigma (calibration knob, §10/§11).
    # Calibrated 2026-06-13 vs 8h real Binance 1s data (tools/calibrate_sigma):
    #   5m  -> 2.0  (scale 1.0 was overconfident: calib_err 0.134 -> 0.044 @ ~2.1)
    #   15m -> 1.0  (already near-optimal: calib_err 0.063)
    #   1h  -> 1.0  (UNCALIBRATED — 8h gave only ~8 windows; re-run with more data)
    sigma_scale: Dict[str, float] = {"5m": 2.0, "15m": 1.0, "1h": 1.0}
    sigma_ewma_halflife_s: float = 300.0
    sigma_min_samples: int = 30
    # signal layer: everything defaults OFF until backtests justify it (§3)
    enable_momentum: bool = False
    enable_microstructure: bool = False
    enable_funding: bool = False
    enable_oi: bool = False
    eval_interval_s: float = 1.0

    # ─── Risk (Claude.md §7) ─────────────────────────────────────────────────
    bankroll_usd: float = 100.0
    kelly_fraction: float = 0.25
    max_trade_usd: float = 5.0          # hard cap dominates Kelly
    max_concurrent_positions: int = 3
    max_new_positions_per_window_per_day: int = 20
    max_capital_at_risk_usd: float = 50.0
    halt_streak_n: int = 4
    halt_minutes: float = 60.0
    exit_prob_floor: float = 0.25       # exit if our side's modelled P falls below this
    # Probability-space early-exit stop. DISABLED 2026-06-13: across 24 live-paper
    # exits it was net -$21.88 vs holding. On a hard-capped binary the stop mostly
    # locks in losses on positions that revert before close, and occasionally dumps
    # a big winner (one +$26 settle exited at -$0.50). Max loss is the cost anyway.
    # Re-enable only with a much lower floor or an information-gated rule (§7).
    enable_prob_stop: bool = False
    paper_queue_penalty: float = 1.5    # multiply visible queue ahead by this in paper fills
    paper_non_fill_prob: float = 0.0    # extra random non-fill haircut (backtester uses its own)

    # ─── Persistence / monitoring ────────────────────────────────────────────
    db_path: str = "bot_data.sqlite"
    validation_record_path: str = "validation/paper_validation.json"
    prometheus_port: int = 9090
    prometheus_enabled: bool = True
    telegram_token: str = ""
    telegram_chat_id: str = ""

    @property
    def is_paper(self) -> bool:
        return self.mode.lower() != "live"


def load_settings() -> Settings:
    return Settings()
