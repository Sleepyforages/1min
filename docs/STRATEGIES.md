# Strategy Compendium — Polymarket 5-Minute Binary Bot

*Written: 2026-03-16. Covers all strategy variants designed, built, and partially tested as of this date.*

---

## What We Are Trading

**Polymarket 5-minute "Up or Down" binary prediction markets on crypto assets.**

Every 5 minutes (also 15 minutes optionally), Polymarket creates a new market per asset:
> *"Will BTC be higher or lower at 18:55 UTC than it was at 18:50 UTC?"*

Two outcome tokens exist: **Up** and **Down**. Each resolves to exactly **$1.00** if correct, **$0.00** if wrong. You buy at whatever price the market offers — typically between $0.01 and $0.99 per share depending on perceived probability.

**Assets:** BTC, ETH, SOL, XRP, DOGE, HYPE, BNB
**Window:** 5 minutes (primary), 15 minutes (secondary)
**Structure:** One market per asset covers both Up and Down tokens simultaneously

---

## The Core Signal

The primary directional signal used throughout all strategy versions is:

> **If the last completed 5-minute candle closed higher than it opened → bet Up. Otherwise → bet Down.**

This is pure short-term price momentum. No predictive claim is made about its accuracy beyond approximately 50%. All filters and enhancements described below are layered on top of this signal to attempt to improve that base rate.

---

## Strategy Versions

---

### V1 — Baseline: Raw Momentum, Fixed Sizing, No Hedge

**Logic:**
One bet per asset per cycle, direction from last closed bar. No filters applied. Fixed dollar amount per trade, never adjusted. Both Up and Down are treated equally — the signal picks one. 

**Configuration:**
- Fixed $1 stake
- No RSI filter
- No H1 filter
- No hedge
- Trades every cycle regardless of conditions

**Theoretical edge:**
None guaranteed. Win rate approximately 50% before fees.

---

### V2 — Momentum + Progression Systems (Four Variants)

Same core signal as V1, but the stake size changes based on prior results.

#### V2a — Martingale
After each loss, the stake doubles, side always remains the same. After a win, stake resets to $1.
Cap at 7 (variable) doublings: loss sequence of 7 → stakes of $1, $2, $4, $8, $16, $32, $64, $128.

- **Best case:** One win at the end of a long losing streak recovers all prior losses plus $1 profit.
- **Worst case:** 7+ consecutive losses (roughly 0.8% probability per series at 50% win rate) exhausts the progression and results in maximum draw.
- **Total exposure at cap:** ~$255 to win back $1.

#### V2b — Martingale shifted
After each loss, the stake doubles, side remains the same until end of sequence. After end of a sequence side changes. After a win, stake resets to $1.
Cap at 7 (variable) doublings: loss sequence of 7 → stakes of $1, $2, $4, $8, $16, $32, $64, $128.

- **Best case:** One win at the end of a long losing streak recovers all prior losses plus $1 profit.
- **Worst case:** 7+ consecutive losses (roughly 0.8% probability per series at 50% win rate) exhausts the progression and results in maximum draw.
- **Total exposure at cap:** ~$255 to win back $1.

#### V2c — Fibonacci
Stake follows the Fibonacci sequence multiplied by the base stake: 1, 1, 2, 3, 5, 8, 13, 21...
Steps forward on a loss, steps back two places on a win.

- Slower escalation than Martingale, less aggressive recovery.
- More resilient to medium-length loss streaks.

#### V2d — D'Alembert
Stake increases by $1 on every loss, decreases by $1 on every win. Floor is always the base stake.

- The most conservative progressive method.
- Near-balanced position after equal wins and losses.
- Best suited for assets with highly volatile win/loss alternation patterns.

**Estimated results across all progression types (50% win rate baseline):**
All four methods have identical mathematical expectation at exactly 50% win rate. The differences are purely in variance and drawdown shape. If win rate is > 50%, progression amplifies gains. If < 50%, it amplifies losses. At 50% exactly, they all lose slowly to fees.

---

### V3 — Momentum + RSI Filter

Same signal as V1 but with a momentum-exhaustion filter:

**Logic:**
- Skip Up bets when RSI (14-period, 5-minute bars) is below 45 — market already pushed down, potential snap-back
- Skip Down bets when RSI is above 55 — market already pushed up, potential snap-back
- When RSI is in the 45–55 neutral zone, allow any direction

**Hypothesis:**
RSI extremes indicate momentum is already exhausted in one direction. Trading in the opposite direction of an RSI extreme has lower probability of success.

**Trade-off:**
Reduces trade frequency by approximately 30–40% (most signals fall outside the neutral zone). In theory this should raise win rate on the trades that do execute. In practice, whether RSI on 5-minute crypto bars has this predictive power at the 5-minute resolution is unconfirmed.

**Estimated result:**
No confirmed data. Theoretically 52–55% win rate on selected trades if hypothesis holds. The reduction in trade count means fewer fees paid but also fewer opportunities for progression recovery.

---

### V4 — Momentum + H1 Bias Filter

**Logic:**
Before placing any 5-minute trade, examine the current 1-hour (H1) candle:

- If the H1 body is larger than 0.3% of price AND the candle is bullish → only take Up bets for the next 12 five-minute windows (~1 hour)
- If the H1 body is large AND bearish → only take Down bets for the next 12 windows
- If the H1 body is small (choppy hour) → no directional bias; fall through to base signal

When a strong H1 bias is active, the progression method is automatically switched to **Martingale** to press the perceived edge harder while the trend is in play.

**Hypothesis:**
A strong 1-hour candle implies genuine directional pressure that is likely to persist through multiple 5-minute sub-windows. Trading only with this momentum for the hour should improve win rate materially.

**Trade-off:**
During a strong H1 bias, this strategy is essentially single-direction. If the H1 signal is wrong (head-fake candle), consecutive losses are suffered in a single direction with an escalating Martingale behind them — the most dangerous combination.

**Estimated result:**
Unconfirmed. If H1 momentum persistence is real at this resolution, win rates of 55–60% during bias windows are plausible. Outside bias windows the strategy falls back to base momentum (~50%).

---

### V5 — Full Filter Stack: H1 + RSI Combined

**Logic:**
Both filters run in series. A trade is only placed when:
1. H1 bias direction matches the momentum signal (or no strong H1 bias exists), AND
2. RSI is not overextended against the trade direction

This is the most selective configuration — a meaningful portion of cycles produce no trade at all.

**Hypothesis:**
Only enter when multiple independent indicators agree. Each filter independently improves signal quality; combined they should produce the highest-quality entry subset.

**Trade-off:**
Potentially 50–70% of all potential trades are skipped. This dramatically slows progression recovery if a losing streak occurs — fewer opportunities to get back to breakeven. But each trade that does execute should have the highest available probability of winning.

**Estimated result:**
No confirmed live data. Theoretically highest win rate of any variant — potentially 55–62% on executed trades — but with lowest trade count and slowest progression cycle.

---

### V6 — Hybrid Hedge

**Logic:**
On every trade, two opposite positions are opened simultaneously:

1. **Main leg:** Full stake in the signal direction
2. **Hedge leg:** Matching stake in the opposite direction

After a short fixed delay (default: 2.5 minutes into the 5-minute window), the hedge leg is sold at whatever the current market price is — typically around $0.20.

**Hypothesis:**
At reasonable fill prices, the hedge cap on losses is worth the cost. Over enough cycles the variance reduction compounds to a smoother equity curve even if expectation is similar.


---

### V7 — US Hours Multiplier

**Logic:**
During 14:00–20:00 UTC (US market open hours, 10 AM–4 PM EST), all bet sizes are scaled up by 2×. All other strategy logic remains identical.

**Hypothesis:**
US market hours produce meaningfully higher crypto volatility. Higher volatility at the 5-minute scale means price moves are more decisive (larger bar bodies, cleaner direction). Larger bet sizes during these hours exploit the presumed edge at its highest point.


---

### V8 — Weekend Mode Variants

On Saturday and Sunday, market dynamics differ: lower overall volume, but Polymarket markets still run 24/7.

Three modes:

**Skip:** No trading on weekends at all. Zero P&L contribution. Simplest and safest.

**Momentum-Only:** Only trade when the last bar's momentum direction is strong and unambiguous. More selective than weekday behavior.

**Off (trade normally):** Run the full strategy with no weekend adjustment, but skip the CLOB market liveness check (markets confirmed running 24/7).


---

### V9 — Kelly Criterion Sizing

**Logic:**
Instead of a fixed stake, compute bet size dynamically using the Kelly Criterion:

> *Optimal stake = (edge × bankroll) / odds*

At 4% estimated edge with half-Kelly safety factor, on a $100 bankroll: optimal stake ≈ $2 per trade.

The stake is then capped at 5% of bankroll maximum regardless of what Kelly calculates.

**Hypothesis:**
Mathematically optimal bet sizing for a positive-expectation game. Maximizes long-run growth rate while preventing ruin. Half-Kelly is used (not full Kelly) for safety against edge estimation error.

**Critical caveat:**
Kelly only produces positive expected value if the edge estimate is accurate. A 4% estimated edge that is actually 0% or negative makes Kelly sizing actively harmful.


---
