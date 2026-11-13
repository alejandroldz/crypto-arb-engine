"""Async ccxt.pro connectivity: public WebSockets + REST fallback (RISK 3).

Every snapshot carries the exchange timestamp and a local monotonic receipt
timestamp; staleness is enforced downstream by the risk gate. Only public
endpoints are used here - no API keys required for ingestion.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
import gc
import logging
import time
from datetime import datetime, timedelta, timezone

import ccxt.pro as ccxtpro

from app.core.feed_guards import check_magnitude, funding_rate_plausible
from app.core.decimals import D
from app.connectors.normalizer import FundingRate, L2OrderBook, OrderBookLevel
from app.core.events import EventBus

logger = logging.getLogger("ingestion")

BOOKS_TOPIC = "books"
FUNDING_TOPIC = "funding"


async def fetch_minimal_markets(exchange_id: str) -> list:
    """Fetch ONLY the market definitions we need, as a raw list for
    ex.set_markets(). Full load_markets pulls entire options chains
    (okx options chains etc.): hundreds of MB per ccxt instance, and BookFeed +
    FundingPoller instances per venue OOM a 2GB sandbox. One list per venue
    is fetched once in main and shared (by reference) across instances.
    """
    ex = getattr(ccxtpro, exchange_id)({"enableRateLimit": True})
    try:
        if exchange_id == "okx":  # EU X-Perps are instType FUTURES
            r = await ex.public_get_public_instruments({"instType": "FUTURES"})
            raw = r.get("data", [])
        elif exchange_id in ("krakenfutures", "kraken"):
            # no parse_markets on these: fetch all (transient peak, one venue
            # at a time in main) and keep only what feeds subscribe to
            markets = await ex.fetch_markets()
            if exchange_id == "kraken":
                return [m for m in markets if m.get("spot") and m.get("active")]
            return [
                m for m in markets
                if (m.get("future") or m.get("swap")) and m.get("active")
            ]
        return ex.parse_markets(raw)
    finally:
        await ex.close()


def _contract_base_qty(qty, price, market: dict | None) -> str:
    """Convert a raw book amount to BASE-currency quantity.

    ccxt leaves derivative book amounts in the venue's native unit: contracts
    (okx: contractSize base units per contract for linear, quote for inverse),
    USD for inverse perps, base units already for krakenfutures.
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
    bids_raw = raw["bids"]
    asks_raw = raw["asks"]
    # F07: a crossed/locked book makes the maker branch fill ANY size at
    # fantasy prices (price "touched" == full fill). Reject the snapshot;
    # the feed retries with the next one.
    if bids_raw and asks_raw and D(str(bids_raw[0][0])) >= D(str(asks_raw[0][0])):
        raise ValueError(
            f"crossed book rejected: bid {bids_raw[0][0]} >= ask {asks_raw[0][0]}"
        )
    # F09: reject non-finite / non-representable values BEFORE they reach
    # engines or DuckDB (a 1e30 tick must not kill persistence)
    bids = tuple(
        OrderBookLevel(
            price=str(check_magnitude(D(str(p)), "bid price")),
            qty=str(check_magnitude(D(_contract_base_qty(q, p, market)), "bid qty")),
        )
        for p, q, *_ in bids_raw
    )
    asks = tuple(
        OrderBookLevel(
            price=str(check_magnitude(D(str(p)), "ask price")),
            qty=str(check_magnitude(D(_contract_base_qty(q, p, market)), "ask qty")),
        )
        for p, q, *_ in asks_raw
    )
    return L2OrderBook(
        exchange=exchange_id,
        symbol=symbol,
        bids=bids,
        asks=asks,
        exchange_ts=(
            datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else None
        ),
        local_ts_ns=time.monotonic_ns(),
        sequence=raw.get("nonce"),
    )


def _parse_interval_hours(raw: dict, default: int = 8) -> int:
    """ccxt 'interval' (e.g. '1h', '8h') -> hours; default when absent."""
    raw_interval = raw.get("interval")
    if isinstance(raw_interval, str) and raw_interval.endswith("h"):
        try:
            h = int(raw_interval[:-1])
            if h > 0:
                return h
        except ValueError:
            pass
    return default


def next_settlement_ts(now: datetime, interval_hours: int) -> datetime:
    """Next funding settlement boundary after `now` (pure, testable).

    dif D2: some venues (Kraken Futures via ccxt) report NO funding
    timestamps at all. The accrual watermark then derives from the wall
    clock and the venue's known interval (Kraken settles hourly).
    """
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    period_s = int(interval_hours) * 3600
    secs = int((now - epoch).total_seconds())
    return epoch + timedelta(seconds=(secs // period_s + 1) * period_s)


def normalize_funding(exchange_id: str, symbol: str, raw: dict) -> FundingRate:
    """ccxt funding rate payload -> canonical FundingRate."""
    interval_hours = _parse_interval_hours(raw)
    # dif D5: prefer fundingTimestamp (the NEXT settlement, e.g. OKX
    # fundingTime). nextFundingTimestamp is the settlement AFTER that; as an
    # accrual watermark it always jumps exactly one interval, defeating the
    # missed-payments gap check.
    next_ts = raw.get("fundingTimestamp") or raw.get("nextFundingTimestamp")
    if next_ts:
        next_funding_ts = datetime.fromtimestamp(next_ts / 1000, tz=timezone.utc)
    elif interval_hours > 0:
        # dif D2: Kraken gives no timestamps - synthesize the next boundary
        # so the accrual watermark advances (funding was never accrued!).
        next_funding_ts = next_settlement_ts(datetime.now(timezone.utc), interval_hours)
    else:
        next_funding_ts = None
    # F08: quarantine implausible magnitudes (rate=10 = +1000% per interval)
    rate = funding_rate_plausible(raw.get("fundingRate") or "0")
    # dif D3: ccxt exposes the prediction as 'nextFundingRate', never
    # 'predictedRate' (the old key was always None -> flip checks used the
    # stale trailing rate).
    predicted = raw.get("nextFundingRate")
    if predicted is not None:
        predicted = str(check_magnitude(D(str(predicted)), "predicted rate"))
    return FundingRate(
        exchange=exchange_id,
        symbol=symbol,
        rate=str(rate),
        interval_hours=interval_hours,
        local_ts_ns=time.monotonic_ns(),
        next_funding_ts=next_funding_ts,
        predicted_rate=predicted,
    )


class BookFeed:
    """Watches L2 books for a list of symbols on one exchange.

    WebSocket first; on any ccxt network/exchange error it falls back to one
    REST poll per symbol, backs off, and retries the socket.
    """

    def __init__(self, exchange_id: str, symbols: list[str], rest_backoff_s: int = 2,
                 markets_list: list | None = None, derivative: bool = True,
                 watch_timeout_s: int = 30):
        self.exchange_id = exchange_id
        self.symbols = list(symbols)
        self.rest_backoff_s = rest_backoff_s
        self.markets_list = markets_list
        self.derivative = derivative
        # op.#13: a watch that neither returns nor raises (stuck socket) now
        # hits this deadline and falls back instead of hanging forever
        self.watch_timeout_s = watch_timeout_s
        self.last_error: str | None = None

    async def run(self, bus: EventBus, stop: asyncio.Event) -> None:
        ex = getattr(ccxtpro, self.exchange_id)({"enableRateLimit": True})
        # market metadata (contractSize/linear) converts contract-denominated
        # book amounts to base units before engines see them
        markets = {}
        # F13: fail closed. Without contract metadata, derivative book amounts
        # are in UNKNOWN units (contracts vs base): publishing them would
        # inflate depth by orders of magnitude. Retry with backoff and never
        # publish until metadata is available. Spot venues pass amounts
        # through unchanged, so for them metadata stays optional.
        while not markets and not stop.is_set():
            try:
                if self.markets_list is not None:
                    ex.set_markets(self.markets_list)
                elif not self.derivative:
                    await ex.load_markets()
                else:
                    # op.#18: full load_markets OOMs the sandbox; a derivative
                    # feed without the minimal market table fails CLOSED
                    # instead of reintroducing that memory route
                    raise RuntimeError(
                        f"derivative feed {self.exchange_id} without minimal "
                        "market table (full load_markets disabled)"
                    )
                markets = {s: ex.market(s) for s in self.symbols}
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("market meta %s: %s", self.exchange_id, exc)
                if not self.derivative:
                    break  # spot: base units already, metadata optional
                await asyncio.sleep(max(self.rest_backoff_s, 5))
        if self.derivative and not markets:
            logger.error("book feed %s disabled: no contract metadata", self.exchange_id)
            await ex.close()
            return
        # NOTE: no ex.number = str here: ccxt.pro order-book maintenance does
        # arithmetic on prices (-price), which breaks on strings. normalize_book
        # stringifies floats via repr, which is exact for Decimal parsing.
        try:
            while not stop.is_set():
                for symbol in self.symbols:
                    if stop.is_set():
                        break
                    try:
                        raw = await asyncio.wait_for(
                            ex.watch_order_book(symbol), timeout=self.watch_timeout_s
                        )
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

    def __init__(self, exchange_id: str, symbols: list[str], interval_s: int = 30,
                 markets_list: list | None = None):
        self.exchange_id = exchange_id
        self.symbols = list(symbols)
        self.interval_s = interval_s
        self.markets_list = markets_list
        self.last_error: str | None = None

    async def run(self, bus: EventBus, stop: asyncio.Event) -> None:
        ex = getattr(ccxtpro, self.exchange_id)({"enableRateLimit": True})
        ex.number = str
        if self.markets_list is not None:
            ex.set_markets(self.markets_list)
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
