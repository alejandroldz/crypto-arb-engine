"""L2 liquidity engine (RISK 2).

Walks real order book levels to compute the exact VWAP for a target size.
Never uses midpoint or top-of-book prices for decisions. Applies the
LIQUIDITY_HAIRCUT rule: a trade is only executable when visible depth is at
least 2x the target size (haircut = 0.50).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.connectors.normalizer import L2OrderBook, OrderBookLevel
from app.core.decimals import D, MATH_PRECISION, ZERO, bps_of, div, mul

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class VwapResult:
    side: Side
    requested: Decimal  # target size, in quote for quote sizing, base for base sizing
    filled_quote: Decimal
    base_qty: Decimal
    vwap: Decimal | None  # None when no level could fill anything
    best_price: Decimal
    worst_price: Decimal  # deepest level consumed
    levels_used: int
    fully_filled: bool
    book_depth_quote: Decimal  # total visible depth on that side, in quote
    executable: bool  # fully_filled AND depth * haircut >= requested quote
    slippage_bps: Decimal  # adverse slippage of vwap vs best price, in bps


def _levels(book: L2OrderBook, side: Side) -> tuple[OrderBookLevel, ...]:
    return book.asks if side == "buy" else book.bids


def book_depth_quote(levels: tuple[OrderBookLevel, ...]) -> Decimal:
    depth = ZERO
    for lvl in levels:
        depth += lvl.quote_volume
    return depth


def _slippage_bps(vwap: Decimal, best: Decimal, side: Side) -> Decimal:
    if side == "buy":
        adverse = vwap - best
    else:
        adverse = best - vwap
    if adverse <= ZERO:
        return ZERO
    return div(mul(adverse, D("10000")), best)


def vwap_for_quote(
    book: L2OrderBook,
    side: Side,
    quote_size: Decimal,
    haircut: Decimal,
) -> VwapResult:
    """VWAP to trade ``quote_size`` of quote currency (e.g. 500 EUR of BTC)."""
    quote_size = D(quote_size)
    haircut = D(haircut)
    if not quote_size.is_finite():
        raise ValueError(f"quote_size must be finite, got {quote_size!r}")
    levels = _levels(book, side)
    depth = book_depth_quote(levels)

    remaining = quote_size
    filled_quote = ZERO
    base = ZERO
    used = 0
    best = levels[0].price
    worst = best
    # fuzz F23: accumulate with surplus precision (like mul/div) - at 28
    # digits a pathological level set could over-fill by 1 ulp, violating
    # "never fill more than requested"
    from decimal import localcontext
    with localcontext() as ctx:
        ctx.prec = MATH_PRECISION + 10
        for lvl in levels:
            if remaining <= ZERO:
                break
            take_quote = lvl.quote_volume if lvl.quote_volume <= remaining else remaining
            filled_quote += take_quote
            base += div(take_quote, lvl.price)
            remaining -= take_quote
            used += 1
            worst = lvl.price
    if quote_size > ZERO and filled_quote > quote_size:
        # defensive clamp: never over-fill. Keep base consistent (F41/F23
        # nit): the excess is valued at the worst level's price.
        excess = filled_quote - quote_size
        base -= div(excess, worst)
        filled_quote = quote_size

    # fuzz F5: a non-positive request "filled" instantly with vwap=None and
    # executable=True, violating the contract and crashing callers' asserts
    fully = remaining <= ZERO and D(quote_size) > ZERO
    vwap = div(filled_quote, base) if base > ZERO else None
    executable = fully and mul(depth, haircut) >= quote_size
    return VwapResult(
        side=side,
        requested=quote_size,
        filled_quote=filled_quote,
        base_qty=base,
        vwap=vwap,
        best_price=best,
        worst_price=worst,
        levels_used=used,
        fully_filled=fully,
        book_depth_quote=depth,
        executable=executable,
        slippage_bps=_slippage_bps(vwap, best, side) if vwap is not None else ZERO,
    )


def vwap_for_base(
    book: L2OrderBook,
    side: Side,
    base_qty: Decimal,
    haircut: Decimal,
) -> VwapResult:
    """VWAP to trade ``base_qty`` of base currency (e.g. sell 0.01 BTC)."""
    base_qty = D(base_qty)
    haircut = D(haircut)
    if not base_qty.is_finite():
        raise ValueError(f"base_qty must be finite, got {base_qty!r}")
    levels = _levels(book, side)
    depth = book_depth_quote(levels)

    remaining = base_qty
    filled_base = ZERO
    quote = ZERO
    used = 0
    best = levels[0].price
    worst = best
    from decimal import localcontext
    with localcontext() as ctx:
        ctx.prec = MATH_PRECISION + 10
        for lvl in levels:
            if remaining <= ZERO:
                break
            take_base = lvl.qty if lvl.qty <= remaining else remaining
            filled_base += take_base
            quote += mul(take_base, lvl.price)
            remaining -= take_base
            used += 1
            worst = lvl.price
    if base_qty > ZERO and filled_base > base_qty:
        excess = filled_base - base_qty
        quote -= mul(excess, worst)
        filled_base = base_qty

    # fuzz F5: same contract for base-sized requests
    fully = remaining <= ZERO and D(base_qty) > ZERO
    vwap = div(quote, filled_base) if filled_base > ZERO else None
    # depth rule applied to the quote notional we intend to move
    executable = fully and mul(depth, haircut) >= quote
    return VwapResult(
        side=side,
        requested=base_qty,
        filled_quote=quote,
        base_qty=filled_base,
        vwap=vwap,
        best_price=best,
        worst_price=worst,
        levels_used=used,
        fully_filled=fully,
        book_depth_quote=depth,
        executable=executable,
        slippage_bps=_slippage_bps(vwap, best, side) if vwap is not None else ZERO,
    )


class InsufficientDepth(Exception):
    """Book cannot fully fill the requested quantity (close must retry)."""


# re-export for callers that compute fee buffers on notional
__all__ = ["Side", "VwapResult", "book_depth_quote", "vwap_for_quote", "vwap_for_base", "bps_of", "InsufficientDepth"]
