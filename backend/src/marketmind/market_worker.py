import argparse
import asyncio
import signal
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from marketmind.alerts import emit_alert
from marketmind.binance_market import BinanceMarketClient, BinanceMarketError, MarketBar, MarketQuote
from marketmind.config import get_settings
from marketmind.db import make_engine
from marketmind.market_service import backfill_crypto, seed_instruments, store_event, store_quote
from marketmind.models import MarketSyncRun

_QUEUE_SIZE = 5000
_FLUSH_SECONDS = 1.0


def backfill() -> dict[str, Any]:
    settings = get_settings()
    engine = make_engine(settings)
    try:
        with Session(engine) as db:
            run = MarketSyncRun(mode="backfill", status="running", statistics={})
            db.add(run)
            db.commit()
            try:
                with BinanceMarketClient(settings) as client:
                    result = backfill_crypto(db, settings, client)
            except Exception as exc:
                run.status = "failed"
                run.error_code = type(exc).__name__
                run.finished_at = datetime.now(UTC)
                db.commit()
                raise
            run.status = "succeeded"
            run.statistics = result
            run.finished_at = datetime.now(UTC)
            run.heartbeat_at = run.finished_at
            db.commit()
            return result
    finally:
        engine.dispose()


async def _stream_forever(
    client: BinanceMarketClient,
    asset_class: str,
    symbols: tuple[str, ...],
    queue: asyncio.Queue[MarketQuote | MarketBar],
    stop: asyncio.Event,
) -> None:
    delay = 1
    while not stop.is_set():
        try:
            async for event in client.stream(asset_class, symbols):  # type: ignore[arg-type]
                await queue.put(event)
                delay = 1
                if stop.is_set():
                    break
        except BinanceMarketError:
            await emit_alert(None, "binance_market_unavailable", "")
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                delay = min(delay * 2, 60)
        else:
            break


async def _poll_quotes(
    client: BinanceMarketClient,
    settings,
    instruments,
    engine,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        failed = False
        with Session(engine) as db:
            try:
                for symbol in settings.crypto_symbols:
                    store_quote(db, instruments[symbol], await asyncio.to_thread(client.spot_quote, symbol))
                if settings.binance_api_key and settings.binance_api_key.get_secret_value():
                    for symbol in settings.stock_symbols:
                        store_quote(db, instruments[symbol], await asyncio.to_thread(client.stock_quote, symbol))
                db.commit()
            except BinanceMarketError:
                db.rollback()
                failed = True
        if failed:
            await emit_alert(None, "binance_market_unavailable", "")
        else:
            await emit_alert(None, "binance_market_unavailable", "", resolved=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.binance_sync_interval_seconds)
        except TimeoutError:
            pass


async def _consume(
    queue: asyncio.Queue[MarketQuote | MarketBar],
    instruments,
    engine,
    run_id,
    stop: asyncio.Event,
) -> None:
    quotes = bars = 0
    while not stop.is_set() or not queue.empty():
        batch: list[MarketQuote | MarketBar] = []
        try:
            first = await asyncio.wait_for(queue.get(), timeout=_FLUSH_SECONDS)
            batch.append(first)
        except TimeoutError:
            continue
        while len(batch) < 500 and not queue.empty():
            batch.append(queue.get_nowait())
        with Session(engine) as db:
            for event in batch:
                added_quotes, added_bars = store_event(db, instruments, event)
                quotes += added_quotes
                bars += added_bars
            run = db.get(MarketSyncRun, run_id)
            if run is not None:
                run.heartbeat_at = datetime.now(UTC)
                run.statistics = {"quotes": quotes, "bars": bars, "queue_depth": queue.qsize()}
            db.commit()
        for _ in batch:
            queue.task_done()


async def serve() -> None:
    settings = get_settings()
    engine = make_engine(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)

    with Session(engine) as db:
        seeded = seed_instruments(db, settings)
        instrument_ids = {symbol: instrument.id for symbol, instrument in seeded.items()}
        run = MarketSyncRun(mode="stream", status="running", statistics={})
        db.add(run)
        db.flush()
        run_id = run.id
        db.commit()

    queue: asyncio.Queue[MarketQuote | MarketBar] = asyncio.Queue(maxsize=_QUEUE_SIZE)
    client = BinanceMarketClient(settings)
    tasks = [
        asyncio.create_task(_stream_forever(client, "crypto", settings.crypto_symbols, queue, stop)),
        asyncio.create_task(_poll_quotes(client, settings, instrument_ids, engine, stop)),
        asyncio.create_task(_consume(queue, instrument_ids, engine, run_id, stop)),
    ]
    if settings.binance_api_key and settings.binance_api_key.get_secret_value():
        tasks.append(asyncio.create_task(_stream_forever(client, "equity", settings.stock_symbols, queue, stop)))
    signal_wait = asyncio.create_task(stop.wait())
    failure: BaseException | None = None
    try:
        done, _ = await asyncio.wait([*tasks, signal_wait], return_when=asyncio.FIRST_COMPLETED)
        if signal_wait not in done:
            completed = next(iter(done))
            completed.result()
            raise RuntimeError("market_worker_task_stopped")
        await queue.join()
    except BaseException as exc:
        failure = exc
        raise
    finally:
        stop.set()
        signal_wait.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(signal_wait, *tasks, return_exceptions=True)
        client.close()
        with Session(engine) as db:
            run = db.get(MarketSyncRun, run_id)
            if run is not None:
                run.status = "failed" if failure is not None else "interrupted"
                run.error_code = type(failure).__name__ if failure is not None else None
                run.finished_at = datetime.now(UTC)
                run.heartbeat_at = run.finished_at
                db.commit()
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="MarketMind Binance market data worker")
    parser.add_argument("command", choices=("backfill", "serve"))
    args = parser.parse_args()
    if args.command == "backfill":
        print(backfill())
    else:
        asyncio.run(serve())


if __name__ == "__main__":
    main()
