# Bybit Trading Bot

Python 3.10+ crypto trading bot with official Bybit integration, paper/live trading, technical-indicator strategy logic, and a FastAPI control UI.

## Modules

- `api.py` - Bybit REST/WebSocket integration with retry and rate limiting
- `strategy.py` - confluence-scored trend / breakout / pullback signals with a cost-aware entry gate (see Strategy below)
- `trader.py` - execution engine, risk controls, logging, and position handling
- `webapp.py` - FastAPI routes, auth session handling, and HTML rendering
- `templates/` - lightweight HTML pages for login and control/dashboard views
- `main.py` - application entrypoint
- `backtest.py` - bar-by-bar backtester that mirrors the live exit logic (also the start-up symbol gate)
- `costs.py` - the single cost model (fees, slippage, cost-derived volatility floor) shared by strategy, trader and backtester
- `research.py` / `research_sweep.py` / `research_sizing.py` - walk-forward harness, candidate sweep, sizing study
- `strategies_research.py` - alternative strategies selectable with `STRATEGY_NAME`
- `parity_check.py` - proves the backtester and the paper trader take identical exits

## Features

- Spot and derivatives support through Bybit category selection
- Long and short trading
- Leverage configuration for derivatives
- Stop-loss, take-profit, and risk-based position sizing
- Trend, volatility, volume, and range filters tuned for intraday trading
- WebSocket market data with polling fallback
- Paper trading mode
- Multiple symbols
- Trade logs in JSONL
- Optional Telegram alerts

## Strategy

The shipped strategy (`STRATEGY_NAME=indicator`, class `IndicatorStrategy` in `strategy.py`) is a confluence scorer: it adds up points from independent indicators on every closed candle and trades only when enough of them agree, the setup is volatile enough to pay for its own transaction costs, and a breakout or a pullback confirms the direction. Signals are generated only on **confirmed (closed) candles**; unconfirmed ticks never reach the strategy.

### 1. Indicators (`apply_indicators`)

All indicators are causal (rolling / exponential / shifted), so a value on bar *t* uses only bars up to *t*.

| Indicator | Definition | Env keys |
|---|---|---|
| Fast / slow EMA | EMA of close, spans 9 and 21 | `EMA_FAST`, `EMA_SLOW` |
| Trend EMA | EMA 50, the regime filter | `TREND_EMA` |
| EMA spread | `abs(ema_fast - ema_slow) / close` | `MIN_TREND_STRENGTH_PCT` |
| RSI | 14-period, simple-average gains/losses | `RSI_PERIOD` |
| MACD | 12 / 26 with a 9 signal line, plus histogram and its change | `MACD_FAST`, `MACD_SLOW`, `MACD_SIGNAL` |
| ATR | 14-period simple average of true range, also as `atr / close` | `ATR_PERIOD` |
| ADX | Wilder-style directional index, 14 periods | `ADX_PERIOD` |
| Volume ratio | `volume / 20-bar average volume` | `VOLUME_MA_PERIOD` |
| Breakout levels | Highest high / lowest low of the previous 12 bars (shifted by one, so the current bar cannot break its own level) | `BREAKOUT_LOOKBACK` |
| Range width | `(breakout_high - breakout_low) / close` | `MIN_RANGE_WIDTH_PCT` |
| Pullback levels | Lowest low / highest high of the previous 4 bars | `PULLBACK_LOOKBACK` |
| Momentum | 4-bar percentage change of close | `PULLBACK_LOOKBACK` |
| Candle quality | body / range ratio and body size in % of close | `MIN_BODY_TO_RANGE_RATIO` |

### 2. Base score

Each side (long / short) starts at zero and collects:

- **+2** trend: `ema_fast > ema_slow` and `close > trend_ema` (mirror for short)
- **+1** RSI in the momentum band: long needs `RSI_LONG_THRESHOLD <= RSI <= MAX_RSI_LONG`, short needs `MIN_RSI_SHORT <= RSI <= RSI_SHORT_THRESHOLD`
- **+1** MACD: line above signal with a positive histogram that is not shrinking (`REQUIRE_MACD_HIST_IMPROVING`), mirrored for short

### 3. Hard filters (any failure returns `hold` and discards the score)

1. **Volatility floor.** `atr_pct` must be at least `IndicatorStrategy.min_atr_pct()`, which is the largest of `ATR_MIN_PCT`, the floor implied by `MIN_TARGET_TO_COST_RATIO`, and the floor implied by `MIN_NET_REWARD_RISK`. The last two are derived from the cost model, so changing the fee tier or the ATR multiples re-tunes the floor automatically:
   `floor_rr = round_trip * (1 + MIN_NET_REWARD_RISK) / (ATR_TARGET_MULTIPLE - MIN_NET_REWARD_RISK * ATR_STOP_MULTIPLE)`.
   If the denominator is zero or negative the floor is infinite and the strategy never trades, so widen the stop and the target together.
2. **ADX** at least `MIN_ADX` (trend strength).
3. **Volume ratio** at least `MIN_VOLUME_RATIO`.
4. **EMA spread** at least `MIN_TREND_STRENGTH_PCT`.
5. **Range width** at least `MIN_RANGE_WIDTH_PCT` (the breakout channel must be wide enough to matter).

### 4. Confirmation score

- **+2** breakout: close beyond the 12-bar high / low by `BREAKOUT_BUFFER_PCT`
- **+2** pullback: in a trend, price retested the fast EMA within `EMA_RETEST_TOLERANCE_PCT` during the last 4 bars and closed back on the trend side of it
- **+1** 4-bar momentum in the same direction
- **+1** volume confirmation: volume ratio at least `1.15 x MIN_VOLUME_RATIO`, awarded to whichever side already leads
- **+1** fast-EMA slope in the same direction
- **+1** strong candle: body at least `MIN_BODY_TO_RANGE_RATIO` of the range and at least `BREAKOUT_BUFFER_PCT` of price, closing in the signal direction

The maximum is 10 points. A side becomes a signal when its score reaches `SIGNAL_SCORE_THRESHOLD`, beats the other side, **and** a breakout or pullback confirmed it. `confidence = score / 10`.

### 5. Extreme entry mode (`EXTREME_ENTRY_MODE=true`)

Three vetoes judged on the detected direction: the setup must have a breakout or pullback (`REQUIRE_BREAKOUT_CONFIRMATION`), the entry candle must be strong, and `confidence` must reach `MIN_SIGNAL_CONFIDENCE`. Because confidence is `score / 10`, `MIN_SIGNAL_CONFIDENCE=0.62` means **at least 7 of 10 points**, which makes it the binding threshold rather than `SIGNAL_SCORE_THRESHOLD=5`.

### 6. Inversion

`INVERT_SIGNALS=true` flips buy to sell and sell to buy **here**, before exits are priced, so a short always carries a stop above its entry and a target below it. Inverting downstream (in the trader or backtester) once produced a fake 100% win rate; keep it inside the strategy.

### 7. Exits and the cost gate (`_finalize_signal`)

Stop and target are ATR multiples of the close: `stop = close -/+ ATR x ATR_STOP_MULTIPLE`, `target = close +/- ATR x ATR_TARGET_MULTIPLE`. The trade is then priced by `costs.assess_edge()`:

```
net_target = target_pct - round_trip
net_risk   = stop_pct + round_trip          # the round trip is paid on losers too
net_rr     = net_target / net_risk
expectancy = confidence * net_target - (1 - confidence) * net_risk
```

and rejected if `net_target <= 0`, `net_target < MIN_EXPECTED_MOVE_PCT`, `net_target < MIN_TARGET_TO_COST_RATIO x round_trip`, `net_rr < MIN_NET_REWARD_RISK`, or the expectancy is not positive (`REQUIRE_POSITIVE_EXPECTANCY`). Every rejection reason is written into the signal and shows up in `logs/bot.log`, so each skipped setup is explained.

The round trip is asymmetric: entries are post-only limit orders (maker fee, no slippage, `MAKER_ENTRY_ENABLED`), exits are market (taker fee plus `EXIT_SLIPPAGE_PCT`). With the default Bybit VIP0 rates it is 0.115% of notional.

### 8. Position management (trader and backtester, kept identical)

- **Entry:** a post-only order rests at the signal close and fills only if the next bar trades *through* it (strict inequality stands in for queue position). It is cancelled after `MAKER_ENTRY_TIMEOUT_BARS` or if price runs away by more than `MAKER_MAX_CHASE_PCT`; `MAKER_ENTRY_FALLBACK=market` opens at market instead of skipping.
- **Sizing:** `qty = min(balance x RISK_PER_TRADE / stop_distance, balance x MAX_POSITION_NOTIONAL_PCT / price)`. Leverage only sets the exchange margin; it never changes the quantity or the simulated return.
- **Each closed bar, in this order:** liquidation test (backtest only) -> stop test against the bar low/high -> target test -> `MAX_BARS_HELD` time stop at the close -> opposite-signal flip at the close -> break-even and trailing-stop update from the bar's high/low. The stop is tested before the target, and the protective stop is advanced only *after* the test, so a stop raised by the bar's own high cannot rescue the position within that bar.
- **Break-even:** after a favorable move of `BREAK_EVEN_TRIGGER_PCT`, the stop moves to entry plus the round trip plus `BREAK_EVEN_OFFSET_PCT` (validation requires the trigger to exceed twice the round trip).
- **Trailing (`TAKE_PROFIT_MODE=trail`):** the fixed target is dropped and, once break-even has armed, the stop trails the peak by `TRAIL_ATR_MULTIPLE x ATR` at entry.
- **Risk gates:** per-symbol cooldown of `COOLDOWN_BARS` after a close, a global entry pause of `COOLDOWN_AFTER_LOSS_BARS` after `MAX_CONSECUTIVE_LOSSES`, at most `MAX_OPEN_POSITIONS` open, and at start-up a backtest over `BACKTEST_FILTER_CANDLES` candles that must pass `MIN_BACKTEST_PROFIT_FACTOR`, `MAX_BACKTEST_DRAWDOWN_PCT`, `MIN_BACKTEST_TRADES` and `MIN_BACKTEST_EXPECTANCY_R` for a symbol to be traded (`FILTER_SYMBOLS_BY_BACKTEST`, `REQUIRE_BACKTEST_APPROVAL`).

### Alternative strategies (`STRATEGY_NAME`)

Both share the indicator set, the volatility floor, inversion and the cost gate above; only the entry rule differs.

- `htf_trend` (`HTFTrendBreakout`): long when the close breaks the previous 48-bar high **and** is above EMA 200 (on hourly bars roughly a 4-hour EMA 50), short mirrored. Meant to run with a wide stop (2.5 ATR), `TAKE_PROFIT_MODE=trail`, `TRAIL_ATR_MULTIPLE=3`, `COOLDOWN_BARS=6`, taker entries. Assumed win probability 0.40 for the gate.
- `mean_reversion` (`MeanReversion60`): long when close is at least 2 standard deviations below its 20-bar EMA and RSI is below 30, short mirrored; the target is the EMA itself, so the gate is priced on the real distance to the mean. Meant to run with a 2 ATR stop and `MAX_BARS_HELD=8`. Assumed win probability 0.55.

### What the evidence says

Scored with `research_sweep.py` over eight ~31-day walk-forward windows (hourly, eight symbols, Dec 2025 to Aug 2026, fees and funding included):

| Configuration | Mean %/symbol/window | t | Windows up |
|---|---|---|---|
| Shipped, inverted (was live) | -0.69 | -1.81 | 2/8 |
| Shipped, uninverted | -1.45 | -2.84 | 2/8 |
| Shipped with 1.5 / 3.6 ATR exits | -0.20 | -0.71 | 4/8 |
| `htf_trend`, taker entries | -0.03 | -0.10 | 5/8 |
| `mean_reversion` | -0.24 to -0.47 | -1.0 to -1.6 | 2/8 |

None reaches the acceptance bar (t >= 2.4, 6/8 windows up, pooled profit factor >= 1.35, positive hold-out). A per-coin screen of the 29 most liquid perps found `htf_trend` positive on ZECUSDT (t = 4.7, long side only, during a strong uptrend) and UNIUSDT; details in `data/research/coin_screen.csv`. Coins whose hourly ATR is below about 10x the round-trip cost (BTC, BNB, LTC, stock perps) lost under every rule because the volatility floor blocks most of their bars. Treat the strategy as unproven: paper trade, keep `MAX_POSITION_NOTIONAL_PCT` at or below 1.0, and re-run the sweep before changing anything.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` before running. Keep `PAPER_TRADING=true` unless you intentionally want live trading with valid Bybit credentials.

Key tuning values:

- `TIMEFRAME=5` or `TIMEFRAME=15` switches the bot to 5-minute or 15-minute closed candles
- `EMA_FAST`, `EMA_SLOW`, and `TREND_EMA` control the intraday trend structure
- `RSI_LONG_THRESHOLD`, `RSI_SHORT_THRESHOLD`, `MAX_RSI_LONG`, and `MIN_RSI_SHORT` keep entries out of exhausted moves
- `ATR_MIN_PCT` skips low-volatility chop
- `SIGNAL_SCORE_THRESHOLD` controls how much indicator agreement is required
- `PULLBACK_LOOKBACK` and `EMA_RETEST_TOLERANCE_PCT` control EMA retest entries
- `MIN_VOLUME_RATIO`, `MIN_RANGE_WIDTH_PCT`, and `MIN_BODY_TO_RANGE_RATIO` filter low-quality breakouts
- `BREAKOUT_BUFFER_PCT` avoids triggering on tiny false breakouts
- `ATR_STOP_MULTIPLE` / `ATR_TARGET_MULTIPLE` size exits to market volatility
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
- Prefer `TIMEFRAME=15` first, then try `TIMEFRAME=5` only after checking backtest quality and trade frequency.
- Confirm symbol, category, and leverage settings before live use.
- Review exchange precision and minimum order constraints for your chosen markets.
