# crypto-arb-engine

Quantitative crypto arbitrage & delta-neutral funding-rate engine.
Spain/EU compliant, zero-cost stack, paper trading first.

## Hard rules

- `decimal.Decimal` only for financial math. `float` is rejected at the
  boundary and linted out of `app/engines/` (see `tests/test_no_floats.py`).
- VWAP from walking real L2 order book levels. Never midpoint or top-of-book
  prices for decisions.
- `LIVE_TRADING_ENABLED=false` by default; the execution engine refuses to
  run without it. Paper trading simulates realistic latency first.
- Worst-case economics: 100% taker fees on every leg (entry AND exit) plus a
  slippage buffer. Approval requires `net_profit > MIN_NET_PROFIT_USD`.

## Strategy

Cash & carry / funding-rate arbitrage only: buy spot + short the perpetual
of equal notional (same or different exchange), delta-neutral, collecting
positive funding. Entry/exit spread, maker/taker fees, estimated funding
payout and break-even holding period are computed deterministically.

## Layout

- `app/core/` config, decimal helpers, circuit breaker, event bus
- `app/connectors/` ccxt async wrapper + canonical normalizer
- `app/engines/` liquidity, arbitrage, validator, risk, paper_trader, execution
- `app/database/` SQLAlchemy models/repository (SQLite) + DuckDB tick store
- `app/ui/` rich CLI dashboard + Telegram notifier
- `tests/` unit + Hypothesis property tests

## Exchange notes

Defaults: Kraken (spot), Deribit + Kraken Futures (perps) - all reachable
from Spain/EU on free public endpoints, verified live from this repo's test
runs. Bybit geo-blocks some jurisdictions at the CDN level; enable it only
after confirming reachability from your own IP (`PERP_EXCHANGES`).

## Quickstart

```bash
uv venv --python 3.12 && uv pip install -e '.[dev]'
pytest
cp .env.example .env   # review thresholds
python -m app.main     # paper trading dashboard
```
