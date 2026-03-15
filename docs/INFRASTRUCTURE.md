# Infrastructure & Codebase Reference

## Deployment

| Component | Detail |
|-----------|--------|
| VPS | Hetzner Cloud, IP `89.167.101.87`, SSH alias `oneminman` |
| Container runtime | Docker Compose (`/root/1min/docker-compose.yml`) |
| Container name | `polymarket-hedge-bot` |
| Dashboard URL | http://89.167.101.87:8501 |
| Git remote | `vasiok` (SSH) → GitHub: `Sleepyforages/1min` |
| Python | 3.12-slim |
| Memory limit | 512 MB |
| CPU limit | 1.0 vCPU |

### Volumes (persisted outside container)
```
./data   → /app/data      (backtest CSVs, future trade records)
./logs   → /app/logs      (bot.log — tailed by UI)
./config → /app/config    (default.yaml — written by UI, read by bot)
```

### Startup sequence
1. Streamlit launches `src/ui.py` on port 8501
2. User clicks "Start Bot" → UI spawns `src/bot.py:main()` in a subprocess
3. Bot runs its cycle loop; logs go to `logs/bot.log`
4. Config is hot-reloaded from `config/default.yaml` at the start of each cycle

---

## Repository Layout

```
polymarket-hybrid-hedge-bot/
├── src/
│   ├── __init__.py
│   ├── bot.py              # Main loop, orchestration
│   ├── config.py           # Config dataclass + YAML load/save
│   ├── market_discovery.py # Polymarket API — find live markets
│   ├── executor.py         # Order placement (paper + live)
│   ├── strategy.py         # Signal generation, progression, filters
│   ├── price_feed.py       # OHLCV data (Massive.com / Binance / Bybit)
│   ├── backtester.py       # Vectorised historical simulation
│   ├── alerts.py           # Telegram notifications
│   └── ui.py               # Streamlit dashboard
├── config/
│   └── default.yaml        # Runtime configuration (overwritten by UI)
├── docs/
│   ├── SCENARIOS.md        # Trading strategies and experiments
│   └── INFRASTRUCTURE.md   # This file
├── data/                   # backtest_trades.csv (git-ignored)
├── logs/                   # bot.log (git-ignored)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env                    # Credentials — NOT in git
└── .env.example
```

---

## Module-by-Module Description

### `config.py`
Single source of truth for all settings. `Config` is a Python dataclass. Credentials (`POLYMARKET_PRIVATE_KEY`, `POLYMARKET_API_KEY`, etc.) come exclusively from environment variables — never from YAML.

`load_config()` reads `config/default.yaml`, filters unknown keys, returns `Config`.
`save_config()` writes non-credential fields back to YAML (called by UI on "Save Config").

**Config hot-reload:** The bot calls `load_config()` at the start of every cycle, so UI changes take effect within one trading window without restart.

---

### `market_discovery.py`
Finds active "Up or Down" Polymarket markets for the current time window.

**Algorithm:**
1. Compute current window end timestamp: `ceil(now / interval) * interval`
2. For each asset, build slug: `{asset}-updown-{interval}-{unix_ts}`
3. Query Gamma API: `GET /events?active=true&slug={slug}` → get `clobTokenIds`
4. Optionally verify CLOB liveness: `GET https://clob.polymarket.com/book?token_id={id}` → must return 200
5. Return `List[PolyMarket]` — one per live asset

**Why slug-based?** Earlier approaches using `/markets` pagination returned thousands of zombie December-2025 markets. Direct slug lookup is O(n_assets) with no pagination.

**`PolyMarket` dataclass fields:**
- `condition_id`, `question`, `asset`, `interval`
- `up_token_id`, `down_token_id` (clobTokenIds[0] = Up, [1] = Down)
- `end_date_iso`, `window_start_iso`
- `best_up_ask`, `best_down_ask` (filled by `enrich_with_prices()`)

**`skip_clob_check`:** Set to `True` when `weekend_behavior == "off"` to bypass the liveness check.

---

### `strategy.py`
Signal generation pipeline. Takes (asset, direction) → `TradeSignal`.

**Pipeline order:**
1. Weekend filter → skip if `weekend_behavior=skip` or momentum mismatch
2. H1 momentum filter → skip if H1 bias opposes this direction
3. RSI filter → skip if RSI is overextended for this direction
4. Kelly sizing (if enabled) or `base_bet_usd`
5. Progression calculation (fixed / martingale / fibonacci / dalembert)
6. US-hours multiplier applied
7. Hedge stake = main stake (if `use_hedge=True`)

**`ProgressionState`** — per-asset-direction, tracks:
- `streak_losses` (martingale)
- `fib_step` (fibonacci)
- `dalembert_units` (dalembert)
- `h1_bias_direction` and `h1_bias_trades_left` (shared per-asset for H1)

States persist in `Bot.states` dict across cycles (reset only on win or manually).

---

### `executor.py`
Order routing: paper simulation or live CLOB submission.

**`PaperLedger`** — simulates fills at the requested price, tracks balance and trades.

**`LiveExecutor`** — wraps `py_clob_client.ClobClient`.
- `place_market_buy(token_id, size_usd, price)`:
  1. `create_market_order(MarketOrderArgs)` → returns `SignedOrder` object
  2. `post_order(signed_order, OrderType.FOK)` → returns dict with `orderID`
  3. **Current blocker:** CLOB returns 401 "Invalid api key" — see Issues section

**`Executor`** — unified facade:
- Routes to `PaperLedger` or `LiveExecutor` based on `cfg.mode`
- `execute_signal(sig, market)` → picks correct token ID by direction, calls `_place_order`
- Spawns a `threading.Timer` to close hedge leg after `hedge_sell_trigger_minutes`
- Daily loss limit check before every trade

---

### `price_feed.py`
OHLCV data for RSI and H1 calculation. Priority chain:

1. **Massive.com** (formerly Polygon.io) — REST API, requires `POLYGON_API_KEY`
   URL: `https://api.massive.com/v2/aggs/ticker/X:{ASSET}USD/range/{N}/minute/...`
   **Current issue:** Returns 403 (Forbidden) or 429 (Too Many Requests) for most calls. Falls through to CCXT.

2. **Binance** (via CCXT) — public endpoint, no key required
   Symbol: `{ASSET}/USDT` (fallback: `/USDC`)

3. **Bybit** (via CCXT) — fallback for assets not on Binance (e.g. HYPE)

`get_rsi(asset, period=14, interval="5m")` — computes Wilder RSI from `fetch_ohlcv`.
`get_h1_data(asset)` — fetches 1h candle, returns body_pct and direction.

---

### `bot.py`
Main loop. Runs forever (or until SIGINT/SIGTERM):

```
while running:
    load_config()
    reset_daily_pnl() if new UTC day
    discover_markets()
    enrich_with_prices()  (live mode only)
    for asset in assets:
        for direction in ["up", "down"]:
            generate_signal()
            execute_signal()
            paper_settle()  (paper mode only)
    sleep(interval_seconds)
```

**Parallel mode:** `parallel_assets=True` → one thread per asset, all directions processed within that thread.

**Paper settle:** Simulates win/loss with 52% win probability, fires Telegram alert.

---

### `backtester.py`
Offline simulation. Fetches `bars=500` historical OHLCV bars, simulates all 4 progression methods × hedge on/off combinations.

Direction signal: `close > open` of completed bar.
Entry price assumed: $0.50 (NOT real ask prices).
Hedge exit: `hedge_sell_price_trigger` ($0.20) — not order-book-based.
Output: `trades_df` (all individual trades) + `summary_df` (per-method stats).

---

### `alerts.py`
Telegram bot integration. Sends messages when:
- Bot starts
- Trade wins
- Trade loses
- Drawdown exceeds threshold
- Daily loss limit hit

Only fires when `telegram_alerts_enabled=True` and credentials are set.

---

### `ui.py`
Streamlit dashboard. Single-page, tabbed:

| Tab | Contents |
|-----|----------|
| Controls | Start/Stop bot, config sidebar (all settings), current config JSON |
| Live Monitor | Bot status, next window countdown, trade feed |
| Backtest | Run backtest, show summary table and trade chart |
| Logs | Scrollable log viewer with "Clear" button, auto-refreshes every 10s |

Bot process managed via `subprocess.Popen`. PID tracked in `st.session_state`.

---

## External APIs

| API | Purpose | Auth | Current Status |
|-----|---------|------|---------------|
| Polymarket Gamma API | Market/event lookup | None | ✅ Working |
| Polymarket CLOB | Order placement, order book | L1 (EIP-712) + L2 (HMAC) | ❌ 401 Unauthorized |
| Massive.com (Polygon.io) | OHLCV price data | API key | ❌ 403/429 errors |
| Binance (CCXT) | OHLCV fallback | None (public) | ✅ Working |
| Bybit (CCXT) | OHLCV fallback for HYPE | None (public) | ✅ Working |
| Telegram Bot API | Alerts | Bot token + chat ID | Configured, not tested |

---

## Known Issues & Blockers

### BLOCKER: CLOB 401 Unauthorized
All live order attempts fail with:
```
PolyApiException[status_code=401, error_message={'error': 'Unauthorized/Invalid api key'}]
```
L1 auth (EIP-712 signature with private key) also returns:
```
PolyApiException[status_code=401, error_message={'error': 'Invalid L1 Request headers'}]
```
**Root cause:** Unknown. Wallet address `0xB7253d998bC266e1d2031BbF4AbD7f771d5e9D32` either:
- Is not registered / has never interacted with Polymarket's CLOB
- The API credentials in `.env` were generated from a different key or nonce
- Polymarket uses a proxy wallet architecture and the private key may be for the proxy, not the EOA
**Resolution needed:** The user needs to verify wallet registration on Polymarket and re-derive or re-create API credentials.

### Massive.com 403/429
Price feed falls back to Binance (working). Not critical but adds latency and burns rate limit quota.

### Backtester uses $0.50 entry price
Real ask prices are ~$0.99, making backtest P&L figures unrealistic.

### `eip712_structs` shim
`py_clob_client` imports `eip712_structs` which is not the installed package name. A post-install shim in the Dockerfile creates a redirect: `eip712_structs/__init__.py` → `from poly_eip712_structs import *`. If this shim is incorrect, EIP-712 signing will produce wrong hashes.

---

## Credentials Required (`.env`)

```bash
POLYMARKET_PRIVATE_KEY=0x...    # EVM private key for signing
POLYMARKET_API_KEY=...           # CLOB L2 auth key (derived from private key)
POLYMARKET_API_SECRET=...        # CLOB L2 auth secret
POLYMARKET_API_PASSPHRASE=...    # CLOB L2 auth passphrase
POLYGON_API_KEY=...              # Massive.com (optional, falls back to Binance)
TELEGRAM_BOT_TOKEN=...           # Optional
TELEGRAM_CHAT_ID=...             # Optional
```

API key derivation: Use `ClobClient(host, chain_id=137, key=private_key).create_or_derive_api_creds()` after wallet is registered on Polymarket.
