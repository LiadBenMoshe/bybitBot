from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from pandas import isna

from models import Signal

if TYPE_CHECKING:
    from config import Settings


@dataclass(slots=True)
class StrategyConfig:
    ema_fast: int = 9
    ema_slow: int = 21
    trend_ema: int = 50
    rsi_period: int = 14
    rsi_long_threshold: float = 52
    rsi_short_threshold: float = 48
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_period: int = 14
    atr_min_pct: float = 0.002
    adx_period: int = 14
    min_adx: float = 20.0
    volume_ma_period: int = 20
    min_volume_ratio: float = 1.1
    breakout_lookback: int = 12
    min_trend_strength_pct: float = 0.001
    signal_score_threshold: int = 5
    extreme_entry_mode: bool = True
    min_expected_move_pct: float = 0.008
    min_signal_confidence: float = 0.65
    require_breakout_confirmation: bool = True
    pullback_lookback: int = 4
    breakout_buffer_pct: float = 0.0008
    atr_stop_multiple: float = 1.2
    atr_target_multiple: float = 2.2
    max_rsi_long: float = 72.0
    min_rsi_short: float = 28.0
    min_body_to_range_ratio: float = 0.45
    min_range_width_pct: float = 0.004
    ema_retest_tolerance_pct: float = 0.0025
    require_macd_hist_improving: bool = True


class IndicatorStrategy:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def apply_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        df = frame.copy()
        df["ema_fast"] = df["close"].ewm(span=self.config.ema_fast, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=self.config.ema_slow, adjust=False).mean()
        df["trend_ema"] = df["close"].ewm(span=self.config.trend_ema, adjust=False).mean()
        df["ema_fast_slope"] = df["ema_fast"].diff()
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
        df["macd_hist_change"] = df["macd_hist"].diff()

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
        df["range_width_pct"] = (df["breakout_high"] - df["breakout_low"]) / df["close"].replace(0, np.nan)
        df["recent_low"] = df["low"].rolling(self.config.pullback_lookback).min().shift(1)
        df["recent_high"] = df["high"].rolling(self.config.pullback_lookback).max().shift(1)
        df["price_change_lookback"] = df["close"].pct_change(self.config.pullback_lookback)
        df["candle_range_pct"] = (df["high"] - df["low"]) / df["close"].replace(0, np.nan)
        df["candle_body_pct"] = (df["close"] - df["open"]).abs() / df["close"].replace(0, np.nan)
        df["body_to_range_ratio"] = df["candle_body_pct"] / df["candle_range_pct"].replace(0, np.nan)
        df["bullish_close"] = df["close"] > df["open"]
        df["bearish_close"] = df["close"] < df["open"]
        df["expected_move_pct"] = np.maximum(df["atr_pct"] * 2.2, df["ema_spread_pct"] * 3.2)
        return df

    def generate_signal(self, symbol: str, frame: pd.DataFrame) -> Signal:
        df = self.apply_indicators(frame)
        latest = df.iloc[-1]
        previous = df.iloc[-2] if len(df) > 1 else latest
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
        ema_fast_slope = latest["ema_fast_slope"]
        breakout_high = latest["breakout_high"]
        breakout_low = latest["breakout_low"]
        range_width_pct = latest["range_width_pct"]
        recent_low = latest["recent_low"]
        recent_high = latest["recent_high"]
        expected_move_pct = latest["expected_move_pct"]
        price_change_lookback = latest["price_change_lookback"]
        body_to_range_ratio = latest["body_to_range_ratio"]
        candle_body_pct = latest["candle_body_pct"]
        bullish_close = bool(latest["bullish_close"])
        bearish_close = bool(latest["bearish_close"])
        macd_hist_change = latest["macd_hist_change"]

        breakout_up = not isna(breakout_high) and latest["close"] >= breakout_high * (1 + self.config.breakout_buffer_pct)
        breakout_down = not isna(breakout_low) and latest["close"] <= breakout_low * (1 - self.config.breakout_buffer_pct)
        trend_up = not isna(ema_fast) and not isna(ema_slow) and not isna(trend_ema) and ema_fast > ema_slow and latest["close"] > trend_ema
        trend_down = not isna(ema_fast) and not isna(ema_slow) and not isna(trend_ema) and ema_fast < ema_slow and latest["close"] < trend_ema
        pullback_long = (
            trend_up
            and not isna(recent_low)
            and not isna(ema_fast)
            and recent_low <= ema_fast * (1 + self.config.ema_retest_tolerance_pct)
            and latest["close"] > ema_fast
            and latest["low"] <= ema_fast * (1 + self.config.ema_retest_tolerance_pct)
        )
        pullback_short = (
            trend_down
            and not isna(recent_high)
            and not isna(ema_fast)
            and recent_high >= ema_fast * (1 - self.config.ema_retest_tolerance_pct)
            and latest["close"] < ema_fast
            and latest["high"] >= ema_fast * (1 - self.config.ema_retest_tolerance_pct)
        )
        strong_candle = (
            not isna(body_to_range_ratio)
            and not isna(candle_body_pct)
            and body_to_range_ratio >= self.config.min_body_to_range_ratio
            and candle_body_pct >= self.config.breakout_buffer_pct
        )
        rsi_supports_long = not isna(rsi) and self.config.rsi_long_threshold <= rsi <= self.config.max_rsi_long
        rsi_supports_short = not isna(rsi) and self.config.min_rsi_short <= rsi <= self.config.rsi_short_threshold
        macd_bullish = (
            not isna(macd)
            and not isna(macd_signal)
            and not isna(macd_hist)
            and macd > macd_signal
            and macd_hist > 0
            and (
                not self.config.require_macd_hist_improving
                or isna(macd_hist_change)
                or macd_hist_change >= 0
                or macd_hist >= previous["macd_hist"]
            )
        )
        macd_bearish = (
            not isna(macd)
            and not isna(macd_signal)
            and not isna(macd_hist)
            and macd < macd_signal
            and macd_hist < 0
            and (
                not self.config.require_macd_hist_improving
                or isna(macd_hist_change)
                or macd_hist_change <= 0
                or macd_hist <= previous["macd_hist"]
            )
        )

        if trend_up:
            long_score += 2
            reasons.append("Trend up")
        elif trend_down:
            short_score += 2
            reasons.append("Trend down")

        if rsi_supports_long:
            long_score += 1
            reasons.append("RSI supports long")
        elif rsi_supports_short:
            short_score += 1
            reasons.append("RSI supports short")

        if macd_bullish:
            long_score += 1
            reasons.append("MACD bullish")
        elif macd_bearish:
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
        if isna(range_width_pct) or range_width_pct < self.config.min_range_width_pct:
            return Signal(
                symbol=symbol,
                action="hold",
                price=float(latest["close"]),
                timestamp=latest["timestamp"].to_pydatetime(),
                confidence=0.0,
                reason="Range filter blocked trade",
            )

        if breakout_up:
            long_score += 2
            reasons.append("Breakout up")
        elif breakout_down:
            short_score += 2
            reasons.append("Breakout down")

        if pullback_long:
            long_score += 2
            reasons.append("Pullback long")
        elif pullback_short:
            short_score += 2
            reasons.append("Pullback short")

        if not isna(price_change_lookback) and price_change_lookback > 0:
            long_score += 1
            reasons.append("Short-term momentum up")
        elif not isna(price_change_lookback) and price_change_lookback < 0:
            short_score += 1
            reasons.append("Short-term momentum down")

        if not isna(volume_ratio) and volume_ratio >= self.config.min_volume_ratio * 1.15:
            if long_score > short_score:
                long_score += 1
                reasons.append("Volume confirms long")
            elif short_score > long_score:
                short_score += 1
                reasons.append("Volume confirms short")

        if not isna(ema_fast_slope) and ema_fast_slope > 0:
            long_score += 1
            reasons.append("EMA slope up")
        elif not isna(ema_fast_slope) and ema_fast_slope < 0:
            short_score += 1
            reasons.append("EMA slope down")

        if strong_candle and bullish_close:
            long_score += 1
            reasons.append("Strong bullish candle")
        elif strong_candle and bearish_close:
            short_score += 1
            reasons.append("Strong bearish candle")

        action = "hold"
        confidence = 0.0
        stop_loss = None
        take_profit = None
        if long_score >= self.config.signal_score_threshold and long_score > short_score and (breakout_up or pullback_long):
            action = "buy"
            confidence = min(long_score / 10, 1.0)
        elif short_score >= self.config.signal_score_threshold and short_score > long_score and (breakout_down or pullback_short):
            action = "sell"
            confidence = min(short_score / 10, 1.0)

        if self.config.extreme_entry_mode and action != "hold":
            breakout_confirmed = breakout_up if action == "buy" else breakout_down
            continuation_confirmed = breakout_confirmed or (pullback_long if action == "buy" else pullback_short)
            if self.config.require_breakout_confirmation and not continuation_confirmed:
                action = "hold"
                confidence = 0.0
                reasons.append("Extreme mode needs continuation")
            elif isna(expected_move_pct) or expected_move_pct < self.config.min_expected_move_pct:
                action = "hold"
                confidence = 0.0
                reasons.append("Expected move too small")
            elif not strong_candle:
                action = "hold"
                confidence = 0.0
                reasons.append("Entry candle too weak")
            elif confidence < self.config.min_signal_confidence:
                action = "hold"
                confidence = 0.0
                reasons.append("Confidence too low")
            elif not isna(latest["atr"]) and latest["atr"] > 0:
                atr = float(latest["atr"])
                if action == "buy":
                    stop_loss = float(latest["close"] - atr * self.config.atr_stop_multiple)
                    take_profit = float(latest["close"] + atr * self.config.atr_target_multiple)
                else:
                    stop_loss = float(latest["close"] + atr * self.config.atr_stop_multiple)
                    take_profit = float(latest["close"] - atr * self.config.atr_target_multiple)

        return Signal(
            symbol=symbol,
            action=action,
            price=float(latest["close"]),
            timestamp=latest["timestamp"].to_pydatetime(),
            confidence=confidence,
            reason=", ".join(reasons[-4:]) or "No confluence",
            stop_loss=stop_loss,
            take_profit=take_profit,
        )


def build_strategy_config(settings: "Settings") -> StrategyConfig:
    return StrategyConfig(
        ema_fast=settings.ema_fast,
        ema_slow=settings.ema_slow,
        trend_ema=settings.trend_ema,
        rsi_period=settings.rsi_period,
        rsi_long_threshold=settings.rsi_long_threshold,
        rsi_short_threshold=settings.rsi_short_threshold,
        macd_fast=settings.macd_fast,
        macd_slow=settings.macd_slow,
        macd_signal=settings.macd_signal,
        atr_period=settings.atr_period,
        atr_min_pct=settings.atr_min_pct,
        adx_period=settings.adx_period,
        min_adx=settings.min_adx,
        volume_ma_period=settings.volume_ma_period,
        min_volume_ratio=settings.min_volume_ratio,
        breakout_lookback=settings.breakout_lookback,
        min_trend_strength_pct=settings.min_trend_strength_pct,
        signal_score_threshold=settings.signal_score_threshold,
        extreme_entry_mode=settings.extreme_entry_mode,
        min_expected_move_pct=settings.min_expected_move_pct,
        min_signal_confidence=settings.min_signal_confidence,
        require_breakout_confirmation=settings.require_breakout_confirmation,
        pullback_lookback=settings.pullback_lookback,
        breakout_buffer_pct=settings.breakout_buffer_pct,
        atr_stop_multiple=settings.atr_stop_multiple,
        atr_target_multiple=settings.atr_target_multiple,
        max_rsi_long=settings.max_rsi_long,
        min_rsi_short=settings.min_rsi_short,
        min_body_to_range_ratio=settings.min_body_to_range_ratio,
        min_range_width_pct=settings.min_range_width_pct,
        ema_retest_tolerance_pct=settings.ema_retest_tolerance_pct,
        require_macd_hist_improving=settings.require_macd_hist_improving,
    )
