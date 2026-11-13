"""Unit + property tests for the decimal core (Development Rule 1)."""
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.decimals import (
    D,
    FloatForbiddenError,
    bps_of,
    div,
    mul,
    q_price_cost,
    q_price_proceeds,
    q_qty,
    q_quote,
)


def test_D_rejects_float():
    with pytest.raises(FloatForbiddenError):
        D(0.1)


def test_D_rejects_bool():
    with pytest.raises(FloatForbiddenError):
        D(True)


def test_D_accepts_str_int_decimal():
    assert D("0.1") == Decimal("0.1")
    assert D(7) == Decimal("7")
    assert D(Decimal("3.14")) == Decimal("3.14")


def test_D_rejects_garbage_string():
    with pytest.raises(Exception):
        D("not-a-number")


def test_cost_rounds_up_proceeds_round_down():
    # 0.011 -> cost 0.011 -> up to 8dp step stays; use a case that actually rounds
    assert q_price_cost("100.000000001") == Decimal("100.00000001")
    assert q_price_proceeds("100.000000009") == Decimal("100.00000000")
    assert q_qty("0.123456789") == Decimal("0.12345678")
    assert q_quote("10.005") == Decimal("10.01")  # half-up


def test_bps_of_exact():
    assert bps_of("10000", "5") == Decimal("5")
    assert bps_of("250", "10") == Decimal("0.25")


def test_div_by_zero_raises():
    with pytest.raises(ZeroDivisionError):
        div("1", "0")


decimal_strings = st.decimals(
    min_value=Decimal("0.00000001"),
    max_value=Decimal("1000000000"),
    places=8,
    allow_nan=False,
    allow_infinity=False,
).map(str)


@given(amount=decimal_strings)
def test_bps_full_roundtrip(amount: str):
    # 10000 bps == 100% of the amount
    assert bps_of(amount, "10000") == D(amount)


@given(a=decimal_strings, b=decimal_strings)
def test_mul_commutative(a: str, b: str):
    assert mul(a, b) == mul(b, a)


@given(a=decimal_strings, b=decimal_strings)
def test_div_mul_inverse_within_precision(a: str, b: str):
    # (a / b) * b ~ a within the 28-digit working precision
    result = mul(div(a, b), b)
    diff = abs(result - D(a))
    assert diff < Decimal("0.0000000000000000001") * D(a) + Decimal("1E-27")
