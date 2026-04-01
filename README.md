# Bybit Trading Bot

Python 3.10+ crypto trading bot with official Bybit integration, paper/live trading, technical-indicator strategy logic, and a FastAPI control UI.

## Modules

- `api.py` - Bybit REST/WebSocket integration with retry and rate limiting
- `strategy.py` - EMA, RSI, and MACD based signal generation
- `trader.py` - execution engine, risk controls, logging, and position handling
- `webapp.py` - FastAPI routes, auth session handling, and HTML rendering
- `templates/` - lightweight HTML pages for login and control/dashboard views
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
- `DASHBOARD_REFRESH_SECONDS` controls how often the full dashboard refreshes
- `CONTROL_REFRESH_SECONDS` controls how often the lighter phone control page refreshes
- `MOBILE_DEFAULT_VIEW=true` makes the lighter control page the default first screen

## Run

```powershell
python main.py
```

The app serves on `http://<host>:8501` by default. For phone access over Tailscale, the control page is:

```text
http://<your-tailnet-host>:8501/control
```

For phone access over Tailscale, open the lighter control page directly:

```text
http://<your-tailnet-host>:8501/?view=control
```

## Web Auth

This app supports a built-in login flow with role-based permissions, Google Authenticator TOTP, and a persistent signed session cookie.

Configure these values in `.env`:

- `AUTH_ENABLED=true`
- `AUTH_USERS_FILE=secrets/auth_users.json`
- `AUTH_SESSION_MINUTES=480`
- `AUTH_TOTP_ISSUER=Bybit Trading Bot`
- `AUTH_COOKIE_SECRET=change-this-to-a-long-random-secret`
- `AUTH_COOKIE_NAME=bybit_bot_session`
- `APP_HOST=0.0.0.0`
- `APP_PORT=8501`

First-time setup:

1. Start the app with `AUTH_ENABLED=true`.
2. If no users exist yet, the app shows a bootstrap screen.
3. Create the first admin user.
4. Scan the generated QR code with Google Authenticator.
5. Log in with username, password, and the 6-digit TOTP code.

Built-in roles:

- `viewer` - can view the dashboard
- `analyst` - can view the dashboard and run backtests
- `operator` - can view, run backtests, and start or stop the bot
- `admin` - same runtime permissions as operator, intended for full control

Auth audit logs are written to `logs/auth_audit.jsonl` by default.

## Logs

- Trade history: `logs/trades.jsonl`
- Runtime logs: `logs/bot.log`

## Safety Notes

- Start in testnet and paper mode.
- Confirm symbol, category, and leverage settings before live use.
- Review exchange precision and minimum order constraints for your chosen markets.
