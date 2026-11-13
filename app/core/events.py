"""Minimal internal async event bus: topic -> subscriber queues."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("events")


class EventBus:
    def __init__(self, max_queue: int = 1000) -> None:
        self._topics: dict[str, list[asyncio.Queue]] = {}
        self._max_queue = max_queue
        self.drops: dict[str, int] = {}  # dropped-oldest count per topic

    def subscribe(self, topic: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._topics.setdefault(topic, []).append(q)
        return q

    def publish(self, topic: str, item: Any) -> None:
        for q in self._topics.get(topic, []):
            if q.full():
                q.get_nowait()  # drop oldest: consumers prefer fresh data
                self.drops[topic] = self.drops.get(topic, 0) + 1
                if self.drops[topic] % 100 == 1:
                    logger.warning("event bus dropping on %s (total %s)", topic, self.drops[topic])
            q.put_nowait(item)
