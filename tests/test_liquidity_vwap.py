"""Unit + property tests for the L2 liquidity engine (RISK 2)."""
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.connectors.normalizer import L2OrderBook, OrderBookLevel
from app.core.decimals import D, div
from app.engines.liquidity import vwap_for_base, vwap_for_quote

HAIRCUT = D("0.50")


def make_book(bids, asks):
    return L2OrderBook(
        exchange="test",
        symbol="BTC/EUR",
        bids=tuple(OrderBookLevel(price=p, qty=q) for p, q in bids),
        asks=tuple(OrderBookLevel(price=p, qty=q) for p, q in asks),
        local_ts_ns=1_000_000_000,
    )


def test_buy_vwap_walks_levels_exactly():
    book = make_book(
        bids=[("99", "1")],
        asks=[("100", "1"), ("101", "2"), ("102", "5")],
    )
    # 150 EUR: 100 EUR @100 (1 BTC) + 50 EUR @101 (50/101 BTC)
    r = vwap_for_quote(book, "buy", D("150"), HAIRCUT)
    assert r.fully_filled
    assert r.levels_used == 2
    assert r.filled_quote == D("150")
    assert r.base_qty == D("1") + div(D("50"), D("101"))
    assert r.vwap == div(D("15150"), D("151"))
    assert r.best_price == D("100")
    assert r.worst_price == D("101")


def test_sell_vwap_for_base_qty():
    book = make_book(
        bids=[("99", "1"), ("98", "2")],
        asks=[("100", "1")],
    )
    # sell 1.5 BTC: 1 @99 + 0.5 @98 -> 148 EUR
    r = vwap_for_base(book, "sell", D("1.5"), HAIRCUT)
    assert r.fully_filled
    assert r.filled_quote == D("148")
    assert r.vwap == div(D("148"), D("1.5"))


def test_haircut_requires_two_x_depth():
    # depth exactly 2x requested -> executable
    book = make_book(bids=[("99", "1")], asks=[("100", "2")])
    r = vwap_for_quote(book, "buy", D("100"), HAIRCUT)
    assert r.executable

    # depth 1.99x -> rejected by haircut
    thin = make_book(bids=[("99", "1")], asks=[("100", "1.99")])
    r2 = vwap_for_quote(thin, "buy", D("100"), HAIRCUT)
    assert r2.fully_filled
    assert not r2.executable


def test_insufficient_depth_not_filled():
    book = make_book(bids=[("99", "1")], asks=[("100", "1"), ("101", "1")])
    r = vwap_for_quote(book, "buy", D("1000"), HAIRCUT)
    assert not r.fully_filled
    assert not r.executable
    assert r.levels_used == 2
    assert r.vwap is not None  # partial VWAP of what could fill


def test_slippage_bps_adverse_only():
    book = make_book(bids=[("99", "1")], asks=[("100", "1"), ("110", "1")])
    r = vwap_for_quote(book, "buy", D("150"), HAIRCUT)
    # vwap = 16500/160 = 103.125 -> (103.125-100)/100 = 312.5 bps
    assert r.vwap == D("103.125")
    assert r.slippage_bps == D("312.5")


prices = st.decimals(min_value=Decimal("1"), max_value=Decimal("200000"), places=4).map(str)
qtys = st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("500"), places=4).map(str)


@given(
    ask_prices=st.lists(prices, min_size=1, max_size=8),
    ask_qtys=st.lists(qtys, min_size=1, max_size=8),
    quote=st.decimals(min_value=Decimal("1"), max_value=Decimal("5000"), places=2).map(str),
)
def test_vwap_bounds_property(ask_prices, ask_qtys, quote):
    n = min(len(ask_prices), len(ask_qtys))
    asks = sorted(zip(ask_prices[:n], ask_qtys[:n]), key=lambda t: D(t[0]))
    # dedupe prices to keep the sorted-side validator happy
    seen, levels = set(), []
    for p, q in asks:
        if p not in seen:
            seen.add(p)
            levels.append((p, q))
    if not levels:
        return
    book = make_book(bids=[("1", "1")], asks=levels)
    r = vwap_for_quote(book, "buy", D(quote), HAIRCUT)
    if r.vwap is not None:
        eps = Decimal("0.00000000000000000001")  # 28-digit division dust
        assert r.vwap >= r.best_price - eps
        assert r.vwap <= r.worst_price + eps
        assert r.slippage_bps >= 0
        assert r.filled_quote <= D(quote)


def test_non_positive_size_is_not_executable():
    book = make_book(bids=[("99", "5")], asks=[("101", "5")])
    r = vwap_for_quote(book, "buy", D("0"), D("0.5"))
    assert not r.executable and not r.fully_filled and r.vwap is None
    r = vwap_for_quote(book, "buy", D("-10"), D("0.5"))
    assert not r.executable and r.vwap is None
    r = vwap_for_base(book, "sell", D("0"), D("0.5"))
    assert not r.executable and not r.fully_filled and r.vwap is None
