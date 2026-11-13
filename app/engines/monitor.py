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
from app.engines.risk import (
    CarryPositionRisk,
    UtaAccountState,
    div,
    funding_flip_should_unwind,
)


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
    account_mmr: Decimal | None = None


def books_fresh(now_ns: int, max_age_ns: int, *books) -> bool:
    """op.#4: no price-driven decision (MMR, liquidation, unwind) may use a
    dead venue's last-known snapshot. Freshness of BOTH legs is required."""
    for b in books:
        if b is None or (now_ns - b.local_ts_ns) > max_age_ns:
            return False
    return True


def evaluate_carry(
    *,
    qty_base: Decimal,
    perp_entry_vwap: Decimal,
    last_payment_ts: datetime | None,
    rate: FundingRate | None,
    perp_book: L2OrderBook | None,
    risk: CarryPositionRisk,
    liquidation_buffer_pct: Decimal,
    uta: UtaAccountState | None = None,
    mmr_emergency: Decimal = Decimal("0.75"),
) -> MonitorDecision:
    # 0. UTA account-MMR emergency wins over everything (spec 2.2/2.4)
    if uta is not None and perp_book is not None:
        delta_p = div(
            perp_book.best_ask.price - D(perp_entry_vwap), D(perp_entry_vwap)
        )
        mmr = uta.account_mmr(delta_p)
        if mmr >= D(mmr_emergency):
            return MonitorDecision(
                action=MonitorAction.UNWIND, reason="account mmr", account_mmr=mmr
            )
    else:
        mmr = None
    # 1. liquidation proximity (RISK 5) - ISOLATED formula only applies when
    # the position is actually isolated. With a Unified Trading Account the
    # account-level MMR above is the real constraint; the isolated leg
    # formula would close the carry at a tiny adverse move (+3% at 5x)
    # while the account would have survived +130%.
    if uta is None and perp_book is not None and risk.must_close(
        perp_book.best_ask.price, D(liquidation_buffer_pct)
    ):
        return MonitorDecision(
            action=MonitorAction.UNWIND, reason="liquidation buffer", account_mmr=mmr
        )

    accrual = ZERO
    uncertain = False
    new_ts = last_payment_ts

    # 2. funding payment processing (F02: strict monotonic timestamps -
    # a replayed or regressed timestamp never accrues and never rewinds)
    if rate is not None and rate.next_funding_ts is not None:
        if last_payment_ts is None:
            # restart recovery: we don't know which payments happened while
            # down - adopt the current timestamp, accrue nothing, flag it
            new_ts = rate.next_funding_ts
            uncertain = True
        elif rate.next_funding_ts < last_payment_ts:
            uncertain = True  # out-of-order/replayed snapshot: ignore payment
        elif rate.next_funding_ts > last_payment_ts:
            # approximation: accrue at the newly observed rate (signed:
            # a negative rate is a cost and must be accounted, F04)
            accrual = mul(mul(D(qty_base), D(perp_entry_vwap)), rate.rate)
            gap_s = (rate.next_funding_ts - last_payment_ts).total_seconds()
            if gap_s > Decimal("3") * D(rate.interval_hours) * 3600 / 2:
                uncertain = True  # payments were missed while unobserved
            new_ts = rate.next_funding_ts

    # 3. funding flip on EVERY fresh snapshot (F03), independent of whether
    # a payment was processed or the venue even provides a timestamp
    if rate is not None:
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
                account_mmr=mmr,
            )
    if accrual != ZERO:
        return MonitorDecision(
            action=MonitorAction.ACCRUE,
            reason="funding payment",
            accrual=accrual,
            new_payment_ts=new_ts,
            accrual_uncertain=uncertain,
            account_mmr=mmr,
        )

    return MonitorDecision(
        action=MonitorAction.NONE,
        new_payment_ts=new_ts,
        accrual_uncertain=uncertain,
        account_mmr=mmr,
    )
