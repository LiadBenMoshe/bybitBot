from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from pandas import isna

from models import Signal


@dataclass(slots=True)
class StrategyConfig:
    ema_fast: int = 12
    ema_slow: int = 26
    trend_ema: int = 200
    rsi_period: int = 14
    rsi_long_threshold: float = 55
    rsi_short_threshold: float = 45
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_period: int = 14
    atr_min_pct: float = 0.003
    signal_score_threshold: int = 3


class IndicatorStrategy:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def apply_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        df = frame.copy()
        df["ema_fast"] = df["close"].ewm(span=self.config.ema_fast, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=self.config.ema_slow, adjust=False).mean()
        df["trend_ema"] = df["close"].ewm(span=self.config.trend_ema, adjust=False).mean()

        delta = df["close"].diff()
        gain = delta.clip(lower=0).rolling(self.config.rsi_period).mean()
        loss = (-delta.clip(upper=0)).rolling(self.config.rsi_period).mean()
        rs = gain / loss.replace(0, pd.NA)
        df["rsi"] = 100 - (100 / (1 + rs))

        macd_fast = df["close"].ewm(span=self.config.macd_fast, adjust=False).mean()
        macd_slow = df["close"].ewm(span=self.config.macd_slow, adjust=False).mean()
        df["macd"] = macd_fast - macd_slow
        df["macd_signal"] = df["macd"].ewm(span=self.config.macd_signal, adjust=False).mean()
        df["macd_hist"] = df["macd"] - df["macd_signal"]

        prev_close = df["close"].shift(1)
        tr_components = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        )
        df["atr"] = tr_components.max(axis=1).rolling(self.config.atr_period).mean()
        df["atr_pct"] = df["atr"] / df["close"]
        return df

    def generate_signal(self, symbol: str, frame: pd.DataFrame) -> Signal:
        df = self.apply_indicators(frame)
        latest = df.iloc[-1]
        long_score = 0
        short_score = 0
        reasons: list[str] = []

        ema_fast = latest["ema_fast"]
        ema_slow = latest["ema_slow"]
        trend_ema = latest["trend_ema"]
        rsi = latest["rsi"]
        macd = latest["macd"]
        macd_signal = latest["macd_signal"]
        macd_hist = latest["macd_hist"]
        atr_pct = latest["atr_pct"]

        if not isna(ema_fast) and not isna(ema_slow) and ema_fast > ema_slow:
            long_score += 1
            reasons.append("EMA bullish")
        elif not isna(ema_fast) and not isna(ema_slow) and ema_fast < ema_slow:
            short_score += 1
            reasons.append("EMA bearish")

        if not isna(trend_ema):
            if latest["close"] > trend_ema:
                long_score += 1
                reasons.append("Trend filter up")
            elif latest["close"] < trend_ema:
                short_score += 1
                reasons.append("Trend filter down")

        if not isna(rsi) and rsi >= self.config.rsi_long_threshold:
            long_score += 1
            reasons.append("RSI strength")
        elif not isna(rsi) and rsi <= self.config.rsi_short_threshold:
            short_score += 1
            reasons.append("RSI weakness")

        if not isna(macd) and not isna(macd_signal) and not isna(macd_hist) and macd > macd_signal and macd_hist > 0:
            long_score += 1
            reasons.append("MACD bullish")
        elif not isna(macd) and not isna(macd_signal) and not isna(macd_hist) and macd < macd_signal and macd_hist < 0:
            short_score += 1
            reasons.append("MACD bearish")

        if isna(atr_pct) or atr_pct < self.config.atr_min_pct:
            return Signal(
                symbol=symbol,
                action="hold",
                price=float(latest["close"]),
                timestamp=latest["timestamp"].to_pydatetime(),
                confidence=0.0,
                reason="ATR filter blocked trade",
            )

        action = "hold"
        confidence = 0.0
        if long_score >= self.config.signal_score_threshold and long_score > short_score:
            action = "buy"
            confidence = long_score / 4
        elif short_score >= self.config.signal_score_threshold and short_score > long_score:
            action = "sell"
            confidence = short_score / 4

        return Signal(
            symbol=symbol,
            action=action,
            price=float(latest["close"]),
            timestamp=latest["timestamp"].to_pydatetime(),
            confidence=confidence,
            reason=", ".join(reasons[-4:]) or "No confluence",
        )
