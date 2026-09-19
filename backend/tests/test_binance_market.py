from decimal import Decimal

import pytest

from marketmind.binance_market import BinanceMarketClient, BinanceMarketError, MarketBar, MarketQuote
from marketmind.config import Settings


def settings(**overrides):
    values = {
        "database_url": "postgresql+psycopg://user:password@localhost/marketmind_test",
        "redis_url": "redis://localhost/0",
        "cookie_secure": False,
        "binance_proxy_url": "socks5://127.0.0.1:12123",
    }
    values.update(overrides)
    return Settings(**values)


def test_spot_stream_events_are_normalized():
    quote = BinanceMarketClient._stream_event(
        "crypto",
        {"e": "24hrTicker", "E": 1_700_000_000_000, "s": "BTCUSDT", "b": "99", "a": "101", "c": "100"},
    )
    bar = BinanceMarketClient._stream_event(
        "crypto",
        {
            "e": "kline",
            "E": 1_700_000_000_000,
            "s": "BTCUSDT",
            "k": {
                "t": 1_700_000_000_000,
                "T": 1_700_000_299_999,
                "s": "BTCUSDT",
                "i": "5m",
                "o": "90",
                "h": "110",
                "l": "80",
                "c": "100",
                "v": "12.5",
                "n": 20,
                "x": True,
            },
        },
    )

    assert isinstance(quote, MarketQuote)
    assert quote.last == Decimal("100")
    assert isinstance(bar, MarketBar)
    assert bar.complete is True
    assert bar.volume == Decimal("12.5")


def test_stock_quote_requires_only_market_data_api_key():
    with BinanceMarketClient(settings()) as client:
        with pytest.raises(BinanceMarketError, match="binance_api_key_required_for_stocks"):
            client.stock_quote("AAPL")


def test_stream_url_keeps_stocks_uppercase_and_crypto_lowercase():
    with BinanceMarketClient(settings()) as client:
        crypto = client._stream_url("crypto", ("BTCUSDT",))
        stock = client._stream_url("equity", ("AAPL",))

    assert "btcusdt@ticker" in crypto
    assert "btcusdt@kline_5m" in crypto
    assert "AAPL@quote" in stock
    assert "AAPL@kline_1d" in stock
