# Bybit Trading Bot

Python 3.10+ crypto trading bot with official Bybit integration, paper/live trading, technical-indicator strategy logic, and a Streamlit dashboard.

## Modules

- `api.py` - Bybit REST/WebSocket integration with retry and rate limiting
- `strategy.py` - EMA, RSI, and MACD based signal generation
- `trader.py` - execution engine, risk controls, logging, and position handling
- `ui.py` - Streamlit dashboard
- `main.py` - application entrypoint
- `backtest.py` - simple historical backtesting

## Features

- Spot and derivatives support through Bybit category selection
- Long and short trading
- Leverage configuration for derivatives
- Stop-loss, take-profit, and risk-based position sizing
- Trend filter, ATR filter, and cooldown between trades
- WebSocket market data with polling fallback
- Paper trading mode
- Multiple symbols
- Trade logs in JSONL
- Optional Telegram alerts

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` before running. Keep `PAPER_TRADING=true` unless you intentionally want live trading with valid Bybit credentials.

Key tuning values:

- `RSI_LONG_THRESHOLD` / `RSI_SHORT_THRESHOLD` tighten entries
- `TREND_EMA` requires trading with the larger trend
- `ATR_MIN_PCT` skips low-volatility chop
- `SIGNAL_SCORE_THRESHOLD` controls how much indicator agreement is required
- `COOLDOWN_BARS` pauses re-entry after a closed trade
- `FEE_RATE` makes backtests more realistic
- `INVERT_SIGNALS=true` flips every buy to a sell and every sell to a buy
- `FILTER_SYMBOLS_BY_BACKTEST=true` only trades symbols that pass the startup backtest gate
- `MIN_BACKTEST_PROFIT_FACTOR`, `MAX_BACKTEST_DRAWDOWN_PCT`, and `MIN_BACKTEST_TRADES` control symbol approval

## Run

```powershell
streamlit run main.py
```

## Logs

- Trade history: `logs/trades.jsonl`
- Runtime logs: `logs/bot.log`

## Safety Notes

- Start in testnet and paper mode.
- Confirm symbol, category, and leverage settings before live use.
- Review exchange precision and minimum order constraints for your chosen markets.
