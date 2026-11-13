"""fuzz F1: SQLite must persist Decimal EXACTLY (no float64 roundtrip)."""
import pytest
from sqlalchemy import select

from app.core.decimals import D
from app.database.models import Position
from app.database.repository import Repository


@pytest.mark.asyncio
async def test_decimal_roundtrip_is_exact(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/t.sqlite")
    await repo.init()
    async with repo._session() as s:
        s.add(Position(
            strategy="funding_carry", symbol="BTC/USD:USD",
            exchange_spot="kraken", exchange_perp="okx",
            qty_base=D("0.123456789012345678"),
            notional=D("99999.99999999"),
            spot_entry_vwap=D("99999.999999999999999999"),
            perp_entry_vwap=D("0.000000000000000001"),
            funding_collected=D("0.000000000000000001"),
            entry_funding_rate=D("0.0005"),
        ))
        await s.commit()
    async with repo._session() as s:
        pos = (await s.execute(select(Position))).scalars().one()
    assert pos.qty_base == D("0.123456789012345678")  # era ...5677 via REAL
    assert pos.spot_entry_vwap == D("99999.999999999999999999")  # era 100000
    assert pos.perp_entry_vwap == D("0.000000000000000001")
    assert pos.funding_collected == D("0.000000000000000001")
    await repo.close()
