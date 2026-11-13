"""Async Telegram alerts via httpx (free, no dependencies beyond httpx)."""
from __future__ import annotations

import httpx


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout_s: int = 10):
        self.enabled = bool(bot_token and chat_id)
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._timeout_s = timeout_s

    async def send(self, text: str) -> bool:
        """Fire-and-forget alert; never raises into the trading loop."""
        if not self.enabled:
            return False
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(
                    self._url, json={"chat_id": self._chat_id, "text": text}
                )
                return resp.status_code == 200
        except Exception:
            return False
