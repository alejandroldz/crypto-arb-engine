"""Runtime configuration (pydantic-settings, .env backed).

Every financial threshold is a Decimal. LIVE_TRADING_ENABLED defaults to
False and the execution engine refuses to run without it (see
app/engines/execution.py).
"""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.decimals import D, ZERO


def _bounded_decimal(raw: str, name: str, lo: Decimal, hi: Decimal) -> Decimal:
    """Parse a config decimal and bound it (fuzz F3/F4).

    Infinity/NaN/negatives reached the engine from fee maps (a negative fee
    APPROVED a carry with negative costs). Out-of-range or malformed values
    raise ValueError naming the setting.
    """
    try:
        d = D(raw)
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError(f"{name}: invalid decimal {raw!r}") from exc
    if not d.is_finite() or d < lo or d > hi:
        raise ValueError(f"{name}: value {d} outside [{lo}, {hi}]")
    return d


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # master switch
    live_trading_enabled: bool = False

    # exchanges (ccxt ids)
    spot_exchanges: str = "kraken"
    spot_symbols: str = "BTC/USD,ETH/USD,SOL/USD"
    perp_exchanges: str = "krakenfutures,okx"
    # OKX EU (MiFID, OKX Europe Malta) serves X-Perps: 5-year linear USD-
    # margined expiry futures with perp-style 8h funding, NOT the global
    # inverse swap BTC/USD:BTC. ccxt symbol for BTC-USD_UM_XPERP-310404:
    # exchange -> perp symbols (';' separates exchanges). SOL trades only
    # on OKX: on Kraken its funding is weak (~3.4% APR, 58% positive
    # intervals, 30d) vs OKX (~9.9% APR, 97%) - rechecked 2026-11-13, the
    # config decision stands.
    # NOTE: krakenfutures PF perps pay funding HOURLY, okx every 8h.
    perp_symbol_map: str = (
        "krakenfutures:BTC/USD:USD,ETH/USD:USD;"
        "okx:BTC/USD:USD-310404,ETH/USD:USD-310404,SOL/USD:USD-310404"
    )

    # sizing / profitability
    target_quote_sizes: str = "100,500,1000"
    min_net_profit_usd: Decimal = Decimal("5")
    # fuzz F57: typos in RISK decimals disabled protections SILENTLY
    # (negative slippage = negative costs = approve_trade passes anything)
    slippage_buffer_bps: Decimal = Field(default=Decimal("5"), ge=0)
    liquidity_haircut: Decimal = Decimal("0.50")
    # per-venue taker fees in bps (worst-case retail tier; user must verify
    # their own tier). kraken spot taker is far pricier than perp venues.
    # verified 2026-11-13 against official pages (tier base, volumen 0):
    # kraken spot taker 0.80% (kraken.com/pro/fees, Tier 1 - lo subieron de 0.40%),
    # krakenfutures 0.05%, okx perps 0.05% (okx.com/fees, regular user)
    taker_fee_bps: str = "kraken:80,krakenfutures:5,bybit:5.5,okx:5"
    # maker fees tier base verificados 2026-11-13: kraken spot 0.40%, perps 0.02% ambos venues
    maker_fee_bps: str = "kraken:40,krakenfutures:2,bybit:2,okx:2"
    # ejecucion de entrada: "maker" (limit pasivo con timeout) o "taker" (cruza siempre)
    execution_mode: str = "maker"
    maker_timeout_seconds: int = Field(default=60, ge=1)
    # que hacer si la limit no se llena a tiempo: "taker" (cruza y paga taker) o "cancel"
    maker_fallback: str = "taker"
    # sizing dinamico (modulo 3): Kelly reducido con cap duro
    sizing_mode: str = "fixed"  # "fixed" (baseline) | "kelly" (modulo 3)
    # fuzz F53: KELLY_FRACTION is an env var - a typo ("25" vs "0.25")
    # oversized every kelly entry 100x; full_score=0 was ZeroDivisionError
    kelly_fraction: Decimal = Field(default=Decimal("0.25"), ge=0, le=1)
    sizing_full_score: Decimal = Field(default=Decimal("0.5"), gt=0)
    sizing_min_notional_usd: Decimal = Field(default=Decimal("250"), ge=0)
    sizing_max_notional_usd: Decimal = Field(default=Decimal("5000"), ge=0)
    # perp-perp (modulo 1): arbitraje de funding entre venues, sin pata spot
    perp_perp_enabled: bool = False  # activado via .env tras validacion
    pp_min_net_profit_usd: Decimal = Decimal("2")
    pp_holding_hours: int = Field(default=720, ge=1)
    pp_max_notional_per_venue_usd: Decimal = Decimal("4000")
    pp_max_notional_per_pair_usd: Decimal = Decimal("2500")
    pp_min_notional_usd: Decimal = Decimal("250")
    # frescura exigida a los libros para operar PP (ms) - un venue caido
    # no es operable
    max_book_age_ms: int = Field(default=2000, ge=0)
    # F06: spread relativo maximo por libro para ENTRAR (bps). Un libro con
    # bid=1/ask=100 es una senal economicamente falsa, no una oportunidad
    max_book_spread_bps: Decimal = Decimal("50")
    # fee oracle: guardrail - cambio relativo de taker fee que bloquea entradas (%)
    fee_change_block_pct: Decimal = Decimal("20")
    fee_refresh_hours: Decimal = Decimal("4")
    # kraken SPOT api (read-only) opcional: sin ella kraken spot no es auto-detectable
    kraken_spot_api_key: str = ""
    kraken_spot_api_secret: str = ""
    # runtime: fees detectadas por FeeOracle (nunca desde env)
    detected_fees: dict[str, dict[str, Decimal]] = Field(default_factory=dict, exclude=True)
    fees_blocked: set[str] = Field(default_factory=set, exclude=True)
    default_taker_fee_bps: Decimal = Decimal("40")
    # minimum funding-rate history samples before approving a carry
    # fuzz F56: 0 is a plausible experiment value and made every
    # candidate ZeroDivisionError on an empty history -> global HALT
    min_funding_history: int = Field(default=30, ge=1)

    # funding payment interval per venue (hours): krakenfutures PF perps pay
    # hourly, OKX X-Perps every 8h - economics must scale by this or Kraken
    # funding is underestimated 8x (found 2026-11-13)
    funding_interval_hours: str = "krakenfutures:1,okx:8"

    # caos H9: book snapshot retention in duckdb (funding history kept)
    book_retention_days: int = Field(default=3, ge=1)

    # staleness gate (RISK 3)
    max_data_age_ms_funding: int = Field(default=2000, ge=0)

    # execution / legging risk (RISK 1)
    max_leg_delay_ms: int = Field(default=1500, ge=1)
    dry_run_latency_ms: int = Field(default=150, ge=0)

    # perp risk (RISK 5). max_leverage is the hard EU MiFID retail cap.
    max_leverage: Decimal = Decimal("10")
    # leverage of the paper perp short leg PER VENUE (1 = fully margined).
    # krakenfutures: multi-collateral UTA (BTC backs the short) -> 5x passes
    # the +80% pump rule with a 3x safety margin. okx: single-currency
    # margin (USD-only collateral) -> treated as isolated, only 1x passes.
    # See scripts/leverage_compare.py.
    short_leverage_map: str = "krakenfutures:5,okx:1"
    default_short_leverage: Decimal = Decimal("1")
    liquidation_buffer_pct: Decimal = Field(default=Decimal("15"), gt=0, le=90)

    # circuit breaker (RISK 6)
    max_daily_loss_usd: Decimal = Decimal("25")
    # fuzz F47: a 0/negative window silently DISABLED the order rate limit
    cb_max_consecutive_failures: int = Field(default=3, ge=0)
    cb_max_orders_per_window: int = Field(default=5, ge=1)
    cb_order_window_seconds: int = Field(default=60, ge=1)

    # storage
    # op.#10: under ./data, the dir docker-compose mounts as a volume - a
    # container recreate no longer loses positions/ticks
    sqlite_url: str = "sqlite+aiosqlite:///./data/arb_state.db"
    duckdb_path: str = "./data/arb_ticks.duckdb"

    # paper trading
    paper_start_balance_usd: Decimal = Decimal("10000")

    # unified trading account (cross-collateral portfolio margin)
    account_margin_mode: str = "UNIFIED"  # UNIFIED | ISOLATED_SPLIT
    uta_capital_utilization_pct: Decimal = Field(default=Decimal("0.90"), gt=0, le=1)
    collateral_haircut_map: str = "BTC:0.95,ETH:0.95,SOL:0.85"
    min_collateral_ratio_required: Decimal = Field(default=Decimal("0.80"), ge=0, le=1)
    max_account_mmr_warning: Decimal = Field(default=Decimal("0.65"), gt=0, le=1)
    max_account_mmr_emergency_unwind: Decimal = Field(default=Decimal("0.75"), gt=0, le=1)
    # simulated perp maintenance-margin rate for paper UTA health; real MMR
    # (~0.5%) can never reach the 75% emergency at 1x, so 25% keeps the
    # emergency path exercisable (documented fallback, not exchange data)
    sim_maintenance_margin_rate: Decimal = Field(default=Decimal("0.25"), gt=0, le=1)

    # screener: periodic re-selection of eligible perps (public data only)
    screener_enabled: bool = True
    # fuzz F58: 0/negative was a HOT LOOP hammering the public venue
    # APIs (ban risk) at 100% CPU - 68k sweeps in 0.5s in the repro
    screener_interval_s: int = Field(default=14400, ge=60)  # 4h; funding moves on 8h intervals
    screener_min_volume_usd: Decimal = Decimal("10000000")
    screener_min_positive_pct: Decimal = Decimal("0.60")
    # configured perp keys are always eligible (seeded), blacklist keys never are
    screener_blacklist: str = ""
    screener_state_path: str = "screener_state.json"

    # optional read-only API keys for real account health (never trading)
    krakenfutures_api_key: str = ""
    krakenfutures_api_secret: str = ""
    okx_api_key: str = ""
    okx_api_secret: str = ""
    okx_api_passphrase: str = ""
    # OKX Europe (MiFID entity) keys live on my.okx.com, not www.okx.com
    # (verified live 2026-11-13: www returns 50119 "API key doesn't exist")
    # SECURITY: validated against an allowlist - API credentials are sent
    # to this host, so a typo/attacker-controlled value must fail closed
    okx_hostname: str = "my.okx.com"

    # telegram alerts
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @field_validator("okx_hostname")
    @classmethod
    def _okx_host_allowlist(cls, v: str) -> str:
        allowed = {"my.okx.com", "www.okx.com", "okx.com", "app.okx.com",
                   "aws.okx.com", "eea.okx.com"}
        if v not in allowed:
            raise ValueError(
                f"okx_hostname {v!r} not in allowlist {sorted(allowed)}"
            )
        return v

    @field_validator(
        "min_net_profit_usd",
        "slippage_buffer_bps",
        "liquidity_haircut",
        "default_taker_fee_bps",
        "max_leverage",
        "default_short_leverage",
        "liquidation_buffer_pct",
        "max_daily_loss_usd",
        "paper_start_balance_usd",
        "uta_capital_utilization_pct",
        "min_collateral_ratio_required",
        "max_account_mmr_warning",
        "max_account_mmr_emergency_unwind",
        "sim_maintenance_margin_rate",
        "screener_min_volume_usd",
        "screener_min_positive_pct",
        mode="before",
    )
    @classmethod
    def _decimal_fields(cls, v: object) -> Decimal:
        # fuzz F4: decimal.InvalidOperation is NOT a ValueError, so pydantic
        # surfaced it raw without naming the setting. Wrap it.
        try:
            return D(v)  # rejects floats with a clear error
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid decimal value {v!r}") from exc

    @field_validator("max_leverage")
    @classmethod
    def _leverage_bounds(cls, v: Decimal) -> Decimal:
        if v < Decimal("1") or v > Decimal("10"):
            raise ValueError("max_leverage must be within [1, 10] (EU cap)")
        return v

    @model_validator(mode="after")
    def _short_leverage_within_cap(self) -> "Settings":
        for venue, lev in self.short_leverage_map_parsed.items():
            if not (Decimal("1") <= lev <= self.max_leverage):
                raise ValueError(
                    f"short leverage for {venue} ({lev}) outside "
                    f"[1, max_leverage={self.max_leverage}]"
                )
        if not (Decimal("1") <= self.default_short_leverage <= self.max_leverage):
            raise ValueError("default_short_leverage outside [1, max_leverage]")
        return self

    @property
    def short_leverage_map_parsed(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for pair in self.short_leverage_map.split(","):
            if ":" in pair:
                ex, lev = pair.split(":", 1)
                out[ex.strip()] = _bounded_decimal(
                    lev.strip(), f"short_leverage_map[{ex.strip()}]",
                    Decimal("1"), Decimal("100"))
        return out

    def short_leverage_for(self, exchange: str) -> Decimal:
        return self.short_leverage_map_parsed.get(
            exchange, self.default_short_leverage
        )

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
                out[venue.strip()] = _bounded_decimal(
                    bps.strip(), f"taker_fee_bps[{venue.strip()}]",
                    ZERO, Decimal("10000"))
        return out

    def taker_fee_for(self, exchange: str) -> Decimal:
        detected = self.detected_fees.get(exchange, {})
        if "taker" in detected:
            return detected["taker"]
        return self.taker_fee_bps_map.get(exchange, self.default_taker_fee_bps)

    @property
    def maker_fee_bps_map(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for pair in self.maker_fee_bps.split(","):
            if ":" in pair:
                venue, bps = pair.split(":", 1)
                out[venue.strip()] = _bounded_decimal(
                    bps.strip(), f"maker_fee_bps[{venue.strip()}]",
                    ZERO, Decimal("10000"))
        return out

    def maker_fee_for(self, exchange: str) -> Decimal:
        detected = self.detected_fees.get(exchange, {})
        if "maker" in detected:
            return detected["maker"]
        return self.maker_fee_bps_map.get(exchange, self.default_taker_fee_bps)

    def entry_fee_for(self, exchange: str) -> Decimal:
        """Fee de la pata de entrada segun el modo de ejecucion."""
        if self.execution_mode == "maker":
            return self.maker_fee_for(exchange)
        return self.taker_fee_for(exchange)

    @property
    def collateral_ratio_map(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for pair in self.collateral_haircut_map.split(","):
            if ":" in pair:
                asset, ratio = pair.split(":", 1)
                out[asset.strip().upper()] = _bounded_decimal(
                    ratio.strip(), f"collateral_haircut_map[{asset.strip()}]",
                    ZERO, Decimal("1"))
        return out

    def collateral_ratio_for(self, asset: str) -> Decimal:
        # unknown assets get 0.00 collateral value: rejected in UNIFIED mode
        return self.collateral_ratio_map.get(asset.strip().upper(), ZERO)

    @property
    def spot_symbol_list(self) -> list[str]:
        syms = [s.strip() for s in self.spot_symbols.split(",") if s.strip()]
        # fuzz F27: empty list died later as a bare min() ValueError
        if not syms:
            raise ValueError("spot_symbols must name at least one symbol")
        return syms

    def perp_symbols_for(self, exchange: str) -> list[str]:
        for group in self.perp_symbol_map.split(";"):
            if ":" not in group:
                continue
            ex, symbols = group.split(":", 1)
            if ex.strip() == exchange:
                return [s.strip() for s in symbols.split(",") if s.strip()]
        return []

    @property
    def perp_symbol_list(self) -> list[str]:
        out: list[str] = []
        for ex in self.perp_exchange_list:
            out.extend(self.perp_symbols_for(ex))
        return out

    @property
    def screener_blacklist_keys(self) -> set[str]:
        return {k.strip() for k in self.screener_blacklist.split(",") if k.strip()}

    def funding_interval_h_for(self, exchange: str) -> Decimal:
        for pair in self.funding_interval_hours.split(","):
            if ":" in pair:
                ex, hours = pair.split(":", 1)
                if ex.strip() == exchange:
                    return _bounded_decimal(
                        hours.strip(), f"funding_interval_hours[{ex.strip()}]",
                        Decimal("0.000001"), Decimal("720"))
        return Decimal("8")

    def spot_symbol_for_base(self, base: str) -> str | None:
        """Configured spot symbol for a base asset, e.g. ETH -> ETH/USD."""
        want = f"{base.strip().upper()}/"
        for sym in self.spot_symbol_list:
            if sym.upper().startswith(want):
                return sym
        return None

    @property
    def target_quote_size_list(self) -> list[Decimal]:
        sizes = [D(s.strip()) for s in self.target_quote_sizes.split(",") if s.strip()]
        # fuzz F6: an empty list died later as a bare IndexError at startup
        if not sizes:
            raise ValueError("target_quote_sizes must name at least one size")
        for s in sizes:
            # fuzz F31: '1e999999' is finite but explodes downstream mul()
            if not s.is_finite() or s <= ZERO or s > Decimal("1e9"):
                raise ValueError(
                    f"target_quote_sizes: {s} outside (0, 1e9]"
                )
        return sizes


@lru_cache
def get_settings() -> Settings:
    return Settings()
