"""Dynamic sizing: reduced Kelly, hard caps, min cut."""
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
