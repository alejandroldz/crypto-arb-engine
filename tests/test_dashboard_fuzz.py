"""fuzz F34-F39: dashboard escaping, oracle state, predicted-rate quarantine."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.connectors.ccxt_manager import normalize_funding
from app.core.circuit_breaker import CircuitBreaker
from app.ui.cli_dashboard import print_opportunity, render


def test_f34_predicted_rate_quarantined():
    raw = {"fundingRate": "0.0001", "nextFundingRate": "-0.5"}
    with pytest.raises(ValueError, match="funding"):
        normalize_funding("okx", "BTC/USD:USD-310404", raw)


def test_f35_hostile_funding_timestamps_synthesize():
    for bad in ("not-a-number", 1e30, [1], -1):
        rate = normalize_funding(
            "krakenfutures", "BTC/USD:USD",
            {"fundingRate": "0.0001", "fundingTimestamp": bad},
        )
        assert rate.next_funding_ts is not None  # synthesized, not a crash


def test_f38_render_escapes_network_strings():
    from app.connectors.normalizer import FundingRate, L2OrderBook, OrderBookLevel
    book = L2OrderBook(
        exchange="okx[/green]", symbol="BTC[/green]/USD:USD",
        bids=(OrderBookLevel(price="100", qty="1"),),
        asks=(OrderBookLevel(price="101", qty="1"),),
        local_ts_ns=1,
    )
    rate = FundingRate(
        exchange="okx[/green]", symbol="BTC/USD:USD-310404", rate="0.0001",
        local_ts_ns=1,
    )
    breaker = CircuitBreaker(max_consecutive_failures=3, max_daily_loss_usd=Decimal("25"))
    breaker.halt("[/] injected")
    from rich.console import Console
    table = render({"k": book}, {"k2": rate}, [], breaker, 0, Decimal("0"))
    console = Console(record=True, width=120)
    console.print(table)  # must not raise MarkupError
    out = console.export_text()
    assert "BTC" in out and "[/green]" in out  # neutralized, still visible


def test_f39_print_opportunity_escapes(capsys):
    from rich.console import Console

    from app.engines.arbitrage import FundingOpportunity
    opp = FundingOpportunity(
        exchange="okx[/green]", symbol="BTC[/green]/USD:USD",
        notional=Decimal("100"), funding_rate=Decimal("0.001"),
        holding_intervals=90, breakeven_intervals=3,
        projected_net=Decimal("1"),
    )
    print_opportunity(Console(file=None, force_terminal=False), opp)  # no raise


def test_f36_fees_state_wrong_shape_ignored(tmp_path):
    from app.core.config import Settings
    from app.engines.fee_oracle import FeeOracle
    (tmp_path / "fees.json").write_text("[]")
    oracle = FeeOracle(Settings(), state_path=str(tmp_path / "fees.json"))
    assert oracle._load_state() == {}
