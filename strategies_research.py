"""Candidate strategies for the walk-forward research program.

Every class here subclasses ``IndicatorStrategy`` so it inherits the indicator
set, the cost-derived volatility floor and, through ``_finalize_signal``, the
identical inversion + cost gate + ATR exit placement. Only the entry rule (and
the assumed win probability fed to the gate) varies, which is what makes the
comparison fair.

``CANDIDATES`` is the pre-registered list the sweep driver runs. Keep it short:
every extra row inflates the best-of-N statistic the acceptance bar has to beat.
``build_named_strategy`` is what ``strategy.build_strategy`` dispatches to when
``STRATEGY_NAME`` is not ``indicator``, so a research winner can be shipped
without copying code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

import pandas as pd
from pandas import isna

from costs import build_cost_model
from models import Signal
from strategy import IndicatorStrategy, StrategyConfig, build_strategy_config

if TYPE_CHECKING:
    from config import Settings


def _hold(symbol: str, latest: Any, reason: str) -> Signal:
    return Signal(
        symbol=symbol,
        action="hold",
        price=float(latest["close"]),
        timestamp=latest["timestamp"].to_pydatetime(),
        confidence=0.0,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# B. higher-timeframe trend following
# ---------------------------------------------------------------------------
class HTFTrendBreakout(IndicatorStrategy):
    """Donchian breakout in the direction of a slow EMA, run with a trailing stop.

    On hourly bars EMA200 is roughly a 4h EMA50, so no resampling is needed and
    causality is trivial. Few trades, wide stops, fee-cheap: the opposite
    profile from the shipped scalper, which the trade history shows dying
    inside single bars.
    """

    assumed_p = 0.40

    def __init__(
        self,
        config: StrategyConfig,
        cost: Any,
        htf_ema: int = 200,
        channel: int = 48,
    ) -> None:
        super().__init__(config, cost)
        self.htf_ema = htf_ema
        self.channel = channel

    def apply_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        df = super().apply_indicators(frame)
        df["ema_htf"] = df["close"].ewm(span=self.htf_ema, adjust=False).mean()
        df["donchian_hi"] = df["high"].rolling(self.channel).max().shift(1)
        df["donchian_lo"] = df["low"].rolling(self.channel).min().shift(1)
        return df

    def signal_from_rows(self, symbol: str, latest: Any, previous: Any) -> Signal:
        close = float(latest["close"])
        hi, lo, ema = latest["donchian_hi"], latest["donchian_lo"], latest["ema_htf"]
        if isna(hi) or isna(lo) or isna(ema):
            return _hold(symbol, latest, "Warming up")
        atr_pct = latest["atr_pct"]
        floor = self.min_atr_pct()
        if isna(atr_pct) or atr_pct < floor:
            return _hold(symbol, latest, f"ATR below floor {floor * 100:.3f}%")
        action = "hold"
        reasons: list[str] = []
        if close > hi and close > ema:
            action, reasons = "buy", ["Donchian breakout up", "Above HTF EMA"]
        elif close < lo and close < ema:
            action, reasons = "sell", ["Donchian breakout down", "Below HTF EMA"]
        if action == "hold":
            return _hold(symbol, latest, "No breakout")
        return self._finalize_signal(symbol, latest, action, self.assumed_p, reasons)


# ---------------------------------------------------------------------------
# D. fast EMA-cross momentum
# ---------------------------------------------------------------------------
class MomentumCross(IndicatorStrategy):
    """Enter on a fresh EMA fast/slow cross in the direction of the trend EMA.

    Deliberately light on filters so it trades several times a day on 15m bars:
    only the cross, the trend side, an RSI band and the cost-derived ATR floor.
    Exits and the cost gate come from ``_finalize_signal`` unchanged.
    """

    assumed_p = 0.45

    def signal_from_rows(self, symbol: str, latest: Any, previous: Any) -> Signal:
        fast, slow, trend, rsi, atr = (
            latest["ema_fast"], latest["ema_slow"], latest["trend_ema"], latest["rsi"], latest["atr"]
        )
        prev_fast, prev_slow = previous["ema_fast"], previous["ema_slow"]
        if any(isna(v) for v in (fast, slow, trend, rsi, atr, prev_fast, prev_slow)):
            return _hold(symbol, latest, "Warming up")
        atr_pct = latest["atr_pct"]
        floor = self.min_atr_pct()
        if isna(atr_pct) or atr_pct < floor:
            return _hold(symbol, latest, f"ATR below floor {floor * 100:.3f}%")
        close = float(latest["close"])
        action = "hold"
        reasons: list[str] = []
        if (
            prev_fast <= prev_slow
            and fast > slow
            and close > trend
            and self.config.rsi_long_threshold <= rsi <= self.config.max_rsi_long
        ):
            action, reasons = "buy", ["EMA cross up", "Above trend EMA", f"RSI {rsi:.0f}"]
        elif (
            prev_fast >= prev_slow
            and fast < slow
            and close < trend
            and self.config.min_rsi_short <= rsi <= self.config.rsi_short_threshold
        ):
            action, reasons = "sell", ["EMA cross down", "Below trend EMA", f"RSI {rsi:.0f}"]
        if action == "hold":
            return _hold(symbol, latest, "No EMA cross")
        return self._finalize_signal(symbol, latest, action, self.assumed_p, reasons)


# ---------------------------------------------------------------------------
# C. mean reversion
# ---------------------------------------------------------------------------
class MeanReversion60(IndicatorStrategy):
    """Fade a z-score extreme back to its mean, with a time stop.

    The target is the EMA itself, so the target multiple passed to the cost gate
    is the actual distance to the mean in ATRs rather than a fixed number; the
    gate then rejects fades whose reversion is too small to pay for itself.
    """

    assumed_p = 0.55

    def __init__(
        self,
        config: StrategyConfig,
        cost: Any,
        mean_span: int = 20,
        z_entry: float = 2.0,
        rsi_low: float = 30.0,
        rsi_high: float = 70.0,
    ) -> None:
        super().__init__(config, cost)
        self.mean_span = mean_span
        self.z_entry = z_entry
        self.rsi_low = rsi_low
        self.rsi_high = rsi_high

    def apply_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        df = super().apply_indicators(frame)
        df["mr_mean"] = df["close"].ewm(span=self.mean_span, adjust=False).mean()
        std = df["close"].rolling(self.mean_span).std()
        df["mr_z"] = (df["close"] - df["mr_mean"]) / std.replace(0, float("nan"))
        return df

    def signal_from_rows(self, symbol: str, latest: Any, previous: Any) -> Signal:
        z, mean, rsi, atr = latest["mr_z"], latest["mr_mean"], latest["rsi"], latest["atr"]
        if isna(z) or isna(mean) or isna(rsi) or isna(atr) or atr <= 0:
            return _hold(symbol, latest, "Warming up")
        atr_pct = latest["atr_pct"]
        if isna(atr_pct) or atr_pct < self.config.atr_min_pct:
            return _hold(symbol, latest, "ATR below ATR_MIN_PCT")
        close = float(latest["close"])
        action = "hold"
        reasons: list[str] = []
        if z <= -self.z_entry and rsi < self.rsi_low:
            action, reasons = "buy", [f"z {z:.2f}", f"RSI {rsi:.0f}"]
        elif z >= self.z_entry and rsi > self.rsi_high:
            action, reasons = "sell", [f"z {z:.2f}", f"RSI {rsi:.0f}"]
        if action == "hold":
            return _hold(symbol, latest, "No extreme")
        target_multiple = abs(float(mean) - close) / float(atr)
        return self._finalize_signal(
            symbol, latest, action, self.assumed_p, reasons, target_multiple=max(target_multiple, 1e-6)
        )


# ---------------------------------------------------------------------------
# D. regime / session filter wrapper
# ---------------------------------------------------------------------------
class RegimeFilter:
    """Wrap any strategy and return hold outside a volatility band or session.

    ``atr_rank`` is the rolling percentile of ATR% over ``window`` bars, so
    ``low``/``high`` select the middle of the volatility distribution: too quiet
    and nothing clears the cost gate, too wild and the stop is noise.
    """

    def __init__(
        self,
        inner: IndicatorStrategy,
        window: int = 200,
        low: float = 0.3,
        high: float = 0.9,
        sessions: Optional[set[int]] = None,
    ) -> None:
        self.inner = inner
        self.config = inner.config
        self.cost = inner.cost
        self.window = window
        self.low = low
        self.high = high
        self.sessions = sessions
        self.assumed_p = getattr(inner, "assumed_p", None)

    def min_atr_pct(self) -> float:
        return self.inner.min_atr_pct()

    def apply_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        df = self.inner.apply_indicators(frame)
        df["atr_rank"] = df["atr_pct"].rolling(self.window).rank(pct=True)
        return df

    def signal_from_rows(self, symbol: str, latest: Any, previous: Any) -> Signal:
        rank = latest["atr_rank"]
        if isna(rank) or rank < self.low or rank > self.high:
            return _hold(symbol, latest, "Outside volatility regime")
        if self.sessions is not None and latest["timestamp"].hour not in self.sessions:
            return _hold(symbol, latest, "Outside session")
        return self.inner.signal_from_rows(symbol, latest, previous)

    def generate_signal(self, symbol: str, frame: pd.DataFrame, indicators_ready: bool = False) -> Signal:
        df = frame if indicators_ready else self.apply_indicators(frame)
        latest = df.iloc[-1]
        previous = df.iloc[-2] if len(df) > 1 else latest
        return self.signal_from_rows(symbol, latest, previous)


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------
def build_named_strategy(name: str, settings: "Settings") -> IndicatorStrategy:
    config = build_strategy_config(settings)
    cost = build_cost_model(settings)
    if name == "indicator":
        return IndicatorStrategy(config, cost)
    if name == "htf_trend":
        return HTFTrendBreakout(config, cost)
    if name == "mean_reversion":
        return MeanReversion60(config, cost)
    if name == "momentum":
        return MomentumCross(config, cost)
    raise ValueError(f"Unknown STRATEGY_NAME {name!r}")


def _factory(name: str, **kwargs: Any) -> Callable[["Settings"], IndicatorStrategy]:
    def build(settings: "Settings") -> IndicatorStrategy:
        strategy = build_named_strategy(name, settings)
        for key, value in kwargs.items():
            setattr(strategy, key, value)
        return strategy

    return build


def _regime_factory(name: str, **regime: Any) -> Callable[["Settings"], RegimeFilter]:
    def build(settings: "Settings") -> RegimeFilter:
        return RegimeFilter(build_named_strategy(name, settings), **regime)

    return build


@dataclass(slots=True)
class Candidate:
    name: str
    family: str
    overrides: dict[str, Any] = field(default_factory=dict)
    strategy_factory: Optional[Callable[["Settings"], Any]] = None
    symbols: Optional[list[str]] = None
    note: str = ""


# Family A widens the stop *and* the target together: the cost-derived ATR
# floor is round_trip*(1+rr)/(target - rr*stop), which goes to infinity (zero
# trades) if only the stop is widened.
_A2 = {"atr_stop_multiple": 2.0, "atr_target_multiple": 4.8}
_B = {
    "invert_signals": False,
    "atr_stop_multiple": 2.5,
    "atr_target_multiple": 6.0,
    "take_profit_mode": "trail",
    "trail_atr_multiple": 3.0,
    "cooldown_bars": 6,
    "atr_min_pct": 0.002,
    "min_net_reward_risk": 1.0,
}
_C = {
    "invert_signals": False,
    "atr_stop_multiple": 2.0,
    "atr_target_multiple": 3.0,  # nominal; the class passes the real distance to the mean
    "max_bars_held": 8,
    "atr_min_pct": 0.002,
    "min_net_reward_risk": 1.0,
    "cooldown_bars": 2,
}
_D = {
    "invert_signals": False,
    "atr_stop_multiple": 1.5,
    "atr_target_multiple": 3.0,
    "atr_min_pct": 0.0015,
    "min_target_to_cost_ratio": 2.0,
    "min_net_reward_risk": 1.0,
    "min_expected_move_pct": 0.002,
    "rsi_long_threshold": 50,
    "rsi_short_threshold": 50,
    "max_rsi_long": 75,
    "min_rsi_short": 25,
    "cooldown_bars": 1,
}

CANDIDATES: list[Candidate] = [
    Candidate("shipped_as_configured", "baseline", {}, note="current .env (inverted, stop 1.0 / target 2.4)"),
    Candidate("shipped_uninverted", "baseline", {"invert_signals": False}),
    Candidate("A1_stop1.5_tgt3.6", "A", {"atr_stop_multiple": 1.5, "atr_target_multiple": 3.6}),
    Candidate("A2_stop2.0_tgt4.8", "A", dict(_A2)),
    Candidate(
        "A3_A2_trail2.0_time24",
        "A",
        {**_A2, "take_profit_mode": "trail", "trail_atr_multiple": 2.0, "max_bars_held": 24},
    ),
    Candidate("A4_A2_no_breakeven", "A", {**_A2, "break_even_trigger_pct": 0.0}),
    Candidate("A5_A2_uninverted", "A", {**_A2, "invert_signals": False}),
    Candidate("B1_htf_trend_maker", "B", dict(_B), strategy_factory=_factory("htf_trend")),
    Candidate("B2_htf_trend_taker", "B", {**_B, "maker_entry_enabled": False}, strategy_factory=_factory("htf_trend")),
    Candidate("C1_mean_rev_maker", "C", dict(_C), strategy_factory=_factory("mean_reversion")),
    Candidate("C2_mean_rev_taker", "C", {**_C, "maker_entry_enabled": False}, strategy_factory=_factory("mean_reversion")),
    Candidate("D1_momentum_cross", "D", dict(_D), strategy_factory=_factory("momentum")),
]

# Round 2 is built at runtime from the round-1 winner; these are the wrappers it can use.
ROUND2_BUILDERS = {
    "regime": _regime_factory,
}
