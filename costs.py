from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Settings


@dataclass(slots=True)
class CostModel:
    """Single source of truth for transaction costs.

    Every module that needs a fee figure builds one of these instead of
    recomputing rates inline, so the strategy gate, the live trader and the
    backtester can never disagree about what a round trip costs.
    """

    maker_fee: float
    taker_fee: float
    entry_slippage_pct: float
    exit_slippage_pct: float
    entry_is_maker: bool

    def entry_cost_pct(self) -> float:
        # A resting post-only order fills at its own price or not at all, so it
        # carries no slippage. Crossing the book costs the taker fee plus slip.
        if self.entry_is_maker:
            return self.maker_fee
        return self.taker_fee + self.entry_slippage_pct

    def exit_cost_pct(self) -> float:
        # Exits always cross the book: a stop loss cannot wait for a fill.
        return self.taker_fee + self.exit_slippage_pct

    def round_trip_pct(self) -> float:
        return self.entry_cost_pct() + self.exit_cost_pct()

    def entry_fee(self, notional: float) -> float:
        return abs(notional) * self.entry_cost_pct()

    def exit_fee(self, notional: float) -> float:
        return abs(notional) * self.exit_cost_pct()

    def gross_pnl(self, side: str, entry_price: float, exit_price: float, qty: float) -> float:
        if side == "long":
            return (exit_price - entry_price) * qty
        return (entry_price - exit_price) * qty

    def net_pnl(
        self,
        side: str,
        entry_price: float,
        exit_price: float,
        qty: float,
        entry_fee: float | None = None,
    ) -> tuple[float, float, float]:
        """Return (gross, fees, net). Pass entry_fee when the real one is known."""
        gross = self.gross_pnl(side, entry_price, exit_price, qty)
        paid_entry = self.entry_fee(entry_price * qty) if entry_fee is None else entry_fee
        paid_exit = self.exit_fee(exit_price * qty)
        fees = paid_entry + paid_exit
        return gross, fees, gross - fees

    def min_atr_pct_for_target(self, atr_target_multiple: float, target_to_cost_ratio: float) -> float:
        """Volatility floor implied by the cost model.

        A trade is only worth taking if its net target clears the round trip by
        the configured ratio: atr_pct * mult - rt >= ratio * rt.
        """
        return self.round_trip_pct() * (1.0 + target_to_cost_ratio) / max(atr_target_multiple, 1e-9)

    def min_atr_pct_for_reward_risk(
        self, atr_target_multiple: float, atr_stop_multiple: float, min_reward_risk: float
    ) -> float:
        """Volatility floor implied by the net reward:risk gate.

        (target*a - rt) / (stop*a + rt) >= rr  =>  a >= rt*(1 + rr) / (target - rr*stop)
        """
        denominator = atr_target_multiple - min_reward_risk * atr_stop_multiple
        if denominator <= 0:
            # No volatility can satisfy this combination of multiples and RR floor.
            return float("inf")
        return self.round_trip_pct() * (1.0 + min_reward_risk) / denominator

    def with_entry_style(self, is_maker: bool) -> "CostModel":
        return replace(self, entry_is_maker=is_maker)


@dataclass(slots=True)
class EdgeAssessment:
    """A candidate trade's economics after transaction costs."""

    stop_pct: float
    target_pct: float
    net_target_pct: float
    net_risk_pct: float
    net_reward_risk: float
    expectancy_pct: float

    def describe(self) -> str:
        return (
            f"net RR {self.net_reward_risk:.2f}, "
            f"net target {self.net_target_pct * 100:.2f}%, "
            f"EV {self.expectancy_pct * 100:.3f}%"
        )


def assess_edge(
    cost: CostModel,
    atr_pct: float,
    stop_multiple: float,
    target_multiple: float,
    confidence: float,
) -> EdgeAssessment:
    round_trip = cost.round_trip_pct()
    stop_pct = stop_multiple * atr_pct
    target_pct = target_multiple * atr_pct
    net_target_pct = target_pct - round_trip
    # A loss costs the stop distance *and* the round trip, so cost hurts twice.
    net_risk_pct = stop_pct + round_trip
    net_reward_risk = net_target_pct / net_risk_pct if net_risk_pct > 0 else 0.0
    expectancy_pct = confidence * net_target_pct - (1.0 - confidence) * net_risk_pct
    return EdgeAssessment(
        stop_pct=stop_pct,
        target_pct=target_pct,
        net_target_pct=net_target_pct,
        net_risk_pct=net_risk_pct,
        net_reward_risk=net_reward_risk,
        expectancy_pct=expectancy_pct,
    )


def build_cost_model(settings: "Settings") -> CostModel:
    return CostModel(
        maker_fee=settings.maker_fee_rate,
        taker_fee=settings.taker_fee_rate,
        entry_slippage_pct=settings.entry_slippage_pct,
        exit_slippage_pct=settings.exit_slippage_pct,
        entry_is_maker=settings.maker_entry_enabled,
    )
