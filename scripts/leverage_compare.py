"""Offline leverage comparison for the funding carry (no engine needed).

Reads the latest funding snapshot per venue from funding_log.csv and prints
what each short-leg leverage would change:

- capital efficiency L/(L+1): funding captured per unit of committed
  capital (spot cash 1 + short margin 1/L)
- funding APR on committed capital, per venue
- pump survival: isolated margin liquidates at ~+1/L (optimistic); the
  UNIFIED account-level max safe pump is leverage-independent at fixed
  notional (cross collateral absorbs the move), so one row serves all L
- whether the level passes the validator's +80% pump-survival rule in each
  margin mode

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


ACTIVE_VENUES = {"kraken", "krakenfutures", "okx"}


def latest_rates(csv_path: Path) -> dict[str, Decimal]:
    """Newest funding rate per active exchange from the soak CSV log."""
    latest: dict[str, tuple[str, Decimal]] = {}
    if not csv_path.exists():
        return {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("exchange") not in ACTIVE_VENUES:
                continue
            try:
                latest[row["exchange"]] = (row["ts"], D(row["rate"]))
            except (KeyError, ValueError):
                continue
    return {ex: rate for ex, (_, rate) in latest.items()}


def main() -> None:
    settings = Settings()
    rates = latest_rates(Path("funding_log.csv"))
    uta = UtaAccountState(
        equity=D(settings.paper_start_balance_usd),
        utilization=D(settings.uta_capital_utilization_pct),
        collateral_ratio=D("0.95"),
        sim_mmr_rate=D(settings.sim_maintenance_margin_rate),
    )
    unified_pump = uta.max_safe_pump_pct(D(settings.max_account_mmr_emergency_unwind))
    unified_txt = "infinito" if unified_pump is None else f"+{unified_pump * 100:.0f}%"

    print(f"Modo UNIFIED (u={settings.uta_capital_utilization_pct}, "
          f"haircut 0.95, mmr emergencia {settings.max_account_mmr_emergency_unwind}):")
    print(f"  max safe pump a nivel cuenta: {unified_txt} "
          f"(independiente del apalancamiento a nocional fijo)")
    print(f"  regla del validador: rechazar si max safe pump < +80%\n")

    header = (
        f"{'lev':>4} | {'efic. capital':>13} | {'pump aislado':>12} | "
        f"{'pasa +80% UNIFIED':>17} | {'pasa +80% ISOLATED':>18}"
    )
    print(header)
    print("-" * len(header))
    for lev in LEVELS:
        eff = capital_efficiency(lev)
        iso = isolated_max_pump_pct(lev)
        iso_ok = "si" if iso >= MIN_SAFE_PUMP else "no"
        uni_ok = (
            "si"
            if unified_pump is None or unified_pump >= MIN_SAFE_PUMP
            else "no"
        )
        legal = "" if lev <= EU_CAP else " (ILEGAL EU)"
        print(
            f"{str(lev) + 'x':>4} | {eff * 100:12.1f}% | {iso * 100:11.0f}% | "
            f"{uni_ok:>17} | {iso_ok:>18}{legal}"
        )

    if rates:
        print("\nFunding APR anualizado sobre capital comprometido "
              "(ultimo snapshot del soak):")
        for ex, rate in sorted(rates.items()):
            apr = rate * 3 * 365
            row = f"  {ex:<15} funding {apr * 100:+.2f}% ->"
            for lev in LEVELS:
                row += f" {lev}x: {apr * capital_efficiency(lev) * 100:+.2f}%"
            print(row)
    else:
        print("\n(sin funding_log.csv: tasas no disponibles)")


if __name__ == "__main__":
    main()
