"""SQLAlchemy 2.0 models: positions, order records, AEAT tax audit log."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Integer, Numeric, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Position(Base):
    """One cash-and-carry position: long spot + short perp, delta-neutral."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(32), default="funding_carry")
    symbol: Mapped[str] = mapped_column(String(32))
    exchange_spot: Mapped[str] = mapped_column(String(32))
    exchange_perp: Mapped[str] = mapped_column(String(32))
    qty_base: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    notional: Mapped[Decimal] = mapped_column(Numeric(38, 8))
    spot_entry_vwap: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    perp_entry_vwap: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    funding_collected: Mapped[Decimal] = mapped_column(Numeric(38, 18), default=0)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open/closed
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    realized_pnl_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 8), nullable=True)
    spot_symbol: Mapped[str] = mapped_column(String(32), default="")
    holding_intervals: Mapped[int] = mapped_column(Integer, default=90)
    entry_funding_rate: Mapped[Decimal] = mapped_column(Numeric(38, 18), default=0)


class OrderRecord(Base):
    """Every (paper or live) fill, per leg."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(Integer)
    leg: Mapped[str] = mapped_column(String(16))  # spot / perp
    action: Mapped[str] = mapped_column(String(16))  # open / close
    side: Mapped[str] = mapped_column(String(8))  # buy / sell
    qty_base: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    vwap: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    fee: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    fee_currency: Mapped[str] = mapped_column(String(8))
    kind: Mapped[str] = mapped_column(String(8), default="paper")  # paper / live
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class TaxAuditLog(Base):
    """Immutable AEAT/IRPF traceability log (RISK 7). USD accounting;
    EUR conversion for AEAT happens at export time (future).

    Every fill is a taxable event in Spain; paired_hedge_id links both legs
    of one carry so losses on one leg are provably tied to gains on the
    other. Application-level immutability: the repository only inserts.
    """

    __tablename__ = "tax_audit_log"

    trade_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String(32))
    paired_hedge_id: Mapped[int] = mapped_column(Integer)  # position id
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    price_usd_equivalent: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    fee_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    fee_currency: Mapped[str] = mapped_column(String(8))
    timestamp_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    net_pnl_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 8), nullable=True)
    kind: Mapped[str] = mapped_column(String(8), default="paper")  # paper / live
