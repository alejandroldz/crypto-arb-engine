"""Async repository: the only path from engines to SQLite."""
from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.decimals import D
from app.database.models import Base, OrderRecord, Position, TaxAuditLog


class Repository:
    def __init__(self, sqlite_url: str):
        self._engine = create_async_engine(sqlite_url)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    async def open_position(self, **fields) -> int:
        async with self._session() as s:
            pos = Position(
                qty_base=D(fields["qty_base"]),
                notional=D(fields["notional"]),
                spot_entry_vwap=D(fields["spot_entry_vwap"]),
                perp_entry_vwap=D(fields["perp_entry_vwap"]),
                symbol=fields["symbol"],
                exchange_spot=fields["exchange_spot"],
                exchange_perp=fields["exchange_perp"],
            )
            s.add(pos)
            await s.commit()
            return pos.id

    async def close_position(
        self, position_id: int, realized_pnl_eur: Decimal, closed_at: datetime
    ) -> None:
        async with self._session() as s:
            pos = await s.get(Position, position_id)
            if pos is None:
                raise KeyError(f"position {position_id} not found")
            pos.status = "closed"
            pos.closed_at = closed_at
            pos.realized_pnl_eur = D(realized_pnl_eur)
            await s.commit()

    async def add_funding(self, position_id: int, amount: Decimal) -> None:
        async with self._session() as s:
            pos = await s.get(Position, position_id)
            if pos is None:
                raise KeyError(f"position {position_id} not found")
            pos.funding_collected = D(pos.funding_collected) + D(amount)
            await s.commit()

    async def record_order(self, **fields) -> int:
        async with self._session() as s:
            rec = OrderRecord(
                position_id=fields["position_id"],
                leg=fields["leg"],
                action=fields["action"],
                side=fields["side"],
                qty_base=D(fields["qty_base"]),
                vwap=D(fields["vwap"]),
                fee=D(fields["fee"]),
                fee_currency=fields["fee_currency"],
                kind=fields.get("kind", "paper"),
            )
            s.add(rec)
            await s.commit()
            return rec.id

    async def audit_fill(
        self,
        *,
        strategy_id: str,
        paired_hedge_id: int,
        exchange: str,
        symbol: str,
        side: str,
        amount: Decimal,
        price_eur_equivalent: Decimal,
        fee_amount: Decimal,
        fee_currency: str,
        timestamp_utc: datetime,
        net_pnl_eur: Decimal | None,
    ) -> None:
        """Append-only insert into the immutable tax log (RISK 7)."""
        async with self._session() as s:
            s.add(
                TaxAuditLog(
                    strategy_id=strategy_id,
                    paired_hedge_id=paired_hedge_id,
                    exchange=exchange,
                    symbol=symbol,
                    side=side,
                    amount=D(amount),
                    price_eur_equivalent=D(price_eur_equivalent),
                    fee_amount=D(fee_amount),
                    fee_currency=fee_currency,
                    timestamp_utc=timestamp_utc,
                    net_pnl_eur=D(net_pnl_eur) if net_pnl_eur is not None else None,
                )
            )
            await s.commit()

    async def open_positions(self) -> list[Position]:
        async with self._session() as s:
            result = await s.execute(select(Position).where(Position.status == "open"))
            return list(result.scalars())

    async def export_tax_csv(self, path: str | Path) -> int:
        """AEAT-ready CSV export of the full audit log. Returns row count."""
        async with self._session() as s:
            result = await s.execute(select(TaxAuditLog).order_by(TaxAuditLog.trade_id))
            rows = list(result.scalars())
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "trade_id",
                    "strategy_id",
                    "paired_hedge_id",
                    "exchange",
                    "symbol",
                    "side",
                    "amount",
                    "price_eur_equivalent",
                    "fee_amount",
                    "fee_currency",
                    "timestamp_utc",
                    "net_pnl_eur",
                ]
            )
            for r in rows:
                writer.writerow(
                    [
                        r.trade_id,
                        r.strategy_id,
                        r.paired_hedge_id,
                        r.exchange,
                        r.symbol,
                        r.side,
                        r.amount,
                        r.price_eur_equivalent,
                        r.fee_amount,
                        r.fee_currency,
                        r.timestamp_utc.isoformat(),
                        r.net_pnl_eur if r.net_pnl_eur is not None else "",
                    ]
                )
        return len(rows)
