"""Realistic dry-run engine for the funding carry (Development Rule 3).

Simulates network latency between signal and fill: after the latency window
the trade is re-validated against the CURRENT books and priced at the
post-latency VWAP. If the edge or the depth died during the delay, no paper
trade happens. All accounting in Decimal; every fill hits SQLite and the
immutable tax log.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from app.connectors.normalizer import L2OrderBook
from app.core.decimals import D, bps_of, mul
from app.database.repository import Repository
from app.engines.arbitrage import FundingOpportunity
from app.engines.liquidity import vwap_for_base, vwap_for_quote

#: returns the CURRENT (spot_book, perp_book) when called after the delay
BookSource = Callable[[], tuple[L2OrderBook, L2OrderBook]]


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
        taker_fee_bps: Decimal,
        slippage_buffer_bps: Decimal,
        liquidity_haircut: Decimal,
        fee_currency: str = "EUR",
    ):
        self.repo = repo
        self.latency_ms = latency_ms
        self.taker_fee_bps = D(taker_fee_bps)
        self.slippage_buffer_bps = D(slippage_buffer_bps)
        self.liquidity_haircut = D(liquidity_haircut)
        self.fee_currency = fee_currency

    async def open_carry(
        self, opp: FundingOpportunity, books_after_latency: BookSource
    ) -> PaperFill | None:
        """Latency simulation, re-validation, then paper fills on both legs."""
        await asyncio.sleep(self.latency_ms / 1000)
        spot_book, perp_book = books_after_latency()

        buy = vwap_for_quote(spot_book, "buy", opp.notional, self.liquidity_haircut)
        short = vwap_for_quote(perp_book, "sell", opp.notional, self.liquidity_haircut)
        if not (buy.executable and short.executable):
            return None  # edge or depth died during the latency window
        assert buy.vwap is not None and short.vwap is not None

        # delta-neutral: equal base exposure on both legs
        qty = min(buy.base_qty, short.base_qty)
        spot_cost = mul(qty, buy.vwap)
        perp_notional = mul(qty, short.vwap)
        spot_fee = bps_of(spot_cost, self.taker_fee_bps)
        perp_fee = bps_of(perp_notional, self.taker_fee_bps)

        position_id = await self.repo.open_position(
            symbol=opp.symbol,
            exchange_spot=spot_book.exchange,
            exchange_perp=perp_book.exchange,
            qty_base=qty,
            notional=spot_cost,
            spot_entry_vwap=buy.vwap,
            perp_entry_vwap=short.vwap,
        )
        now = datetime.now(timezone.utc)
        for leg, exchange, side, vwap, fee in (
            ("spot", spot_book.exchange, "buy", buy.vwap, spot_fee),
            ("perp", perp_book.exchange, "sell", short.vwap, perp_fee),
        ):
            await self.repo.record_order(
                position_id=position_id,
                leg=leg,
                action="open",
                side=side,
                qty_base=qty,
                vwap=vwap,
                fee=fee,
                fee_currency=self.fee_currency,
            )
            await self.repo.audit_fill(
                strategy_id="funding_carry",
                paired_hedge_id=position_id,
                exchange=exchange,
                symbol=opp.symbol,
                side=side,
                amount=qty,
                price_eur_equivalent=vwap,
                fee_amount=fee,
                fee_currency=self.fee_currency,
                timestamp_utc=now,
                net_pnl_eur=None,
            )
        return PaperFill(
            position_id=position_id,
            qty_base=qty,
            spot_vwap=buy.vwap,
            perp_vwap=short.vwap,
        )

    async def close_carry(
        self,
        fill: PaperFill,
        funding_collected: Decimal,
        books_after_latency: BookSource,
    ) -> Decimal:
        """Close both legs after the latency window; return realized PnL (EUR)."""
        await asyncio.sleep(self.latency_ms / 1000)
        spot_book, perp_book = books_after_latency()

        sell = vwap_for_base(spot_book, "sell", fill.qty_base, self.liquidity_haircut)
        cover = vwap_for_base(perp_book, "buy", fill.qty_base, self.liquidity_haircut)
        assert sell.vwap is not None and cover.vwap is not None

        spot_proceeds = sell.filled_quote
        perp_close_cost = cover.filled_quote
        spot_entry_cost = mul(fill.qty_base, fill.spot_vwap)
        perp_entry_proceeds = mul(fill.qty_base, fill.perp_vwap)
        spot_fee = bps_of(spot_proceeds, self.taker_fee_bps)
        perp_fee = bps_of(perp_close_cost, self.taker_fee_bps)

        pnl = (
            D(funding_collected)
            + (spot_proceeds - spot_entry_cost)
            + (perp_entry_proceeds - perp_close_cost)
            - spot_fee
            - perp_fee
        )
        now = datetime.now(timezone.utc)
        for leg, exchange, side, vwap, fee in (
            ("spot", spot_book.exchange, "sell", sell.vwap, spot_fee),
            ("perp", perp_book.exchange, "buy", cover.vwap, perp_fee),
        ):
            await self.repo.record_order(
                position_id=fill.position_id,
                leg=leg,
                action="close",
                side=side,
                qty_base=fill.qty_base,
                vwap=vwap,
                fee=fee,
                fee_currency=self.fee_currency,
            )
            await self.repo.audit_fill(
                strategy_id="funding_carry",
                paired_hedge_id=fill.position_id,
                exchange=exchange,
                symbol=spot_book.symbol,
                side=side,
                amount=fill.qty_base,
                price_eur_equivalent=vwap,
                fee_amount=fee,
                fee_currency=self.fee_currency,
                timestamp_utc=now,
                net_pnl_eur=pnl if leg == "perp" else None,
            )
        await self.repo.close_position(fill.position_id, pnl, now)
        return pnl
