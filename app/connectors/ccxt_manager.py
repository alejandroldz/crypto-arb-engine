"""Async ccxt.pro connectivity: public WebSockets + REST fallback (RISK 3).

Every snapshot carries the exchange timestamp and a local monotonic receipt
timestamp; staleness is enforced downstream by the risk gate. Only public
endpoints are used here - no API keys required for ingestion.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
import logging
import time
from datetime import datetime, timezone

import ccxt.pro as ccxtpro

from app.connectors.normalizer import FundingRate, L2OrderBook, OrderBookLevel
from app.core.events import EventBus

logger = logging.getLogger("ingestion")

BOOKS_TOPIC = "books"
FUNDING_TOPIC = "funding"


def _contract_base_qty(qty, price, market: dict | None) -> str:
    """Convert a raw book amount to BASE-currency quantity.

    ccxt leaves derivative book amounts in the venue's native unit: contracts
    (okx: contractSize base units per contract for linear, quote for inverse),
    USD for deribit inverse perps, base units already for krakenfutures.
    Spot markets and unit-sized contracts pass through unchanged.
    """
    q = Decimal(str(qty))
    if not market:
        return str(q)
    cs = market.get("contractSize")
    if not cs or not market.get("contract"):
        return str(q)
    cs = Decimal(str(cs))
    if market.get("linear"):
        return str(q * cs)
    p = Decimal(str(price))
    return str(q * cs / p)  # inverse: contract worth `cs` quote units


def normalize_book(
    exchange_id: str, symbol: str, raw: dict, market: dict | None = None
) -> L2OrderBook:
    """ccxt order book payload -> canonical L2OrderBook."""
    ts_ms = raw.get("timestamp")
    return L2OrderBook(
        exchange=exchange_id,
        symbol=symbol,
        bids=tuple(
            OrderBookLevel(price=str(p), qty=_contract_base_qty(q, p, market))
            for p, q, *_ in raw["bids"]
        ),
        asks=tuple(
            OrderBookLevel(price=str(p), qty=_contract_base_qty(q, p, market))
            for p, q, *_ in raw["asks"]
        ),
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
        # market metadata (contractSize/linear) converts contract-denominated
        # book amounts to base units before engines see them
        markets = {}
        try:
            await ex.load_markets()
            markets = {s: ex.market(s) for s in self.symbols}
        except Exception as exc:
            logger.warning("market meta %s: %s", self.exchange_id, exc)
        # NOTE: no ex.number = str here: ccxt.pro order-book maintenance does
        # arithmetic on prices (-price), which breaks on strings. normalize_book
        # stringifies floats via repr, which is exact for Decimal parsing.
        try:
            while not stop.is_set():
                for symbol in self.symbols:
                    if stop.is_set():
                        break
                    try:
                        raw = await ex.watch_order_book(symbol)
                        bus.publish(
                            BOOKS_TOPIC,
                            normalize_book(self.exchange_id, symbol, raw, markets.get(symbol)),
                        )
                        self.last_error = None
                    except Exception as exc:  # ccxt NetworkError/ExchangeError
                        self.last_error = f"{type(exc).__name__}: {exc}"
                        logger.warning("book feed %s %s: %s", self.exchange_id, symbol, self.last_error)
                        await self._rest_once(ex, bus, symbol, markets.get(symbol))
                        await asyncio.sleep(self.rest_backoff_s)
        finally:
            await ex.close()

    async def _rest_once(self, ex, bus: EventBus, symbol: str, market=None) -> None:
        try:
            raw = await ex.fetch_order_book(symbol)
            bus.publish(BOOKS_TOPIC, normalize_book(self.exchange_id, symbol, raw, market))
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


@dataclass(frozen=True)
class AccountHealth:
    """Real unified-account health read from an exchange (needs read-only keys)."""

    exchange: str
    total_equity: Decimal
    total_margin_balance: Decimal
    total_maintenance_margin: Decimal

    @property
    def account_mmr(self) -> Decimal:
        if self.total_margin_balance <= 0:
            # empty account: no positions, no risk - not an emergency
            return Decimal("1") if self.total_equity < 0 else Decimal("0")
        return self.total_maintenance_margin / self.total_margin_balance


async def fetch_unified_account_health(
    exchange_id: str,
    api_key: str,
    secret: str,
    password: str = "",
    hostname: str = "",
) -> AccountHealth | None:
    """Real account MMR from private endpoints (read-only keys).

    Returns None when keys are missing: paper mode then simulates account
    health with UtaAccountState against live books (documented fallback).
    Requires ccxt pro/async classes only for REST; closes the instance.
    """
    if not api_key or not secret:
        return None
    cfg = {"apiKey": api_key, "secret": secret, "enableRateLimit": True}
    if password:
        cfg["password"] = password
    ex = getattr(ccxtpro, exchange_id)(cfg)
    if hostname:
        # e.g. OKX Europe (MiFID) keys live on my.okx.com, not www.okx.com
        ex.hostname = hostname
    try:
        if exchange_id == "deribit":
            r = await ex.private_get_account_summary({"currency": "BTC"})
            res = r.get("result", {})
            return AccountHealth(
                exchange=exchange_id,
                total_equity=Decimal(str(res.get("equity", "0"))),
                total_margin_balance=Decimal(str(res.get("margin_balance", "0"))),
                total_maintenance_margin=Decimal(str(res.get("maintenance_margin", "0"))),
            )
        if exchange_id == "okx":
            # v5 unified account: totalEq/adjEq; mgnRatio = adjEq/MM (OKX
            # inverts the spec's MMR). Empty account: mgnRatio is "".
            r = await ex.private_get_account_balance()
            data = (r.get("data") or [{}])[0]
            adj_eq = Decimal(str(data.get("adjEq") or "0"))
            mgn_ratio = Decimal(str(data.get("mgnRatio") or "0"))
            mm = adj_eq / mgn_ratio if mgn_ratio > 0 else Decimal("0")
            return AccountHealth(
                exchange=exchange_id,
                total_equity=Decimal(str(data.get("totalEq", "0"))),
                total_margin_balance=adj_eq,
                total_maintenance_margin=mm,
            )
        if exchange_id == "bybit":
            # v5 UNIFIED account: equity/margin/MMR straight from the account
            r = await ex.private_get_v5_account_info()
            res = r.get("result") or {}
            return AccountHealth(
                exchange=exchange_id,
                total_equity=Decimal(str(res.get("totalEquity", "0"))),
                total_margin_balance=Decimal(str(res.get("totalMarginBalance", "0"))),
                total_maintenance_margin=Decimal(str(res.get("totalMaintenanceMargin", "0"))),
            )
        if exchange_id == "krakenfutures":
            r = await ex.private_get_accounts()
            acc = (r.get("accounts") or {}).get("multi_collateral") or (
                r.get("accounts") or {}
            ).get("flex", {})
            portfolio = Decimal(str(acc.get("portfolioValue", "0")))
            mm = Decimal(str(acc.get("maintenanceMargin", "0")))
            return AccountHealth(
                exchange=exchange_id,
                total_equity=portfolio,
                total_margin_balance=portfolio,
                total_maintenance_margin=mm,
            )
        return None  # venue not mapped: simulate instead
    finally:
        await ex.close()


def fetch_collateral_ratio(
    exchange_id: str, asset: str, fallback_map: dict[str, Decimal]
) -> Decimal:
    """Per-asset collateral value ratio.

    Honest note: no ccxt exchange exposes collateral haircut tiers through a
    public endpoint, and private variants differ per venue. The configured
    haircut map (BTC 0.95, ETH 0.95, SOL 0.85, default 0.00) is the source
    of truth, verifiable against the exchange docs.
    """
    return fallback_map.get(asset.strip().upper(), Decimal("0"))
