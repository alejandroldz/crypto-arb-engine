"""Network IO for the screener: fetches raw perp metrics from public APIs.

Kept out of app/engines (floats are allowed here; the engine module
converts to Decimal at the boundary). All endpoints are public, no keys.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)
import ccxt.async_support as ccxt

OKX_HOST = "https://my.okx.com"
KRAKEN_FUT = "https://futures.kraken.com/derivatives/api"


@dataclass
class RawPerpMetrics:
    """Raw (float) metrics for one perp; engines/screener.py Decimal-izes."""

    exchange: str
    symbol: str  # ccxt unified symbol
    base: str
    volume_24h_usd: float
    funding_mean: float | None  # None = no history available
    funding_pct_positive: float | None
    funding_std: float | None
    funding_interval_h: float  # krakenfutures PF perps pay HOURLY, okx 8h


def _okx_unified(inst_id: str) -> tuple[str, str] | None:
    """BTC-USD_UM_XPERP-310404 -> (BTC, BTC/USD:USD-310404)."""
    parts = inst_id.split("-")
    if len(parts) < 3 or not parts[1].startswith("USD"):
        return None
    base, expiry = parts[0], parts[-1]
    return base, f"{base}/USD:USD-{expiry}"


async def fetch_okx_xperp_metrics(history_limit: int = 100,
                                  min_volume_usd: float = 0.0,
                                  bases: set[str] | None = None) -> list[RawPerpMetrics]:
    out: list[RawPerpMetrics] = []
    async with aiohttp.ClientSession() as sess:
        async with sess.get(
            f"{OKX_HOST}/api/v5/public/instruments", params={"instType": "FUTURES"}
        ) as r:
            if r.status != 200:
                logger.error("okx instruments HTTP %s - venue skipped this sweep", r.status)
                return out
            instruments = (await r.json()).get("data", [])
        async with sess.get(
            f"{OKX_HOST}/api/v5/market/tickers", params={"instType": "FUTURES"}
        ) as r:
            if r.status != 200:
                logger.error("okx tickers HTTP %s - venue skipped this sweep", r.status)
                return out
            tickers = {t["instId"]: t for t in (await r.json()).get("data", [])}
        xperps = [
            i for i in instruments
            if i.get("ruleType") == "xperp" and i.get("state") == "live"
        ]
        for inst in xperps:
            parsed = _okx_unified(inst["instId"])
            if parsed is None:
                continue
            base, symbol = parsed
            tick = tickers.get(inst["instId"], {})
            # dif D1: OKX vol24h is in CONTRACTS (ctVal base units each),
            # NOT USD. USD volume = volCcy24h * last (fallback vol24h*ctVal*last).
            try:
                vol_ccy = float(tick.get("volCcy24h", 0) or 0)
                last = float(tick.get("last", 0) or 0)
                ct_val = float(inst.get("ctVal", 0) or 0)
                vol_ct = float(tick.get("vol24h", 0) or 0)
                volume = vol_ccy * last if vol_ccy > 0 else vol_ct * ct_val * last
            except (TypeError, ValueError):
                volume = 0.0
            # cheap filters first: funding history costs one request each
            if volume < min_volume_usd or (bases is not None and base not in bases):
                continue
            mean = pct_pos = std = None
            try:
                async with sess.get(
                    f"{OKX_HOST}/api/v5/public/funding-rate-history",
                    params={"instId": inst["instId"], "limit": str(history_limit)},
                ) as r:
                    hist = (await r.json()).get("data", [])
                rates = [float(h["fundingRate"]) for h in hist]
                if rates:
                    mean = statistics.fmean(rates)
                    pct_pos = sum(1 for x in rates if x > 0) / len(rates)
                    std = statistics.pstdev(rates) if len(rates) > 1 else 0.0
            except Exception:
                pass  # no history -> metric stays None -> fails the cut
            out.append(RawPerpMetrics(
                exchange="okx", symbol=symbol, base=base,
                volume_24h_usd=volume, funding_mean=mean,
                funding_pct_positive=pct_pos, funding_std=std,
                funding_interval_h=8.0,
            ))
    return out


def _kraken_unified(raw_symbol: str) -> tuple[str, str] | None:
    """PF_XBTUSD -> (BTC, BTC/USD:USD). PF_ linear USD perps only: the old
    PI_ inverse perps no longer trade (verified 2026-11-13: 0 in v3)."""
    if not raw_symbol.startswith("PF_") or not raw_symbol.endswith("USD"):
        return None
    base = raw_symbol[3:-3]
    base = {"XBT": "BTC"}.get(base, base)
    return base, f"{base}/USD:USD"


async def fetch_krakenfutures_metrics(history_limit: int = 720,
                                      min_volume_usd: float = 0.0,
                                      bases: set[str] | None = None) -> list[RawPerpMetrics]:
    out: list[RawPerpMetrics] = []
    async with aiohttp.ClientSession() as sess:
        async with sess.get(f"{KRAKEN_FUT}/v3/tickers") as r:
            if r.status != 200:
                logger.error("kraken tickers HTTP %s - venue skipped this sweep", r.status)
                return out
            tickers = (await r.json()).get("tickers", [])
        for t in tickers:
            parsed = _kraken_unified(t.get("symbol", ""))
            if parsed is None:
                continue
            base, symbol = parsed
            try:
                # kraken futures vol24h is in BASE units (BTC, PEPE...): convert
                volume = float(t.get("vol24h", 0) or 0) * float(t.get("last", 0) or 0)
            except (TypeError, ValueError):
                volume = 0.0
            if volume < min_volume_usd or (bases is not None and base not in bases):
                continue
            mean = pct_pos = std = None
            try:
                async with sess.get(
                    f"{KRAKEN_FUT}/v4/historicalfundingrates",
                    params={"symbol": t["symbol"]},
                ) as r:
                    # v4 returns the FULL history ascending: take the LATEST
                    # window, not the oldest (a year-old mean quarantined ETH
                    # against the live polled mean)
                    hist = (await r.json()).get("rates", [])[-history_limit:]
                # relativeFundingRate is the rate; fundingRate is absolute USD
                rates = [float(h["relativeFundingRate"]) for h in hist]
                if rates:
                    mean = statistics.fmean(rates)
                    pct_pos = sum(1 for x in rates if x > 0) / len(rates)
                    std = statistics.pstdev(rates) if len(rates) > 1 else 0.0
            except Exception:
                pass
            out.append(RawPerpMetrics(
                exchange="krakenfutures", symbol=symbol, base=base,
                volume_24h_usd=volume, funding_mean=mean,
                funding_pct_positive=pct_pos, funding_std=std,
                funding_interval_h=1.0,  # PF perps pay funding hourly
            ))
    return out


async def fetch_kraken_spot_bases() -> set[str]:
    """Base assets with an active <BASE>/USD spot market on Kraken."""
    ex = ccxt.kraken({"enableRateLimit": True})
    try:
        markets = await ex.fetch_markets()
        return {
            m["base"] for m in markets
            if m.get("spot") and m.get("active") and m.get("quote") == "USD"
        }
    finally:
        await ex.close()
