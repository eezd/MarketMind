from types import SimpleNamespace

from marketmind.market_indicators import technical_snapshot


def test_technical_snapshot_calculates_momentum_and_risk_metrics():
    bars = [
        SimpleNamespace(
            open=100 + index,
            high=102 + index,
            low=99 + index,
            close=101 + index,
        )
        for index in range(40)
    ]

    result = technical_snapshot(bars)

    assert result.return_1 is not None and result.return_1 > 0
    assert result.ema_12 is not None and result.ema_26 is not None
    assert result.ema_12 > result.ema_26
    assert result.rsi_14 == 100
    assert result.atr_14 is not None and result.atr_14 > 0
    assert result.volatility_20 is not None
    assert result.direction == "bullish"
    assert result.signal_strength > 0


def test_technical_snapshot_handles_missing_history_explicitly():
    result = technical_snapshot([])

    assert result.direction == "neutral"
    assert result.signal_strength == 0
    assert result.rsi_14 is None
