# crypto-arb-engine

Quantitative crypto arbitrage & delta-neutral funding-rate engine.
Zero-cost stack, paper trading first.

Legal note: nothing here verifies retail eligibility or derivative
availability for Spain/EU residents. That is the user's responsibility
(check each venue's terms and your tax obligations) before any live use.

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

Defaults: Kraken spot `BTC/USD` against Kraken Futures `BTC/USD:USD` (and
Deribit `BTC-PERPETUAL`) - all USD-quoted, so the carry legs are exactly
delta-neutral with no EUR/USD conversion.

What was actually verified: public market-data endpoints (order books and
funding rates) are reachable from the build/test environment without API
keys for kraken, krakenfutures and deribit. This says nothing about retail
eligibility, derivative availability or account access from Spain/EU -
unverified, user's responsibility. Bybit geo-blocks some jurisdictions at
the CDN level (observed); enable it only after confirming reachability from
your own IP (`PERP_EXCHANGES`).

OKX EU (verified live 2026-11-13 against a real OKX Europe account):
EEA accounts trade **X-Perps** (`BTC-USD_UM_XPERP-310404`, ccxt
`BTC/USD:USD-310404`): 5-year linear USD-margined expiry futures with
perp-style 8h funding (00/08/16 UTC), ruleType `xperp`. The global inverse
swap `BTC/USD:BTC` is not the MiFID product. EU API keys live on host
`my.okx.com` (www.okx.com returns 50119 for them) - set `OKX_HOSTNAME`.
API trading of X-Perps requires passing the appropriateness assessment and
accepting the X-Perp risk disclosure in the OKX web/app first.

## UTA paper-mode assumption

The unified-margin simulation uses ONE `UtaAccountState` holding the full
paper balance and sizes every carry at the configured utilization,
regardless of which venue the short leg is on. That model is realistic only
when spot collateral and the short live in the same account (e.g. Kraken
spot + Kraken Futures multi-collateral, or OKX spot + OKX X-Perp in one
unified account). It does NOT model cross-venue collateral: BTC sitting on
Kraken cannot margin an OKX short in reality. Paper results at 90%
utilization therefore demonstrate single-venue UTA economics, not a
cross-venue 90% deployment. Cross-venue carries would need split collateral
and lower effective utilization.

## Quickstart

```bash
uv venv --python 3.12 && uv pip install -e '.[dev]'
pytest
cp .env.example .env   # review thresholds
python -m app.main     # paper trading dashboard
```
