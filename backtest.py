from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import pandas as pd

from api import BybitClient
from config import Settings
from costs import build_cost_model
from strategy import build_strategy


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
    total_fees: float = 0.0
    fee_drag_pct: float = 0.0
    expectancy_r: float = 0.0
    gross_profit_factor: float = 0.0
    gross_return_pct: float = 0.0
    entries_missed: int = 0


def _empty_result(settings: Settings, symbol: str, reason: str) -> BacktestResult:
    return BacktestResult(
        symbol=symbol,
        total_return_pct=0.0,
        trades=0,
        win_rate_pct=0.0,
        final_equity=round(settings.initial_balance, 2),
        max_drawdown_pct=0.0,
        profit_factor=0.0,
        avg_trade_pct=0.0,
        filter_reason=reason,
    )


class Backtester:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = BybitClient(settings.api_key, settings.api_secret, testnet=settings.testnet)
        self.cost = build_cost_model(settings)
        self.strategy = build_strategy(settings)

    # ------------------------------------------------------------------
    # sizing - must mirror TraderEngine._calculate_position_size exactly
    # ------------------------------------------------------------------
    def _notional_ratio(self, stop_distance_pct: float) -> float:
        """Position notional as a multiple of equity, capped like the live trader."""
        risk_based = self.settings.risk_per_trade / max(stop_distance_pct, 1e-9)
        return min(risk_based, self.settings.max_position_notional_pct)

    # ------------------------------------------------------------------
    # entry simulation
    # ------------------------------------------------------------------
    def _limit_filled(self, side: str, limit_price: float, bar: Any) -> bool:
        """A resting post-only order fills only if the bar trades *through* it.

        Strict inequality stands in for queue position: merely touching the level
        does not guarantee the queue ahead of us cleared.
        """
        if side == "long":
            return float(bar["low"]) < limit_price
        return float(bar["high"]) > limit_price

    # ------------------------------------------------------------------
    # exit simulation
    # ------------------------------------------------------------------
    def _resolve_exit(self, position: dict[str, Any], bar: Any) -> tuple[Optional[float], str]:
        """Intrabar stop/target test. When a bar touches both, assume the stop filled first."""
        high = float(bar["high"])
        low = float(bar["low"])
        stop = position["stop_loss"]
        target = position["take_profit"]
        if position["side"] == "long":
            if low <= stop:
                return stop, "Stop loss"
            if target is not None and high >= target:
                return target, "Take profit"
        else:
            if high >= stop:
                return stop, "Stop loss"
            if target is not None and low <= target:
                return target, "Take profit"
        return None, ""

    def _advance_protective_stop(self, position: dict[str, Any], bar: Any) -> None:
        """Arm break-even and trail using this bar's range.

        Deliberately runs *after* the stop test, so a stop raised by this bar's
        own high can never rescue the position within that same bar.
        """
        entry = position["entry"]
        round_trip = self.cost.round_trip_pct()
        position["peak"] = max(position["peak"], float(bar["high"]))
        position["trough"] = min(position["trough"], float(bar["low"]))

        trigger = self.settings.break_even_trigger_pct
        if trigger > 0 and not position["break_even_armed"]:
            if position["side"] == "long":
                favorable = (position["peak"] - entry) / max(entry, 1e-9)
            else:
                favorable = (entry - position["trough"]) / max(entry, 1e-9)
            if favorable >= trigger:
                # Break-even must clear the round trip or it locks in a net loss.
                offset = round_trip + self.settings.break_even_offset_pct
                if position["side"] == "long":
                    position["stop_loss"] = max(position["stop_loss"], entry * (1 + offset))
                else:
                    position["stop_loss"] = min(position["stop_loss"], entry * (1 - offset))
                position["break_even_armed"] = True

        if self.settings.take_profit_mode == "trail" and position["break_even_armed"]:
            atr = position["entry_atr"]
            if atr > 0:
                distance = atr * self.settings.trail_atr_multiple
                if position["side"] == "long":
                    position["stop_loss"] = max(position["stop_loss"], position["peak"] - distance)
                else:
                    position["stop_loss"] = min(position["stop_loss"], position["trough"] + distance)

    def _new_position(
        self, entry_price: float, side: str, stop_loss: float, take_profit: float, atr: float
    ) -> dict[str, Any]:
        stop_distance_pct = abs(entry_price - stop_loss) / max(entry_price, 1e-9)
        return {
            "side": side,
            "entry": entry_price,
            "stop_loss": stop_loss,
            # In trail mode the fixed target is discarded so winners can run.
            "take_profit": None if self.settings.take_profit_mode == "trail" else take_profit,
            "entry_atr": atr,
            "peak": entry_price,
            "trough": entry_price,
            "break_even_armed": False,
            "stop_distance_pct": stop_distance_pct,
            "notional_ratio": self._notional_ratio(stop_distance_pct),
        }

    # ------------------------------------------------------------------
    def run(self, symbol: str, candles: int = 500) -> BacktestResult:
        frame = self.client.get_kline_history(self.settings.category, symbol, self.settings.timeframe, candles)
        required_columns = {"timestamp", "open", "high", "low", "close", "volume", "turnover"}
        if frame.empty or not required_columns.issubset(frame.columns):
            missing = sorted(required_columns.difference(frame.columns))
            reason = "No market data returned." if frame.empty else f"Missing columns: {', '.join(missing)}"
            return self.evaluate_result(_empty_result(self.settings, symbol, reason))

        frame = self.strategy.apply_indicators(frame).dropna().reset_index(drop=True)
        if frame.empty or len(frame) <= 50:
            return self.evaluate_result(_empty_result(self.settings, symbol, "Not enough indicator-ready candles."))

        maker_entries = self.settings.maker_entry_enabled
        entry_cost = self.cost.with_entry_style(maker_entries)
        fees_pct = entry_cost.entry_cost_pct() + entry_cost.exit_cost_pct()

        equity = self.settings.initial_balance
        peak_equity = equity
        wins = 0
        trades = 0
        entries_missed = 0
        max_drawdown_pct = 0.0
        gross_profit = 0.0
        gross_loss = 0.0
        gross_currency_total = 0.0
        total_fees = 0.0
        trade_returns: list[float] = []
        r_multiples: list[float] = []
        position: Optional[dict[str, Any]] = None
        pending: Optional[dict[str, Any]] = None
        cooldown_until = -1

        for idx in range(50, len(frame)):
            bar = frame.iloc[idx]
            signal = self.strategy.generate_signal(symbol, frame.iloc[: idx + 1], indicators_ready=True)
            action = signal.action
            close_price = float(bar["close"])

            # 1. manage an open position against this bar
            if position is not None:
                exit_price, reason = self._resolve_exit(position, bar)
                if exit_price is None:
                    flipped = (position["side"] == "long" and action == "sell") or (
                        position["side"] == "short" and action == "buy"
                    )
                    if flipped:
                        exit_price = close_price
                if exit_price is None:
                    self._advance_protective_stop(position, bar)
                else:
                    entry_price = position["entry"]
                    if position["side"] == "long":
                        gross_pct = (exit_price - entry_price) / entry_price
                    else:
                        gross_pct = (entry_price - exit_price) / entry_price
                    ratio = position["notional_ratio"]
                    trade_return = max((gross_pct - fees_pct) * ratio, -0.99)
                    total_fees += equity * ratio * fees_pct
                    gross_currency_total += equity * ratio * gross_pct
                    equity *= 1 + trade_return
                    trades += 1
                    wins += int(trade_return > 0)
                    trade_returns.append(trade_return * 100)
                    risk_pct = ratio * (position["stop_distance_pct"] + fees_pct)
                    r_multiples.append(trade_return / risk_pct if risk_pct > 0 else 0.0)
                    if trade_return > 0:
                        gross_profit += trade_return
                    else:
                        gross_loss += abs(trade_return)
                    peak_equity = max(peak_equity, equity)
                    max_drawdown_pct = max(max_drawdown_pct, ((peak_equity - equity) / peak_equity) * 100)
                    cooldown_until = idx + self.settings.cooldown_bars
                    position = None

            # 2. settle a resting maker entry against this bar
            if pending is not None:
                if position is not None:
                    pending = None
                elif self._limit_filled(pending["side"], pending["limit_price"], bar):
                    position = self._new_position(
                        pending["limit_price"],
                        pending["side"],
                        pending["stop_loss"],
                        pending["take_profit"],
                        pending["entry_atr"],
                    )
                    pending = None
                else:
                    pending["bars_waited"] += 1
                    chase = abs(close_price - pending["limit_price"]) / max(pending["limit_price"], 1e-9)
                    expired = pending["bars_waited"] > self.settings.maker_entry_timeout_bars
                    if expired or chase > self.settings.maker_max_chase_pct:
                        if self.settings.maker_entry_fallback == "market":
                            position = self._new_position(
                                close_price,
                                pending["side"],
                                pending["stop_loss"],
                                pending["take_profit"],
                                pending["entry_atr"],
                            )
                        else:
                            entries_missed += 1
                        pending = None

            # 3. look for a new entry
            if position is None and pending is None and idx >= cooldown_until and action in {"buy", "sell"}:
                side = "long" if action == "buy" else "short"
                stop_loss = signal.stop_loss
                take_profit = signal.take_profit
                if stop_loss is None:
                    stop_loss = (
                        close_price * (1 - self.settings.stop_loss_pct)
                        if side == "long"
                        else close_price * (1 + self.settings.stop_loss_pct)
                    )
                if take_profit is None:
                    take_profit = (
                        close_price * (1 + self.settings.take_profit_pct)
                        if side == "long"
                        else close_price * (1 - self.settings.take_profit_pct)
                    )
                atr = 0.0 if pd.isna(bar.get("atr")) else float(bar["atr"])
                if maker_entries:
                    # The order rests at this bar's close and can only fill from the next bar on.
                    pending = {
                        "side": side,
                        "limit_price": close_price,
                        "stop_loss": stop_loss,
                        "take_profit": take_profit,
                        "entry_atr": atr,
                        "bars_waited": 0,
                    }
                else:
                    position = self._new_position(close_price, side, stop_loss, take_profit, atr)

        total_return_pct = ((equity - self.settings.initial_balance) / self.settings.initial_balance) * 100
        win_rate_pct = (wins / trades * 100) if trades else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss else (999.0 if gross_profit > 0 else 0.0)
        if math.isinf(profit_factor):
            profit_factor = 999.0
        avg_trade_pct = sum(trade_returns) / len(trade_returns) if trade_returns else 0.0
        expectancy_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0.0
        gross_return_pct = (gross_currency_total / self.settings.initial_balance) * 100
        fee_drag_pct = (total_fees / abs(gross_currency_total) * 100) if gross_currency_total else 0.0
        gross_pf = profit_factor * (1 + total_fees / abs(gross_currency_total)) if gross_currency_total else 0.0

        result = BacktestResult(
            symbol=symbol,
            total_return_pct=round(total_return_pct, 2),
            trades=trades,
            win_rate_pct=round(win_rate_pct, 2),
            final_equity=round(equity, 2),
            max_drawdown_pct=round(max_drawdown_pct, 2),
            profit_factor=round(profit_factor, 2),
            avg_trade_pct=round(avg_trade_pct, 3),
            total_fees=round(total_fees, 2),
            fee_drag_pct=round(fee_drag_pct, 2),
            expectancy_r=round(expectancy_r, 4),
            gross_profit_factor=round(gross_pf, 2),
            gross_return_pct=round(gross_return_pct, 2),
            entries_missed=entries_missed,
        )
        return self.evaluate_result(result)

    def evaluate_result(self, result: BacktestResult) -> BacktestResult:
        reasons: list[str] = []
        if result.filter_reason and result.filter_reason != "Passed":
            reasons.append(result.filter_reason)
        if result.profit_factor < self.settings.min_backtest_profit_factor:
            reasons.append(
                f"net profit factor {result.profit_factor:.2f} < {self.settings.min_backtest_profit_factor:.2f}"
            )
        if result.max_drawdown_pct > self.settings.max_backtest_drawdown_pct:
            reasons.append(f"drawdown {result.max_drawdown_pct:.2f}% > {self.settings.max_backtest_drawdown_pct:.2f}%")
        if result.trades < self.settings.min_backtest_trades:
            reasons.append(f"trades {result.trades} < {self.settings.min_backtest_trades}")
        if result.expectancy_r < self.settings.min_backtest_expectancy_r:
            reasons.append(f"expectancy {result.expectancy_r:.3f}R < {self.settings.min_backtest_expectancy_r:.3f}R")
        result.passed_filter = not reasons
        result.filter_reason = "Passed" if result.passed_filter else "; ".join(reasons)
        return result

    def run_for_symbols(self, symbols: Iterable[str], candles: int | None = None) -> list[BacktestResult]:
        lookback = candles or self.settings.backtest_filter_candles
        return [self.run(symbol, candles=lookback) for symbol in symbols]
