"""
ui.py — Streamlit dashboard.

Tabs:
  1. Controls    — start/stop bot, show current config
  2. Live Monitor — auto-refreshes every 15s, heartbeat indicator, no full-page reload
  3. Backtest    — run all 4 progression methods side-by-side
  4. Logs        — tail logs/bot.log

All sidebar widgets include (i) tooltip help text.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Config, load_config, save_config  # noqa: E402

st.set_page_config(
    page_title="Polymarket Hedge Bot",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session state ──────────────────────────────────────────────────────────────
for key, default in [
    ("bot_process",      None),
    ("backtest_trades",  None),
    ("backtest_summary", None),
    ("asset_error",      ""),
    ("last_validated_ticker", ""),
    ("working_assets",   None),   # persists asset list across reruns
]:
    if key not in st.session_state:
        st.session_state[key] = default


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════

def render_sidebar() -> Config:
    st.sidebar.title("⚙️ Bot Configuration")
    cfg = load_config()

    # ── Mode & Market ──────────────────────────────────────────────────────────
    with st.sidebar.expander("Mode & Market", expanded=True):
        cfg.mode = st.selectbox(
            "Operating Mode",
            ["paper", "live", "backtest"],
            index=["paper", "live", "backtest"].index(cfg.mode),
            help="paper = simulate trades locally | live = real orders on Polymarket | backtest = historical simulation",
        )
        cfg.interval = st.selectbox(
            "Candle Interval",
            ["5m", "15m"],
            index=["5m", "15m"].index(cfg.interval),
            help="Which Polymarket markets to trade: 5-minute or 15-minute Up/Down markets.",
        )

        # ── Dynamic asset management ───────────────────────────────────────────
        # working_assets lives in session_state so it survives reruns between
        # "Add ticker" and "Save Config" button clicks.
        if st.session_state.working_assets is None:
            st.session_state.working_assets = list(cfg.assets)

        # If config file was changed externally, sync session state
        if set(st.session_state.working_assets) != set(cfg.assets) and \
                st.session_state.get("last_saved_assets") == st.session_state.working_assets:
            st.session_state.working_assets = list(cfg.assets)

        current_assets = st.session_state.working_assets

        st.caption("**Assets** — type any ticker (BTC, DOGE, HYPE…) and press Enter to add")

        new_ticker = st.text_input(
            "Add ticker",
            value="",
            placeholder="e.g. DOGE",
            key="new_ticker_input",
            help="Any crypto ticker. The bot will warn in logs if it has no Polymarket market or price feed.",
        ).strip().lower()

        # Clear sticky error as soon as user types a different ticker
        if new_ticker != st.session_state.last_validated_ticker:
            st.session_state.asset_error = ""

        if new_ticker and new_ticker not in current_assets:
            if st.button(f"+ Add {new_ticker.upper()}"):
                st.session_state.asset_error = ""
                with st.spinner(f"Checking {new_ticker.upper()} on Binance / Bybit…"):
                    from src.price_feed import validate_asset
                    ok, reason = validate_asset(new_ticker)
                st.session_state.last_validated_ticker = new_ticker
                if ok:
                    st.session_state.working_assets = current_assets + [new_ticker]
                    st.success(f"{new_ticker.upper()} added ✅")
                else:
                    st.session_state.asset_error = (
                        f"⚠️ **{new_ticker.upper()}** not found on Binance or Bybit.\n\n"
                        f"{reason}\n\n"
                        f"Added anyway — bot will skip price-feed filters for this asset "
                        f"but will still search for its Polymarket markets."
                    )
                    st.session_state.working_assets = current_assets + [new_ticker]
                st.rerun()
        elif new_ticker and new_ticker in current_assets:
            st.caption(f"{new_ticker.upper()} is already in the list.")

        if st.session_state.asset_error:
            st.warning(st.session_state.asset_error)

        # Show current asset list with remove buttons
        st.caption("Current assets:")
        remove = None
        for a in current_assets:
            col_a, col_b = st.columns([4, 1])
            col_a.write(a.upper())
            if col_b.button("✕", key=f"rm_{a}"):
                remove = a
        if remove:
            st.session_state.working_assets = [a for a in current_assets if a != remove]
            st.rerun()

        # Always keep cfg.assets in sync with session state
        cfg.assets = list(st.session_state.working_assets)

        cfg.base_bet_usd = st.number_input(
            "Base Bet (USD)", min_value=0.10, max_value=1000.0,
            value=float(cfg.base_bet_usd), step=0.10,
            help="Stake per trade in fixed mode. In Kelly mode this acts as a floor (minimum bet).",
        )

    # ── Progression ────────────────────────────────────────────────────────────
    with st.sidebar.expander("📈 Progression Method", expanded=True):
        PROG_OPTIONS = {
            "Fixed (no progression)": "fixed",
            "Martingale":             "martingale",
            "Fibonacci":              "fibonacci",
            "D'Alembert":             "dalembert",
        }
        prog_label = st.selectbox(
            "Progression Method",
            list(PROG_OPTIONS.keys()),
            index=list(PROG_OPTIONS.values()).index(cfg.progression_type),
            help=(
                "fixed = always bet base stake\n"
                "martingale = double on loss, reset on win\n"
                "fibonacci = advance Fib sequence on loss, back 2 on win\n"
                "dalembert = +1 unit on loss, -1 on win"
            ),
        )
        cfg.progression_type = PROG_OPTIONS[prog_label]

        is_progressive = cfg.progression_type != "fixed"
        cfg.progression_cap = st.slider(
            "Progression Cap (steps)", min_value=3, max_value=7,
            value=int(cfg.progression_cap),
            disabled=not is_progressive,
            help="Maximum number of loss steps before the stake is frozen. Limits max exposure.",
        )
        if is_progressive:
            _render_progression_preview(cfg)

    # ── Hedge ──────────────────────────────────────────────────────────────────
    with st.sidebar.expander("🛡 Hedge Settings"):
        cfg.use_hedge = st.toggle(
            "Enable Hedge Layer", value=cfg.use_hedge,
            help="Places a simultaneous bet on the opposite direction. Reduces variance at the cost of lower upside.",
        )
        cfg.hedge_sell_trigger_minutes = st.number_input(
            "Sell Hedge After (min)", min_value=0.5, max_value=5.0,
            value=float(cfg.hedge_sell_trigger_minutes), step=0.5,
            disabled=not cfg.use_hedge,
            help="Close the hedge leg N minutes into the window. Earlier = less hedge protection but lower cost.",
        )
        cfg.hedge_sell_price_trigger = st.number_input(
            "Sell Hedge if Price ≤", min_value=0.05, max_value=0.50,
            value=float(cfg.hedge_sell_price_trigger), step=0.01,
            disabled=not cfg.use_hedge,
            help="Also close the hedge if the Yes token price drops to this level (e.g. 0.20 = 20¢). Cuts losses early.",
        )

    # ── US Hours ───────────────────────────────────────────────────────────────
    with st.sidebar.expander("⏰ US Hours Multiplier"):
        cfg.us_hours_multiplier = st.number_input(
            "Multiplier", min_value=1.0, max_value=5.0,
            value=float(cfg.us_hours_multiplier), step=0.5,
            help="Multiply all stakes by this factor during US market hours. Higher liquidity → bigger bets.",
        )
        col1, col2 = st.columns(2)
        cfg.us_hours_start_utc = col1.number_input(
            "Start UTC", 0, 23, cfg.us_hours_start_utc,
            help="Start of US trading hours in UTC (default 14 = 10am EST).",
        )
        cfg.us_hours_end_utc = col2.number_input(
            "End UTC", 0, 23, cfg.us_hours_end_utc,
            help="End of US trading hours in UTC (default 20 = 4pm EST).",
        )

    # ── RSI Filter ─────────────────────────────────────────────────────────────
    with st.sidebar.expander("📊 RSI Filter"):
        cfg.rsi_filter_enabled = st.toggle(
            "Enable RSI Filter", value=cfg.rsi_filter_enabled,
            help="Skip trades when RSI is overextended. Avoids chasing moves that are likely to reverse.",
        )
        cfg.rsi_period = st.slider(
            "RSI Period", 5, 30, cfg.rsi_period,
            help="Number of candles for RSI calculation. 14 is standard. Lower = more sensitive.",
        )
        cfg.rsi_overextended_low = st.slider(
            "Skip UP bets when RSI <", 20, 60, int(cfg.rsi_overextended_low),
            disabled=not cfg.rsi_filter_enabled,
            help="If RSI is below this level the market is already oversold — skip UP bets.",
        )
        cfg.rsi_overextended_high = st.slider(
            "Skip DOWN bets when RSI >", 40, 80, int(cfg.rsi_overextended_high),
            disabled=not cfg.rsi_filter_enabled,
            help="If RSI is above this level the market is already overbought — skip DOWN bets.",
        )

    # ── H1 Filter ──────────────────────────────────────────────────────────────
    with st.sidebar.expander("📉 H1 Momentum Filter"):
        cfg.h1_filter_enabled = st.toggle(
            "Enable H1 Filter", value=cfg.h1_filter_enabled,
            help="Reads the developing 1-hour candle. If it has strong directional momentum (body > threshold), only trade in that direction for the next N five-minute windows.",
        )
        cfg.h1_body_threshold = st.slider(
            "H1 Body Threshold %", 0.1, 2.0,
            float(cfg.h1_body_threshold * 100), 0.1,
            disabled=not cfg.h1_filter_enabled,
            help="Minimum H1 candle body size to trigger bias. 0.3% = price moved >0.3% since the hour opened.",
        ) / 100
        cfg.h1_bias_duration_trades = st.slider(
            "Bias Duration (trades)", 3, 24, int(cfg.h1_bias_duration_trades),
            disabled=not cfg.h1_filter_enabled,
            help="How many 5-min cycles to enforce the H1 directional bias. 12 = ~1 hour.",
        )
        H1_PROG = {"Martingale": "martingale", "Fibonacci": "fibonacci",
                   "D'Alembert": "dalembert", "Fixed": "fixed"}
        cfg.h1_force_progression = H1_PROG[st.selectbox(
            "Force Progression During Bias",
            list(H1_PROG.keys()),
            index=list(H1_PROG.values()).index(cfg.h1_force_progression),
            disabled=not cfg.h1_filter_enabled,
            help="Override the default progression method while H1 bias is active. Martingale maximises momentum recovery.",
        )]

    # ── Kelly ──────────────────────────────────────────────────────────────────
    with st.sidebar.expander("💰 Kelly Sizing"):
        cfg.kelly_sizing_enabled = st.toggle(
            "Enable Kelly Sizing", value=cfg.kelly_sizing_enabled,
            help="Replaces the fixed base_bet with a mathematically optimal stake based on your estimated edge and bankroll.",
        )
        cfg.kelly_fraction = st.slider(
            "Kelly Fraction", 0.1, 1.0, float(cfg.kelly_fraction), 0.05,
            disabled=not cfg.kelly_sizing_enabled,
            help="1.0 = full Kelly (aggressive, high variance). 0.5 = half-Kelly (recommended). 0.25 = quarter-Kelly (conservative).",
        )
        cfg.kelly_bankroll_usd = st.number_input(
            "Bankroll (USD)", 10.0, 100000.0, float(cfg.kelly_bankroll_usd), 10.0,
            disabled=not cfg.kelly_sizing_enabled,
            help="Your total trading bankroll. Kelly stake = edge × fraction × bankroll.",
        )
        cfg.kelly_estimated_edge = st.slider(
            "Estimated Edge %", 0.5, 10.0, float(cfg.kelly_estimated_edge * 100), 0.5,
            disabled=not cfg.kelly_sizing_enabled,
            help="Your expected edge per trade as a percentage. 4% is a reasonable estimate with RSI + H1 filters active.",
        ) / 100
        cfg.kelly_max_bet_pct = st.slider(
            "Max Bet % of Bankroll", 1.0, 10.0, float(cfg.kelly_max_bet_pct), 0.5,
            disabled=not cfg.kelly_sizing_enabled,
            help="Hard cap on any single bet as a % of bankroll. Prevents ruin from overconfident Kelly estimates.",
        )
        if cfg.kelly_sizing_enabled:
            ks = min(cfg.kelly_estimated_edge * cfg.kelly_fraction * cfg.kelly_bankroll_usd,
                     cfg.kelly_bankroll_usd * cfg.kelly_max_bet_pct / 100)
            st.caption(f"Next Kelly stake ≈ **${max(ks, cfg.base_bet_usd):.2f}**")

    # ── Telegram ───────────────────────────────────────────────────────────────
    with st.sidebar.expander("🔔 Telegram Alerts"):
        cfg.telegram_alerts_enabled = st.toggle(
            "Enable Telegram Alerts", value=cfg.telegram_alerts_enabled,
            help="Send notifications to a Telegram bot. Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.",
        )
        cfg.telegram_alert_on_win = st.toggle(
            "Alert on Win", value=cfg.telegram_alert_on_win,
            disabled=not cfg.telegram_alerts_enabled,
            help="Send a message for every winning trade. Can be noisy if trading many assets.",
        )
        cfg.telegram_alert_on_loss = st.toggle(
            "Alert on Loss", value=cfg.telegram_alert_on_loss,
            disabled=not cfg.telegram_alerts_enabled,
            help="Send a message for every losing trade.",
        )
        cfg.telegram_drawdown_alert_pct = st.slider(
            "Drawdown Alert %", 1.0, 20.0, float(cfg.telegram_drawdown_alert_pct), 0.5,
            disabled=not cfg.telegram_alerts_enabled,
            help="Send an alert if daily drawdown exceeds this percentage.",
        )
        if cfg.telegram_alerts_enabled:
            import os
            st.caption("Token: " + ("✅ set in .env" if os.getenv("TELEGRAM_BOT_TOKEN") else "⚠️ missing"))

    # ── Execution ──────────────────────────────────────────────────────────────
    with st.sidebar.expander("⚡ Execution"):
        cfg.parallel_assets = st.toggle(
            "Parallel Asset Execution", value=cfg.parallel_assets,
            help="Run all assets simultaneously in separate threads each cycle. Faster, but uses more CPU.",
        )
        cfg.max_daily_loss_pct = st.number_input(
            "Max Daily Loss %", 1.0, 50.0, float(cfg.max_daily_loss_pct), 1.0,
            help="Stop trading for the rest of the day if cumulative loss exceeds this % of starting balance.",
        )
        cfg.dry_run = st.toggle(
            "Dry Run (log only, no orders)", value=cfg.dry_run,
            help="Simulate everything including signal generation, but never place any actual orders.",
        )

    # ── Weekend ────────────────────────────────────────────────────────────────
    with st.sidebar.expander("🗓 Weekend Behaviour"):
        cfg.weekend_behavior = st.selectbox(
            "Weekend Behaviour",
            ["momentum_only", "skip"],
            index=["momentum_only", "skip"].index(cfg.weekend_behavior),
            help="skip = no trading on Sat/Sun | momentum_only = only trade when signal matches H1 direction",
        )

    if st.sidebar.button("💾 Save Config", type="primary",
                         help="Write all settings to config/default.yaml. Bot hot-reloads on next cycle."):
        save_config(cfg)
        st.session_state.last_saved_assets = list(cfg.assets)
        st.sidebar.success(f"Config saved! Assets: {[a.upper() for a in cfg.assets]}")

    return cfg


def _render_progression_preview(cfg: Config):
    from src.strategy import ProgressionState, calculate_next_bet_size
    st.caption("**Stake preview — consecutive losses:**")
    state = ProgressionState()
    rows, last = [], "none"
    for step in range(cfg.progression_cap + 1):
        stake = calculate_next_bet_size(
            cfg.base_bet_usd, last, cfg.progression_type, cfg.progression_cap, state
        )
        rows.append({"Losses": step, "Stake": f"${stake:.2f}"})
        last = "loss"
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Controls
# ══════════════════════════════════════════════════════════════════════════════

def tab_controls(cfg: Config):
    st.header("🤖 Bot Controls")
    running = (
        st.session_state.bot_process is not None
        and st.session_state.bot_process.poll() is None
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("▶ Start Bot", disabled=running, type="primary"):
            proc = subprocess.Popen(
                [sys.executable, "-m", "src.bot"],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            st.session_state.bot_process = proc
            st.success("Bot started!")
            st.rerun()
    with col2:
        if st.button("⏹ Stop Bot", disabled=not running):
            st.session_state.bot_process.terminate()
            st.session_state.bot_process = None
            st.warning("Bot stopped.")
            st.rerun()
    with col3:
        st.metric("Status", "🟢 Running" if running else "🔴 Stopped")

    st.divider()

    # ── Multiple bot instances info ────────────────────────────────────────────
    with st.expander("ℹ️ Running multiple bot instances"):
        st.markdown("""
**One Polymarket account = one private key = multiple bots OK**

Polymarket positions are tracked on-chain per token — a single account can
hold positions opened by multiple bots simultaneously. You do **not** need
separate wallets.

**However**, concurrent order signing from the same key can cause nonce collisions.
The safest setup:

| Setup | How |
|---|---|
| **Different assets** | Bot A trades BTC/ETH, Bot B trades SOL/XRP — no conflict |
| **Different strategies** | Same assets, sequential execution (`parallel_assets: false`) |
| **Fully isolated** | Generate separate API keys from the same wallet in the Polymarket UI |

To run two instances on this server, duplicate the project folder and use
a different port in each `docker-compose.yml`:
```bash
cp -r /root/1min /root/1min-bot2
# Edit /root/1min-bot2/docker-compose.yml → change port 8501 to 8502
# Edit /root/1min-bot2/config/default.yaml → set different assets/strategy
cd /root/1min-bot2 && docker compose up -d
```
""")

    st.subheader("Current Configuration")
    cfg_dict = {k: getattr(cfg, k) for k in cfg.__dataclass_fields__}
    st.json(cfg_dict)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Live Monitor  (auto-refresh fragment — no full page reload)
# ══════════════════════════════════════════════════════════════════════════════

@st.fragment(run_every=15)
def tab_monitor(cfg: Config):
    """
    This function is a Streamlit fragment — it refreshes every 15 seconds
    independently without triggering a full page reload.
    """
    st.header("📡 Live Monitor")

    # ── Heartbeat / countdown ─────────────────────────────────────────────────
    now_utc       = datetime.now(timezone.utc)
    interval_secs = 300 if cfg.interval == "5m" else 900
    secs_elapsed  = (now_utc.minute * 60 + now_utc.second) % interval_secs
    secs_left     = interval_secs - secs_elapsed
    mins, secs    = divmod(secs_left, 60)

    hour    = now_utc.hour
    weekday = now_utc.weekday()  # 0=Mon … 4=Fri, 5=Sat, 6=Sun
    in_us   = (weekday < 5) and (cfg.us_hours_start_utc <= hour < cfg.us_hours_end_utc)
    running = (
        st.session_state.bot_process is not None
        and st.session_state.bot_process.poll() is None
    )

    # Pulsing heartbeat text
    pulse = "🟢" if running else "🔴"
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Bot Status", f"{pulse} {'Running' if running else 'Stopped'}")
    c2.metric("Next Window", f"{mins:02d}:{secs:02d}",
              help="Time until the next 5m/15m market window opens.")
    c3.metric("UTC Time", now_utc.strftime("%H:%M:%S"))
    c4.metric("Interval", cfg.interval)
    c5.metric("US Hours", "🇺🇸 Active" if in_us else "🌙 Off",
              help=f"US hours multiplier (×{cfg.us_hours_multiplier}) active {cfg.us_hours_start_utc}–{cfg.us_hours_end_utc} UTC")

    st.caption(f"_Auto-refreshes every 15s — last refresh: {now_utc.strftime('%H:%M:%S')} UTC_")
    st.divider()

    # ── Trade data ────────────────────────────────────────────────────────────
    trades_path = ROOT / "data" / "paper_trades.csv"
    if trades_path.exists():
        df = pd.read_csv(trades_path)

        if "pnl_usd" in df.columns and len(df) > 0:
            total_pnl = df["pnl_usd"].sum()
            wins      = (df["pnl_usd"] > 0).sum()
            losses    = len(df) - wins
            win_rate  = wins / len(df) * 100

            m1, m2, m3, m4 = st.columns(4)
            last_pnl = df["pnl_usd"].iloc[-1]
            m1.metric("Total P&L", f"${total_pnl:.2f}",
                      delta=f"${last_pnl:+.2f} last trade")
            m2.metric("Win Rate",    f"{win_rate:.1f}%")
            m3.metric("Total Trades", len(df))
            m4.metric("W / L",       f"{wins} / {losses}")

            # Cumulative P&L chart
            cum = df["pnl_usd"].cumsum()
            fig = px.line(cum, title="Cumulative P&L",
                          labels={"value": "P&L (USD)", "index": "Trade #"})
            fig.add_hline(y=0, line_dash="dash", line_color="gray")
            st.plotly_chart(fig, use_container_width=True)

        # Per-asset breakdown
        if "asset" in df.columns and "pnl_usd" in df.columns:
            asset_pnl = df.groupby("asset")["pnl_usd"].sum().reset_index()
            fig2 = px.bar(asset_pnl, x="asset", y="pnl_usd", color="pnl_usd",
                          color_continuous_scale="RdYlGn", title="P&L by Asset")
            st.plotly_chart(fig2, use_container_width=True)

        # Recent trades table
        st.subheader("Recent Trades")
        show_cols = [c for c in ["timestamp", "asset", "direction", "stake_usd",
                                  "progression_used", "h1_bias", "rsi", "pnl_usd", "outcome"]
                     if c in df.columns]
        st.dataframe(df[show_cols].tail(50), use_container_width=True)
    else:
        st.info("No trade data yet. Start the bot in paper mode to generate data.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — Backtest
# ══════════════════════════════════════════════════════════════════════════════

def tab_backtest(cfg: Config):
    st.header("🔬 Backtest")

    col1, col2, col3 = st.columns(3)
    with col1:
        _bt_options = sorted(set(cfg.assets) | {"btc", "eth", "sol", "xrp"})
        bt_assets = st.multiselect(
            "Assets to test", _bt_options, default=cfg.assets,
            help="Only assets with a price feed can be backtested.",
        )
    with col2:
        bt_bars = st.slider("Historical bars", 100, 2000, 500, 100,
                            help="Number of OHLCV candles to simulate. More bars = slower but more reliable.")
    with col3:
        bt_methods = st.multiselect(
            "Progression methods",
            ["fixed", "martingale", "fibonacci", "dalembert"],
            default=["fixed", "martingale", "fibonacci", "dalembert"],
            help="Run any combination of methods in parallel and compare results.",
        )

    col_a, col_b = st.columns(2)
    run_single = col_a.button("▶ Run Selected Methods")
    run_all    = col_b.button("▶ Run All Methods (comparison)", type="primary")

    if run_single or run_all:
        methods = bt_methods if run_single else ["fixed", "martingale", "fibonacci", "dalembert"]
        with st.spinner("Running backtest …"):
            from src.backtester import export_trades_csv, run_backtest
            bt_cfg = Config(**{k: getattr(cfg, k) for k in cfg.__dataclass_fields__})
            trades_df, summary_df = run_backtest(bt_cfg, assets=bt_assets,
                                                  progression_types=methods, bars=bt_bars)
            st.session_state.backtest_trades  = trades_df
            st.session_state.backtest_summary = summary_df

    summary_df = st.session_state.backtest_summary
    trades_df  = st.session_state.backtest_trades

    if summary_df is not None and not summary_df.empty:
        st.subheader("📊 Comparison Table")
        styled = summary_df.style.background_gradient(subset=["net_pnl"], cmap="RdYlGn")
        st.dataframe(styled, use_container_width=True)

        fig = px.bar(summary_df, x="progression_type", y="net_pnl", color="use_hedge",
                     barmode="group", title="Net PNL by Method × Hedge",
                     labels={"net_pnl": "Net P&L (USD)", "progression_type": "Method"})
        st.plotly_chart(fig, use_container_width=True)

        fig2 = px.scatter(summary_df, x="win_rate", y="max_drawdown",
                          color="progression_type", symbol="use_hedge", size="total_trades",
                          hover_data=["net_pnl", "sharpe"],
                          title="Drawdown vs Win Rate (bubble = # trades)")
        st.plotly_chart(fig2, use_container_width=True)

        if st.button("📥 Export Trades CSV"):
            from src.backtester import export_trades_csv
            export_trades_csv(trades_df, str(ROOT / "data" / "backtest_trades.csv"))
            st.success("Exported to data/backtest_trades.csv")

    if trades_df is not None and not trades_df.empty:
        with st.expander("Raw trade records"):
            st.dataframe(trades_df, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — Logs
# ══════════════════════════════════════════════════════════════════════════════

@st.fragment(run_every=10)
def tab_logs():
    """Auto-refreshes every 10s — no button needed."""
    st.header("📋 Logs")
    log_path = ROOT / "logs" / "bot.log"
    if log_path.exists():
        with open(log_path) as f:
            lines = f.readlines()
        tail = "".join(lines[-300:])
        now  = datetime.now(timezone.utc).strftime("%H:%M:%S")
        st.caption(f"_Auto-refreshes every 10s — last: {now} UTC — {len(lines)} total lines_")
        st.code(tail, language="text")
    else:
        st.info("No log file yet — start the bot to generate logs.")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    cfg = render_sidebar()

    tab1, tab2, tab3, tab4 = st.tabs([
        "🤖 Controls",
        "📡 Live Monitor",
        "🔬 Backtest",
        "📋 Logs",
    ])
    with tab1:
        tab_controls(cfg)
    with tab2:
        tab_monitor(cfg)
    with tab3:
        tab_backtest(cfg)
    with tab4:
        tab_logs()


if __name__ == "__main__":
    main()
