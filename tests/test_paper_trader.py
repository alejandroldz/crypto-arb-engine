"""Paper trading with simulated latency (Development Rule 3)."""
import pytest

from app.connectors.normalizer import L2OrderBook, OrderBookLevel
from app.core.decimals import D, div
from app.database.repository import Repository
from app.engines.arbitrage import FundingOpportunity
from app.engines.paper_trader import PaperTrader


def book(exchange: str, bid: str, ask: str, depth: str = "5") -> L2OrderBook:
    return L2OrderBook(
        exchange=exchange,
        symbol="BTC/EUR",
        bids=(OrderBookLevel(price=bid, qty=depth),),
        asks=(OrderBookLevel(price=ask, qty=depth),),
        local_ts_ns=1,
    )


OPP = FundingOpportunity(
    exchange="bybit",
    symbol="BTC/EUR",
    notional=D("100"),
    funding_rate=D("0.0003"),
    holding_intervals=90,
    breakeven_intervals=20,
    projected_net=D("21"),
)


@pytest.fixture
async def repo(tmp_path):
    r = Repository(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await r.init()
    yield r
    await r.close()


def trader(repo: Repository, latency_ms: int = 1) -> PaperTrader:
    return PaperTrader(
        repo,
        latency_ms=latency_ms,
        taker_fee_bps=D("10"),
        slippage_buffer_bps=D("5"),
        liquidity_haircut=D("0.50"),
    )


async def test_open_and_close_carry(repo):
    t = trader(repo)
    spot = book("kraken", "49990", "50000")
    perp = book("bybit", "50010", "50020")

    fill = await t.open_carry(OPP, lambda: (spot, perp))
    assert fill is not None
    # delta-neutral: qty limited by the perp leg (100 quote at 50010 bid)
    assert fill.qty_base == div(D("100"), D("50010"))
    assert fill.spot_vwap == D("50000")

    # prices unchanged on close: pnl = funding - leg drift - fees
    pnl = await t.close_carry(fill, D("0.5"), lambda: (spot, perp))
    assert pnl > D("0")  # funding dominates the tiny drift + fees
    assert pnl < D("0.5")  # but cannot exceed the funding collected

    open_positions = await repo.open_positions()
    assert open_positions == []


async def test_edge_dying_during_latency_means_no_trade(repo):
    t = trader(repo)
    good = book("kraken", "49990", "50000")

    def thin_after_latency():
        # depth collapses below the 2x haircut while the order "travels"
        return book("kraken", "49990", "50000", depth="0.0005"), good

    fill = await t.open_carry(OPP, thin_after_latency)
    assert fill is None
    assert await repo.open_positions() == []


async def test_tax_csv_export(repo, tmp_path):
    t = trader(repo)
    spot = book("kraken", "49990", "50000")
    perp = book("bybit", "50010", "50020")
    fill = await t.open_carry(OPP, lambda: (spot, perp))
    assert fill is not None
    await t.close_carry(fill, D("0.5"), lambda: (spot, perp))

    out = tmp_path / "aeat.csv"
    rows = await repo.export_tax_csv(out)
    assert rows == 4  # 2 entry fills + 2 exit fills
    content = out.read_text()
    assert "paired_hedge_id" in content
    assert "kraken" in content and "bybit" in content
