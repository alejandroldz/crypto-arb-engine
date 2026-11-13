"""Offline per-venue leverage comparison for the funding carry.

Reads the latest funding snapshot per venue from funding_log.csv and shows,
per venue and leverage level:

- capital efficiency L/(L+1): funding captured per unit of committed
  capital (spot cash 1 + short margin 1/L)
- funding APR on committed capital (latest soak snapshot)
- pump survival vs the validator's +80% rule:
  * UNIFIED multi-collateral (krakenfutures): account-level. Deploying
    90% of equity as N = u*E*L/(L+1) notional (spot cash + short margin),
    so effective utilization u_eff = 0.9*L/(L+1) and the account-level
    max safe pump from UtaAccountState applies.
  * single-currency / USD-only collateral (okx): treated as isolated -
    the short liquidates at ~+1/L (optimistic; MMR makes it earlier).

Run:  uv run python scripts/leverage_compare.py
"""
from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from app.core.config import Settings
from app.core.decimals import D
from app.engines.risk import (
    UtaAccountState,
    capital_efficiency,
    isolated_max_pump_pct,
)

LEVELS = [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("5"), Decimal("10")]
EU_CAP = Decimal("10")
MIN_SAFE_PUMP = Decimal("0.80")  # validator rule (spec 2.2.3)
ACTIVE_VENUES = {"krakenfutures", "okx"}
# venues whose real account is unified multi-collateral (BTC backs the short)
UNIFIED_VENUES = {"krakenfutures"}


def latest_rates(csv_path: Path) -> dict[str, Decimal]:
    """Newest funding rate per active exchange from the soak CSV log."""
    latest: dict[str, tuple[str, Decimal]] = {}
    if not csv_path.exists():
        return {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        # fuzz F25: a >128KB field raises _csv.Error; a corrupt row must not
        # kill the report
        csv.field_size_limit(10 * 1024 * 1024)
        for row in csv.DictReader(fh):
            if row.get("exchange") not in ACTIVE_VENUES:
                continue
            try:
                latest[row["exchange"]] = (row["ts"], D(row["rate"]))
            except (KeyError, ValueError):
                continue
    return {ex: rate for ex, (_, rate) in latest.items()}


def unified_max_pump(settings: Settings, lev: Decimal) -> Decimal | None:
    """Account-level max safe pump at the utilization the leverage enables."""
    u_eff = D(settings.uta_capital_utilization_pct) * lev / (lev + 1)
    uta = UtaAccountState(
        equity=D(settings.paper_start_balance_usd),
        utilization=u_eff,
        collateral_ratio=D("0.95"),
        sim_mmr_rate=D(settings.sim_maintenance_margin_rate),
        leverage=lev,
    )
    return uta.max_safe_pump_pct(D(settings.max_account_mmr_emergency_unwind))


def main() -> None:
    settings = Settings()
    rates = latest_rates(Path("funding_log.csv"))
    chosen = settings.short_leverage_map_parsed

    for venue in sorted(ACTIVE_VENUES):
        mode = "UNIFIED multi-collateral" if venue in UNIFIED_VENUES else (
            "single-currency (USD-only, como isolated)"
        )
        print(f"\n=== {venue} - modo real: {mode} ===")
        print(f"apalancamiento configurado: {chosen.get(venue, settings.default_short_leverage)}x")
        header = (
            f"{'lev':>4} | {'efic. capital':>13} | {'max safe pump':>13} | "
            f"{'pasa regla +80%':>15}"
        )
        print(header)
        print("-" * len(header))
        for lev in LEVELS:
            eff = capital_efficiency(lev)
            if venue in UNIFIED_VENUES:
                pump = unified_max_pump(settings, lev)
                pump_txt = "inf" if pump is None else f"+{pump * 100:.0f}%"
                ok = pump is None or pump >= MIN_SAFE_PUMP
            else:
                pump = isolated_max_pump_pct(lev)
                pump_txt = f"+{pump * 100:.0f}%"
                ok = pump >= MIN_SAFE_PUMP
            legal = "" if lev <= EU_CAP else " ILEGAL EU"
            print(
                f"{str(lev) + 'x':>4} | {eff * 100:12.1f}% | {pump_txt:>13} | "
                f"{'si' if ok else 'NO':>15}{legal}"
            )
        rate = rates.get(venue)
        if rate is not None:
            apr = rate * 3 * 365
            row = f"  funding bruto {apr * 100:+.2f}% -> APR/capital:"
            for lev in LEVELS:
                row += f" {lev}x: {apr * capital_efficiency(lev) * 100:+.2f}%"
            print(row)


if __name__ == "__main__":
    main()
