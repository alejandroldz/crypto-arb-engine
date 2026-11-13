"""Audit regression: execution fees resolve per ACTUAL venue at fill time
from the live settings reference (oracle updates land on the next fill), and
the oracle fallback covers venues without a fetcher."""
import json
import time

import pytest
from sqlalchemy import select

from app.connectors.normalizer import L2OrderBook, OrderBookLevel
from app.core.config import Settings
from app.core.decimals import D, bps_of, mul
from app.database.models import OrderRecord
from app.database.repository import Repository
from app.engines.arbitrage import FundingOpportunity
from app.engines.fee_oracle import FeeOracle
from app.engines.paper_trader import PaperTrader
from app.engines.perp_perp import PerpPerpOpportunity, PerpPerpTrader


def book(exchange, bid, ask) -> L2OrderBook:
    return L2OrderBook(
        exchange=exchange, symbol="BTC/USD",
        bids=(OrderBookLevel(price=str(bid), qty="500"),),
        asks=(OrderBookLevel(price=str(ask), qty="500"),),
        local_ts_ns=time.monotonic_ns(),
    )


async def fees_by_leg(repo):
    async with repo._session() as s:
        orders = (await s.execute(select(OrderRecord))).scalars().all()
    return {(o.leg, o.action): D(str(o.fee)) for o in orders}


def carry_opp() -> FundingOpportunity:
    return FundingOpportunity(
        exchange="krakenfutures", symbol="BTC/USD:USD", notional=D("1000"),
        funding_rate=D("0.0005"), holding_intervals=90,
        breakeven_intervals=10, projected_net=D("30"),
    )


async def test_carry_fees_per_venue_and_live_update(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    s = Settings()
    s.detected_fees["kraken"] = {"maker": D("20"), "taker": D("30")}
    s.detected_fees["krakenfutures"] = {"maker": D("1"), "taker": D("4")}
    t = PaperTrader(
        repo, latency_ms=0, spot_taker_fee_bps=D("40"),
        perp_taker_fee_bps=D("5"), execution_mode="taker", settings=s,
    )
    spot, perp = book("kraken", 99.98, 100.00), book("krakenfutures", 100.02, 100.04)
    fill = await t.open_carry(carry_opp(), lambda: (spot, perp))
    assert fill is not None
    fees = await fees_by_leg(repo)
    # per-venue detected rates, NOT the constructor defaults (40/5)
    assert abs(fees[("spot", "open")] - bps_of(mul(fill.qty_base, fill.spot_vwap), D("30"))) < D("0.0000001")
    assert abs(fees[("perp", "open")] - bps_of(mul(fill.qty_base, fill.perp_vwap), D("4"))) < D("0.0000001")

    # oracle update AFTER construction must hit the next fill (the close)
    s.detected_fees["kraken"] = {"maker": D("20"), "taker": D("50")}
    s.detected_fees["krakenfutures"] = {"maker": D("1"), "taker": D("7")}
    await t.close_carry(fill, D("1"), lambda: (spot, perp))
    fees = await fees_by_leg(repo)
    # exit legs price at exit vwap: spot sells at bid 99.98
    assert abs(fees[("spot", "close")] - bps_of(mul(fill.qty_base, D("99.98")), D("50"))) < D("0.0000001")
    # perp covers at ask 100.04
    assert abs(fees[("perp", "close")] - bps_of(mul(fill.qty_base, D("100.04")), D("7"))) < D("0.0000001")


async def test_pp_fees_per_venue_at_fill(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    s = Settings()
    s.detected_fees["krakenfutures"] = {"maker": D("1"), "taker": D("4")}
    s.detected_fees["okx"] = {"maker": D("2"), "taker": D("6")}
    t = PerpPerpTrader(
        repo, latency_ms=0, maker_fee_bps=D("2"), taker_fee_bps=D("5"),
        execution_mode="taker", settings=s,
    )
    opp = PerpPerpOpportunity(
        base="BTC", long_exchange="krakenfutures", long_symbol="BTC/USD:USD",
        short_exchange="okx", short_symbol="BTC/USD:USD-310404",
        notional=D("1000"), net_rate_per_hour=D("0.0001"), holding_hours=72,
        breakeven_hours=10, projected_net=D("5"),
    )
    lb, sb = book("krakenfutures", 99.98, 100.00), book("okx", 100.02, 100.04)
    fill = await t.open_pair(opp, lambda: (lb, sb))
    assert fill is not None
    fees = await fees_by_leg(repo)
    assert abs(fees[("spot", "open")] - bps_of(mul(fill.qty_base, fill.long_vwap), D("4"))) < D("0.0000001")
    assert abs(fees[("perp", "open")] - bps_of(mul(fill.qty_base, fill.short_vwap), D("6"))) < D("0.0000001")


async def test_oracle_config_fallback_without_fetcher(tmp_path):
    s = Settings()
    oracle = FeeOracle(s, str(tmp_path / "fees.json"))
    out = await oracle.refresh({})
    # every active venue gets a fallback quote even with no fetchers at all
    assert set(out) >= {"kraken", "krakenfutures", "okx"}
    assert all(q.source == "config" for q in out.values())


async def test_oracle_cache_fallback_without_fetcher(tmp_path):
    path = tmp_path / "fees.json"
    path.write_text(json.dumps({
        "venues": {"okx": {"maker_bps": "2", "taker_bps": "5"}},
        "review_required": [],
    }))
    s = Settings()
    oracle = FeeOracle(s, str(path))
    out = await oracle.refresh({})
    assert out["okx"].source == "cache"
    assert out["okx"].taker_bps == D("5")
