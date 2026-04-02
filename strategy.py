from __future__ import annotations

from dataclasses import dataclass

import numpy as np
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
    adx_period: int = 14
    min_adx: float = 18.0
    volume_ma_period: int = 20
    min_volume_ratio: float = 1.05
    breakout_lookback: int = 20
    min_trend_strength_pct: float = 0.0015
    signal_score_threshold: int = 4
    extreme_entry_mode: bool = True
    min_expected_move_pct: float = 0.02
    min_signal_confidence: float = 0.8
    require_breakout_confirmation: bool = True


class IndicatorStrategy:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def apply_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        df = frame.copy()
        df["ema_fast"] = df["close"].ewm(span=self.config.ema_fast, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=self.config.ema_slow, adjust=False).mean()
        df["trend_ema"] = df["close"].ewm(span=self.config.trend_ema, adjust=False).mean()
        df["ema_spread_pct"] = (df["ema_fast"] - df["ema_slow"]).abs() / df["close"].replace(0, np.nan)

        delta = df["close"].diff()
        gain = delta.clip(lower=0).rolling(self.config.rsi_period).mean()
        loss = (-delta.clip(upper=0)).rolling(self.config.rsi_period).mean()
        rs = gain / loss.replace(0, np.nan)
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
        up_move = df["high"].diff()
        down_move = -df["low"].diff()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        tr = tr_components.max(axis=1).replace(0, np.nan)
        plus_di = 100 * plus_dm.ewm(alpha=1 / self.config.adx_period, adjust=False).mean() / tr.ewm(
            alpha=1 / self.config.adx_period, adjust=False
        ).mean()
        minus_di = 100 * minus_dm.ewm(alpha=1 / self.config.adx_period, adjust=False).mean() / tr.ewm(
            alpha=1 / self.config.adx_period, adjust=False
        ).mean()
        dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
        df["adx"] = dx.ewm(alpha=1 / self.config.adx_period, adjust=False).mean()
        df["volume_ma"] = df["volume"].rolling(self.config.volume_ma_period).mean()
        df["volume_ratio"] = df["volume"] / df["volume_ma"].replace(0, np.nan)
        df["breakout_high"] = df["high"].rolling(self.config.breakout_lookback).max().shift(1)
        df["breakout_low"] = df["low"].rolling(self.config.breakout_lookback).min().shift(1)
        df["expected_move_pct"] = np.maximum(df["atr_pct"] * 2.2, df["ema_spread_pct"] * 3.2)
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
        adx = latest["adx"]
        volume_ratio = latest["volume_ratio"]
        ema_spread_pct = latest["ema_spread_pct"]
        breakout_high = latest["breakout_high"]
        breakout_low = latest["breakout_low"]
        expected_move_pct = latest["expected_move_pct"]
        breakout_up = not isna(breakout_high) and latest["close"] > breakout_high
        breakout_down = not isna(breakout_low) and latest["close"] < breakout_low

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
        if isna(adx) or adx < self.config.min_adx:
            return Signal(
                symbol=symbol,
                action="hold",
                price=float(latest["close"]),
                timestamp=latest["timestamp"].to_pydatetime(),
                confidence=0.0,
                reason="ADX filter blocked trade",
            )
        if isna(volume_ratio) or volume_ratio < self.config.min_volume_ratio:
            return Signal(
                symbol=symbol,
                action="hold",
                price=float(latest["close"]),
                timestamp=latest["timestamp"].to_pydatetime(),
                confidence=0.0,
                reason="Volume filter blocked trade",
            )
        if isna(ema_spread_pct) or ema_spread_pct < self.config.min_trend_strength_pct:
            return Signal(
                symbol=symbol,
                action="hold",
                price=float(latest["close"]),
                timestamp=latest["timestamp"].to_pydatetime(),
                confidence=0.0,
                reason="Trend strength filter blocked trade",
            )

        if breakout_up:
            long_score += 1
            reasons.append("Breakout up")
        elif breakout_down:
            short_score += 1
            reasons.append("Breakout down")

        if not isna(volume_ratio) and volume_ratio >= self.config.min_volume_ratio * 1.15:
            if long_score > short_score:
                long_score += 1
                reasons.append("Volume confirms long")
            elif short_score > long_score:
                short_score += 1
                reasons.append("Volume confirms short")

        action = "hold"
        confidence = 0.0
        if long_score >= self.config.signal_score_threshold and long_score > short_score:
            action = "buy"
            confidence = min(long_score / 6, 1.0)
        elif short_score >= self.config.signal_score_threshold and short_score > long_score:
            action = "sell"
            confidence = min(short_score / 6, 1.0)

        if self.config.extreme_entry_mode and action != "hold":
            breakout_confirmed = breakout_up if action == "buy" else breakout_down
            if self.config.require_breakout_confirmation and not breakout_confirmed:
                action = "hold"
                confidence = 0.0
                reasons.append("Extreme mode needs breakout")
            elif isna(expected_move_pct) or expected_move_pct < self.config.min_expected_move_pct:
                action = "hold"
                confidence = 0.0
                reasons.append("Expected move too small")
            elif confidence < self.config.min_signal_confidence:
                action = "hold"
                reasons.append("Confidence too low")

        return Signal(
            symbol=symbol,
            action=action,
            price=float(latest["close"]),
            timestamp=latest["timestamp"].to_pydatetime(),
            confidence=confidence,
            reason=", ".join(reasons[-4:]) or "No confluence",
        )
