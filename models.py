from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

SignalType = Literal["buy", "sell", "hold"]
Side = Literal["long", "short"]


@dataclass(slots=True)
class Signal:
    symbol: str
    action: SignalType
    price: float
    timestamp: datetime
    confidence: float
    reason: str
    stop_loss: float | None = None
    take_profit: float | None = None


@dataclass(slots=True)
class Position:
    symbol: str
    side: Side
    qty: float
    entry_price: float
    stop_loss: float
    take_profit: float
    leverage: int
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    exchange_order_id: Optional[str] = None
    unrealized_pnl: float = 0.0
    peak_price: float = 0.0
    trough_price: float = 0.0
    break_even_armed: bool = False


@dataclass(slots=True)
class TradeRecord:
    timestamp: str
    symbol: str
    mode: str
    side: Side
    action: str
    qty: float
    entry_price: float
    exit_price: float
    pnl: float
    reason: str
