# Experiments Reference

Locked definitions. Do not change without user approval.

---

## Shared Timing Contract (all experiments)

```
T - 30s   Wake: discover next-window markets
T - 15s   Check last-trade prices of current window tokens
            price >= 0.80 on either side → confirmed winner
T - 10s   Place pre-market limit orders for next window
T + 0s    Window closes / next window opens — orders already resting
```

Winner detection uses last-trade price, not order book. A price ≥ 0.80 ten seconds
before resolution is a reliable win signal — markets converge strongly near expiry.

---

## experiment_1 — Observe then Bet (single side)

**Concept:** Read the market's own verdict just before resolution, then follow it.

**Flow:**
1. At T-15s, fetch last-trade price of both UP and DOWN tokens for the current window.
2. Side with price ≥ 0.80 is the confirmed winner.
3. At T-10s, place a single pre-market limit order for the **next** window on the **winning side**.
4. If no clear winner (neither side ≥ 0.80), skip this cycle — no order placed.

**Order:** 1 side × base_bet_usd @ entry_price (config)
**Reset:** No state between cycles — each cycle is independent.

---

## experiment_2 — Buy Both Sides, Sell the Loser

**Concept:** Enter both sides cheaply pre-market, exit the loser early and let the winner ride.

**Flow:**
1. At T-10s, place pre-market limit buys on **both** UP and DOWN @ $0.51 (5 shares each).
2. 2 minutes after the window opens, check last-trade prices of both sides.
3. The lower-priced side is losing — place a limit sell on it at current price.
4. Hold the winning side to $1.00 resolution.

**Order:** 2 sides × $2.55 each = $5.10 per cycle per asset
**Entry price:** $0.51 fixed
**Sell price:** current last-trade of the losing side at T+2min

---

## experiment_3 — Buy Both at $0.40, Hold, Martingale on One-Side Fill Loss

**Concept:** Enter both sides at a discount. Hold everything. Only martingale when
market makers didn't fill one side AND that unfilled side later lost.

**Flow:**
1. At T-10s, place pre-market limit buys on **both** UP and DOWN @ $0.40 (5 shares each).
2. **Never sell** — hold both positions to resolution.
3. At T-15s of the following cycle, check prices of the previous window's tokens
   AND check whether each order was actually filled (matched or live→filled).

**Martingale rule — triggers ONLY when:**
- Exactly one side was filled (market maker didn't quote the other at $0.40), AND
- That one filled side lost (price → 0.00)

**On martingale trigger:**
- Next cycle: place both sides again, but the losing side gets **doubled** in size.
- The other side stays at base size.
- Repeat doubling on consecutive losses of the same pattern.
- Reset to base on any win or when both sides fill normally.

**Entry price:** $0.40 fixed
**Order:** 2 sides per cycle. Sizes: base on normal cycles, base×2^n on martingale side.

---

## Budget Estimate — 10 Hours Live Testing

Assumptions: 5-min interval, 2 assets (SOL + DOGE), 120 cycles/10h, base_bet_usd = $1.00
(effective min spend = $2.55/order due to 5-share minimum at $0.51, or $2.00 at $0.40)

| Experiment | Per-cycle spend | Gross 10h | Working capital needed |
|---|---|---|---|
| experiment_1 | $2.55 × 2 assets = $5.10 | $612 | **$50 min / $100 comfortable** |
| experiment_2 | $5.10 × 2 assets = $10.20 (sell loser recovers ~$1) | $1,224 gross | **$80 min / $150 comfortable** |
| experiment_3 | $4.00 × 2 assets = $8.00 base | $960 base | **$100 min / $200 comfortable** |

Auto-redeem recycles winners continuously — gross spend does not equal net loss.
The working capital figures cover losing streaks and martingale depth (exp_3 up to level 4:
$2 → $4 → $8 → $16 on losing side = $30 cumulative per asset before reset).

**Recommended: top up to $100 before starting any single experiment.**
