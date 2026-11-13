"""Runtime configuration (pydantic-settings, .env backed).

Every financial threshold is a Decimal. LIVE_TRADING_ENABLED defaults to
False and the execution engine refuses to run without it (see
app/engines/execution.py).
"""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic import field_validator, model_validator
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
    perp_exchanges: str = "krakenfutures,okx"
    # OKX EU (MiFID, OKX Europe Malta) serves X-Perps: 5-year linear USD-
    # margined expiry futures with perp-style 8h funding, NOT the global
    # inverse swap BTC/USD:BTC. ccxt symbol for BTC-USD_UM_XPERP-310404:
    perp_symbols: str = "BTC/USD:USD,BTC/USD:USD-310404"

    # sizing / profitability
    target_quote_sizes: str = "100,500,1000"
    min_net_profit_usd: Decimal = Decimal("5")
    slippage_buffer_bps: Decimal = Decimal("5")
    liquidity_haircut: Decimal = Decimal("0.50")
    # per-venue taker fees in bps (worst-case retail tier; user must verify
    # their own tier). kraken spot taker is far pricier than perp venues.
    taker_fee_bps: str = "kraken:40,krakenfutures:5,bybit:5.5,okx:5"
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
    # leverage of the paper perp short leg (1 = fully margined). EU MiFID
    # venues cap retail crypto perps at 10x; see scripts/leverage_compare.py
    short_leverage: Decimal = Decimal("1")
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

    # unified trading account (cross-collateral portfolio margin)
    account_margin_mode: str = "UNIFIED"  # UNIFIED | ISOLATED_SPLIT
    uta_capital_utilization_pct: Decimal = Decimal("0.90")
    collateral_haircut_map: str = "BTC:0.95,ETH:0.95,SOL:0.85"
    min_collateral_ratio_required: Decimal = Decimal("0.80")
    max_account_mmr_warning: Decimal = Decimal("0.65")
    max_account_mmr_emergency_unwind: Decimal = Decimal("0.75")
    # simulated perp maintenance-margin rate for paper UTA health; real MMR
    # (~0.5%) can never reach the 75% emergency at 1x, so 25% keeps the
    # emergency path exercisable (documented fallback, not exchange data)
    sim_maintenance_margin_rate: Decimal = Decimal("0.25")

    # optional read-only API keys for real account health (never trading)
    krakenfutures_api_key: str = ""
    krakenfutures_api_secret: str = ""
    okx_api_key: str = ""
    okx_api_secret: str = ""
    okx_api_passphrase: str = ""
    # OKX Europe (MiFID entity) keys live on my.okx.com, not www.okx.com
    # (verified live 2026-11-13: www returns 50119 "API key doesn't exist")
    okx_hostname: str = "my.okx.com"

    # telegram alerts
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @field_validator(
        "min_net_profit_usd",
        "slippage_buffer_bps",
        "liquidity_haircut",
        "default_taker_fee_bps",
        "max_leverage",
        "short_leverage",
        "liquidation_buffer_pct",
        "max_daily_loss_usd",
        "paper_start_balance_usd",
        "uta_capital_utilization_pct",
        "min_collateral_ratio_required",
        "max_account_mmr_warning",
        "max_account_mmr_emergency_unwind",
        "sim_maintenance_margin_rate",
        mode="before",
    )
    @classmethod
    def _decimal_fields(cls, v: object) -> Decimal:
        return D(v)  # rejects floats with a clear error

    @field_validator("max_leverage")
    @classmethod
    def _leverage_bounds(cls, v: Decimal) -> Decimal:
        if v < Decimal("1") or v > Decimal("10"):
            raise ValueError("max_leverage must be within [1, 10] (EU cap)")
        return v

    @model_validator(mode="after")
    def _short_leverage_within_cap(self) -> "Settings":
        if not (Decimal("1") <= self.short_leverage <= self.max_leverage):
            raise ValueError(
                f"short_leverage {self.short_leverage} outside "
                f"[1, max_leverage={self.max_leverage}]"
            )
        return self

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
    def collateral_ratio_map(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for pair in self.collateral_haircut_map.split(","):
            if ":" in pair:
                asset, ratio = pair.split(":", 1)
                out[asset.strip().upper()] = D(ratio.strip())
        return out

    def collateral_ratio_for(self, asset: str) -> Decimal:
        # unknown assets get 0.00 collateral value: rejected in UNIFIED mode
        return self.collateral_ratio_map.get(asset.strip().upper(), ZERO)

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
