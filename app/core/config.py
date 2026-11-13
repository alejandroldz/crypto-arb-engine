"""Runtime configuration (pydantic-settings, .env backed).

Every financial threshold is a Decimal. LIVE_TRADING_ENABLED defaults to
False and the execution engine refuses to run without it (see
app/engines/execution.py).
"""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.decimals import D, ZERO


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # master switch
    live_trading_enabled: bool = False

    # exchanges (ccxt ids)
    spot_exchanges: str = "kraken"
    spot_symbols: str = "BTC/USD"
    perp_exchanges: str = "deribit,krakenfutures"
    perp_symbols: str = "BTC-PERPETUAL,BTC/USD:USD"

    # sizing / profitability
    target_quote_sizes: str = "100,500,1000"
    min_net_profit_usd: Decimal = Decimal("5")
    slippage_buffer_bps: Decimal = Decimal("5")
    liquidity_haircut: Decimal = Decimal("0.50")
    # per-venue taker fees in bps (worst-case retail tier; user must verify
    # their own tier). kraken spot taker is far pricier than perp venues.
    taker_fee_bps: str = "kraken:40,krakenfutures:5,deribit:5"
    default_taker_fee_bps: Decimal = Decimal("40")
    # minimum funding-rate history samples before approving a carry
    min_funding_history: int = 30

    # staleness gate (RISK 3)
    max_data_age_ms_funding: int = 2000

    # execution / legging risk (RISK 1)
    max_leg_delay_ms: int = 1500
    dry_run_latency_ms: int = 150

    # perp risk (RISK 5)
    max_leverage: Decimal = Decimal("2")
    liquidation_buffer_pct: Decimal = Decimal("15")

    # circuit breaker (RISK 6)
    max_daily_loss_usd: Decimal = Decimal("25")
    cb_max_consecutive_failures: int = 3
    cb_max_orders_per_window: int = 5
    cb_order_window_seconds: int = 60

    # storage
    sqlite_url: str = "sqlite+aiosqlite:///./arb_state.db"
    duckdb_path: str = "./arb_ticks.duckdb"

    # paper trading
    paper_start_balance_usd: Decimal = Decimal("10000")

    # telegram alerts
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @field_validator(
        "min_net_profit_usd",
        "slippage_buffer_bps",
        "liquidity_haircut",
        "default_taker_fee_bps",
        "max_leverage",
        "liquidation_buffer_pct",
        "max_daily_loss_usd",
        "paper_start_balance_usd",
        mode="before",
    )
    @classmethod
    def _decimal_fields(cls, v: object) -> Decimal:
        return D(v)  # rejects floats with a clear error

    @field_validator("max_leverage")
    @classmethod
    def _leverage_bounds(cls, v: Decimal) -> Decimal:
        if v < Decimal("1") or v > Decimal("2"):
            raise ValueError("max_leverage must be within [1, 2] (RISK 5)")
        return v

    @field_validator("liquidity_haircut")
    @classmethod
    def _haircut_bounds(cls, v: Decimal) -> Decimal:
        if v <= ZERO or v >= Decimal("1"):
            raise ValueError("liquidity_haircut must be within (0, 1)")
        return v

    @property
    def spot_exchange_list(self) -> list[str]:
        return [e.strip() for e in self.spot_exchanges.split(",") if e.strip()]

    @property
    def perp_exchange_list(self) -> list[str]:
        return [e.strip() for e in self.perp_exchanges.split(",") if e.strip()]

    @property
    def taker_fee_bps_map(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for pair in self.taker_fee_bps.split(","):
            if ":" in pair:
                venue, bps = pair.split(":", 1)
                out[venue.strip()] = D(bps.strip())
        return out

    def taker_fee_for(self, exchange: str) -> Decimal:
        return self.taker_fee_bps_map.get(exchange, self.default_taker_fee_bps)

    @property
    def spot_symbol_list(self) -> list[str]:
        return [s.strip() for s in self.spot_symbols.split(",") if s.strip()]

    @property
    def perp_symbol_list(self) -> list[str]:
        return [s.strip() for s in self.perp_symbols.split(",") if s.strip()]

    @property
    def target_quote_size_list(self) -> list[Decimal]:
        return [D(s.strip()) for s in self.target_quote_sizes.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
