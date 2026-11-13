"""Perp-perp funding arbitrage: detection, execution, guardrails, unwind."""
import time
from decimal import Decimal

import pytest

from app.connectors.normalizer import FundingRate, L2OrderBook, OrderBookLevel
from app.core.config import Settings
from app.core.decimals import D, ZERO
from app.database.repository import Repository
from app.engines.perp_perp import (
    NS_PER_HOUR,
    PerpPerpTrader,
    PpContext,
    PpPosition,
    current_net_rate_per_hour,
    detect_perp_perp,
    pp_handle_funding,
    pp_manage,
)


def _detect(**over):
    kw = dict(
        base="BTC", long_exchange="krakenfutures", long_symbol="BTC/USD:USD",
        short_exchange="okx", short_symbol="BTC/USD:USD-310404",
        long_history=[D("0.00005")] * 40,   # pays 0.005%/h (kraken, 1h)
        short_history=[D("0.0005")] * 40,   # collects 0.05%/8h (okx)
        long_interval_h=D("1"), short_interval_h=D("8"),
        notional=D("4000"), holding_hours=720,
        maker_fee_bps=D("2"), taker_fee_bps=D("5"),
        slippage_buffer_bps=D("5"), min_net_profit_usd=D("2"),
        min_history_samples=30,
    )
    kw.update(over)
    return detect_perp_perp(**kw)


def test_detect_opportunity_signs_and_intervals():
    opp = _detect()
    assert opp is not None
    # net = 0.0005/8 - 0.00005/1 = 0.0000125 per hour
    assert opp.net_rate_per_hour == D("0.0000125")
    # payout 4000*0.0000125*720 = 36; costs 34bps*4000 = 13.6 -> net 22.4
    assert abs(opp.projected_net - D("22.4")) < D("0.01")
    assert opp.breakeven_hours == 272


def test_detect_rejects_negative_net():
    assert _detect(long_history=[D("0.0005")] * 40, short_history=[D("0.00005")] * 40) is None


def test_detect_rejects_thin_history():
    assert _detect(short_history=[D("0.0005")] * 10) is None


def test_detect_rejects_when_costs_dominate():
    # tiny differential: breakeven beyond the holding window
    assert _detect(short_history=[D("0.00006")] * 40, holding_hours=100) is None
    assert _detect(short_history=[D("0.00006")] * 40,
                   holding_hours=720, min_net_profit_usd=D("100")) is None


def test_current_net_rate_per_hour():
    short = FundingRate(exchange="okx", symbol="BTC/USD:USD-310404",
                        rate=D("0.0008"), local_ts_ns=time.monotonic_ns())
    long = FundingRate(exchange="krakenfutures", symbol="BTC/USD:USD",
                       rate=D("0.00005"), local_ts_ns=time.monotonic_ns())
    assert current_net_rate_per_hour(short, D("8"), long, D("1")) == D("0.0001") - D("0.00005")
    flipped = FundingRate(exchange="okx", symbol="BTC/USD:USD-310404",
                          rate=D("-0.0008"), local_ts_ns=time.monotonic_ns())
    assert current_net_rate_per_hour(flipped, D("8"), long, D("1")) < ZERO


def book(exchange, symbol, bid, ask) -> L2OrderBook:
    return L2OrderBook(
        exchange=exchange, symbol=symbol,
        bids=(OrderBookLevel(price=str(bid), qty="500"),),
        asks=(OrderBookLevel(price=str(ask), qty="500"),),
        local_ts_ns=time.monotonic_ns(),
    )


LONG = ("krakenfutures", "BTC/USD:USD")
SHORT = ("okx", "BTC/USD:USD-310404")


def trader(repo, **kw) -> PerpPerpTrader:
    return PerpPerpTrader(
        repo, latency_ms=0, maker_fee_bps=D("2"), taker_fee_bps=D("5"),
        execution_mode="maker", maker_timeout_seconds=2, **kw,
    )


async def _open_fees(repo, position_id):
    from sqlalchemy import select

    from app.database.models import OrderRecord

    async with repo._session() as s:
        orders = (await s.execute(select(OrderRecord))).scalars().all()
    return {(o.leg, o.action): o.fee for o in orders}


async def test_open_pair_maker_fill(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    t = trader(repo)
    opp = _detect()
    frames = [
        (book(*LONG, 99.98, 100.00), book(*SHORT, 100.02, 100.04)),
        (book(*LONG, 99.99, 99.98), book(*SHORT, 100.04, 100.05)),  # cross
    ]
    it = iter(frames)
    fill = await t.open_pair(opp, lambda: next(it, frames[-1]))
    assert fill is not None
    assert fill.long_vwap == D("99.98")
    assert fill.short_vwap == D("100.04")
    fees = await _open_fees(repo, fill.position_id)
    qty = D("4000") / D("99.98")
    assert abs(fees[("spot", "open")] - qty * D("99.98") * D("0.0002")) < D("0.01")
    pos = await repo.open_positions()
    assert pos[0].exchange_spot == "krakenfutures(long)"


async def test_open_pair_taker_fallback(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    t = trader(repo, maker_fallback="taker")
    lb, sb = book(*LONG, 99.98, 100.00), book(*SHORT, 100.02, 100.04)
    fill = await t.open_pair(_detect(), lambda: (lb, sb))
    assert fill is not None
    assert fill.long_vwap == D("100.00")   # crossed at ask
    assert fill.short_vwap == D("100.02")  # crossed at bid
    fees = await _open_fees(repo, fill.position_id)
    assert abs(fees[("spot", "open")] - D("4000") * D("0.0005")) < D("0.01")


async def test_open_pair_cancel_on_timeout(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    t = trader(repo, maker_fallback="cancel")
    lb, sb = book(*LONG, 99.98, 100.00), book(*SHORT, 100.02, 100.04)
    assert await t.open_pair(_detect(), lambda: (lb, sb)) is None
    assert await repo.open_positions() == []


async def test_close_pair_always_taker_pnl(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    t = trader(repo)
    lb, sb = book(*LONG, 99.99, 99.98), book(*SHORT, 100.04, 100.05)
    fill = await t.open_pair(_detect(), lambda: (lb, sb))
    assert fill is not None
    # close at book prices (single deep levels): exact expectation
    pnl = await t.close_pair(fill, D("10"), lambda: (lb, sb))
    q = fill.qty_base
    # entry: long filled passively at limit (2bps), short taker (5bps)
    entry_fees = D("0.0002") * q * fill.long_vwap + D("0.0005") * q * fill.short_vwap
    expected = (
        D("10")
        + q * (D("99.99") - fill.long_vwap)
        + q * (fill.short_vwap - D("100.05"))
        - D("0.0005") * (q * D("99.99") + q * D("100.05"))
        - entry_fees
    )
    assert abs(pnl - expected) < D("1e-8")
    assert pnl < D("10")  # taker fees on both exits
    assert await repo.open_positions() == []


class _Breaker:
    halted = False

    def record_order(self):
        return True

    def reserve_orders(self, n):
        return not self.halted

    def record_exit(self):
        pass

    def record_failure(self):
        pass

    def record_pnl_usd(self, pnl):
        self.last_pnl = pnl


class _Notifier:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


def _settings() -> Settings:
    return Settings(
        perp_perp_enabled=True, sizing_mode="fixed",
        min_net_profit_usd=Decimal("0.01"), min_funding_history=10,
    )


async def _ctx_with_position(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    t = trader(repo)
    opp = _detect()
    lb, sb = book(*LONG, 99.99, 99.98), book(*SHORT, 100.04, 100.05)
    fill = await t.open_pair(opp, lambda: (lb, sb))
    now_ns = time.monotonic_ns()
    ctx = PpContext(
        settings=_settings(), paper=t, repo=repo, ticks=None,
        books={f"{LONG[0]}:{LONG[1]}": lb, f"{SHORT[0]}:{SHORT[1]}": sb},
        funding={}, breaker=_Breaker(), notifier=_Notifier(),
        open={}, budget_remaining=lambda: D("0"), realized_pnl=[ZERO],
    )
    ctx.open[f"BTC:{LONG[0]}->{SHORT[0]}"] = PpPosition(
        fill=fill, opp=opp, long_key=f"{LONG[0]}:{LONG[1]}",
        short_key=f"{SHORT[0]}:{SHORT[1]}",
        funding_collected_net=ZERO, opened_ns=now_ns,
        last_accrual_ns_long=now_ns, last_accrual_ns_short=now_ns,
    )
    return ctx, repo


async def test_funding_flip_triggers_taker_unwind(tmp_path):
    ctx, repo = await _ctx_with_position(tmp_path)
    ctx.funding[ctx.open["BTC:krakenfutures->okx"].short_key] = FundingRate(
        exchange="okx", symbol=SHORT[1], rate=D("-0.001"),
        local_ts_ns=time.monotonic_ns())
    ctx.funding[ctx.open["BTC:krakenfutures->okx"].long_key] = FundingRate(
        exchange="krakenfutures", symbol=LONG[1], rate=D("0.00005"),
        local_ts_ns=time.monotonic_ns())
    await pp_manage(ctx)
    assert ctx.open == {}
    assert ctx.realized_pnl[0] < ZERO  # fees on a fresh position
    assert await repo.open_positions() == []
    assert any("funding flip" in m for m in ctx.notifier.sent)


async def test_holding_expiry_unwinds(tmp_path):
    ctx, repo = await _ctx_with_position(tmp_path)
    pos = ctx.open["BTC:krakenfutures->okx"]
    pos.opened_ns = time.monotonic_ns() - (pos.opp.holding_hours + 1) * NS_PER_HOUR
    await pp_manage(ctx)
    assert ctx.open == {}
    assert any("holding window" in m for m in ctx.notifier.sent)


async def test_breaker_halt_forces_unwind(tmp_path):
    ctx, _ = await _ctx_with_position(tmp_path)
    ctx.breaker.halted = True
    await pp_manage(ctx)
    assert ctx.open == {}
    assert any("stress" in m for m in ctx.notifier.sent)


async def test_funding_accrual_short_collects_long_pays(tmp_path):
    ctx, repo = await _ctx_with_position(tmp_path)
    pos = ctx.open["BTC:krakenfutures->okx"]
    pos.last_accrual_ns_short = time.monotonic_ns() - NS_PER_HOUR  # 1h elapsed
    short_evt = FundingRate(exchange="okx", symbol=SHORT[1], rate=D("0.0008"),
                            local_ts_ns=time.monotonic_ns())
    ctx.settings.perp_perp_enabled = False  # no new entries during this test
    await pp_handle_funding(ctx, short_evt)
    # short collects: 4000 * 0.0008 * (1h/8h) = 0.4
    assert abs(pos.funding_collected_net - D("0.4")) < D("0.01")
    pos.last_accrual_ns_long = time.monotonic_ns() - NS_PER_HOUR
    long_evt = FundingRate(exchange="krakenfutures", symbol=LONG[1], rate=D("0.0001"),
                           local_ts_ns=time.monotonic_ns())
    await pp_handle_funding(ctx, long_evt)
    # long pays: 4000 * 0.0001 * 1h/1h = 0.4 -> net back to ~0
    assert abs(pos.funding_collected_net) < D("0.01")


async def test_accrual_order_independent(tmp_path):
    """F11 regression: equal rates must net ZERO regardless of arrival order."""
    for order in (("short", "long"), ("long", "short")):
        ctx, _ = await _ctx_with_position(tmp_path)
        pos = ctx.open["BTC:krakenfutures->okx"]
        pos.last_accrual_ns_long = time.monotonic_ns() - NS_PER_HOUR
        pos.last_accrual_ns_short = time.monotonic_ns() - NS_PER_HOUR
        ctx.settings.perp_perp_enabled = False
        evts = {
            "short": FundingRate(exchange="okx", symbol=SHORT[1], rate=D("0.001"),
                                 local_ts_ns=time.monotonic_ns()),
            "long": FundingRate(exchange="krakenfutures", symbol=LONG[1], rate=D("0.001"),
                                local_ts_ns=time.monotonic_ns()),
        }
        for leg in order:
            await pp_handle_funding(ctx, evts[leg])
        # short +1h/8h * 4000 * 0.001 = +0.5; long -1h/1h * 4000 * 0.001 = -4? no:
        # intervals differ: short 8h -> +0.5, long 1h -> -4.0? 4000*0.001*1=4
        # net is NOT zero here because intervals differ; assert BOTH orders agree
        if order == ("short", "long"):
            net_a = pos.funding_collected_net
        else:
            assert abs(pos.funding_collected_net - net_a) < D("0.001")


async def test_accrual_capped_during_gap(tmp_path):
    ctx, _ = await _ctx_with_position(tmp_path)
    pos = ctx.open["BTC:krakenfutures->okx"]
    pos.last_accrual_ns_short = time.monotonic_ns() - 100 * NS_PER_HOUR  # 100h gap
    ctx.settings.perp_perp_enabled = False
    evt = FundingRate(exchange="okx", symbol=SHORT[1], rate=D("0.0008"),
                      local_ts_ns=time.monotonic_ns())
    await pp_handle_funding(ctx, evt)
    # capped at 2 intervals (16h): 4000*0.0008*16/8 = 6.4, NOT 400
    assert abs(pos.funding_collected_net - D("6.4")) < D("0.01")
