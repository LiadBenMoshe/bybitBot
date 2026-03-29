from __future__ import annotations

import logging
from typing import Optional

import requests


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.logger = logging.getLogger(self.__class__.__name__)

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, message: str) -> None:
        if not self.enabled:
            return
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": message},
                timeout=10,
            )
            response.raise_for_status()
        except Exception as exc:  # pragma: no cover
            self.logger.warning("Telegram notification failed: %s", exc)


def build_trade_message(symbol: str, side: str, action: str, price: float, pnl: Optional[float] = None) -> str:
    message = f"{symbol} {side.upper()} {action.upper()} @ {price:.4f}"
    if pnl is not None:
        message += f" | PnL: {pnl:.2f}"
    return message
