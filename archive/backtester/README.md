# Backtester (standalone)

Moved out of the main bot. Run independently — not wired into the live trading loop.

**Known issue:** the current simulator uses same-bar close/open for both signal and outcome (look-ahead bias). Results are not decision-grade until this is fixed.

```bash
cd backtester
python backtester.py
```
