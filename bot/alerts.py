"""Telegram alerts. Optional: disabled (no-op) unless a token + chat id are set,
so paper runs and tests never touch the network. Uses httpx (already a dep).

Failures are logged, never raised — a notification problem must not affect
trading (lesson from the sister bot, where a silent send failure looked like
'no alerts' when the real issue was elsewhere)."""

from __future__ import annotations

import logging

import httpx

from .config import Settings

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, settings: Settings):
        self.token = (settings.telegram_token or "").strip()
        self.chat_id = (settings.telegram_chat_id or "").strip()
        self.enabled = bool(self.token) and bool(self.chat_id) \
            and "YOUR" not in self.token
        if not self.enabled:
            log.info("telegram alerts disabled (no token/chat id configured)")

    def send(self, msg: str) -> None:
        if not self.enabled:
            return
        try:
            r = httpx.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": f"🤖 BTC-FV BOT\n{msg}"},
                timeout=8.0)
            if r.status_code != 200:
                # bad chat id, bot blocked, message too long, etc. — was silent
                log.warning("telegram rejected alert (HTTP %s): %s",
                            r.status_code, r.text[:200])
        except Exception as e:  # noqa: BLE001 — never let alerts break trading
            log.warning("telegram send failed: %s", e)
