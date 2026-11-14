"""Fee oracle: venue fees are DETECTED from each venue's API, never hardcoded.

Chain per venue: live API -> last known good (fees_state.json, warns) ->
config fallback (warns loudly). Guardrail: if a detected fee moves more than
``fee_change_block_pct``% vs the last known value, the venue is added to
``settings.fees_blocked`` and the engine stops opening new carries until the
state file is cleared by a human review.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Awaitable, Callable

import base64
import hashlib
import hmac

import aiohttp

from app.core.decimals import D

logger = logging.getLogger(__name__)

#: venue -> coroutine returning (maker_bps, taker_bps)
FeeFetcher = Callable[[], Awaitable[tuple[Decimal, Decimal]]]

KRAKEN_FUTURES_API = "https://futures.kraken.com/derivatives/api/v3"
KRAKEN_SPOT_API = "https://api.kraken.com/0"


@dataclass(frozen=True)
class FeeQuote:
    maker_bps: Decimal
    taker_bps: Decimal
    source: str  # "api" | "cache" | "config"


async def fetch_krakenfutures_fee(symbol_map: dict[str, str]) -> tuple[Decimal, Decimal]:
    """Public: instrument -> feeScheduleUid -> tier-0 fees (percents -> bps).

    symbol_map: unified symbol -> raw PF symbol (e.g. BTC/USD:USD -> PF_XBTUSD).
    All configured perps share a schedule in practice; the first one wins.
    """
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{KRAKEN_FUTURES_API}/instruments") as r:
            instruments = (await r.json())["instruments"]
        async with s.get(f"{KRAKEN_FUTURES_API}/feeschedules") as r:
            schedules = (await r.json())["feeSchedules"]
    by_uid = {fs.get("uid"): fs for fs in schedules}
    raw = next(iter(symbol_map.values()))
    inst = next(i for i in instruments if i.get("symbol") == raw and i.get("tradeable"))
    tier0 = by_uid[inst["feeScheduleUid"]]["tiers"][0]
    return D(str(tier0["makerFee"])) * 100, D(str(tier0["takerFee"])) * 100


async def fetch_okx_fee(
    api_key: str, api_secret: str, passphrase: str, hostname: str
) -> tuple[Decimal, Decimal]:
    """Private (read-only key) /account/trade-fee for FUTURES (X-Perps).

    Raw REST with HMAC signing: ccxt's full load_markets OOMs a 2GB sandbox.
    OKX returns futures fees with a negative sign convention; abs() them.
    """
    path = "/api/v5/account/trade-fee?instType=FUTURES&instFamily=BTC-USD"
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    sign = base64.b64encode(
        hmac.new(api_secret.encode(), f"{ts}GET{path}".encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
    }
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://{hostname}{path}", headers=headers) as r:
            data = await r.json()
    if data.get("code") != "0":
        raise RuntimeError(f"okx trade-fee error: {data.get('msg')}")
    row = data["data"][0]
    return abs(D(row["maker"])) * 10_000, abs(D(row["taker"])) * 10_000


async def fetch_kraken_spot_fee(api_key: str, api_secret: str) -> tuple[Decimal, Decimal]:
    """Private TradeVolume: current-tier fees (percents -> bps)."""
    import ccxt.async_support as ccxt

    client = ccxt.kraken({"apiKey": api_key, "secret": api_secret})
    try:
        resp = await client.private_post_trade_volume({"pair": "XBTUSD"})
        result = resp.get("result", {})
        taker = next(iter(result.get("fees", {}).values()))["fee"]
        maker = next(iter(result.get("fees_maker", {}).values()))["fee"]
        return D(str(maker)) * 100, D(str(taker)) * 100
    finally:
        await client.close()


_MIN_FEE_BPS = D("-100")   # rebates exist; beyond this is garbage
_MAX_FEE_BPS = D("1000")   # 10% per trade: no real venue charges that


def fee_plausible(maker: Decimal, taker: Decimal) -> bool:
    """fuzz F20/F30: a NaN/Infinity/absurd fee must never be admitted - it
    poisons fills, the ledger and (via seed_daily_pnl) engine startup."""
    return (
        maker.is_finite() and taker.is_finite()
        and _MIN_FEE_BPS <= maker <= _MAX_FEE_BPS
        and _MIN_FEE_BPS <= taker <= _MAX_FEE_BPS
    )


class FeeOracle:
    def __init__(self, settings, state_path: str = "fees_state.json") -> None:
        self.settings = settings
        self.state_path = Path(state_path)

    def _load_state(self) -> dict:
        try:
            raw = json.loads(self.state_path.read_text())
        except Exception:
            # fuzz F54: same escape as F51 - UnicodeDecodeError and
            # RecursionError killed fee_refresh_loop forever; a corrupt
            # state file means fresh start, not a dead subsystem
            return {}
        if not isinstance(raw, dict):
            # fuzz F36: valid JSON of the wrong shape silently froze fees
            logger.error("fees state is not a dict (%r) - ignoring", type(raw).__name__)
            return {}
        # fuzz F21: schema-validate the two keys we rely on. A string
        # "review_required" becomes a set of CHARACTERS and blocks every
        # venue silently; corrupt "venues" poisons the cache chain.
        rr = raw.get("review_required")
        if rr is not None and (
            not isinstance(rr, list) or not all(isinstance(v, str) for v in rr)
        ):
            logger.error("fees state review_required malformed (%r) - ignoring state", rr)
            return {}
        venues = raw.get("venues")
        if venues is not None and not isinstance(venues, dict):
            logger.error("fees state venues malformed - ignoring state")
            return {}
        return raw

    def _save_state(self, state: dict) -> None:
        # caos H10: tmp+replace like breaker/screener - a crash mid-write
        # must not leave a truncated state file
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=1, default=str))
        tmp.replace(self.state_path)

    def apply(self, detected: dict[str, FeeQuote], blocked: set[str]) -> None:
        """Push detected fees into settings (read by *_fee_for) + guardrail."""
        self.settings.detected_fees.update(
            {v: {"maker": q.maker_bps, "taker": q.taker_bps} for v, q in detected.items()}
        )
        self.settings.fees_blocked |= blocked

    async def refresh(self, fetchers: dict[str, FeeFetcher]) -> dict[str, FeeQuote]:
        """Detect every venue's fees once. Never raises: fallback chain per venue."""
        state = self._load_state()
        cached = state.get("venues", {})
        out: dict[str, FeeQuote] = {}
        blocked: set[str] = set(state.get("review_required", []))
        now = datetime.now(timezone.utc).isoformat()

        # audit C1: iterate ALL active venues, not only those with a
        # fetcher - a venue without keys still applies its cached/config
        # fallback instead of silently keeping stale config values
        venues = set(fetchers) | set(self.settings.spot_exchange_list) | set(
            self.settings.perp_exchange_list
        )
        for venue in venues:
            fetch = fetchers.get(venue)
            if fetch is None:
                prev = cached.get(venue)
                prev_ok = False
                if prev is not None:
                    try:
                        if not isinstance(prev, dict):
                            raise TypeError("cache entry not a dict")
                        pq = FeeQuote(D(prev["maker_bps"]), D(prev["taker_bps"]), "cache")
                        prev_ok = fee_plausible(pq.maker_bps, pq.taker_bps)
                    except (ValueError, KeyError, ArithmeticError, TypeError):
                        prev_ok = False
                if prev_ok:
                    out[venue] = pq
                    logger.info(
                        "fees %s: maker %s bps, taker %s bps (cache, sin fetcher)",
                        venue, prev["maker_bps"], prev["taker_bps"],
                    )
                else:
                    if prev is not None:
                        logger.error("fees %s: cached values invalid, ignored", venue)
                    out[venue] = FeeQuote(
                        self.settings.maker_fee_bps_map.get(
                            venue, self.settings.default_taker_fee_bps),
                        self.settings.taker_fee_bps_map.get(
                            venue, self.settings.default_taker_fee_bps),
                        "config",
                    )
                    logger.info("fees %s: config fallback (sin fetcher ni cache)", venue)
                continue
            try:
                maker, taker = await fetch()
                if not fee_plausible(maker, taker):
                    raise ValueError(f"implausible fees {maker}/{taker} bps")
                quote = FeeQuote(maker, taker, "api")
                prev = cached.get(venue)
                if prev is not None:
                    prev_taker = D(str(prev["taker_bps"]))
                    if prev_taker > 0:
                        change_pct = abs(taker - prev_taker) / prev_taker * 100
                        if change_pct > D(self.settings.fee_change_block_pct):
                            blocked.add(venue)
                            logger.error(
                                "FEE GUARDRAIL %s: taker %s -> %s bps (+%.0f%%) - "
                                "entradas nuevas bloqueadas hasta revision manual",
                                venue, prev_taker, taker, change_pct,
                            )
                cached[venue] = {
                    "maker_bps": str(maker), "taker_bps": str(taker), "ts": now,
                }
            except Exception as exc:  # network/permission: degrade, don't die
                prev = cached.get(venue)
                try:
                    if prev is not None and not isinstance(prev, dict):
                        raise TypeError("cache entry not a dict")
                    cq = FeeQuote(D(prev["maker_bps"]), D(prev["taker_bps"]), "cache") if prev else None
                    cache_ok = cq is not None and fee_plausible(cq.maker_bps, cq.taker_bps)
                except (ValueError, KeyError, ArithmeticError, TypeError):
                    cache_ok = False
                if cache_ok:
                    quote = cq
                    logger.warning("fee fetch %s failed (%s) - using cached %s", venue, exc, prev["ts"])
                else:
                    quote = FeeQuote(
                        self.settings.maker_fee_bps_map.get(venue, self.settings.default_taker_fee_bps),
                        self.settings.taker_fee_bps_map.get(venue, self.settings.default_taker_fee_bps),
                        "config",
                    )
                    logger.warning(
                        "fee fetch %s failed (%s) - FALLBACK config %s/%s bps (ultimo recurso)",
                        venue, exc, quote.maker_bps, quote.taker_bps,
                    )
            out[venue] = quote
            logger.info("fees %s: maker %s bps, taker %s bps (%s)", venue, quote.maker_bps, quote.taker_bps, quote.source)

        self._save_state({"venues": cached, "review_required": sorted(blocked)})
        self.apply(out, blocked)
        return out
