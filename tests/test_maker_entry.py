"""Maker (limit) entry simulation: passive fill, taker fallback, cancel."""
import time

from app.connectors.normalizer import L2OrderBook, OrderBookLevel
from app.core.decimals import D
from app.database.repository import Repository
from app.engines.arbitrage import FundingOpportunity
from app.engines.paper_trader import PaperTrader


async def open_fees(repo, position_id):
    from sqlalchemy import select

    from app.database.models import OrderRecord

    async with repo._session() as s:
        orders = (await s.execute(select(OrderRecord))).scalars().all()
    return {o.leg: o.fee for o in orders if o.action == "open"}


def book(bid, ask) -> L2OrderBook:
    return L2OrderBook(
        exchange="kraken", symbol="BTC/USD",
        bids=(OrderBookLevel(price=str(bid), qty="50"),),
        asks=(OrderBookLevel(price=str(ask), qty="50"),),
        local_ts_ns=time.monotonic_ns(),
    )


def opp() -> FundingOpportunity:
    return FundingOpportunity(
        exchange="krakenfutures", symbol="BTC/USD:USD", notional=D("1000"),
        funding_rate=D("0.0005"), holding_intervals=90,
        breakeven_intervals=10, projected_net=D("30"),
    )


def trader(repo, **kw) -> PaperTrader:
    return PaperTrader(
        repo, latency_ms=0, spot_taker_fee_bps=D("80"), perp_taker_fee_bps=D("5"),
        spot_maker_fee_bps=D("40"), perp_maker_fee_bps=D("2"),
        execution_mode="maker", maker_timeout_seconds=2, **kw,
    )


async def test_maker_fill_when_price_trades_through(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    t = trader(repo)
    # limits: buy @ 99.98 bid, short @ 100.04 ask; next tick price crosses both
    frames = [
        (book(99.98, 100.00), book(100.02, 100.04)),
        (book(99.99, 99.98), book(100.04, 100.05)),  # ask<=limit, bid>=limit
    ]
    it = iter(frames)
    fill = await t.open_carry(opp(), lambda: frames[-1] if False else next(it, frames[-1]))
    assert fill is not None
    assert fill.spot_vwap == D("99.98")   # limit price, not ask
    assert fill.perp_vwap == D("100.04")
    fees = await open_fees(repo, fill.position_id)
    # maker fees: 1000$ * 40bps = 4.00 spot, 1000$ * 2bps = 0.20 perp
    assert abs(fees["spot"] - D("4")) < D("0.05")
    assert abs(fees["perp"] - D("0.2")) < D("0.01")


async def test_maker_timeout_taker_fallback(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    t = trader(repo, maker_fallback="taker")
    # price never crosses: buy limit 99.98, ask stays 100.00
    spot, perp = book(99.98, 100.00), book(100.02, 100.04)
    fill = await t.open_carry(opp(), lambda: (spot, perp))
    assert fill is not None
    assert fill.spot_vwap == D("100.00")  # crossed at ask (taker)
    fees = await open_fees(repo, fill.position_id)
    assert abs(fees["spot"] - D("8")) < D("0.05")    # taker 80bps
    assert abs(fees["perp"] - D("0.5")) < D("0.01")   # taker 5bps


async def test_maker_timeout_cancel(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    t = trader(repo, maker_fallback="cancel")
    spot, perp = book(99.98, 100.00), book(100.02, 100.04)
    fill = await t.open_carry(opp(), lambda: (spot, perp))
    assert fill is None
