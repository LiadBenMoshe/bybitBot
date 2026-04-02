from __future__ import annotations

from dataclasses import dataclass

import math
from typing import Iterable

from api import BybitClient
from config import Settings
from strategy import IndicatorStrategy, StrategyConfig


@dataclass(slots=True)
class BacktestResult:
    symbol: str
    total_return_pct: float
    trades: int
    win_rate_pct: float
    final_equity: float
    max_drawdown_pct: float
    profit_factor: float
    avg_trade_pct: float
    passed_filter: bool = False
    filter_reason: str = ""


class Backtester:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = BybitClient(settings.api_key, settings.api_secret, testnet=settings.testnet)
        self.strategy = IndicatorStrategy(
            StrategyConfig(
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
            )
        )

    def run(self, symbol: str, candles: int = 500) -> BacktestResult:
        frame = self.client.get_kline(self.settings.category, symbol, self.settings.timeframe, limit=candles)
        required_columns = {"timestamp", "open", "high", "low", "close", "volume", "turnover"}
        if frame.empty or not required_columns.issubset(frame.columns):
            missing = sorted(required_columns.difference(frame.columns))
            reason = "No market data returned." if frame.empty else f"Missing columns: {', '.join(missing)}"
            return self.evaluate_result(
                BacktestResult(
                    symbol=symbol,
                    total_return_pct=0.0,
                    trades=0,
                    win_rate_pct=0.0,
                    final_equity=round(self.settings.initial_balance, 2),
                    max_drawdown_pct=0.0,
                    profit_factor=0.0,
                    avg_trade_pct=0.0,
                    filter_reason=reason,
                )
            )
        frame = self.strategy.apply_indicators(frame).dropna().reset_index(drop=True)
        if frame.empty:
            return self.evaluate_result(
                BacktestResult(
                    symbol=symbol,
                    total_return_pct=0.0,
                    trades=0,
                    win_rate_pct=0.0,
                    final_equity=round(self.settings.initial_balance, 2),
                    max_drawdown_pct=0.0,
                    profit_factor=0.0,
                    avg_trade_pct=0.0,
                    filter_reason="Not enough indicator-ready candles.",
                )
            )
        equity = self.settings.initial_balance
        peak_equity = equity
        wins = 0
        trades = 0
        max_drawdown_pct = 0.0
        gross_profit = 0.0
        gross_loss = 0.0
        trade_returns: list[float] = []
        position = None
        cooldown_until = -1

        for idx in range(50, len(frame)):
            current = frame.iloc[: idx + 1]
            signal = self.strategy.generate_signal(symbol, current)
            action = self._effective_action(signal.action)
            price = float(current.iloc[-1]["close"])
            if position is None:
                if idx < cooldown_until:
                    continue
                if action in {"buy", "sell"}:
                    position = {"side": "long" if action == "buy" else "short", "entry": price}
                continue
            pnl_pct = (
                (price - position["entry"]) / position["entry"]
                if position["side"] == "long"
                else (position["entry"] - price) / position["entry"]
            )
            net_pnl_pct = pnl_pct - (self.settings.fee_rate * 2)
            exit_on_flip = (position["side"] == "long" and action == "sell") or (
                position["side"] == "short" and action == "buy"
            )
            if (
                pnl_pct <= -self.settings.stop_loss_pct
                or pnl_pct >= self.settings.take_profit_pct
                or exit_on_flip
            ):
                trades += 1
                wins += int(net_pnl_pct > 0)
                multiplier = self.settings.leverage if self.settings.category != "spot" else 1
                risk_unit = self.settings.risk_per_trade / max(self.settings.stop_loss_pct, 0.0001)
                trade_return = net_pnl_pct * multiplier * risk_unit
                trade_return = max(trade_return, -0.99)
                equity *= 1 + trade_return
                trade_returns.append(trade_return * 100)
                if trade_return > 0:
                    gross_profit += trade_return
                else:
                    gross_loss += abs(trade_return)
                peak_equity = max(peak_equity, equity)
                max_drawdown_pct = max(max_drawdown_pct, ((peak_equity - equity) / peak_equity) * 100)
                cooldown_until = idx + self.settings.cooldown_bars
                position = None

        total_return_pct = ((equity - self.settings.initial_balance) / self.settings.initial_balance) * 100
        win_rate_pct = (wins / trades * 100) if trades else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss else (999.0 if gross_profit > 0 else 0.0)
        avg_trade_pct = sum(trade_returns) / len(trade_returns) if trade_returns else 0.0
        if math.isinf(profit_factor):
            profit_factor = 999.0
        result = BacktestResult(
            symbol,
            round(total_return_pct, 2),
            trades,
            round(win_rate_pct, 2),
            round(equity, 2),
            round(max_drawdown_pct, 2),
            round(profit_factor, 2),
            round(avg_trade_pct, 2),
        )
        return self.evaluate_result(result)

    def evaluate_result(self, result: BacktestResult) -> BacktestResult:
        reasons: list[str] = []
        if result.filter_reason and result.filter_reason != "Passed":
            reasons.append(result.filter_reason)
        if result.profit_factor < self.settings.min_backtest_profit_factor:
            reasons.append(f"profit factor {result.profit_factor:.2f} < {self.settings.min_backtest_profit_factor:.2f}")
        if result.max_drawdown_pct > self.settings.max_backtest_drawdown_pct:
            reasons.append(f"drawdown {result.max_drawdown_pct:.2f}% > {self.settings.max_backtest_drawdown_pct:.2f}%")
        if result.trades < self.settings.min_backtest_trades:
            reasons.append(f"trades {result.trades} < {self.settings.min_backtest_trades}")
        result.passed_filter = not reasons
        result.filter_reason = "Passed" if result.passed_filter else "; ".join(reasons)
        return result

    def run_for_symbols(self, symbols: Iterable[str], candles: int | None = None) -> list[BacktestResult]:
        lookback = candles or self.settings.backtest_filter_candles
        return [self.run(symbol, candles=lookback) for symbol in symbols]

    def _effective_action(self, action: str) -> str:
        if not self.settings.invert_signals:
            return action
        if action == "buy":
            return "sell"
        if action == "sell":
            return "buy"
        return action
