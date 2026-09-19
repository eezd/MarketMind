import math
from dataclasses import dataclass
from statistics import pstdev
from typing import Protocol


class BarLike(Protocol):
    open: object
    high: object
    low: object
    close: object


@dataclass(frozen=True, slots=True)
class TechnicalSnapshot:
    return_1: float | None
    ema_12: float | None
    ema_26: float | None
    rsi_14: float | None
    macd: float | None
    macd_signal: float | None
    atr_14: float | None
    volatility_20: float | None
    score: float
    direction: str
    signal_strength: int


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    changes = [current - previous for previous, current in zip(values, values[1:], strict=False)]
    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
    if average_loss == 0:
        return 100.0
    return 100 - 100 / (1 + average_gain / average_loss)


def _atr(bars: list[BarLike], period: int = 14) -> float | None:
    if len(bars) <= period:
        return None
    ranges = []
    previous_close = float(bars[0].close)
    for bar in bars[1:]:
        high = float(bar.high)
        low = float(bar.low)
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = float(bar.close)
    result = sum(ranges[:period]) / period
    for value in ranges[period:]:
        result = (result * (period - 1) + value) / period
    return result


def technical_snapshot(bars: list[BarLike]) -> TechnicalSnapshot:
    closes = [float(bar.close) for bar in bars]
    if not closes:
        return TechnicalSnapshot(None, None, None, None, None, None, None, None, 0, "neutral", 0)
    ema_12_values = _ema(closes, 12)
    ema_26_values = _ema(closes, 26)
    ema_12 = ema_12_values[-1]
    ema_26 = ema_26_values[-1]
    macd_values = [fast - slow for fast, slow in zip(ema_12_values, ema_26_values, strict=True)]
    macd = macd_values[-1]
    macd_signal = _ema(macd_values, 9)[-1]
    current = closes[-1]
    return_1 = (current / closes[-2] - 1) if len(closes) > 1 and closes[-2] else None
    returns = [math.log(current / previous) for previous, current in zip(closes, closes[1:], strict=False) if previous]
    volatility = pstdev(returns[-20:]) if len(returns) >= 2 else None
    rsi = _rsi(closes)
    atr = _atr(bars)

    ema_component = max(-1.0, min(1.0, ((ema_12 - ema_26) / current) * 100)) if current else 0
    rsi_component = max(-1.0, min(1.0, ((rsi or 50) - 50) / 30))
    macd_component = max(-1.0, min(1.0, ((macd - macd_signal) / current) * 200)) if current else 0
    score = round(ema_component * 0.45 + rsi_component * 0.3 + macd_component * 0.25, 4)
    direction = "bullish" if score >= 0.15 else "bearish" if score <= -0.15 else "neutral"
    available = sum(value is not None for value in (rsi, atr, volatility)) / 3
    signal_strength = round(min(100, abs(score) * 75 + available * 25))
    return TechnicalSnapshot(
        return_1=return_1,
        ema_12=ema_12,
        ema_26=ema_26,
        rsi_14=rsi,
        macd=macd,
        macd_signal=macd_signal,
        atr_14=atr,
        volatility_20=volatility,
        score=score,
        direction=direction,
        signal_strength=signal_strength,
    )
