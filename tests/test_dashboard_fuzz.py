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


def test_f21_review_required_string_does_not_block_all(tmp_path):
    from app.core.config import Settings
    from app.engines.fee_oracle import FeeOracle
    path = tmp_path / "fees.json"
    path.write_text('{"venues": {}, "review_required": "kraken"}')
    oracle = FeeOracle(Settings(), state_path=str(path))
    assert oracle._load_state() == {}  # malformed -> ignored, not char-set
    path.write_text('{"venues": [1,2], "review_required": []}')
    assert oracle._load_state() == {}
    path.write_text('{"venues": {"kraken": {"maker_bps": "2", "taker_bps": "5", "ts": "x"}}, "review_required": ["okx"]}')
    assert oracle._load_state()["review_required"] == ["okx"]


def test_h10_fee_state_save_is_atomic(tmp_path):
    import json

    from app.core.config import Settings
    from app.engines.fee_oracle import FeeOracle
    path = tmp_path / "fees.json"
    oracle = FeeOracle(Settings(), state_path=str(path))
    oracle._save_state({"venues": {}, "review_required": []})
    assert json.loads(path.read_text()) == {"venues": {}, "review_required": []}
    assert not (tmp_path / "fees.json.tmp").exists()


def test_f40_fee_cache_entry_wrong_type_ignored(tmp_path):
    import asyncio

    from app.core.config import Settings
    from app.engines.fee_oracle import FeeOracle
    path = tmp_path / "fees.json"
    path.write_text('{"venues": {"okx": "nulo"}, "review_required": []}')
    settings = Settings(
        spot_exchanges="kraken", perp_exchanges="okx",
        default_taker_fee_bps=Decimal("5"),
    )
    oracle = FeeOracle(settings, state_path=str(path))

    async def dead():
        raise ConnectionError("down")

    out = asyncio.run(oracle.refresh({"okx": dead}))
    assert out["okx"].source == "config"  # falls back, never crashes


def test_f41_vwap_negative_size_zero_fill():
    from app.connectors.normalizer import L2OrderBook, OrderBookLevel
    from app.engines.liquidity import vwap_for_quote
    book = L2OrderBook(
        exchange="t", symbol="X/USD",
        bids=(OrderBookLevel(price="100", qty="10"),),
        asks=(OrderBookLevel(price="101", qty="10"),),
        local_ts_ns=1,
    )
    r = vwap_for_quote(book, "sell", "-5", "0")
    assert r.filled_quote == 0 and not r.fully_filled and not r.executable


def test_f42_validator_fails_loud_on_nan():
    import pytest

    from app.engines.validator import funding_breakeven_intervals, funding_net_pnl
    with pytest.raises(ValueError, match="finite"):
        funding_net_pnl(
            notional=Decimal("1000"), funding_rate=Decimal("NaN"), intervals=5,
            spot_taker_fee_bps=Decimal("5"), perp_taker_fee_bps=Decimal("5"),
            slippage_buffer_bps=Decimal("5"),
        )
    with pytest.raises(ValueError, match="finite"):
        funding_breakeven_intervals(
            notional=Decimal("1000"), funding_rate=Decimal("Infinity"),
            spot_taker_fee_bps=Decimal("5"), perp_taker_fee_bps=Decimal("5"),
            slippage_buffer_bps=Decimal("5"),
        )


def test_f24_funding_log_survives_corrupt_csv(tmp_path, monkeypatch):
    import front.data as fd

    bad = tmp_path / "funding_log.csv"
    bad.write_text("exchange,symbol\nokx,BTC/USD:USD\n\x00\x01garbage")
    monkeypatch.setattr(fd, "FUNDING_CSV", bad)
    df = fd.funding_log()
    assert list(df.columns) == ["exchange", "symbol", "ts", "rate",
                                "predicted_rate"]
    assert df.empty


def test_f24_positions_survives_corrupt_sqlite(tmp_path, monkeypatch):
    import front.data as fd

    bad = tmp_path / "arb_state.db"
    bad.write_bytes(b"not a sqlite database at all" * 100)
    monkeypatch.setattr(fd, "SQLITE_PATH", bad)
    assert fd.positions().empty
