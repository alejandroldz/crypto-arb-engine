"""Dynamic sizing: reduced Kelly, hard caps, min cut."""
import pytest
from decimal import Decimal

from app.core.decimals import D, ZERO
from app.engines.sizing import dynamic_notional, rate_std


def test_rate_std_empty_and_single():
    assert rate_std([]) == ZERO
    assert rate_std([D("0.001")]) == ZERO


def test_rate_std_known_value():
    # values 0.001, 0.002, 0.003 -> mean 0.002, var = (1e-6+0+1e-6)/3
    std = rate_std([D("0.001"), D("0.002"), D("0.003")])
    assert abs(std - D("0.000816496580927726")) < D("1e-12")


def _kw(**over):
    base = dict(
        edge_per_interval=D("0.0005"),
        history=[D("0.0004"), D("0.0005"), D("0.0006")],
        budget_remaining=D("10000"),
        kelly_fraction=D("0.25"),
        full_score=D("0.5"),
        min_notional=D("250"),
        max_notional=D("5000"),
    )
    base.update(over)
    return base


def test_nonpositive_edge_or_budget_skips():
    assert dynamic_notional(**_kw(edge_per_interval=D("0"))) == ZERO
    assert dynamic_notional(**_kw(edge_per_interval=D("-0.001"))) == ZERO
    assert dynamic_notional(**_kw(budget_remaining=D("0"))) == ZERO
    assert dynamic_notional(**_kw(budget_remaining=D("-5"))) == ZERO


def test_zero_variance_gives_kelly_fraction_of_cap():
    # zero observed variance -> top score -> fraction = 1 * kelly_fraction
    n = dynamic_notional(**_kw(history=[D("0.0005")] * 40))
    assert n == D("5000") * D("0.25")


def test_capped_by_budget():
    n = dynamic_notional(**_kw(budget_remaining=D("1000"), history=[D("0.0005")] * 40))
    assert n == D("1000") * D("0.25") == D("250")


def test_below_min_skips():
    assert dynamic_notional(**_kw(budget_remaining=D("800"), history=[D("0.0005")] * 40)) == ZERO


def test_low_score_scales_down():
    # noisy history -> score < full_score -> fraction < 1
    noisy = [D("0.001"), D("-0.002"), D("0.003"), D("-0.001"), D("0.002")] * 10
    n = dynamic_notional(**_kw(history=noisy))
    assert ZERO < n < D("5000") * D("0.25")


def test_d4_perp_perp_kelly_normalizes_history_per_hour():
    """dif D4: edge per hour vs history per interval (8h) skewed Kelly 8x."""
    from app.engines.sizing import dynamic_notional
    # edge per hour 0.001; history per interval 0.008 (8h) == same 0.001/h
    hist_interval = [D("0.008")] * 10
    hist_hourly = [D("0.001")] * 10
    kw = dict(
        edge_per_interval=D("0.001"), budget_remaining=D("10000"),
        kelly_fraction=D("0.5"), full_score=D("10"),
        min_notional=D("10"), max_notional=D("5000"),
    )
    # zero-variance history saturates the score either way; use variance
    hist_interval = [D("0.007"), D("0.009")] * 5
    hist_hourly = [D("0.000875"), D("0.001125")] * 5
    a = dynamic_notional(history=hist_interval, **kw)
    b = dynamic_notional(history=hist_hourly, **kw)
    # the per-interval history understates the score by ~8x -> smaller size
    assert a < b


def test_f53_kelly_fraction_cannot_exceed_cap():
    """kelly_fraction=100 used to give 100x the documented per-carry cap."""
    from app.engines.sizing import dynamic_notional

    n = dynamic_notional(
        edge_per_interval=Decimal("0.01"),
        history=[Decimal("0.01"), Decimal("0.011"), Decimal("0.009")],
        budget_remaining=Decimal("100000"),
        kelly_fraction=Decimal("100"),  # typo-class value
        full_score=Decimal("0.5"),
        min_notional=Decimal("250"),
        max_notional=Decimal("5000"),
    )
    assert n <= Decimal("5000")


def test_f53_config_rejects_hostile_kelly_env():
    import pydantic
    from app.core.config import Settings

    with pytest.raises(pydantic.ValidationError):
        Settings(kelly_fraction=Decimal("25"))
    with pytest.raises(pydantic.ValidationError):
        Settings(kelly_fraction=Decimal("-0.1"))
    with pytest.raises(pydantic.ValidationError):
        Settings(sizing_full_score=Decimal("0"))
    Settings(kelly_fraction=Decimal("0.25"))  # sane value still works
