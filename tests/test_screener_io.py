"""Screener connectors: the Kraken v4 funding history is ASCENDING and
unbounded - the metrics window must be the LATEST N, never the oldest."""
import statistics

import pytest

from app.connectors import screener_io


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload


class _Session:
    def __init__(self, tickers, hist):
        self._tickers, self._hist = tickers, hist

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, params=None):
        if "tickers" in url:
            return _Resp({"tickers": self._tickers})
        if "historicalfundingrates" in url:
            return _Resp({"rates": self._hist})
        raise AssertionError(url)


@pytest.mark.asyncio
async def test_kraken_history_window_is_latest_not_oldest(monkeypatch):
    tickers = [{"symbol": "PF_ETHUSD", "vol24h": "1000", "last": "3000"}]
    # ascending full history: old rates strongly positive, recent negative
    hist = ([{"relativeFundingRate": "0.001"}] * 500
            + [{"relativeFundingRate": "-0.001"}] * 500)
    monkeypatch.setattr(screener_io.aiohttp, "ClientSession",
                        lambda: _Session(tickers, hist))
    (m,) = await screener_io.fetch_krakenfutures_metrics(history_limit=720)
    latest = [-0.001] * 500 + [0.001] * 220  # the LAST 720 of the list
    assert m.funding_mean == pytest.approx(statistics.fmean(latest))
    # the stale-window bug gave the mean of the FIRST 720 (mostly +0.001)
    assert m.funding_mean < 0
