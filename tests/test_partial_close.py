"""F05: a close with insufficient depth must NOT record a full close."""
import time
from decimal import Decimal

import pytest

from app.connectors.normalizer import L2OrderBook, OrderBookLevel
from app.core.decimals import D
from app.database.repository import Repository
from app.engines.arbitrage import FundingOpportunity
from app.engines.liquidity import InsufficientDepth
from app.engines.paper_trader import PaperTrader
from app.engines.perp_perp import PerpPerpTrader
from tests.test_perp_perp import _detect


def book(exchange, symbol, bid, ask, qty="500") -> L2OrderBook:
    return L2OrderBook(
        exchange=exchange, symbol=symbol,
        bids=(OrderBookLevel(price=str(bid), qty=qty),),
        asks=(OrderBookLevel(price=str(ask), qty=qty),),
        local_ts_ns=time.monotonic_ns(),
    )


async def test_carry_partial_close_aborts_and_keeps_position(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    t = PaperTrader(repo, latency_ms=0, spot_taker_fee_bps=D("80"),
                    perp_taker_fee_bps=D("5"), execution_mode="taker")
    deep = book("kraken", "BTC/USD", 99.9, 100.0)
    deep_perp = book("krakenfutures", "BTC/USD:USD", 100.1, 100.2)
    fill = await t.open_carry(
        FundingOpportunity(exchange="krakenfutures", symbol="BTC/USD:USD",
                           notional=D("1000"), funding_rate=D("0.001"),
                           holding_intervals=90, breakeven_intervals=10,
                           projected_net=D("30")),
        lambda: (deep, deep_perp),
    )
    assert fill is not None
    # perp side can only cover a tiny fraction of the position
    shallow_perp = book("krakenfutures", "BTC/USD:USD", 100.1, 100.2, qty="0.001")
    with pytest.raises(InsufficientDepth):
        await t.close_carry(fill, D("0"), lambda: (deep, shallow_perp))
    # position still open, no close orders recorded
    assert len(await repo.open_positions()) == 1
    from sqlalchemy import select

    from app.database.models import OrderRecord

    async with repo._session() as s:
        closes = [o for o in (await s.execute(select(OrderRecord))).scalars().all()
                  if o.action == "close"]
    assert closes == []
    # with depth restored, the close succeeds
    pnl = await t.close_carry(fill, D("0"), lambda: (deep, deep_perp))
    assert await repo.open_positions() == []


async def test_pp_partial_close_aborts_and_keeps_position(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    t = PerpPerpTrader(repo, latency_ms=0, maker_fee_bps=D("2"), taker_fee_bps=D("5"),
                       execution_mode="taker")
    lb = book("krakenfutures", "BTC/USD:USD", 99.9, 100.0)
    sb = book("okx", "BTC/USD:USD-310404", 100.1, 100.2)
    fill = await t.open_pair(_detect(), lambda: (lb, sb))
    assert fill is not None
    shallow_short = book("okx", "BTC/USD:USD-310404", 100.1, 100.2, qty="0.001")
    with pytest.raises(InsufficientDepth):
        await t.close_pair(fill, D("0"), lambda: (lb, shallow_short))
    assert len(await repo.open_positions()) == 1
    await t.close_pair(fill, D("0"), lambda: (lb, sb))
    assert await repo.open_positions() == []
