"""Strict decimal mathematics for the engine.

Every price, quantity, fee and threshold is a ``decimal.Decimal`` built from a
string, int or another Decimal. ``float`` is rejected at the boundary by
:func:`D` and banned from ``app/engines`` by test_no_floats.py.
"""
from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal, localcontext
from typing import Union

DecimalInput = Union[str, int, Decimal]

#: digits of precision for intermediate arithmetic (not decimal places)
MATH_PRECISION = 28

#: canonical quantization steps
PRICE_STEP = Decimal("0.00000001")
QTY_STEP = Decimal("0.00000001")
QUOTE_STEP = Decimal("0.01")

ZERO = Decimal("0")
BPS_DENOMINATOR = Decimal("10000")
HUNDRED = Decimal("100")


class FloatForbiddenError(TypeError):
    """Raised when a float reaches financial math."""


def D(value: DecimalInput) -> Decimal:
    """Build a Decimal from str/int/Decimal. Rejects float and bool."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise FloatForbiddenError("bool is not a valid financial value")
    if isinstance(value, float):
        raise FloatForbiddenError(
            "float is forbidden in financial math; pass str or Decimal"
        )
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value.strip())
    raise TypeError(f"unsupported Decimal input type: {type(value)!r}")


def quantize_down(value: DecimalInput, step: DecimalInput) -> Decimal:
    """Round toward zero at ``step`` (conservative for proceeds/quantities)."""
    return D(value).quantize(D(step), rounding=ROUND_DOWN)


def quantize_up(value: DecimalInput, step: DecimalInput) -> Decimal:
    """Round away from zero at ``step`` (conservative for costs)."""
    return D(value).quantize(D(step), rounding=ROUND_UP)


def quantize_round(value: DecimalInput, step: DecimalInput) -> Decimal:
    """Round half-up at ``step`` (reporting)."""
    return D(value).quantize(D(step), rounding=ROUND_HALF_UP)


def q_price_cost(value: DecimalInput) -> Decimal:
    """Price quantization for amounts we pay (rounds up)."""
    return quantize_up(value, PRICE_STEP)


def q_price_proceeds(value: DecimalInput) -> Decimal:
    """Price quantization for amounts we receive (rounds down)."""
    return quantize_down(value, PRICE_STEP)


def q_qty(value: DecimalInput) -> Decimal:
    return quantize_down(value, QTY_STEP)


def q_quote(value: DecimalInput) -> Decimal:
    return quantize_round(value, QUOTE_STEP)


def mul(a: DecimalInput, b: DecimalInput) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = MATH_PRECISION
        return D(a) * D(b)


def div(a: DecimalInput, b: DecimalInput) -> Decimal:
    if D(b) == ZERO:
        raise ZeroDivisionError("division by zero in financial math")
    with localcontext() as ctx:
        ctx.prec = MATH_PRECISION
        return D(a) / D(b)


def bps_of(amount: DecimalInput, bps: DecimalInput) -> Decimal:
    """Exact basis points of ``amount`` (5 bps = 0.05%)."""
    with localcontext() as ctx:
        ctx.prec = MATH_PRECISION
        return D(amount) * D(bps) / BPS_DENOMINATOR


def pct_of(amount: DecimalInput, pct: DecimalInput) -> Decimal:
    """Exact percent of ``amount`` (15 = 15%)."""
    with localcontext() as ctx:
        ctx.prec = MATH_PRECISION
        return D(amount) * D(pct) / HUNDRED
