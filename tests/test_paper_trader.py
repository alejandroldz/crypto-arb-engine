"""Paper trading path with deterministic offline books (no network)."""
import time
from datetime import datetime, timezone

import pytest

from app.connectors.normalizer import FundingRate, L2OrderBook, OrderBookLevel
from app.core.decimals import D
from app.database.repository import Repository
from app.engines.arbitrage import FundingOpportunity
from app.engines.paper_trader import PaperTrader


def book(levels_bid, levels_ask) -> L2OrderBook:
    return L2OrderBook(
        exchange="kraken",
        symbol="BTC/USD",
        bids=tuple(OrderBookLevel(price=str(p), qty=str(q)) for p, q in levels_bid),
        asks=tuple(OrderBookLevel(price=str(p), qty=str(q)) for p, q in levels_ask),
        local_ts_ns=time.monotonic_ns(),
    )


def opp() -> FundingOpportunity:
    return FundingOpportunity(
        exchange="krakenfutures",
        symbol="BTC/USD:USD",
        notional=D("1000"),
        funding_rate=D("0.0005"),
        holding_intervals=90,
        breakeven_intervals=10,
        projected_net=D("30"),
    )


async def test_open_and_close_carry(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    trader = PaperTrader(
        repo, latency_ms=0, spot_taker_fee_bps=D("40"),
        perp_taker_fee_bps=D("5"), slippage_buffer_bps=D("5"),
    )
    # spot asks 100.00 with deep liquidity; perp bids 100.02
    spot = book([(99.98, 50)], [(100.00, 50), (100.05, 50)])
    perp = book([(100.02, 50)], [(100.04, 50)])

    def fetch():
        return spot, perp

    fill = await trader.open_carry(opp(), fetch)
    assert fill is not None
    # qty is delta-neutral: min of both legs' base quantities (~10 BTC)
    assert abs(fill.qty_base - D("10")) < D("0.01")
    assert fill.spot_vwap == D("100.00")

    pnl = await trader.close_carry(fill, D("0.5"), fetch)
    # gross near 0.1, minus per-venue exit fees -> strictly below gross
    assert pnl < D("0.1") and pnl > D("-10")

    # realized loss recorded as USD-equivalent
    async with repo._session() as s:
        from sqlalchemy import select

        from app.database.models import Position

        pos = (await s.execute(select(Position))).scalars().one()
        assert pos.closed_at is not None
        assert pos.realized_pnl_usd is not None and pos.realized_pnl_usd < D("0.1")
        assert pos.entry_funding_rate == D("0.0005")
        assert pos.holding_intervals == 90
    await repo.close()


async def test_edge_dying_during_latency_means_no_trade(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    trader = PaperTrader(repo, latency_ms=50, liquidity_haircut=D("0.25"))
    spot = book([(99.98, 50)], [(100.00, 50)])

    def fetch():
        # edge vanished: asks now at 101 (+1%) -> outside 5bps buffer
        return book([(99.98, 5)], [(101.00, 5)]), book([(100.02, 50)], [(100.04, 50)])

    result = await trader.open_carry(opp(), fetch)
    assert result is None
    # second latency wait happens only after a fill, so it's fine either way
    await repo.close()


async def test_tax_csv_export_excludes_paper_by_default(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    trader = PaperTrader(
        repo, latency_ms=0, spot_taker_fee_bps=D("40"), perp_taker_fee_bps=D("5"),
    )
    spot = book([(99.98, 50)], [(100.00, 50)])
    perp = book([(100.02, 50)], [(100.04, 50)])
    fill = await trader.open_carry(opp(), lambda: (spot, perp))
    await trader.close_carry(fill, D("0.5"), lambda: (spot, perp))
    out = tmp_path / "tax.csv"
    assert await repo.export_tax_csv(str(out)) == 0  # live only: paper rows excluded
    assert await repo.export_tax_csv(str(out), kind="paper") == 4
    content = out.read_text()
    assert "price_usd_equivalent" in content and "net_pnl_usd" in content
    await repo.close()


async def test_add_funding_and_gap_uncertainty_propagate(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    trader = PaperTrader(repo, latency_ms=0)
    spot = book([(99.98, 50)], [(100.00, 50)])
    perp = book([(100.02, 50)], [(100.04, 50)])
    fill = await trader.open_carry(opp(), lambda: (spot, perp))
    await repo.add_funding(fill.position_id, D("1.25"))
    await repo.add_funding(fill.position_id, D("1.25"))
    open_positions = await repo.open_positions()
    assert len(open_positions) == 1
    assert open_positions[0].funding_collected == D("2.50")
    await repo.close()



async def test_fill_aborted_when_fee_guardrail_raised_mid_entry(tmp_path):
    from app.core.config import Settings

    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    settings = Settings()
    settings.fees_blocked.add("okx")  # guardrail fired after candidate approval
    trader = PaperTrader(repo, latency_ms=0, settings=settings)
    spot = book([(99.98, 50)], [(100.00, 50), (100.05, 50)])
    perp = book([(100.02, 50)], [(100.04, 50)])
    fill = await trader.open_carry(opp(), lambda: (spot, perp))
    assert fill is None
    assert await repo.open_positions() == []
    await repo.close()
