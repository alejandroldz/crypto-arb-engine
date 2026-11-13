"""Async ccxt.pro connectivity: public WebSockets + REST fallback (RISK 3).

Every snapshot carries the exchange timestamp and a local monotonic receipt
timestamp; staleness is enforced downstream by the risk gate. Only public
endpoints are used here - no API keys required for ingestion.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import ccxt.pro as ccxtpro

from app.connectors.normalizer import FundingRate, L2OrderBook, OrderBookLevel
from app.core.events import EventBus

logger = logging.getLogger("ingestion")

BOOKS_TOPIC = "books"
FUNDING_TOPIC = "funding"


def normalize_book(exchange_id: str, symbol: str, raw: dict) -> L2OrderBook:
    """ccxt order book payload -> canonical L2OrderBook."""
    ts_ms = raw.get("timestamp")
    return L2OrderBook(
        exchange=exchange_id,
        symbol=symbol,
        bids=tuple(OrderBookLevel(price=str(p), qty=str(q)) for p, q, *_ in raw["bids"]),
        asks=tuple(OrderBookLevel(price=str(p), qty=str(q)) for p, q, *_ in raw["asks"]),
        exchange_ts=(
            datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else None
        ),
        local_ts_ns=time.monotonic_ns(),
        sequence=raw.get("nonce"),
    )


def normalize_funding(exchange_id: str, symbol: str, raw: dict) -> FundingRate:
    """ccxt funding rate payload -> canonical FundingRate."""
    next_ts = raw.get("nextFundingTimestamp")
    return FundingRate(
        exchange=exchange_id,
        symbol=symbol,
        rate=str(raw.get("fundingRate") or "0"),
        local_ts_ns=time.monotonic_ns(),
        next_funding_ts=(
            datetime.fromtimestamp(next_ts / 1000, tz=timezone.utc) if next_ts else None
        ),
        predicted_rate=(
            str(raw["predictedRate"]) if raw.get("predictedRate") is not None else None
        ),
    )


class BookFeed:
    """Watches L2 books for a list of symbols on one exchange.

    WebSocket first; on any ccxt network/exchange error it falls back to one
    REST poll per symbol, backs off, and retries the socket.
    """

    def __init__(self, exchange_id: str, symbols: list[str], rest_backoff_s: int = 2):
        self.exchange_id = exchange_id
        self.symbols = list(symbols)
        self.rest_backoff_s = rest_backoff_s
        self.last_error: str | None = None

    async def run(self, bus: EventBus, stop: asyncio.Event) -> None:
        ex = getattr(ccxtpro, self.exchange_id)({"enableRateLimit": True})
        ex.number = str  # parse numbers as strings, never floats
        try:
            while not stop.is_set():
                for symbol in self.symbols:
                    if stop.is_set():
                        break
                    try:
                        raw = await ex.watch_order_book(symbol)
                        bus.publish(
                            BOOKS_TOPIC, normalize_book(self.exchange_id, symbol, raw)
                        )
                        self.last_error = None
                    except Exception as exc:  # ccxt NetworkError/ExchangeError
                        self.last_error = f"{type(exc).__name__}: {exc}"
                        logger.warning("book feed %s %s: %s", self.exchange_id, symbol, self.last_error)
                        await self._rest_once(ex, bus, symbol)
                        await asyncio.sleep(self.rest_backoff_s)
        finally:
            await ex.close()

    async def _rest_once(self, ex, bus: EventBus, symbol: str) -> None:
        try:
            raw = await ex.fetch_order_book(symbol)
            bus.publish(BOOKS_TOPIC, normalize_book(self.exchange_id, symbol, raw))
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"


class FundingPoller:
    """Polls public funding rates for perp symbols (REST; rates move slowly)."""

    def __init__(self, exchange_id: str, symbols: list[str], interval_s: int = 30):
        self.exchange_id = exchange_id
        self.symbols = list(symbols)
        self.interval_s = interval_s
        self.last_error: str | None = None

    async def run(self, bus: EventBus, stop: asyncio.Event) -> None:
        ex = getattr(ccxtpro, self.exchange_id)({"enableRateLimit": True})
        ex.number = str
        try:
            while not stop.is_set():
                for symbol in self.symbols:
                    try:
                        raw = await ex.fetch_funding_rate(symbol)
                        bus.publish(
                            FUNDING_TOPIC,
                            normalize_funding(self.exchange_id, symbol, raw),
                        )
                        self.last_error = None
                    except Exception as exc:
                        self.last_error = f"{type(exc).__name__}: {exc}"
                        logger.warning("funding poll %s %s: %s", self.exchange_id, symbol, self.last_error)
                await asyncio.sleep(self.interval_s)
        finally:
            await ex.close()
