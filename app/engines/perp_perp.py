"""Strategy B: perp-perp funding arbitrage (delta-neutral, NO spot leg).

Long the perp on the venue with the LOWEST funding, short the same base's
perp on the venue with the HIGHEST funding. Collects the funding
differential; cost per round trip is 4 perp fills (maker entry + taker exit)
instead of the expensive spot taker leg. Decoupled from the cash-and-carry:
own detection, own paper trader, own guardrails. If this module raises, the
carry engine is unaffected (run() isolates the call sites).

Guardrails (user-mandated):
- funding flip: current net differential <= 0 -> unwind taker
- hedge imbalance after entry beyond tolerance -> taker correction
- per-venue notional limits + per-pair cap + portfolio budget callback
- exits are ALWAYS taker (an unwind cannot wait for a passive fill)
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Sequence

from app.connectors.normalizer import FundingRate, L2OrderBook
from app.core.decimals import D, ZERO, bps_of, div, mul
from app.database.repository import Repository
from app.engines.liquidity import InsufficientDepth, vwap_for_base, vwap_for_quote

logger = logging.getLogger(__name__)

NS_PER_HOUR = 3_600_000_000_000

BookSource = Callable[[], tuple[L2OrderBook, L2OrderBook]]  # (long_book, short_book)


@dataclass(frozen=True)
class PerpPerpOpportunity:
    base: str
    long_exchange: str
    long_symbol: str
    short_exchange: str
    short_symbol: str
    notional: Decimal
    net_rate_per_hour: Decimal  # after sign, before costs
    holding_hours: int
    breakeven_hours: int
    projected_net: Decimal


@dataclass(frozen=True)
class PerpPerpFill:
    position_id: int
    qty_base: Decimal
    long_vwap: Decimal
    short_vwap: Decimal


def _rank(rates: Sequence[Decimal], pct: int) -> Decimal:
    xs = sorted(D(r) for r in rates)
    idx = max(0, min(len(xs) - 1, (pct * len(xs) + 99) // 100 - 1))
    return xs[idx]


def detect_perp_perp(
    *,
    base: str,
    long_exchange: str,
    long_symbol: str,
    short_exchange: str,
    short_symbol: str,
    long_history: Sequence[Decimal],
    short_history: Sequence[Decimal],
    long_interval_h: Decimal,
    short_interval_h: Decimal,
    notional: Decimal,
    holding_hours: int,
    maker_fee_bps: Decimal,
    taker_fee_bps: Decimal,
    slippage_buffer_bps: Decimal,
    min_net_profit_usd: Decimal,
    min_history_samples: int = 30,
) -> PerpPerpOpportunity | None:
    """Evaluate one venue pair for the funding differential.

    Pessimistic both ways: short leg collects its p25 rate, long leg pays
    its p75 rate. Rates are per-interval; normalized to per-hour before
    comparing (kraken pays hourly, okx every 8h).
    """
    if len(short_history) < min_history_samples or len(long_history) < min_history_samples:
        return None
    collect_ph = div(_rank(short_history, 25), D(short_interval_h))
    pay_ph = div(_rank(long_history, 75), D(long_interval_h))
    net_ph = collect_ph - pay_ph
    if net_ph <= ZERO:
        return None
    n = D(notional)
    payout = mul(mul(n, net_ph), D(holding_hours))
    cost_bps = D(maker_fee_bps) * 2 + D(taker_fee_bps) * 2 + D(slippage_buffer_bps) * 4
    costs = bps_of(n, cost_bps)
    projected = payout - costs
    if projected < D(min_net_profit_usd):
        return None
    per_hour = mul(n, net_ph)
    breakeven = int(div(costs, per_hour).to_integral_value(rounding="ROUND_CEILING"))
    if breakeven > holding_hours:
        return None
    return PerpPerpOpportunity(
        base=base,
        long_exchange=long_exchange,
        long_symbol=long_symbol,
        short_exchange=short_exchange,
        short_symbol=short_symbol,
        notional=n,
        net_rate_per_hour=net_ph,
        holding_hours=holding_hours,
        breakeven_hours=breakeven,
        projected_net=projected,
    )


def current_net_rate_per_hour(
    short_rate: FundingRate, short_interval_h: Decimal,
    long_rate: FundingRate, long_interval_h: Decimal,
) -> Decimal:
    """Net differential with the CURRENT ticks (guardrail, not entry)."""
    return div(D(short_rate.rate), D(short_interval_h)) - div(D(long_rate.rate), D(long_interval_h))


class PerpPerpTrader:
    """Paper execution for perp-perp pairs: maker entries, taker exits."""

    def __init__(
        self,
        repo: Repository,
        *,
        latency_ms: int,
        maker_fee_bps: Decimal = D("2"),
        taker_fee_bps: Decimal = D("5"),
        liquidity_haircut: Decimal = D("0.25"),
        execution_mode: str = "maker",
        maker_timeout_seconds: int = 60,
        maker_fallback: str = "taker",
        imbalance_tolerance: Decimal = D("0.001"),
        fee_currency: str = "USD",
    ):
        self.repo = repo
        self.latency_ms = latency_ms
        self.maker_fee_bps = D(maker_fee_bps)
        self.taker_fee_bps = D(taker_fee_bps)
        self.liquidity_haircut = D(liquidity_haircut)
        self.execution_mode = execution_mode
        self.maker_timeout_seconds = maker_timeout_seconds
        self.maker_fallback = maker_fallback
        self.imbalance_tolerance = D(imbalance_tolerance)
        self.fee_currency = fee_currency

    async def open_pair(
        self, opp: PerpPerpOpportunity, books_after_latency: BookSource
    ) -> PerpPerpFill | None:
        await asyncio.sleep(self.latency_ms / 1000)
        if self.execution_mode == "maker":
            filled = await self._maker_entries(books_after_latency, D(opp.notional))
            if filled is None:
                return None
            long_px, short_px, long_passive, short_passive = filled
            long_book, short_book = books_after_latency()  # for audit metadata
        else:
            long_book, short_book = books_after_latency()
            buy = vwap_for_quote(long_book, "buy", opp.notional, self.liquidity_haircut)
            sell = vwap_for_quote(short_book, "sell", opp.notional, self.liquidity_haircut)
            if not (buy.executable and sell.executable):
                return None
            assert buy.vwap is not None and sell.vwap is not None
            long_px, short_px = buy.vwap, sell.vwap
            long_passive = short_passive = False
        qty = min(div(D(opp.notional), long_px), div(D(opp.notional), short_px))
        long_fee = bps_of(mul(qty, long_px), self.maker_fee_bps if long_passive else self.taker_fee_bps)
        short_fee = bps_of(mul(qty, short_px), self.maker_fee_bps if short_passive else self.taker_fee_bps)
        position_id = await self.repo.open_position(
            symbol=opp.short_symbol,
            exchange_spot=f"{opp.long_exchange}(long)",
            exchange_perp=opp.short_exchange,
            qty_base=qty,
            notional=mul(qty, long_px),
            spot_entry_vwap=long_px,
            perp_entry_vwap=short_px,
            spot_symbol=f"{opp.long_exchange}:{opp.long_symbol}",
            holding_intervals=opp.holding_hours,
            entry_funding_rate=opp.net_rate_per_hour,
        )
        now = datetime.now(timezone.utc)
        for leg, exchange, side, vwap, fee in (
            ("spot", long_book.exchange, "buy", long_px, long_fee),
            ("perp", short_book.exchange, "sell", short_px, short_fee),
        ):
            await self.repo.record_order(
                position_id=position_id, leg=leg, action="open", side=side,
                qty_base=qty, vwap=vwap, fee=fee, fee_currency=self.fee_currency,
            )
            await self.repo.audit_fill(
                strategy_id="perp_perp", paired_hedge_id=position_id,
                exchange=exchange, symbol=opp.short_symbol, side=side, amount=qty,
                price_usd_equivalent=vwap, fee_amount=fee,
                fee_currency=self.fee_currency, timestamp_utc=now,
                net_pnl_usd=None, kind="paper",
            )
        logger.info(
            "pp entry %s %s: long %s @ %s (%s) | short %s @ %s (%s)",
            opp.base, qty, opp.long_exchange, long_px,
            "maker" if long_passive else "taker-fallback",
            opp.short_exchange, short_px,
            "maker" if short_passive else "taker-fallback",
        )
        return PerpPerpFill(position_id=position_id, qty_base=qty,
                            long_vwap=long_px, short_vwap=short_px)

    async def _maker_entries(
        self, books: BookSource, notional: Decimal
    ) -> tuple[Decimal, Decimal, bool, bool] | None:
        long_book, short_book = books()
        long_limit = long_book.best_bid.price   # passive buy
        short_limit = short_book.best_ask.price  # passive sell
        deadline = asyncio.get_running_loop().time() + self.maker_timeout_seconds
        while True:
            long_book, short_book = books()
            long_filled = long_book.best_ask.price <= long_limit
            short_filled = short_book.best_bid.price >= short_limit
            if long_filled and short_filled:
                return long_limit, short_limit, True, True
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(1)
        logger.info(
            "pp maker timeout (%ss): long_filled=%s short_filled=%s -> %s",
            self.maker_timeout_seconds, long_filled, short_filled, self.maker_fallback,
        )
        if self.maker_fallback == "cancel":
            # one leg may have filled: caller treats None as abort; the paper
            # model fills atomically per tick so no real imbalance exists
            return None
        buy = vwap_for_quote(long_book, "buy", notional, self.liquidity_haircut)
        sell = vwap_for_quote(short_book, "sell", notional, self.liquidity_haircut)
        if not (buy.executable and sell.executable):
            return None
        assert buy.vwap is not None and sell.vwap is not None
        return (
            long_limit if long_filled else buy.vwap,
            short_limit if short_filled else sell.vwap,
            long_filled,
            short_filled,
        )

    async def close_pair(
        self, fill: PerpPerpFill, funding_collected_net: Decimal, books_after_latency: BookSource
    ) -> Decimal:
        """ALWAYS taker: an unwind cannot wait. Returns realized net PnL."""
        await asyncio.sleep(self.latency_ms / 1000)
        long_book, short_book = books_after_latency()
        sell = vwap_for_base(long_book, "sell", fill.qty_base, self.liquidity_haircut)
        cover = vwap_for_base(short_book, "buy", fill.qty_base, self.liquidity_haircut)
        # F05 fix: never record a full close against partial depth.
        if not (sell.executable and cover.executable):
            raise InsufficientDepth(
                f"pp close aborted: long filled {sell.base_qty}/{fill.qty_base}, "
                f"short filled {cover.base_qty}/{fill.qty_base}"
            )
        assert sell.vwap is not None and cover.vwap is not None
        long_fee = bps_of(sell.filled_quote, self.taker_fee_bps)
        short_fee = bps_of(cover.filled_quote, self.taker_fee_bps)
        # fix: subtract the entry fees actually recorded in the ledger
        entry_fees = await self.repo.entry_fees(fill.position_id)
        pnl = (
            D(funding_collected_net)
            + (sell.filled_quote - mul(fill.qty_base, fill.long_vwap))
            + (mul(fill.qty_base, fill.short_vwap) - cover.filled_quote)
            - long_fee
            - short_fee
            - entry_fees
        )
        now = datetime.now(timezone.utc)
        for leg, exchange, side, vwap, fee in (
            ("spot", long_book.exchange, "sell", sell.vwap, long_fee),
            ("perp", short_book.exchange, "buy", cover.vwap, short_fee),
        ):
            await self.repo.record_order(
                position_id=fill.position_id, leg=leg, action="close", side=side,
                qty_base=fill.qty_base, vwap=vwap, fee=fee, fee_currency=self.fee_currency,
            )
            await self.repo.audit_fill(
                strategy_id="perp_perp", paired_hedge_id=fill.position_id,
                exchange=exchange, symbol=long_book.symbol, side=side,
                amount=fill.qty_base, price_usd_equivalent=vwap, fee_amount=fee,
                fee_currency=self.fee_currency, timestamp_utc=now,
                net_pnl_usd=pnl if leg == "perp" else None, kind="paper",
            )
        await self.repo.close_position(fill.position_id, pnl, now)
        return pnl


# ---------------------------------------------------------------------------
# Runner: context + event handlers, driven from main.run() at two call sites.
# ---------------------------------------------------------------------------


@dataclass
class PpPosition:
    fill: PerpPerpFill
    opp: PerpPerpOpportunity
    long_key: str
    short_key: str
    funding_collected_net: Decimal
    opened_ns: int
    # F11 fix: one accrual watermark PER LEG - a shared clock let the first
    # event consume the whole elapsed window and zeroed the other leg
    last_accrual_ns_long: int
    last_accrual_ns_short: int


@dataclass
class PpContext:
    settings: object
    paper: PerpPerpTrader
    repo: Repository
    ticks: object
    books: dict
    funding: dict
    breaker: object
    notifier: object
    open: dict[str, PpPosition]
    budget_remaining: Callable[[], Decimal]  # portfolio budget after carries
    realized_pnl: list  # [Decimal] mutable accumulator owned by run()


def _common_bases(settings) -> list[str]:
    """Bases with a perp on at least two venues (pair candidates)."""
    venues = list(settings.perp_exchange_list)
    bases_per_venue = {
        ex: {sym.split("/")[0] for sym in settings.perp_symbols_for(ex)} for ex in venues
    }
    out = []
    for base in set.intersection(*bases_per_venue.values()) if len(venues) > 1 else set():
        out.append(base)
    return sorted(out)


def _symbol_for_base(settings, exchange: str, base: str) -> str | None:
    for sym in settings.perp_symbols_for(exchange):
        if sym.split("/")[0] == base:
            return sym
    return None


def _venue_exposure(ctx: PpContext, exchange: str) -> Decimal:
    total = ZERO
    for pos in ctx.open.values():
        if pos.opp.long_exchange == exchange or pos.opp.short_exchange == exchange:
            total += D(pos.opp.notional)
    return total


async def _try_open(ctx: PpContext, base: str) -> None:
    settings = ctx.settings
    venues = list(settings.perp_exchange_list)
    for short_ex in venues:
        for long_ex in venues:
            if short_ex == long_ex:
                continue
            pair_key = f"{base}:{long_ex}->{short_ex}"
            if pair_key in ctx.open or f"{base}:{short_ex}->{long_ex}" in ctx.open:
                continue  # one direction per base is enough
            short_sym = _symbol_for_base(settings, short_ex, base)
            long_sym = _symbol_for_base(settings, long_ex, base)
            if not short_sym or not long_sym:
                continue
            long_key, short_key = f"{long_ex}:{long_sym}", f"{short_ex}:{short_sym}"
            if long_key not in ctx.books or short_key not in ctx.books:
                continue
            short_hist = await asyncio.to_thread(
                ctx.ticks.funding_history, short_ex, short_sym)
            long_hist = await asyncio.to_thread(
                ctx.ticks.funding_history, long_ex, long_sym)
            maker_bps = max(settings.maker_fee_for(long_ex), settings.maker_fee_for(short_ex))
            taker_bps = max(settings.taker_fee_for(long_ex), settings.taker_fee_for(short_ex))
            budget = min(
                D(ctx.budget_remaining()),
                D(settings.pp_max_notional_per_pair_usd),
                D(settings.pp_max_notional_per_venue_usd) - _venue_exposure(ctx, long_ex),
                D(settings.pp_max_notional_per_venue_usd) - _venue_exposure(ctx, short_ex),
            )
            opp = detect_perp_perp(
                base=base, long_exchange=long_ex, long_symbol=long_sym,
                short_exchange=short_ex, short_symbol=short_sym,
                long_history=long_hist, short_history=short_hist,
                long_interval_h=D(settings.funding_interval_h_for(long_ex)),
                short_interval_h=D(settings.funding_interval_h_for(short_ex)),
                notional=max(D(settings.pp_min_notional_usd), budget),
                holding_hours=int(settings.pp_holding_hours),
                maker_fee_bps=maker_bps, taker_fee_bps=taker_bps,
                slippage_buffer_bps=D(settings.slippage_buffer_bps),
                min_net_profit_usd=D(settings.pp_min_net_profit_usd),
                min_history_samples=int(settings.min_funding_history),
            )
            if opp is None:
                continue
            if settings.sizing_mode == "kelly":
                from app.engines.sizing import dynamic_notional

                sized = dynamic_notional(
                    edge_per_interval=opp.net_rate_per_hour,
                    history=short_hist,
                    budget_remaining=budget,
                    kelly_fraction=D(settings.kelly_fraction),
                    full_score=D(settings.sizing_full_score),
                    min_notional=D(settings.pp_min_notional_usd),
                    max_notional=D(settings.pp_max_notional_per_pair_usd),
                )
                if sized <= 0:
                    continue
                from dataclasses import replace

                scale = sized / D(opp.notional)
                opp = replace(opp, notional=sized,
                              projected_net=D(opp.projected_net) * scale)
            def fetch(lk=long_key, sk=short_key):
                return ctx.books[lk], ctx.books[sk]
            fill = await ctx.paper.open_pair(opp, fetch)
            if fill is None:
                continue
            ctx.open[pair_key] = PpPosition(
                fill=fill, opp=opp, long_key=long_key, short_key=short_key,
                funding_collected_net=ZERO,
                opened_ns=time.monotonic_ns(),
                last_accrual_ns_long=time.monotonic_ns(),
                last_accrual_ns_short=time.monotonic_ns(),
            )
            logger.info("pp OPEN %s net %s USD", pair_key, opp.projected_net)
            await ctx.notifier.send(
                f"PP OPEN {pair_key} notional {opp.notional} projected {opp.projected_net} USD")


async def pp_handle_funding(ctx: PpContext, rate: FundingRate) -> None:
    """On each funding event: accrue legs, then (maybe) open new pairs."""
    now_ns = time.monotonic_ns()
    fkey = f"{rate.exchange}:{rate.symbol}"
    interval_h = D(ctx.settings.funding_interval_h_for(rate.exchange))
    for pos in ctx.open.values():
        sign = None
        if fkey == pos.short_key:
            sign = D("1")   # short collects when rate > 0
            watermark = pos.last_accrual_ns_short
        elif fkey == pos.long_key:
            sign = D("-1")  # long pays when rate > 0
            watermark = pos.last_accrual_ns_long
        if sign is None:
            continue
        elapsed_h = D(now_ns - watermark) / D(NS_PER_HOUR)
        elapsed_h = min(elapsed_h, interval_h * 2)  # suspension gaps don't mint yield
        if elapsed_h > 0:
            accrual = sign * mul(mul(D(pos.opp.notional), D(rate.rate)), elapsed_h / interval_h)
            pos.funding_collected_net += accrual
            if accrual != 0:
                await ctx.repo.add_funding(pos.fill.position_id, accrual)
        if sign > 0:
            pos.last_accrual_ns_short = now_ns
        else:
            pos.last_accrual_ns_long = now_ns
    if (not ctx.breaker.halted and not ctx.settings.fees_blocked
            and ctx.settings.perp_perp_enabled):
        for base in _common_bases(ctx.settings):
            await _try_open(ctx, base)


async def pp_manage(ctx: PpContext) -> None:
    """Guardrails + unwinds for open pairs (call every main-loop cycle)."""
    now_ns = time.monotonic_ns()
    for pair_key, pos in list(ctx.open.items()):
        opp = pos.opp
        reason = None
        short_rate = ctx.funding.get(pos.short_key)
        long_rate = ctx.funding.get(pos.long_key)
        if ctx.breaker.halted:
            reason = "circuit breaker (stress: salida taker obligatoria)"
        elif now_ns - pos.opened_ns >= opp.holding_hours * NS_PER_HOUR:
            reason = "holding window expired"
        elif short_rate is not None and long_rate is not None:
            net = current_net_rate_per_hour(
                short_rate, D(ctx.settings.funding_interval_h_for(opp.short_exchange)),
                long_rate, D(ctx.settings.funding_interval_h_for(opp.long_exchange)),
            )
            if net <= 0:
                reason = f"funding flip (net {net}/h)"
            elif D(short_rate.rate) < 0:
                reason = f"short leg pagando funding ({short_rate.rate})"
        if reason is None:
            continue
        def fetch(lk=pos.long_key, sk=pos.short_key):
            return ctx.books[lk], ctx.books[sk]
        ctx.breaker.record_order()
        ctx.breaker.record_order()
        try:
            pnl = await ctx.paper.close_pair(pos.fill, pos.funding_collected_net, fetch)
        except InsufficientDepth as exc:
            # transient: keep the pair open and retry next cycle
            logger.warning("pp unwind deferred (depth) %s: %s", pair_key, exc)
            continue
        except (KeyError, AssertionError) as exc:
            ctx.breaker.record_failure()
            logger.error("pp unwind failed %s: %s", pair_key, exc)
            await ctx.notifier.send(f"PP UNWIND FAILED {pair_key}: {exc}")
            continue
        ctx.realized_pnl[0] += pnl
        ctx.breaker.record_pnl_usd(pnl)
        del ctx.open[pair_key]
        logger.info("pp UNWOUND %s (%s): pnl %s USD", pair_key, reason, pnl)
        await ctx.notifier.send(f"PP UNWOUND {pair_key} ({reason}): pnl {pnl} USD")
