# polymarket-hybrid-hedge-bot

A Dockerised trading bot for Polymarket 5-min / 15-min crypto Up/Down markets,
with a full Streamlit UI and four selectable progression methods.

---

## Quick Start

```bash
# 1. Copy and fill in credentials
cp .env.example .env
nano .env

# 2. Build and launch
docker compose up --build

# 3. Open the dashboard
open http://localhost:8501
```

---

## Features

| Feature | Details |
|---|---|
| Markets | All active BTC / ETH / SOL / XRP 5-min or 15-min Up/Down binary markets |
| Progression | Fixed, Martingale, Fibonacci, D'Alembert (UI dropdown) |
| H1 momentum filter | Reads developing 1h candle; if body > 0.3%, locks direction + forces progression for ~12 trades |
| Hedge mode | Buys both sides; sells hedge leg after configurable time/price trigger |
| US-hours boost | Configurable stake multiplier during peak hours (default ×2) |
| RSI filter | Skips overextended entries based on RSI(14) threshold |
| Kelly sizing | Optional half-Kelly dynamic stake based on edge × bankroll |
| Telegram alerts | Win/loss/drawdown notifications via bot token |
| Parallel execution | All assets traded concurrently (one thread per asset per cycle) |
| Weekend mode | Skip trading or allow momentum-only trades on weekends |
| Paper trading | Full simulation — no real orders placed |
| Live trading | Via py-clob-client (Polygon mainnet) |
| Backtester | All 4 methods side-by-side with PNL, drawdown, Sharpe comparison |

---

## Switching Progression Methods

### Via the UI
1. Open `http://localhost:8501`
2. In the sidebar → **Progression Method** dropdown → choose one of:
   - **Fixed (no progression)** — always bet the base stake
   - **Martingale** — double on loss, reset on win
   - **Fibonacci** — advance one Fibonacci step on loss, back two on win
   - **D'Alembert** — add 1 unit on loss, subtract 1 unit on win
3. Set **Progression Cap** (3–7) — controls the maximum stake
4. Click **Save Config** then **Start Bot**

### Via YAML
Edit `config/default.yaml`:
```yaml
progression_type: fibonacci   # fixed | martingale | fibonacci | dalembert
progression_cap: 7
```
The bot hot-reloads this file on every cycle — no restart needed.

---

## Progression Formulas

### Fixed
```
stake = base_bet_usd
```

### Martingale
```
stake = base_bet_usd × 2^(consecutive_losses)
max   = base_bet_usd × 2^cap
```
Resets to `base_bet_usd` on a win.

### Fibonacci
Sequence: `[1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, …]`
```
on loss : advance 1 step  →  stake = base × fib[step]
on win  : retreat 2 steps →  stake = base × fib[max(0, step-2)]
cap     : step is clamped at progression_cap
```
**Example** — `progression_type: fibonacci`, `progression_cap: 7`, `base_bet_usd: 1.0`:

| Loss streak | Fib step | Stake |
|---|---|---|
| 0 | 0 | $1.00 |
| 1 | 1 | $1.00 |
| 2 | 2 | $2.00 |
| 3 | 3 | $3.00 |
| 4 | 4 | $5.00 |
| 5 | 5 | $8.00 |
| 6 | 6 | $13.00 |
| 7 (cap) | 7 | $21.00 |

### D'Alembert
```
on loss : units += 1  (capped at cap−1 extra units)
on win  : units -= 1  (floor = 0)
stake   = base_bet_usd × (1 + units)
```
**Example** — cap 7, base $1:
after 3 losses → stake = $4, then win → $3, win → $2 …

---

## H1 Momentum Filter

The bot reads the **current developing 1h candle** (via CCXT 1h OHLCV) on every cycle and computes:

```
body_pct = abs(h1_close - h1_open) / h1_open
```

### Logic

| Condition | Action |
|---|---|
| `body_pct >= h1_body_threshold` (default 0.3%) | Set directional bias = candle direction |
| Bias is **up** and signal is **down** | Skip the trade (`h1_counter_trend`) |
| Bias is **up** and signal is **up** | Allow trade + force `h1_force_progression` (default: martingale) |
| Bias window expires after `h1_bias_duration_trades` (default 12) | Return to configured progression method |

### Example

```
H1 candle: open=65000, current=65250 → body = 0.38% → bias = UP
Next 12 five-minute cycles: only UP bets fire, progression forced to martingale
After 12 trades (or if a new opposing H1 body forms): bias resets
```

### Config keys

```yaml
h1_filter_enabled: true
h1_body_threshold: 0.003        # 0.3% minimum H1 body to trigger bias
h1_bias_duration_trades: 12     # trades to ride the bias window
h1_force_progression: martingale  # fixed | martingale | fibonacci | dalembert
```

### UI

Sidebar → **H1 Momentum Filter** expander:
- Toggle on/off
- Body threshold slider (0.1–2.0%)
- Bias duration slider (3–24 trades)
- Force progression dropdown

---

## Kelly / Half-Kelly Sizing

When enabled, replaces the static `base_bet_usd` with a dynamic stake:

```
f     = kelly_estimated_edge × kelly_fraction
stake = f × kelly_bankroll_usd
stake = min(stake, bankroll × kelly_max_bet_pct / 100)
stake = max(stake, base_bet_usd)   # floor
```

```yaml
kelly_sizing_enabled: false
kelly_fraction: 0.5          # 0.5 = half-Kelly
kelly_bankroll_usd: 100.0
kelly_estimated_edge: 0.04   # 4% estimated edge
kelly_max_bet_pct: 5.0       # never bet more than 5% of bankroll
```

**Example** — bankroll $100, edge 4%, half-Kelly:
`f = 0.04 × 0.5 = 0.02 → stake = $2.00`

---

## Telegram Alerts

Add to `.env`:
```
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Enable in config:
```yaml
telegram_alerts_enabled: true
telegram_alert_on_win: false    # optional, can be noisy
telegram_alert_on_loss: true
telegram_drawdown_alert_pct: 5.0
```

Alerts fire asynchronously (background thread) and never block the trading loop.

---

## Config Reference

```yaml
mode: paper                  # live | paper | backtest
interval: 5m                 # 5m | 15m
assets: [btc, eth, sol, xrp]
base_bet_usd: 1.0

# Progression
progression_type: fixed      # fixed | martingale | fibonacci | dalembert
progression_cap: 7

# Hedge
use_hedge: true
hedge_sell_trigger_minutes: 2.5
hedge_sell_price_trigger: 0.20

# US-hours multiplier
us_hours_multiplier: 2.0
us_hours_start_utc: 14
us_hours_end_utc: 20

# RSI filter
rsi_filter_enabled: true
rsi_period: 14
rsi_overextended_low: 45
rsi_overextended_high: 55

# H1 momentum filter
h1_filter_enabled: true
h1_body_threshold: 0.003        # 0.3% minimum H1 candle body
h1_bias_duration_trades: 12     # trades to hold the H1 bias
h1_force_progression: martingale

# Kelly sizing
kelly_sizing_enabled: false
kelly_fraction: 0.5
kelly_bankroll_usd: 100.0
kelly_estimated_edge: 0.04
kelly_max_bet_pct: 5.0

# Telegram alerts
telegram_alerts_enabled: false
telegram_alert_on_win: false
telegram_alert_on_loss: true
telegram_drawdown_alert_pct: 5.0

# Execution
parallel_assets: true

# Weekend / risk
weekend_behavior: momentum_only   # skip | momentum_only
dry_run: false
max_daily_loss_pct: 10
log_level: INFO
```

---

## Backtesting

### Run all methods from the UI
1. Go to **Backtest** tab
2. Select assets and bar count
3. Click **Run All Methods (comparison)**
4. The results table shows PNL, drawdown, win rate, and Sharpe for every
   combination of (method × hedge/no-hedge)
5. Click **Export Trades CSV** → `data/backtest_trades.csv`
   (includes a `progression_type` column for each row)

### Run from CLI
```bash
docker compose run --rm bot python -m src.bot
# (set mode: backtest in config/default.yaml first)
```

---

## Environment Variables (.env)

```
POLYMARKET_PRIVATE_KEY=0x...   # Required for live mode
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
POLYGON_API_KEY=...            # Optional — CCXT/Binance used as fallback
```

**The private key is never written to YAML or logs.**

---

## Architecture

```
ui.py               → Streamlit dashboard (config, backtest, monitor, logs)
config.py           → YAML load/save + credential handling via env vars
market_discovery.py → CLOB API pagination, market matching
price_feed.py       → OHLCV + H1 candle data via Polygon.io or CCXT
strategy.py         → Progression sizing, H1 bias, Kelly sizing, RSI/weekend filters
alerts.py           → Telegram fire-and-forget notifications
executor.py         → Paper ledger + live py-clob-client order placement
backtester.py       → Vectorised simulation across all 4 methods
bot.py              → Main loop, parallel assets, hot-reload, daily P&L reset
```

---

## Simulation Example

> "Set `progression_type: fibonacci`, `progression_cap: 7` → matches our simulation"

With $1 base, hedge enabled, RSI filter ON, US-hours ×2:

- During 14–20 UTC: all stakes doubled (e.g. step-4 = $10 instead of $5)
- Hedge leg enters at same stake on the opposite market
- Hedge sold at 2.5 min or when price ≤ $0.20, whichever comes first
- On win: retreat two Fibonacci steps
- After cap (step 7 = $21): stake stays at $21 until the next win

---

## License

MIT
