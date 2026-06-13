"""Prometheus endpoint stubs for Grafana (Claude.md §15). Wire-up-later, not
load-bearing for trading: every method no-ops if prometheus_client is missing."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:
    from prometheus_client import Gauge, start_http_server
    _AVAILABLE = True
except ImportError:  # pragma: no cover
    _AVAILABLE = False


class Metrics:
    def __init__(self, port: int = 9090, enabled: bool = True):
        self.enabled = enabled and _AVAILABLE
        if not self.enabled:
            return
        self.open_positions = Gauge("bot_open_positions", "Open positions")
        self.net_ev = Gauge("bot_net_ev_per_trade", "Rolling net-of-fee EV per trade")
        self.calibration_error = Gauge("bot_calibration_error",
                                       "Mean |modelled P - realised| across buckets")
        self.fill_rate = Gauge("bot_fill_rate", "Maker fill rate")
        self.feed_staleness = Gauge("bot_feed_staleness_seconds",
                                    "Seconds since last feed value", ["feed"])
        self.halted = Gauge("bot_halted", "1 if entries halted", ["window"])
        try:
            start_http_server(port)
            log.info("prometheus endpoint on :%d", port)
        except OSError as e:
            log.warning("prometheus server failed to start: %s", e)
            self.enabled = False

    def set_feed_staleness(self, feed_name: str, seconds: float) -> None:
        if self.enabled:
            self.feed_staleness.labels(feed=feed_name).set(seconds)

    def set_halted(self, window: str, halted: bool) -> None:
        if self.enabled:
            self.halted.labels(window=window).set(1.0 if halted else 0.0)

    def set_summary(self, open_positions: int, net_ev: float,
                    calibration_error: float, fill_rate: float) -> None:
        if self.enabled:
            self.open_positions.set(open_positions)
            self.net_ev.set(net_ev)
            self.calibration_error.set(calibration_error)
            self.fill_rate.set(fill_rate)
