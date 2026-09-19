import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

import httpx
import websockets

from marketmind.config import Settings

AssetClass = Literal["crypto", "equity", "etf"]
SUPPORTED_INTERVALS = ("5m", "1h", "1d")


class BinanceMarketError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MarketQuote:
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal
    event_at: datetime


@dataclass(frozen=True, slots=True)
class MarketBar:
    symbol: str
    interval: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trades: int | None
    complete: bool


def _utc(milliseconds: int | str) -> datetime:
    return datetime.fromtimestamp(int(milliseconds) / 1000, UTC)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _required_decimal(value: Any, field: str) -> Decimal:
    result = _decimal(value)
    if result is None:
        raise BinanceMarketError(f"missing_{field}")
    return result


class BinanceMarketClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.proxy = settings.binance_proxy_url.get_secret_value()
        headers = {"Accept": "application/json"}
        if settings.binance_api_key and settings.binance_api_key.get_secret_value():
            headers["X-MBX-APIKEY"] = settings.binance_api_key.get_secret_value()
        self.http = httpx.Client(
            base_url=settings.binance_rest_base_url.rstrip("/"),
            proxy=self.proxy,
            headers=headers,
            timeout=httpx.Timeout(15, connect=10),
            trust_env=False,
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "BinanceMarketClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self.http.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BinanceMarketError(type(exc).__name__) from exc

    def ping(self) -> None:
        self._get("/api/v3/ping")

    def spot_quote(self, symbol: str) -> MarketQuote:
        payload = self._get("/api/v3/ticker/24hr", {"symbol": symbol})
        return MarketQuote(
            symbol=symbol,
            bid=_decimal(payload.get("bidPrice")),
            ask=_decimal(payload.get("askPrice")),
            last=_required_decimal(payload.get("lastPrice"), "last_price"),
            event_at=datetime.now(UTC),
        )

    def stock_quote(self, symbol: str) -> MarketQuote:
        if not self.settings.binance_api_key or not self.settings.binance_api_key.get_secret_value():
            raise BinanceMarketError("binance_api_key_required_for_stocks")
        payload = self._get("/sapi/v1/equity/market/quote", {"symbol": symbol})
        if not isinstance(payload, dict):
            raise BinanceMarketError("invalid_stock_quote")
        bid = _decimal(payload.get("bp", payload.get("bidPrice")))
        ask = _decimal(payload.get("ap", payload.get("askPrice")))
        last = _decimal(payload.get("p", payload.get("price", payload.get("lastPrice"))))
        if last is None and bid is not None and ask is not None:
            last = (bid + ask) / 2
        if last is None:
            raise BinanceMarketError("missing_last_price")
        event_time = payload.get("T", payload.get("time", payload.get("timestamp")))
        return MarketQuote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            last=last,
            event_at=_utc(event_time) if event_time is not None else datetime.now(UTC),
        )

    def spot_klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        limit: int = 1000,
    ) -> list[MarketBar]:
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError("unsupported_interval")
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_at is not None:
            params["startTime"] = int(start_at.timestamp() * 1000)
        if end_at is not None:
            params["endTime"] = int(end_at.timestamp() * 1000)
        payload = self._get("/api/v3/klines", params)
        if not isinstance(payload, list):
            raise BinanceMarketError("invalid_spot_klines")
        return [
            MarketBar(
                symbol=symbol,
                interval=interval,
                open_time=_utc(item[0]),
                close_time=_utc(item[6]),
                open=Decimal(item[1]),
                high=Decimal(item[2]),
                low=Decimal(item[3]),
                close=Decimal(item[4]),
                volume=Decimal(item[5]),
                trades=int(item[8]),
                complete=_utc(item[6]) <= datetime.now(UTC),
            )
            for item in payload
        ]

    def _stream_url(self, asset_class: AssetClass, symbols: tuple[str, ...]) -> str:
        intervals = "/".join(
            f"{symbol.lower() if asset_class == 'crypto' else symbol}@kline_{interval}"
            for symbol in symbols
            for interval in SUPPORTED_INTERVALS
        )
        quotes = "/".join(
            f"{symbol.lower()}@ticker" if asset_class == "crypto" else f"{symbol}@quote" for symbol in symbols
        )
        base = self.settings.binance_spot_ws_url if asset_class == "crypto" else self.settings.binance_stocks_ws_url
        return f"{base.rstrip('/')}/stream?streams={quotes}/{intervals}"

    async def stream(self, asset_class: AssetClass, symbols: tuple[str, ...]) -> AsyncIterator[MarketQuote | MarketBar]:
        url = self._stream_url(asset_class, symbols)
        try:
            async with websockets.connect(
                url,
                proxy=self.proxy,
                open_timeout=15,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10,
                max_size=1_048_576,
            ) as socket:
                async for message in socket:
                    payload = json.loads(message)
                    data = payload.get("data", payload)
                    event = self._stream_event(asset_class, data)
                    if event is not None:
                        yield event
        except Exception as exc:
            raise BinanceMarketError(type(exc).__name__) from exc

    @staticmethod
    def _stream_event(asset_class: AssetClass, data: dict[str, Any]) -> MarketQuote | MarketBar | None:
        event_type = data.get("e")
        if asset_class == "crypto" and event_type == "24hrTicker":
            return MarketQuote(
                symbol=data["s"],
                bid=_decimal(data.get("b")),
                ask=_decimal(data.get("a")),
                last=_required_decimal(data.get("c"), "last_price"),
                event_at=_utc(data["E"]),
            )
        if asset_class != "crypto" and event_type == "quote":
            bid = _decimal(data.get("bp"))
            ask = _decimal(data.get("ap"))
            if bid is None or ask is None:
                return None
            return MarketQuote(
                symbol=data["s"],
                bid=bid,
                ask=ask,
                last=(bid + ask) / 2,
                event_at=_utc(data.get("T", data["E"])),
            )
        if event_type == "kline":
            item = data.get("k", data)
            return MarketBar(
                symbol=data.get("s", item.get("s")),
                interval=item.get("i", data.get("i")),
                open_time=_utc(item.get("t", data.get("t"))),
                close_time=_utc(item.get("T", data.get("T"))),
                open=_required_decimal(item.get("o"), "open"),
                high=_required_decimal(item.get("h"), "high"),
                low=_required_decimal(item.get("l"), "low"),
                close=_required_decimal(item.get("c"), "close"),
                volume=_required_decimal(item.get("v", 0), "volume"),
                trades=int(item["n"]) if item.get("n") is not None else None,
                complete=bool(item.get("x", data.get("x", False))),
            )
        return None
