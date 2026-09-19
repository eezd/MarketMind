from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from marketmind.binance_market import BinanceMarketClient
from marketmind.binance_market import MarketBar as IncomingBar
from marketmind.binance_market import MarketQuote as IncomingQuote
from marketmind.config import Settings
from marketmind.models import MarketBar, MarketInstrument, MarketQuote

_BACKFILL_WINDOWS = {
    "5m": timedelta(days=7),
    "1h": timedelta(days=90),
    "1d": timedelta(days=365 * 3),
}
_ETFS = frozenset({"SPY", "QQQ"})


def seed_instruments(db: Session, settings: Settings) -> dict[str, MarketInstrument]:
    instruments = db.scalars(select(MarketInstrument).where(MarketInstrument.provider == "binance"))
    existing = {item.symbol: item for item in instruments}
    now = datetime.now(UTC)
    specifications = [(symbol, "crypto", symbol.removesuffix("USDT"), "USDT") for symbol in settings.crypto_symbols] + [
        (symbol, "etf" if symbol in _ETFS else "equity", symbol, "USD") for symbol in settings.stock_symbols
    ]
    for symbol, asset_class, base_asset, quote_asset in specifications:
        item = existing.get(symbol)
        if item is None:
            item = MarketInstrument(
                provider="binance",
                symbol=symbol,
                asset_class=asset_class,
                base_asset=base_asset,
                quote_asset=quote_asset,
                metadata_json={
                    "history_mode": "rest_backfill" if asset_class == "crypto" else "stream_from_enabled_at",
                },
            )
            db.add(item)
            existing[symbol] = item
        else:
            item.asset_class = asset_class
            item.base_asset = base_asset
            item.quote_asset = quote_asset
            item.enabled = True
            item.updated_at = now
    configured = {symbol for symbol, *_ in specifications}
    for symbol, item in existing.items():
        if symbol not in configured:
            item.enabled = False
            item.updated_at = now
    db.flush()
    return {symbol: item for symbol, item in existing.items() if symbol in configured}


def store_quote(db: Session, instrument: MarketInstrument | UUID, incoming: IncomingQuote) -> None:
    instrument_id = instrument.id if isinstance(instrument, MarketInstrument) else instrument
    statement = insert(MarketQuote).values(
        instrument_id=instrument_id,
        bid=incoming.bid,
        ask=incoming.ask,
        last=incoming.last,
        event_at=incoming.event_at,
        observed_at=datetime.now(UTC),
    )
    db.execute(
        statement.on_conflict_do_update(
            index_elements=[MarketQuote.instrument_id],
            set_={
                "bid": statement.excluded.bid,
                "ask": statement.excluded.ask,
                "last": statement.excluded.last,
                "event_at": statement.excluded.event_at,
                "observed_at": statement.excluded.observed_at,
            },
            where=statement.excluded.event_at >= MarketQuote.event_at,
        )
    )


def store_bars(db: Session, instrument: MarketInstrument | UUID, bars: list[IncomingBar]) -> int:
    if not bars:
        return 0
    instrument_id = instrument.id if isinstance(instrument, MarketInstrument) else instrument
    values = [
        {
            "instrument_id": instrument_id,
            "interval": bar.interval,
            "open_time": bar.open_time,
            "close_time": bar.close_time,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "trades": bar.trades,
            "complete": bar.complete,
            "observed_at": datetime.now(UTC),
        }
        for bar in bars
    ]
    statement = insert(MarketBar).values(values)
    db.execute(
        statement.on_conflict_do_update(
            index_elements=[MarketBar.instrument_id, MarketBar.interval, MarketBar.open_time],
            set_={
                "close_time": statement.excluded.close_time,
                "open": statement.excluded.open,
                "high": statement.excluded.high,
                "low": statement.excluded.low,
                "close": statement.excluded.close,
                "volume": statement.excluded.volume,
                "trades": statement.excluded.trades,
                "complete": statement.excluded.complete,
                "observed_at": statement.excluded.observed_at,
            },
        )
    )
    return len(values)


def store_event(
    db: Session,
    instruments: dict[str, MarketInstrument | UUID],
    event: IncomingQuote | IncomingBar,
) -> tuple[int, int]:
    instrument = instruments.get(event.symbol)
    if instrument is None:
        return 0, 0
    if isinstance(event, IncomingQuote):
        store_quote(db, instrument, event)
        return 1, 0
    return 0, store_bars(db, instrument, [event])


def backfill_crypto(db: Session, settings: Settings, client: BinanceMarketClient) -> dict[str, Any]:
    instruments = seed_instruments(db, settings)
    db.commit()
    now = datetime.now(UTC)
    bars_written = 0
    quote_count = 0
    by_symbol: dict[str, int] = {}
    for symbol in settings.crypto_symbols:
        instrument = instruments[symbol]
        quote = client.spot_quote(symbol)
        store_quote(db, instrument, quote)
        quote_count += 1
        symbol_bars = 0
        for interval, window in _BACKFILL_WINDOWS.items():
            latest = db.scalar(
                select(func.max(MarketBar.open_time)).where(
                    MarketBar.instrument_id == instrument.id,
                    MarketBar.interval == interval,
                )
            )
            cursor = latest + timedelta(milliseconds=1) if latest else now - window
            while cursor < now:
                batch = client.spot_klines(symbol, interval, start_at=cursor, end_at=now, limit=1000)
                if not batch:
                    break
                symbol_bars += store_bars(db, instrument, batch)
                db.commit()
                next_cursor = batch[-1].close_time + timedelta(milliseconds=1)
                if next_cursor <= cursor or len(batch) < 1000:
                    break
                cursor = next_cursor
        first_bar = db.scalar(select(func.min(MarketBar.open_time)).where(MarketBar.instrument_id == instrument.id))
        if first_bar is not None:
            instrument.history_start_at = first_bar
        by_symbol[symbol] = symbol_bars
        bars_written += symbol_bars
        db.commit()
    return {"quotes": quote_count, "bars": bars_written, "by_symbol": by_symbol}


def decimal_or_none(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None
