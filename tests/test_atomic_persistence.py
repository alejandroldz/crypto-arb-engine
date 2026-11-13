"""op.#2: entry/exit writes are atomic; recovery validates recorded legs."""
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.connectors.normalizer import L2OrderBook, OrderBookLevel
from app.core.decimals import D
from app.database.models import OrderRecord, Position, TaxAuditLog
from app.database.repository import Repository
from app.engines.arbitrage import FundingOpportunity
from app.engines.paper_trader import PaperTrader


def book(exchange, bid, ask) -> L2OrderBook:
    return L2OrderBook(
        exchange=exchange, symbol="BTC/USD",
        bids=(OrderBookLevel(price=str(bid), qty="500"),),
        asks=(OrderBookLevel(price=str(ask), qty="500"),),
        local_ts_ns=time.monotonic_ns(),
    )


async def test_open_position_atomic_rolls_back_on_leg_failure(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    fields = {
        "symbol": "BTC/USD:USD", "exchange_spot": "kraken",
        "exchange_perp": "krakenfutures", "qty_base": D("1"),
        "notional": D("100"), "spot_entry_vwap": D("100"),
        "perp_entry_vwap": D("100"),
    }
    good = {"leg": "spot", "action": "open", "side": "buy", "qty_base": D("1"),
            "vwap": D("100"), "fee": D("0.1"), "fee_currency": "USD"}
    bad = {"leg": "perp", "action": "open", "bogus_field": True}
    with pytest.raises(Exception):
        await repo.open_position_atomic(fields, [good, bad], [])
    # nothing survived: no position, no orphan order
    assert await repo.open_positions() == []
    async with repo._session() as s:
        assert (await s.execute(select(OrderRecord))).scalars().all() == []


async def test_close_atomic_rolls_back_on_leg_failure(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    pid = await repo.open_position(
        symbol="BTC/USD:USD", exchange_spot="kraken",
        exchange_perp="krakenfutures", qty_base=D("1"), notional=D("100"),
        spot_entry_vwap=D("100"), perp_entry_vwap=D("100"),
    )
    with pytest.raises(Exception):
        await repo.close_position_atomic(
            pid, D("1"), datetime.now(timezone.utc),
            [{"leg": "spot", "bogus": True}], [],
        )
    # position stays OPEN with zero close orders: the retry cannot duplicate
    assert len(await repo.open_positions()) == 1
    assert await repo.count_orders(pid, "close") == 0


async def test_entry_writes_position_two_orders_two_audits(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    t = PaperTrader(repo, latency_ms=0, execution_mode="taker")
    opp = FundingOpportunity(
        exchange="krakenfutures", symbol="BTC/USD:USD", notional=D("1000"),
        funding_rate=D("0.0005"), holding_intervals=90,
        breakeven_intervals=10, projected_net=D("30"),
    )
    spot, perp = book("kraken", 99.98, 100.00), book("krakenfutures", 100.02, 100.04)
    fill = await t.open_carry(opp, lambda: (spot, perp))
    assert await repo.count_orders(fill.position_id, "open") == 2
    async with repo._session() as s:
        audits = (await s.execute(select(TaxAuditLog))).scalars().all()
    assert len(audits) == 2
    # and the close lands as one atomic unit too
    await t.close_carry(fill, D("1"), lambda: (spot, perp))
    assert await repo.count_orders(fill.position_id, "close") == 2
    assert await repo.open_positions() == []
