"""fuzz F23: VWAP never over-fills, even with >28-digit pathological sizes."""
from decimal import Decimal

from app.connectors.normalizer import L2OrderBook, OrderBookLevel
from app.engines.liquidity import vwap_for_base, vwap_for_quote


def _book(levels):
    return L2OrderBook(
        exchange="t", symbol="X/USD",
        bids=tuple(levels), asks=tuple(levels), local_ts_ns=1,
    )


def test_f23_no_overfill_pathological_precision():
    levels = [
        OrderBookLevel(price="1.0000000000000000000000000001", qty="2.0000000000000000000000000002"),
        OrderBookLevel(price="1.0000000000000000000000000003", qty="2.0000000000000000000000000004"),
    ]
    size = Decimal("3.0000000000000000000000000005")  # >28 significant digits
    r = vwap_for_quote(_book(levels), "sell", size, Decimal("1"))
    assert r.filled_quote <= size
    rb = vwap_for_base(_book(levels), "sell", size, Decimal("1"))
    assert rb.base_qty <= size
