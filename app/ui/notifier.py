"""Async Telegram alerts via httpx (free, no dependencies beyond httpx)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout_s: int = 10,
                 outbox_path: str = "alerts_outbox.jsonl"):
        self.enabled = bool(bot_token and chat_id)
        self._outbox_path = outbox_path
        # token lives only inside the client URL; NEVER log it (Telegram
        # exceptions embed the full URL, token included)
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._timeout_s = timeout_s

    def _outbox(self, text: str) -> None:
        """op.#17: a failed alert is never silently lost - it lands in a
        durable outbox file for inspection/replay."""
        try:
            with open(self._outbox_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "text": text,
                }) + "\n")
        except Exception:
            logger.exception("alert outbox write failed")

    async def send(self, text: str) -> bool:
        """Fire-and-forget alert; never raises into the trading loop.

        Failures ARE logged (sanitized, without the token URL) AND persisted
        to the outbox, so a dead alert channel is visible instead of
        silently lost.
        """
        if not self.enabled:
            self._outbox(text)
            return False
        ok = False
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(
                    self._url, json={"chat_id": self._chat_id, "text": text}
                )
                if resp.status_code != 200:
                    logger.warning(
                        "telegram alert failed: HTTP %s", resp.status_code
                    )
                else:
                    ok = True
        except Exception as exc:
            # exc may carry the token URL - log only the exception TYPE
            logger.warning("telegram alert failed: %s", type(exc).__name__)
        if not ok:
            self._outbox(text)
        return ok
