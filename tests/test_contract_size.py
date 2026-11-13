"""Derivative book amounts arrive in the venue's native unit (contracts or
USD), not base currency. normalize_book must convert to base units or VWAP
sizing is wrong by orders of magnitude (okx linear ctVal 0.0001 BTC,
deribit inverse $10/contract)."""
from decimal import Decimal

from app.connectors.ccxt_manager import normalize_book

RAW = {
    "bids": [[69000.0, 1000.0, 0]],
    "asks": [[69001.0, 500.0, 0]],
    "timestamp": None,
    "nonce": None,
}


def test_linear_contracts_convert_with_contract_size():
    # okx BTC-USD_UM_XPERP-310404: linear, 1 contract = 0.0001 BTC
    market = {"contract": True, "linear": True, "contractSize": 0.0001}
    book = normalize_book("okx", "BTC/USD:USD-310404", RAW, market)
    assert book.best_bid.qty == Decimal("0.1")  # 1000 * 0.0001
    assert book.best_ask.qty == Decimal("0.05")


def test_inverse_contracts_convert_with_price():
    # deribit BTC-PERPETUAL: inverse, 1 contract = 10 USD
    market = {"contract": True, "linear": False, "contractSize": 10}
    book = normalize_book("deribit", "BTC-PERPETUAL", RAW, market)
    expected = Decimal("10000") / Decimal("69000")
    assert abs(book.best_bid.qty - expected) < Decimal("1e-12")


def test_unit_contract_and_spot_pass_through():
    # krakenfutures: contractSize 1 (amounts already base units)
    market = {"contract": True, "linear": True, "contractSize": 1}
    book = normalize_book("krakenfutures", "BTC/USD:USD", RAW, market)
    assert book.best_bid.qty == Decimal("1000")
    # no market metadata (spot): unchanged
    book = normalize_book("kraken", "BTC/USD", RAW)
    assert book.best_bid.qty == Decimal("1000")
