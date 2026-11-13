"""Async Telegram alerts via httpx (free, no dependencies beyond httpx)."""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout_s: int = 10):
        self.enabled = bool(bot_token and chat_id)
        # token lives only inside the client URL; NEVER log it (Telegram
        # exceptions embed the full URL, token included)
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._timeout_s = timeout_s

    async def send(self, text: str) -> bool:
        """Fire-and-forget alert; never raises into the trading loop.

        Failures ARE logged (sanitized, without the token URL) so a dead
        alert channel is visible instead of silently lost.
        """
        if not self.enabled:
            return False
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(
                    self._url, json={"chat_id": self._chat_id, "text": text}
                )
                if resp.status_code != 200:
                    logger.warning(
                        "telegram alert failed: HTTP %s", resp.status_code
                    )
                    return False
                return True
        except Exception as exc:
            # exc may carry the token URL - log only the exception TYPE
            logger.warning("telegram alert failed: %s", type(exc).__name__)
            return False
