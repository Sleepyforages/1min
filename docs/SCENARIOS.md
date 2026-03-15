# Trading Scenarios & Experiment Ideas

## What We Are Trading

**Polymarket "Up or Down" binary prediction markets.**
Every 5 or 15 minutes, Polymarket creates a new market for each crypto asset:
> "Will BTC be higher or lower at 18:55 UTC than at 18:50 UTC?"

Two outcome tokens: **Up** and **Down**. Each resolves to $1.00 or $0.00.
Current observed ask prices: ~$0.99 for both tokens simultaneously — meaning the market is nearly empty / wide spread. Entry price is effectively the ask price paid.

Assets traded: BTC, ETH, SOL, XRP, DOGE, HYPE, BNB.
Interval: 5m (primary), 15m (optional).

---

## Core Strategy: Hybrid Hedge

**Hypothesis:** On any short binary market, we can reduce variance by buying *both* Up and Down simultaneously — the "hedge" — and closing the losing leg early once it becomes cheap, letting the winning leg ride to $1.

**Structure per cycle:**
1. Generate a directional signal (main bet direction)
2. Place **Main leg** — bet on the signal direction
3. Place **Hedge leg** — bet on opposite direction (same stake)
4. After `hedge_sell_trigger_minutes` (default: 2.5 min), cancel/sell the hedge leg
5. At window close: one leg pays $1, the other already exited at ~$0.20 (configurable `hedge_sell_price_trigger`)

**Expected math (rough):**
- Main stake $1 @ $0.99 ask → pays $1.01 if wins (≈ break-even)
- Hedge $1 @ $0.99 → sold early at $0.20 → lose $0.79 on hedge
- Net per cycle: either +$0.02 (win) or -$1.79 (loss + hedge loss)

> **Open question:** The current ask prices (~$0.99 for BOTH sides) suggest extremely thin liquidity. Real fill prices and the market maker spread are the key unknowns.

---

## Signal Filters (Experiments)

### 1. RSI Filter
**Idea:** Skip trades where RSI suggests the move is already exhausted.
- Skip **UP** bet if RSI < 45 (oversold, already pushed down, may snap back)
- Skip **DOWN** bet if RSI > 55 (overbought, already pushed up, may snap back)

Config: `rsi_filter_enabled`, `rsi_overextended_low=45`, `rsi_overextended_high=55`

**Status:** Currently enabled. RSI computed from 5m OHLCV (Binance fallback).
**Open question:** Whether a 14-period RSI on 5m bars has any predictive value for the next 5-minute binary outcome.

---

### 2. H1 Momentum Filter
**Idea:** Use the current 1-hour candle direction as a bias. If the H1 candle has a strong body (>0.3%), assume momentum will continue for the next ~12 five-minute windows.

- When H1 is strongly bullish → only take UP bets (skip DOWN)
- When H1 is strongly bearish → only take DOWN bets (skip UP)
- During the bias window, override progression with Martingale to press the edge

Config: `h1_filter_enabled`, `h1_body_threshold=0.003`, `h1_bias_duration_trades=12`, `h1_force_progression=martingale`

**Status:** Currently enabled.
**Open question:** Whether H1 momentum has any predictive power at the 5-minute resolution.

---

### 3. No Filter (Baseline)
**Idea:** Bet both Up and Down on every single asset every cycle, regardless of RSI or H1. Pure volume play.

This is what happens when all filters are disabled. Pure progression/edge hypothesis.

---

## Bet Sizing & Progression Systems

### Fixed (baseline)
Always bet `base_bet_usd` ($1). No progression.
Expected outcome: pure win-rate test.

### Martingale
Double the stake after each loss, reset on win. Cap at 7 doublings → max stake = $128.
**Risk:** 7 consecutive losses = $127 + $1 base = $255 total exposure.
**Hypothesis:** Binary markets with ~50% win rate → Martingale recovers losses quickly. Works until a long loss streak hits the cap.

### Fibonacci
Stake follows Fibonacci sequence (×base) on loss, steps back 2 on win.
Sequence: 1, 1, 2, 3, 5, 8, 13, 21... × $1
Slower escalation than Martingale, more conservative.

### D'Alembert
Add $1 to stake on loss, subtract $1 on win. Floor = base_bet. Cap = base × progression_cap.
Most conservative of the three progressive methods.

---

## Multipliers & Time-of-Day

**US Hours Multiplier:** 2× stake during 14:00–20:00 UTC (US market open).
**Hypothesis:** Higher crypto volatility during US hours → more decisive 5m moves → better win rate.

---

## Weekend Behavior

Three options:
- `skip` — do nothing on weekends
- `momentum_only` — only bet in the direction of the detected momentum signal
- `off` — trade normally, skip the CLOB liveness check

Markets run 24/7 on Polymarket. The `off` mode was added after confirming markets are live on weekends.

---

## Backtest Mode

Simulates all 4 progression methods simultaneously on historical OHLCV data.
Direction signal: **close > open** of the 5m bar = UP, else DOWN.
Hedge simulated as: enter opposite, exit at `hedge_sell_price_trigger` ($0.20).
Entry price assumed: $0.50 (binary midpoint).

**Limitations of current backtest:**
- Does not model actual ask prices (uses fixed $0.50 assumption)
- Hedge exit is approximated, not simulated from the order book
- RSI filter applied, H1 filter NOT applied in backtest
- No slippage or fee modeling

---

## Key Open Experiments (Not Yet Validated)

| # | Experiment | What to Test |
|---|-----------|--------------|
| 1 | **Can orders actually fill?** | 401 auth error currently blocking all live orders — needs credentials fix |
| 2 | **What are real fill prices?** | $0.99 ask on both sides looks anomalous — real spread unknown |
| 3 | **RSI predictive value** | Disable RSI filter and compare win rate vs. with filter |
| 4 | **H1 bias value** | Disable H1 filter and compare directional accuracy |
| 5 | **Hedge profitability** | Is the hedge closing at $0.20 actually reducing variance or just eating P&L? |
| 6 | **Martingale vs Fixed over 100 cycles** | Run paper mode with both, compare risk-adjusted P&L |
| 7 | **Asset selection** | Are some assets more predictable than others at 5m resolution? |

---

## Current Status (as of 2026-03-15)

- **Market discovery:** Working — finds all 7 assets via slug-based Gamma API lookup
- **Price feeds:** Working — Binance CCXT fallback (Massive.com 429/403 errors on bulk calls)
- **Order placement:** BLOCKED — `401 Unauthorized/Invalid api key` from CLOB
- **Paper mode:** Available and functional
- **Dashboard:** Running at http://89.167.101.87:8501
