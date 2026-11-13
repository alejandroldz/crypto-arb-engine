"""TickStore is used from the event loop AND worker threads (funding_history
via asyncio.to_thread): concurrent access must never raise ResourceInUse."""
import threading
import time
from datetime import datetime, timezone

from app.connectors.normalizer import FundingRate, L2OrderBook, OrderBookLevel
from app.database.duckdb_store import TickStore


def _book(i):
    return L2OrderBook(
        exchange="okx", symbol="BTC/USD:USD-310404",
        bids=(OrderBookLevel(price=str(100 - i % 3), qty="1"),),
        asks=(OrderBookLevel(price=str(101 + i % 3), qty="1"),),
        local_ts_ns=time.monotonic_ns(),
    )


def test_concurrent_reads_and_flushes_never_race(tmp_path):
    store = TickStore(str(tmp_path / "t.duckdb"))
    errors = []

    def writer():
        try:
            for i in range(600):  # 3 buffer flushes
                store.record_book(_book(i))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def reader():
        try:
            for _ in range(60):
                store.funding_history("okx", "BTC/USD:USD-310404")
                store.funding_max_ts()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def funder():
        try:
            for i in range(40):
                store.record_funding(FundingRate(
                    exchange="okx", symbol="BTC/USD:USD-310404",
                    rate=__import__("decimal").Decimal("0.0001") + i,
                    local_ts_ns=time.monotonic_ns(),
                ))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=f) for f in (writer, writer, reader, reader, funder)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    store.close()
    assert errors == []
