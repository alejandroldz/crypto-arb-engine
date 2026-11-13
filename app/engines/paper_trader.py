"""Realistic dry-run engine for the funding carry (Development Rule 3).

Simulates network latency between signal and fill: after the latency window
the trade is re-validated against the CURRENT books and priced at the
post-latency VWAP. If the edge or the depth died during the delay, no paper
trade happens. All accounting in Decimal USD; every fill hits SQLite and
the immutable tax log (marked kind="paper").
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from app.connectors.normalizer import L2OrderBook
from app.core.decimals import D, ZERO, bps_of, div, mul
from app.database.repository import Repository
from app.engines.arbitrage import FundingOpportunity
from app.engines.liquidity import InsufficientDepth, vwap_for_base, vwap_for_quote

#: returns the CURRENT (spot_book, perp_book) when called after the delay
BookSource = Callable[[], tuple[L2OrderBook, L2OrderBook]]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaperFill:
    position_id: int
    qty_base: Decimal
    spot_vwap: Decimal
    perp_vwap: Decimal


class PaperTrader:
    def __init__(
        self,
        repo: Repository,
        *,
        latency_ms: int,
        spot_taker_fee_bps: Decimal = D("40"),
        perp_taker_fee_bps: Decimal = D("5"),
        slippage_buffer_bps: Decimal = D("5"),
        liquidity_haircut: Decimal = D("0.25"),
        fee_currency: str = "USD",
        execution_mode: str = "taker",
        spot_maker_fee_bps: Decimal | None = None,
        perp_maker_fee_bps: Decimal | None = None,
        maker_timeout_seconds: int = 60,
        maker_fallback: str = "taker",
        min_net_profit_usd: Decimal = Decimal("0"),
        settings=None,
    ):
        self.repo = repo
        self.latency_ms = latency_ms
        self.spot_taker_fee_bps = D(spot_taker_fee_bps)
        self.perp_taker_fee_bps = D(perp_taker_fee_bps)
        self.slippage_buffer_bps = D(slippage_buffer_bps)
        self.liquidity_haircut = D(liquidity_haircut)
        self.fee_currency = fee_currency
        self.execution_mode = execution_mode
        self.spot_maker_fee_bps = D(spot_maker_fee_bps) if spot_maker_fee_bps is not None else self.spot_taker_fee_bps
        self.perp_maker_fee_bps = D(perp_maker_fee_bps) if perp_maker_fee_bps is not None else self.perp_taker_fee_bps
        self.maker_timeout_seconds = maker_timeout_seconds
        self.maker_fallback = maker_fallback
        # revalidation floor: if the REAL fill mode (e.g. taker fallback)
        # costs more than the detection assumed and the net drops below
        # this floor, the entry is aborted instead of opening a loser
        self.min_net_profit_usd = D(min_net_profit_usd)
        # audit fix: optional live settings reference. When present, fees
        # resolve per ACTUAL venue at fill time (oracle updates detected_fees
        # at runtime); the constructor bps remain the fallback (tests, no
        # settings).
        self.settings = settings

    def _bps(self, leg: str, passive: bool, exchange: str | None = None) -> Decimal:
        if self.settings is not None and exchange:
            if passive:
                return D(self.settings.maker_fee_for(exchange))
            return D(self.settings.taker_fee_for(exchange))
        if leg == "spot":
            return self.spot_maker_fee_bps if passive else self.spot_taker_fee_bps
        return self.perp_maker_fee_bps if passive else self.perp_taker_fee_bps

    def _fee(
        self,
        leg: str,
        notional: Decimal,
        *,
        passive: bool = False,
        exchange: str | None = None,
    ) -> Decimal:
        return bps_of(notional, self._bps(leg, passive, exchange))

    async def _maker_entries(
        self, books: BookSource, notional: Decimal
    ) -> tuple[Decimal, Decimal, bool, bool] | None:
        """Simulate passive limit entries: spot buy at best bid, perp short at
        best ask. A limit fills when the opposite side trades through our
        price (conservative proxy with sampled books). On timeout the
        fallback applies: "taker" crosses the book at VWAP, "cancel" aborts.
        Returns (spot_price, perp_price, spot_passive, perp_passive)."""
        spot_book, perp_book = books()
        spot_limit = spot_book.best_bid.price
        perp_limit = perp_book.best_ask.price
        deadline = asyncio.get_running_loop().time() + self.maker_timeout_seconds
        while True:
            spot_book, perp_book = books()
            spot_filled = spot_book.best_ask.price <= spot_limit
            perp_filled = perp_book.best_bid.price >= perp_limit
            if spot_filled and perp_filled:
                return spot_limit, perp_limit, True, True
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(1)
        logger.info(
            "maker timeout (%ss): spot_filled=%s perp_filled=%s -> %s",
            self.maker_timeout_seconds, spot_filled, perp_filled, self.maker_fallback,
        )
        if self.maker_fallback == "cancel":
            return None
        buy = vwap_for_quote(spot_book, "buy", notional, self.liquidity_haircut)
        short = vwap_for_quote(perp_book, "sell", notional, self.liquidity_haircut)
        if not (buy.executable and short.executable):
            return None
        assert buy.vwap is not None and short.vwap is not None
        return (
            spot_limit if spot_filled else buy.vwap,
            perp_limit if perp_filled else short.vwap,
            spot_filled,
            perp_filled,
        )

    async def open_carry(
        self,
        opp: FundingOpportunity,
        books_after_latency: BookSource,
        *,
        spot_symbol: str = "",
    ) -> PaperFill | None:
        """Latency simulation, re-validation, then paper fills on both legs."""
        await asyncio.sleep(self.latency_ms / 1000)
        # screener #7: fee guardrail raised after candidate approval aborts the fill
        if self.settings is not None and getattr(self.settings, "fees_blocked", False):
            return None

        if self.execution_mode == "maker":
            entries = await self._maker_entries(books_after_latency, D(opp.notional))
            if entries is None:
                return None
            spot_px, perp_px, spot_passive, perp_passive = entries
            qty = min(
                div(D(opp.notional), spot_px), div(D(opp.notional), perp_px)
            )
            spot_book, perp_book = books_after_latency()
            spot_fee = self._fee(
                "spot", mul(qty, spot_px), passive=spot_passive,
                exchange=spot_book.exchange)
            perp_fee = self._fee(
                "perp", mul(qty, perp_px), passive=perp_passive,
                exchange=perp_book.exchange)
            logger.info(
                "maker entry %s: spot %s @ %s (%s) | perp %s @ %s (%s)",
                opp.symbol, qty, spot_px, "maker" if spot_passive else "taker-fallback",
                qty, perp_px, "maker" if perp_passive else "taker-fallback",
            )
            buy_vwap, short_vwap = spot_px, perp_px
        else:
            spot_book, perp_book = books_after_latency()
            buy = vwap_for_quote(spot_book, "buy", opp.notional, self.liquidity_haircut)
            short = vwap_for_quote(perp_book, "sell", opp.notional, self.liquidity_haircut)
            if not (buy.executable and short.executable):
                return None  # edge or depth died during the latency window
            assert buy.vwap is not None and short.vwap is not None

            # delta-neutral: equal base exposure on both legs
            qty = min(buy.base_qty, short.base_qty)
            spot_passive = perp_passive = False
            spot_fee = self._fee(
                "spot", mul(qty, buy.vwap), exchange=spot_book.exchange)
            perp_fee = self._fee(
                "perp", mul(qty, short.vwap), exchange=perp_book.exchange)
            buy_vwap, short_vwap = buy.vwap, short.vwap
        # audit fix #6: the detection priced entries at maker fees (in maker
        # mode). If a leg actually crossed as taker, re-price the delta and
        # abort when the trade no longer clears the profit floor.
        spot_ex, perp_ex = spot_book.exchange, perp_book.exchange
        extra = ZERO
        if self.execution_mode == "maker" and not spot_passive:
            extra += bps_of(mul(qty, buy_vwap),
                            self._bps("spot", False, spot_ex)
                            - self._bps("spot", True, spot_ex))
        if self.execution_mode == "maker" and not perp_passive:
            extra += bps_of(mul(qty, short_vwap),
                            self._bps("perp", False, perp_ex)
                            - self._bps("perp", True, perp_ex))
        if extra > ZERO and D(opp.projected_net) - extra < self.min_net_profit_usd:
            logger.info(
                "entry aborted %s: taker fallback costs +%s USD, net %s < floor %s",
                opp.symbol, extra, D(opp.projected_net) - extra, self.min_net_profit_usd,
            )
            return None
        if self.execution_mode != "maker":
            spot_book, perp_book = books_after_latency()

        now = datetime.now(timezone.utc)
        legs = (
            ("spot", spot_book.exchange, "buy", buy_vwap, spot_fee),
            ("perp", perp_book.exchange, "sell", short_vwap, perp_fee),
        )
        # op.#2: position + both legs in ONE transaction (crash-safe entry)
        position_id = await self.repo.open_position_atomic(
            {
                "symbol": opp.symbol,
                "exchange_spot": spot_book.exchange,
                "exchange_perp": perp_book.exchange,
                "qty_base": qty,
                "notional": mul(qty, buy_vwap),
                "spot_entry_vwap": buy_vwap,
                "perp_entry_vwap": short_vwap,
                "spot_symbol": spot_symbol or spot_book.symbol,
                "holding_intervals": opp.holding_intervals,
                "entry_funding_rate": opp.funding_rate,
            },
            [
                {"leg": leg, "action": "open", "side": side, "qty_base": qty,
                 "vwap": vwap, "fee": fee, "fee_currency": self.fee_currency}
                for leg, _, side, vwap, fee in legs
            ],
            [
                {"strategy_id": "funding_carry", "exchange": exchange,
                 "symbol": opp.symbol, "side": side, "amount": qty,
                 "price_usd_equivalent": vwap, "fee_amount": fee,
                 "fee_currency": self.fee_currency, "timestamp_utc": now,
                 "net_pnl_usd": None, "kind": "paper"}
                for leg, exchange, side, vwap, fee in legs
            ],
        )
        return PaperFill(
            position_id=position_id,
            qty_base=qty,
            spot_vwap=buy_vwap,
            perp_vwap=short_vwap,
        )

    async def close_carry(
        self,
        fill: PaperFill,
        funding_collected: Decimal,
        books_after_latency: BookSource,
    ) -> Decimal:
        """Close both legs after the latency window; return realized PnL (USD)."""
        await asyncio.sleep(self.latency_ms / 1000)
        spot_book, perp_book = books_after_latency()

        sell = vwap_for_base(spot_book, "sell", fill.qty_base, self.liquidity_haircut)
        cover = vwap_for_base(perp_book, "buy", fill.qty_base, self.liquidity_haircut)
        # F05 fix: a partial fill is NOT a close. Never record the full
        # position quantity against partial depth; keep the position open
        # and let the caller retry with fresh books.
        if not (sell.executable and cover.executable):
            raise InsufficientDepth(
                f"close aborted: spot filled {sell.base_qty}/{fill.qty_base}, "
                f"perp filled {cover.base_qty}/{fill.qty_base}"
            )
        assert sell.vwap is not None and cover.vwap is not None

        spot_proceeds = sell.filled_quote
        perp_close_cost = cover.filled_quote
        spot_entry_cost = mul(fill.qty_base, fill.spot_vwap)
        perp_entry_proceeds = mul(fill.qty_base, fill.perp_vwap)
        spot_fee = self._fee("spot", spot_proceeds, exchange=spot_book.exchange)
        perp_fee = self._fee("perp", perp_close_cost, exchange=perp_book.exchange)
        # fix: realized PnL must include the entry fees actually paid,
        # recovered from the orders ledger (not recomputed at current tiers)
        entry_fees = await self.repo.entry_fees(fill.position_id)

        pnl = (
            D(funding_collected)
            + (spot_proceeds - spot_entry_cost)
            + (perp_entry_proceeds - perp_close_cost)
            - spot_fee
            - perp_fee
            - entry_fees
        )
        now = datetime.now(timezone.utc)
        legs = (
            ("spot", spot_book.exchange, "sell", sell.vwap, spot_fee),
            ("perp", perp_book.exchange, "buy", cover.vwap, perp_fee),
        )
        # op.#2: close legs + status flip in ONE transaction (crash-safe exit)
        await self.repo.close_position_atomic(
            fill.position_id, pnl, now,
            [
                {"leg": leg, "action": "close", "side": side,
                 "qty_base": fill.qty_base, "vwap": vwap, "fee": fee,
                 "fee_currency": self.fee_currency}
                for leg, _, side, vwap, fee in legs
            ],
            [
                {"strategy_id": "funding_carry", "exchange": exchange,
                 "symbol": spot_book.symbol, "side": side,
                 "amount": fill.qty_base, "price_usd_equivalent": vwap,
                 "fee_amount": fee, "fee_currency": self.fee_currency,
                 "timestamp_utc": now,
                 "net_pnl_usd": pnl if leg == "perp" else None, "kind": "paper"}
                for leg, exchange, side, vwap, fee in legs
            ],
        )
        return pnl
