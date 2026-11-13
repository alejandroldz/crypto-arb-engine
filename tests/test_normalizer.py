"""Normalizer tests: ccxt payloads -> canonical models (the 3am bug class)."""
import time

from app.connectors.ccxt_manager import normalize_book, normalize_funding


def test_kraken_three_element_levels():
    raw = {
        "bids": [[63599.2, 1.5, 1731570000], [63599.0, 0.5, 1731570001]],
        "asks": [[63599.3, 2.0, 1731570000], [63600.1, 1.0, 1731570001]],
        "timestamp": 1731570000000,
        "nonce": None,
    }
    book = normalize_book("kraken", "BTC/USD", raw)
    assert book.best_bid.price and str(book.best_bid.price).startswith("63599.2")
    assert len(book.bids) == 2 and len(book.asks) == 2
    assert book.exchange_ts is not None


def test_string_number_parsing_has_no_float_artifacts():
    raw = {
        "bids": [[0.1, 0.2]],
        "asks": [["0.3", "0.4"]],
        "timestamp": None,
    }
    book = normalize_book("test", "X/USD", raw)
    # str(0.1) is "0.1" in py3 repr; Decimal from it is exact
    assert book.bids[0].price.__class__.__name__ == "Decimal"
    assert book.asks[0].qty.__class__.__name__ == "Decimal"


def test_missing_timestamp_ok():
    raw = {"bids": [["100", "1"]], "asks": [["101", "1"]]}
    book = normalize_book("test", "X/USD", raw)
    assert book.exchange_ts is None
    assert book.age_ms(book.local_ts_ns) == 0


def test_normalize_funding_with_and_without_predicted():
    raw = {
        "fundingRate": "0.0001",
        "nextFundingTimestamp": 1731570000000,
        "predictedRate": "-0.0002",
    }
    rate = normalize_funding("okx", "BTC/USD:USD-310404", raw)
    assert rate.rate and rate.predicted_rate < 0
    raw2 = {"fundingRate": "0.0001"}
    rate2 = normalize_funding("okx", "BTC/USD:USD-310404", raw2)
    assert rate2.predicted_rate is None and rate2.next_funding_ts is None


def test_local_timestamp_is_monotonic_ns():
    before = time.monotonic_ns()
    raw = {"bids": [["100", "1"]], "asks": [["101", "1"]]}
    book = normalize_book("t", "X/USD", raw)
    assert before <= book.local_ts_ns <= time.monotonic_ns()


def test_funding_interval_carried_from_payload():
    """F14: Kraken '1h' interval must not become the 8h default."""
    from app.connectors.ccxt_manager import normalize_funding

    r = normalize_funding("krakenfutures", "BTC/USD:USD",
                          {"fundingRate": "0.0001", "interval": "1h"})
    assert r.interval_hours == 1
    r8 = normalize_funding("okx", "BTC/USD:USD-310404",
                           {"fundingRate": "0.0001", "interval": "8h"})
    assert r8.interval_hours == 8
    r_default = normalize_funding("okx", "BTC/USD:USD-310404", {"fundingRate": "0.0001"})
    assert r_default.interval_hours == 8
