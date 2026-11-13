"""Async repository: the only path from engines to SQLite."""
from __future__ import annotations

import csv
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.decimals import D
from app.database.models import Base, OrderRecord, Position, TaxAuditLog

logger = logging.getLogger(__name__)


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
                spot_symbol=fields.get("spot_symbol", ""),
                holding_intervals=fields.get("holding_intervals", 90),
                entry_funding_rate=D(fields.get("entry_funding_rate", "0")),
            )
            s.add(pos)
            await s.commit()
            return pos.id

    async def close_position(
        self, position_id: int, realized_pnl_usd: Decimal, closed_at: datetime
    ) -> None:
        async with self._session() as s:
            pos = await s.get(Position, position_id)
            if pos is None:
                raise KeyError(f"position {position_id} not found")
            pos.status = "closed"
            pos.closed_at = closed_at
            pos.realized_pnl_usd = D(realized_pnl_usd)
            await s.commit()

    async def add_funding(self, position_id: int, amount: Decimal) -> None:
        async with self._session() as s:
            pos = await s.get(Position, position_id)
            if pos is None:
                raise KeyError(f"position {position_id} not found")
            pos.funding_collected = D(pos.funding_collected) + D(amount)
            await s.commit()

    async def entry_fees(self, position_id: int) -> Decimal:
        """Sum of fees actually recorded at entry (ledger is the truth)."""
        async with self._session() as s:
            rows = (
                await s.execute(
                    select(OrderRecord).where(
                        OrderRecord.position_id == position_id,
                        OrderRecord.action == "open",
                    )
                )
            ).scalars().all()
        return sum((D(r.fee) for r in rows), D("0"))

    @staticmethod
    def _order_row(fields: dict) -> OrderRecord:
        return OrderRecord(
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

    @staticmethod
    def _audit_row(fields: dict) -> TaxAuditLog:
        return TaxAuditLog(
            strategy_id=fields["strategy_id"],
            paired_hedge_id=fields["paired_hedge_id"],
            exchange=fields["exchange"],
            symbol=fields["symbol"],
            side=fields["side"],
            amount=D(fields["amount"]),
            price_usd_equivalent=D(fields["price_usd_equivalent"]),
            fee_amount=D(fields["fee_amount"]),
            fee_currency=fields["fee_currency"],
            timestamp_utc=fields["timestamp_utc"],
            net_pnl_usd=(
                D(fields["net_pnl_usd"]) if fields.get("net_pnl_usd") is not None else None
            ),
            kind=fields.get("kind", "paper"),
        )

    async def open_position_atomic(
        self, position_fields: dict, orders: list[dict], audits: list[dict]
    ) -> int:
        """op.#2: position + both entry orders + both audit rows in ONE
        transaction. A crash mid-entry can no longer leave a position with
        a single recorded leg."""
        async with self._session() as s:
            pos = Position(
                qty_base=D(position_fields["qty_base"]),
                notional=D(position_fields["notional"]),
                spot_entry_vwap=D(position_fields["spot_entry_vwap"]),
                perp_entry_vwap=D(position_fields["perp_entry_vwap"]),
                symbol=position_fields["symbol"],
                exchange_spot=position_fields["exchange_spot"],
                exchange_perp=position_fields["exchange_perp"],
                spot_symbol=position_fields.get("spot_symbol", ""),
                holding_intervals=position_fields.get("holding_intervals", 90),
                entry_funding_rate=D(position_fields.get("entry_funding_rate", "0")),
            )
            s.add(pos)
            await s.flush()  # assign pos.id inside the transaction
            for f in orders:
                s.add(self._order_row({**f, "position_id": pos.id}))
            for f in audits:
                s.add(self._audit_row({**f, "paired_hedge_id": pos.id}))
            await s.commit()
            return pos.id

    async def close_position_atomic(
        self,
        position_id: int,
        realized_pnl_usd: Decimal,
        closed_at: datetime,
        orders: list[dict],
        audits: list[dict],
    ) -> None:
        """op.#2: close orders + audit rows + status flip in ONE transaction.
        A crash mid-close no longer leaves close orders on an open position
        (which made the retry duplicate them)."""
        async with self._session() as s:
            pos = await s.get(Position, position_id)
            if pos is None:
                raise KeyError(f"position {position_id} not found")
            pos.status = "closed"
            pos.closed_at = closed_at
            pos.realized_pnl_usd = D(realized_pnl_usd)
            for f in orders:
                s.add(self._order_row({**f, "position_id": position_id}))
            for f in audits:
                s.add(self._audit_row({**f, "paired_hedge_id": position_id}))
            await s.commit()

    async def count_orders(self, position_id: int, action: str = "open") -> int:
        async with self._session() as s:
            rows = (
                await s.execute(
                    select(OrderRecord).where(
                        OrderRecord.position_id == position_id,
                        OrderRecord.action == action,
                    )
                )
            ).scalars().all()
        return len(rows)

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
        price_usd_equivalent: Decimal,
        fee_amount: Decimal,
        fee_currency: str,
        timestamp_utc: datetime,
        net_pnl_usd: Decimal | None,
        kind: str = "paper",
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
                    price_usd_equivalent=D(price_usd_equivalent),
                    fee_amount=D(fee_amount),
                    fee_currency=fee_currency,
                    timestamp_utc=timestamp_utc,
                    net_pnl_usd=D(net_pnl_usd) if net_pnl_usd is not None else None,
                    kind=kind,
                )
            )
            await s.commit()

    async def realized_pnl_since(self, since: datetime) -> Decimal:
        """op.#8: today's realized PnL from the ledger (restart-proof)."""
        async with self._session() as s:
            rows = (
                await s.execute(
                    select(Position).where(
                        Position.status == "closed",
                        Position.closed_at >= since,
                    )
                )
            ).scalars().all()
        # fuzz F30: a poisoned row (e.g. 'NaN' TEXT) must not crash startup
        # (seed_daily_pnl) or poison the daily limit - skip it loudly.
        total = D("0")
        for r in rows:
            pnl = D(r.realized_pnl_usd)
            if pnl.is_finite():
                total += pnl
            else:
                logger.error(
                    "realized_pnl_since: non-finite pnl row %s ignored (%r)",
                    r.id, r.realized_pnl_usd,
                )
        return total

    async def open_positions(self) -> list[Position]:
        async with self._session() as s:
            result = await s.execute(select(Position).where(Position.status == "open"))
            return list(result.scalars())

    async def export_tax_csv(self, path: str | Path, kind: str = "live") -> int:
        """AEAT-ready CSV export. Only real trades: paper rows are excluded
        by default (kind="live"); pass kind="paper" for a simulation report."""
        async with self._session() as s:
            result = await s.execute(
                select(TaxAuditLog)
                .where(TaxAuditLog.kind == kind)
                .order_by(TaxAuditLog.trade_id)
            )
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
                    "price_usd_equivalent",
                    "fee_amount",
                    "fee_currency",
                    "timestamp_utc",
                    "net_pnl_usd",
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
                        r.price_usd_equivalent,
                        r.fee_amount,
                        r.fee_currency,
                        r.timestamp_utc.isoformat(),
                        r.net_pnl_usd if r.net_pnl_usd is not None else "",
                    ]
                )
        return len(rows)
