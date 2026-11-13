"""Open-carry monitoring decisions, extracted from the main loop for tests.

One pure function per cycle: given the latest funding snapshot and perp
book, decide whether to accrue a funding payment and/or unwind (funding
flip or liquidation buffer). Missed funding payments are surfaced as
``accrual_uncertain`` instead of being silently lost.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from app.connectors.normalizer import FundingRate, L2OrderBook
from app.core.decimals import D, ZERO, mul
from app.engines.risk import CarryPositionRisk, funding_flip_should_unwind


class MonitorAction(Enum):
    NONE = "none"
    ACCRUE = "accrue"
    UNWIND = "unwind"


@dataclass(frozen=True)
class MonitorDecision:
    action: MonitorAction
    reason: str = ""
    accrual: Decimal = ZERO
    new_payment_ts: datetime | None = None
    accrual_uncertain: bool = False


def evaluate_carry(
    *,
    qty_base: Decimal,
    perp_entry_vwap: Decimal,
    last_payment_ts: datetime | None,
    rate: FundingRate | None,
    perp_book: L2OrderBook | None,
    risk: CarryPositionRisk,
    liquidation_buffer_pct: Decimal,
) -> MonitorDecision:
    # 1. liquidation proximity wins over everything (RISK 5)
    if perp_book is not None and risk.must_close(
        perp_book.best_ask.price, D(liquidation_buffer_pct)
    ):
        return MonitorDecision(action=MonitorAction.UNWIND, reason="liquidation buffer")

    accrual = ZERO
    uncertain = False
    new_ts = last_payment_ts

    # 2. funding payment processing
    if rate is not None and rate.next_funding_ts is not None:
        if last_payment_ts is None:
            # restart recovery: we don't know which payments happened while
            # down - adopt the current timestamp, accrue nothing, flag it
            new_ts = rate.next_funding_ts
            uncertain = True
        elif rate.next_funding_ts != last_payment_ts:
            # approximation: accrue at the newly observed rate
            accrual = mul(mul(D(qty_base), D(perp_entry_vwap)), rate.rate)
            gap_s = (rate.next_funding_ts - last_payment_ts).total_seconds()
            if gap_s > Decimal("3") * D(rate.interval_hours) * 3600 / 2:
                uncertain = True  # payments were missed while unobserved
            new_ts = rate.next_funding_ts
            # 3. funding flip on the fresh snapshot (RISK 4)
            effective = (
                rate.predicted_rate if rate.predicted_rate is not None else rate.rate
            )
            if funding_flip_should_unwind(effective):
                return MonitorDecision(
                    action=MonitorAction.UNWIND,
                    reason="funding flip",
                    accrual=accrual,
                    new_payment_ts=new_ts,
                    accrual_uncertain=uncertain,
                )
            return MonitorDecision(
                action=MonitorAction.ACCRUE,
                reason="funding payment",
                accrual=accrual,
                new_payment_ts=new_ts,
                accrual_uncertain=uncertain,
            )

    return MonitorDecision(
        action=MonitorAction.NONE,
        new_payment_ts=new_ts,
        accrual_uncertain=uncertain,
    )
