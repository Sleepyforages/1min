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
| Hedge mode | Buys both sides; sells hedge leg after configurable time/price trigger |
| US-hours boost | Configurable stake multiplier during peak hours (default ×2) |
| RSI filter | Skips overextended entries based on RSI(14) threshold |
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

## Config Reference

```yaml
mode: paper                  # live | paper | backtest
interval: 5m                 # 5m | 15m
assets: [btc, eth, sol, xrp]
base_bet_usd: 1.0
progression_type: fixed      # fixed | martingale | fibonacci | dalembert
progression_cap: 7           # 3–7 (used only for progressive methods)
use_hedge: true
hedge_sell_trigger_minutes: 2.5
hedge_sell_price_trigger: 0.20
us_hours_multiplier: 2.0
us_hours_start_utc: 14
us_hours_end_utc: 20
rsi_filter_enabled: true
rsi_period: 14
rsi_overextended_low: 45
rsi_overextended_high: 55
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
ui.py          → Streamlit dashboard (config, backtest, monitor, logs)
config.py      → YAML load/save + credential handling via env vars
market_discovery.py → CLOB API pagination, market matching
price_feed.py  → OHLCV via Polygon.io or CCXT (Binance public)
strategy.py    → Progression sizing, RSI filter, weekend filter, signal gen
executor.py    → Paper ledger + live py-clob-client order placement
backtester.py  → Vectorised simulation across all 4 methods
bot.py         → Main loop, hot-reload, daily P&L reset
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
