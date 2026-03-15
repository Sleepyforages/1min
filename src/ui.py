"""
ui.py — Streamlit dashboard.

Tabs:
  1. Config & Controls  — edit all settings, save, start/stop bot
  2. Live Monitor       — open positions, P&L, RSI gauge
  3. Backtest           — run any or all progression methods, compare results
  4. Logs               — tail logs/bot.log

Run:
  streamlit run src/ui.py
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ── Resolve project root (works whether CWD is /app or the repo root) ─────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Config, load_config, save_config  # noqa: E402

st.set_page_config(
    page_title="Polymarket Hedge Bot",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session state helpers ──────────────────────────────────────────────────────
if "bot_process" not in st.session_state:
    st.session_state.bot_process = None
if "backtest_trades" not in st.session_state:
    st.session_state.backtest_trades = None
if "backtest_summary" not in st.session_state:
    st.session_state.backtest_summary = None


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR — Configuration
# ══════════════════════════════════════════════════════════════════════════════

def render_sidebar() -> Config:
    st.sidebar.title("⚙️ Bot Configuration")
    cfg = load_config()

    with st.sidebar.expander("Mode & Market", expanded=True):
        cfg.mode = st.selectbox(
            "Operating Mode",
            ["paper", "live", "backtest"],
            index=["paper", "live", "backtest"].index(cfg.mode),
        )
        cfg.interval = st.selectbox(
            "Candle Interval",
            ["5m", "15m"],
            index=["5m", "15m"].index(cfg.interval),
        )
        all_assets = ["btc", "eth", "sol", "xrp"]
        cfg.assets = st.multiselect(
            "Assets", all_assets, default=cfg.assets
        )
        cfg.base_bet_usd = st.number_input(
            "Base Bet (USD)", min_value=0.10, max_value=1000.0,
            value=float(cfg.base_bet_usd), step=0.10,
        )

    with st.sidebar.expander("📈 Progression Method", expanded=True):
        PROG_OPTIONS = {
            "Fixed (no progression)": "fixed",
            "Martingale": "martingale",
            "Fibonacci": "fibonacci",
            "D'Alembert": "dalembert",
        }
        prog_label = st.selectbox(
            "Progression Method",
            list(PROG_OPTIONS.keys()),
            index=list(PROG_OPTIONS.values()).index(cfg.progression_type),
        )
        cfg.progression_type = PROG_OPTIONS[prog_label]

        is_progressive = cfg.progression_type != "fixed"
        cfg.progression_cap = st.slider(
            "Progression Cap (steps)",
            min_value=3, max_value=7,
            value=int(cfg.progression_cap),
            disabled=not is_progressive,
            help="Maximum number of progression steps before the stake is frozen.",
        )

        # Live preview of the next bet sizes
        if is_progressive:
            _render_progression_preview(cfg)

    with st.sidebar.expander("🛡 Hedge Settings"):
        cfg.use_hedge = st.toggle("Enable Hedge Layer", value=cfg.use_hedge)
        cfg.hedge_sell_trigger_minutes = st.number_input(
            "Sell Hedge After (min)", min_value=0.5, max_value=5.0,
            value=float(cfg.hedge_sell_trigger_minutes), step=0.5,
            disabled=not cfg.use_hedge,
        )
        cfg.hedge_sell_price_trigger = st.number_input(
            "Sell Hedge if Price ≤", min_value=0.05, max_value=0.50,
            value=float(cfg.hedge_sell_price_trigger), step=0.01,
            disabled=not cfg.use_hedge,
        )

    with st.sidebar.expander("⏰ US Hours Multiplier"):
        cfg.us_hours_multiplier = st.number_input(
            "Multiplier", min_value=1.0, max_value=5.0,
            value=float(cfg.us_hours_multiplier), step=0.5,
        )
        col1, col2 = st.columns(2)
        cfg.us_hours_start_utc = col1.number_input("Start UTC", 0, 23, cfg.us_hours_start_utc)
        cfg.us_hours_end_utc = col2.number_input("End UTC", 0, 23, cfg.us_hours_end_utc)

    with st.sidebar.expander("📊 RSI Filter"):
        cfg.rsi_filter_enabled = st.toggle("Enable RSI Filter", value=cfg.rsi_filter_enabled)
        cfg.rsi_period = st.slider("RSI Period", 5, 30, cfg.rsi_period)
        cfg.rsi_overextended_low = st.slider(
            "Skip UP bets when RSI <", 20, 60, int(cfg.rsi_overextended_low),
            disabled=not cfg.rsi_filter_enabled,
        )
        cfg.rsi_overextended_high = st.slider(
            "Skip DOWN bets when RSI >", 40, 80, int(cfg.rsi_overextended_high),
            disabled=not cfg.rsi_filter_enabled,
        )

    with st.sidebar.expander("📉 H1 Momentum Filter"):
        cfg.h1_filter_enabled = st.toggle("Enable H1 Filter", value=cfg.h1_filter_enabled)
        cfg.h1_body_threshold = st.slider(
            "H1 Body Threshold %", 0.1, 2.0,
            float(cfg.h1_body_threshold * 100), 0.1,
            disabled=not cfg.h1_filter_enabled,
            help="Min H1 candle body size to trigger directional bias",
        ) / 100
        cfg.h1_bias_duration_trades = st.slider(
            "Bias Duration (trades)", 3, 24,
            int(cfg.h1_bias_duration_trades),
            disabled=not cfg.h1_filter_enabled,
        )
        H1_PROG_OPTIONS = {
            "Martingale": "martingale",
            "Fibonacci": "fibonacci",
            "D'Alembert": "dalembert",
            "Fixed": "fixed",
        }
        h1_prog_label = st.selectbox(
            "Force Progression During Bias",
            list(H1_PROG_OPTIONS.keys()),
            index=list(H1_PROG_OPTIONS.values()).index(cfg.h1_force_progression),
            disabled=not cfg.h1_filter_enabled,
        )
        cfg.h1_force_progression = H1_PROG_OPTIONS[h1_prog_label]

    with st.sidebar.expander("💰 Kelly Sizing"):
        cfg.kelly_sizing_enabled = st.toggle("Enable Kelly Sizing", value=cfg.kelly_sizing_enabled,
            help="Replaces fixed base_bet with dynamic half-Kelly stake")
        cfg.kelly_fraction = st.slider(
            "Kelly Fraction", 0.1, 1.0, float(cfg.kelly_fraction), 0.05,
            disabled=not cfg.kelly_sizing_enabled,
            help="0.5 = half-Kelly (recommended), 1.0 = full Kelly",
        )
        cfg.kelly_bankroll_usd = st.number_input(
            "Bankroll (USD)", 10.0, 100000.0, float(cfg.kelly_bankroll_usd), 10.0,
            disabled=not cfg.kelly_sizing_enabled,
        )
        cfg.kelly_estimated_edge = st.slider(
            "Estimated Edge %", 0.5, 10.0,
            float(cfg.kelly_estimated_edge * 100), 0.5,
            disabled=not cfg.kelly_sizing_enabled,
        ) / 100
        cfg.kelly_max_bet_pct = st.slider(
            "Max Bet % of Bankroll", 1.0, 10.0, float(cfg.kelly_max_bet_pct), 0.5,
            disabled=not cfg.kelly_sizing_enabled,
        )
        if cfg.kelly_sizing_enabled:
            kelly_stake = cfg.kelly_estimated_edge * cfg.kelly_fraction * cfg.kelly_bankroll_usd
            kelly_stake = min(kelly_stake, cfg.kelly_bankroll_usd * cfg.kelly_max_bet_pct / 100)
            st.caption(f"Next Kelly stake ≈ **${kelly_stake:.2f}**")

    with st.sidebar.expander("🔔 Telegram Alerts"):
        cfg.telegram_alerts_enabled = st.toggle("Enable Telegram Alerts", value=cfg.telegram_alerts_enabled)
        cfg.telegram_alert_on_win = st.toggle("Alert on Win", value=cfg.telegram_alert_on_win,
            disabled=not cfg.telegram_alerts_enabled)
        cfg.telegram_alert_on_loss = st.toggle("Alert on Loss", value=cfg.telegram_alert_on_loss,
            disabled=not cfg.telegram_alerts_enabled)
        cfg.telegram_drawdown_alert_pct = st.slider(
            "Drawdown Alert %", 1.0, 20.0, float(cfg.telegram_drawdown_alert_pct), 0.5,
            disabled=not cfg.telegram_alerts_enabled,
        )
        if cfg.telegram_alerts_enabled:
            import os
            has_token = bool(os.getenv("TELEGRAM_BOT_TOKEN"))
            st.caption("Token: " + ("✅ set in .env" if has_token else "⚠️ missing — add to .env"))

    with st.sidebar.expander("⚡ Execution"):
        cfg.parallel_assets = st.toggle(
            "Parallel Asset Execution",
            value=cfg.parallel_assets,
            help="Run all assets in parallel threads each cycle",
        )

    with st.sidebar.expander("🗓 Weekend / Risk"):
        cfg.weekend_behavior = st.selectbox(
            "Weekend Behaviour",
            ["momentum_only", "skip"],
            index=["momentum_only", "skip"].index(cfg.weekend_behavior),
        )
        cfg.max_daily_loss_pct = st.number_input(
            "Max Daily Loss %", 1.0, 50.0, float(cfg.max_daily_loss_pct), 1.0
        )
        cfg.dry_run = st.toggle("Dry Run (log only, no orders)", value=cfg.dry_run)

    if st.sidebar.button("💾 Save Config", type="primary"):
        save_config(cfg)
        st.sidebar.success("Config saved!")

    return cfg


def _render_progression_preview(cfg: Config):
    """Show a mini table of how the stake evolves over N steps."""
    from src.strategy import ProgressionState, calculate_next_bet_size

    st.caption("**Next-bet preview (consecutive losses)**")
    state = ProgressionState()
    rows = []
    last = "none"
    for step in range(cfg.progression_cap + 1):
        stake = calculate_next_bet_size(
            cfg.base_bet_usd, last, cfg.progression_type, cfg.progression_cap, state
        )
        rows.append({"Loss streak": step, "Stake $": f"${stake:.2f}"})
        last = "loss"

    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Controls
# ══════════════════════════════════════════════════════════════════════════════

def tab_controls(cfg: Config):
    st.header("🤖 Bot Controls")

    col1, col2, col3 = st.columns(3)
    running = st.session_state.bot_process is not None and st.session_state.bot_process.poll() is None

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
        status = "🟢 Running" if running else "🔴 Stopped"
        st.metric("Status", status)

    st.divider()
    st.subheader("Current Configuration")
    cfg_dict = {k: getattr(cfg, k) for k in cfg.__dataclass_fields__}
    st.json(cfg_dict)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Live Monitor
# ══════════════════════════════════════════════════════════════════════════════

def tab_monitor(cfg: Config):
    st.header("📡 Live Monitor")

    # ── Next-window countdown ─────────────────────────────────────────────────
    from datetime import datetime, timezone
    import math
    now_utc = datetime.now(timezone.utc)
    interval_secs = 300 if cfg.interval == "5m" else 900
    secs_into_window = (now_utc.minute * 60 + now_utc.second) % interval_secs
    secs_remaining = interval_secs - secs_into_window
    mins, secs = divmod(secs_remaining, 60)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Next Window", f"{mins:02d}:{secs:02d}")
    col2.metric("Interval", cfg.interval)
    col3.metric("Mode", cfg.mode.upper())
    col4.metric("Progression", cfg.progression_type.capitalize())

    # US-hours indicator
    hour = now_utc.hour
    in_us = cfg.us_hours_start_utc <= hour < cfg.us_hours_end_utc
    st.info(f"{'🇺🇸 US hours active — stakes ×' + str(cfg.us_hours_multiplier) if in_us else '🌙 Outside US hours — base stakes'}")

    st.divider()

    # ── Open positions (from paper ledger CSV if available) ───────────────────
    trades_path = ROOT / "data" / "paper_trades.csv"
    if trades_path.exists():
        df = pd.read_csv(trades_path)

        # Summary metrics
        if "pnl_usd" in df.columns:
            total_pnl = df["pnl_usd"].sum()
            wins = (df["pnl_usd"] > 0).sum()
            losses = (df["pnl_usd"] <= 0).sum()
            win_rate = wins / len(df) * 100 if len(df) > 0 else 0

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total P&L", f"${total_pnl:.2f}", delta=f"${df['pnl_usd'].iloc[-1]:.2f}" if len(df) else "")
            m2.metric("Win Rate", f"{win_rate:.1f}%")
            m3.metric("Total Trades", len(df))
            m4.metric("Wins / Losses", f"{wins} / {losses}")

        # Cumulative P&L chart
        if "pnl_usd" in df.columns and len(df) > 0:
            cum_df = df.copy()
            cum_df["cumulative_pnl"] = cum_df["pnl_usd"].cumsum()
            fig = px.line(
                cum_df, y="cumulative_pnl",
                title="Cumulative P&L",
                labels={"cumulative_pnl": "P&L (USD)", "index": "Trade #"},
            )
            fig.add_hline(y=0, line_dash="dash", line_color="gray")
            st.plotly_chart(fig, use_container_width=True)

        # Per-asset breakdown
        if "asset" in df.columns and "pnl_usd" in df.columns:
            st.subheader("P&L by Asset")
            asset_pnl = df.groupby("asset")["pnl_usd"].sum().reset_index()
            fig2 = px.bar(asset_pnl, x="asset", y="pnl_usd", color="pnl_usd",
                          color_continuous_scale="RdYlGn", title="P&L per Asset")
            st.plotly_chart(fig2, use_container_width=True)

        st.subheader("Recent Trades")
        display_cols = [c for c in ["timestamp", "asset", "direction", "progression_used",
                                     "h1_bias", "stake_usd", "pnl_usd", "outcome"] if c in df.columns]
        st.dataframe(df[display_cols].tail(50), use_container_width=True)

    else:
        st.warning("No trade data yet. Start the bot in paper mode to generate data.")

    if st.button("🔄 Refresh Monitor"):
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — Backtest
# ══════════════════════════════════════════════════════════════════════════════

def tab_backtest(cfg: Config):
    st.header("🔬 Backtest")

    col1, col2, col3 = st.columns(3)
    with col1:
        bt_assets = st.multiselect(
            "Assets to test", ["btc", "eth", "sol", "xrp"], default=cfg.assets
        )
    with col2:
        bt_bars = st.slider("Historical bars", 100, 2000, 500, 100)
    with col3:
        bt_methods = st.multiselect(
            "Progression methods",
            ["fixed", "martingale", "fibonacci", "dalembert"],
            default=["fixed", "martingale", "fibonacci", "dalembert"],
        )

    col_a, col_b = st.columns(2)
    run_single = col_a.button("▶ Run Selected Methods")
    run_all = col_b.button("▶ Run All Methods (comparison)", type="primary")

    if run_single or run_all:
        methods = bt_methods if run_single else ["fixed", "martingale", "fibonacci", "dalembert"]
        with st.spinner("Running backtest …"):
            from src.backtester import export_trades_csv, run_backtest
            bt_cfg = Config(**{k: getattr(cfg, k) for k in cfg.__dataclass_fields__})
            trades_df, summary_df = run_backtest(bt_cfg, assets=bt_assets, progression_types=methods, bars=bt_bars)
            st.session_state.backtest_trades = trades_df
            st.session_state.backtest_summary = summary_df

    summary_df = st.session_state.backtest_summary
    trades_df = st.session_state.backtest_trades

    if summary_df is not None and not summary_df.empty:
        st.subheader("📊 Comparison Table")
        # Colour net_pnl column
        styled = summary_df.style.background_gradient(subset=["net_pnl"], cmap="RdYlGn")
        st.dataframe(styled, use_container_width=True)

        st.subheader("Net P&L by Method × Hedge")
        fig = px.bar(
            summary_df,
            x="progression_type",
            y="net_pnl",
            color="use_hedge",
            barmode="group",
            title="Net PNL Comparison",
            labels={"net_pnl": "Net P&L (USD)", "progression_type": "Method"},
        )
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("Drawdown vs Win Rate")
        fig2 = px.scatter(
            summary_df,
            x="win_rate",
            y="max_drawdown",
            color="progression_type",
            symbol="use_hedge",
            size="total_trades",
            hover_data=["net_pnl", "sharpe"],
            title="Drawdown vs Win Rate (bubble size = # trades)",
        )
        st.plotly_chart(fig2, use_container_width=True)

        if st.button("📥 Export Trades CSV"):
            export_trades_csv(trades_df, str(ROOT / "data" / "backtest_trades.csv"))
            st.success("Exported to data/backtest_trades.csv")

    if trades_df is not None and not trades_df.empty:
        with st.expander("Raw trade records"):
            st.dataframe(trades_df, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — Logs
# ══════════════════════════════════════════════════════════════════════════════

def tab_logs():
    st.header("📋 Logs")
    log_path = ROOT / "logs" / "bot.log"
    if log_path.exists():
        with open(log_path) as f:
            lines = f.readlines()
        tail = "".join(lines[-200:])
        st.code(tail, language="text")
        if st.button("🔄 Refresh"):
            st.rerun()
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
    # Auto-refresh every 30s when bot is running
    running = st.session_state.bot_process is not None and st.session_state.bot_process.poll() is None
    if running:
        time.sleep(0)  # yields to Streamlit; real refresh driven by st.rerun below
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
