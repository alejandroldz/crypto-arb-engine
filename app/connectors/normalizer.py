"""Canonical market-data models.

This module is the single boundary between connectors (ccxt) and engines:
engines only ever see these types, never raw ccxt payloads.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.decimals import D, ZERO


class OrderBookLevel(BaseModel):
    """One L2 level: price and available base quantity."""

    model_config = ConfigDict(frozen=True)

    price: Decimal
    qty: Decimal

    @field_validator("price", "qty", mode="before")
    @classmethod
    def _strict_decimal(cls, v: object) -> Decimal:
        return D(v)

    @field_validator("price", "qty")
    @classmethod
    def _positive(cls, v: Decimal) -> Decimal:
        if v <= ZERO:
            raise ValueError("order book level price/qty must be positive")
        return v

    @property
    def quote_volume(self) -> Decimal:
        return self.price * self.qty


class L2OrderBook(BaseModel):
    """Canonical L2 order book with dual timestamps (RISK 3).

    ``exchange_ts`` is the exchange's own timestamp; ``local_ts_ns`` is
    ``time.monotonic_ns()`` at receipt and drives the staleness gate.
    """

    model_config = ConfigDict(frozen=True)

    exchange: str
    symbol: str  # ccxt unified symbol, e.g. "BTC/EUR"
    bids: tuple[OrderBookLevel, ...]  # sorted: highest price first
    asks: tuple[OrderBookLevel, ...]  # sorted: lowest price first
    local_ts_ns: int
    exchange_ts: datetime | None = None
    sequence: int | None = None

    @field_validator("bids", "asks")
    @classmethod
    def _non_empty(cls, v: tuple[OrderBookLevel, ...]) -> tuple[OrderBookLevel, ...]:
        if not v:
            raise ValueError("order book side must not be empty")
        return v

    @field_validator("bids")
    @classmethod
    def _bids_sorted(cls, v: tuple[OrderBookLevel, ...]) -> tuple[OrderBookLevel, ...]:
        prices = [lvl.price for lvl in v]
        if prices != sorted(prices, reverse=True):
            raise ValueError("bids must be sorted highest price first")
        return v

    @field_validator("asks")
    @classmethod
    def _asks_sorted(cls, v: tuple[OrderBookLevel, ...]) -> tuple[OrderBookLevel, ...]:
        prices = [lvl.price for lvl in v]
        if prices != sorted(prices):
            raise ValueError("asks must be sorted lowest price first")
        return v


    @property
    def best_bid(self) -> OrderBookLevel:
        return self.bids[0]

    @property
    def best_ask(self) -> OrderBookLevel:
        return self.asks[0]

    def age_ms(self, now_ns: int) -> int:
        """Age of this snapshot in milliseconds against a monotonic clock."""
        return (now_ns - self.local_ts_ns) // 1_000_000


class FundingRate(BaseModel):
    """Canonical funding-rate snapshot for one perpetual market."""

    model_config = ConfigDict(frozen=True)

    exchange: str
    symbol: str
    rate: Decimal  # signed, per funding interval (e.g. 0.0001 = 0.01%)
    interval_hours: int = 8
    local_ts_ns: int
    next_funding_ts: datetime | None = None
    predicted_rate: Decimal | None = None

    @field_validator("rate", "predicted_rate", mode="before")
    @classmethod
    def _strict_decimal(cls, v: object) -> Decimal | None:
        if v is None:
            return None
        return D(v)

    def age_ms(self, now_ns: int) -> int:
        return (now_ns - self.local_ts_ns) // 1_000_000
