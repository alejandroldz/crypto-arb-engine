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
        # dif D3: ccxt exposes the prediction as nextFundingRate; the old
        # 'predictedRate' key does not exist and must be ignored
        "nextFundingRate": "-0.0002",
        "predictedRate": "999",
    }
    rate = normalize_funding("okx", "BTC/USD:USD-310404", raw)
    assert rate.rate and rate.predicted_rate < 0
    raw2 = {"fundingRate": "0.0001"}
    rate2 = normalize_funding("okx", "BTC/USD:USD-310404", raw2)
    assert rate2.predicted_rate is None
    # dif D2: a venue with no timestamps gets a synthesized settlement
    # watermark so funding actually accrues (was None -> accrual dead)
    assert rate2.next_funding_ts is not None


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


def test_crossed_book_rejected_at_normalization():
    """F07: crossed/locked books never reach the engines."""
    import pytest

    from app.connectors.ccxt_manager import normalize_book

    with pytest.raises(ValueError, match="crossed"):
        normalize_book("kraken", "BTC/USD",
                       {"bids": [[101, 1]], "asks": [[100, 1]]})
    # locked (bid == ask) is rejected too
    with pytest.raises(ValueError, match="crossed"):
        normalize_book("kraken", "BTC/USD",
                       {"bids": [[100, 1]], "asks": [[100, 1]]})
    # sane book passes
    b = normalize_book("kraken", "BTC/USD", {"bids": [[99, 1]], "asks": [[100, 1]]})
    from app.core.decimals import D as _D
    assert b.best_bid.price == _D("99")


def test_next_settlement_ts_boundaries():
    from datetime import datetime, timezone

    from app.connectors.ccxt_manager import next_settlement_ts
    now = datetime(2026, 11, 13, 11, 30, tzinfo=timezone.utc)
    assert next_settlement_ts(now, 1) == datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    assert next_settlement_ts(now, 8) == datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc)
    on_boundary = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    assert next_settlement_ts(on_boundary, 1) == datetime(2026, 11, 13, 13, 0, tzinfo=timezone.utc)


def test_normalize_funding_prefers_funding_timestamp_over_next():
    from datetime import datetime, timezone

    raw = {
        "fundingRate": "0.0001",
        # dif D5: fundingTimestamp (next settlement) wins over
        # nextFundingTimestamp (the settlement after), so the watermark jump
        # detects missed payments
        "fundingTimestamp": 1731570000000,
        "nextFundingTimestamp": 1731598800000,
    }
    rate = normalize_funding("okx", "BTC/USD:USD-310404", raw)
    assert rate.next_funding_ts == datetime.fromtimestamp(1731570000, tz=timezone.utc)


def test_f19_order_book_level_magnitude_bounds():
    import pytest

    from app.connectors.normalizer import OrderBookLevel
    with pytest.raises(ValueError):
        OrderBookLevel(price="1e999999", qty="10")
    with pytest.raises(ValueError):
        OrderBookLevel(price="1e20", qty="1")
    with pytest.raises(ValueError):
        OrderBookLevel(price="1e19", qty="1e19")  # product overflows DECIMAL(38,18)
    ok = OrderBookLevel(price="50000", qty="0.5")
    assert ok.price * ok.qty == 25000


def test_f10b_order_book_level_bad_string_is_valueerror():
    import pytest

    from app.connectors.normalizer import OrderBookLevel
    for bad in ("..", "NaN", "abc"):
        with pytest.raises(ValueError):
            OrderBookLevel(price=bad, qty="1")


def test_f22_normalization_error_is_single_domain_exception():
    import pytest

    from app.connectors.ccxt_manager import NormalizationError, normalize_book, normalize_funding
    hostile_books = [
        {},  # missing bids/asks
        {"bids": None, "asks": []},
        {"bids": ["x"], "asks": []},
        {"bids": [[None, "1"]], "asks": [["2", "1"]]},
        {"bids": [["1", "1"]], "asks": [["2", "1"]], "timestamp": "huge" * 100},
    ]
    for raw in hostile_books:
        with pytest.raises(NormalizationError):
            normalize_book("okx", "X/USD", raw)
    for raw in ({"fundingRate": ".."}, {"fundingRate": None, "nextFundingRate": "x"}):
        with pytest.raises(NormalizationError):
            normalize_funding("okx", "X/USD", raw)


def test_h8_book_levels_truncated_not_fatal():
    from app.connectors.ccxt_manager import MAX_BOOK_LEVELS, normalize_book
    raw = {
        "bids": [[str(100 - i * 0.01), "1"] for i in range(MAX_BOOK_LEVELS + 500)],
        "asks": [[str(101 + i * 0.01), "1"] for i in range(MAX_BOOK_LEVELS + 500)],
    }
    book = normalize_book("okx", "X/USD", raw)
    assert len(book.bids) == MAX_BOOK_LEVELS and len(book.asks) == MAX_BOOK_LEVELS
    assert book.bids[0].price == 100  # top of book preserved


def test_c6_empty_string_account_fields_are_zero():
    from app.connectors.ccxt_manager import _dec_or_zero
    from decimal import Decimal
    assert _dec_or_zero("") == Decimal("0")
    assert _dec_or_zero(None) == Decimal("0")
    assert _dec_or_zero("12.5") == Decimal("12.5")
