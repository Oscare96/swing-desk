# Independent backtest guide

This file is for **any AI or person reviewing this strategy** (ChatGPT, Muse, a friend who codes). It contains everything needed to run the 2020–2025 backtest, check that it's honest, and give a verdict in a standard format.

---

## Prompt to paste into the reviewing AI

> I'm attaching the code for a stock swing-trading system (swing-desk.zip). Please read BACKTEST_GUIDE.md and follow it exactly:
> 1. Run the tests, then the backtest, using unchanged settings.
> 2. Review the code for look-ahead bias and unrealistic assumptions.
> 3. Reply using the "Verdict template" at the end of the guide.
>
> Do not change config.toml or any strategy parameters to make the backtest pass. If you think a change would help, list it under "Suggested changes" with your reasoning, and run it only as a separate, clearly labeled experiment.

If the AI can't reach the internet (ChatGPT's sandbox usually can't), also attach **price_data.zip**. See Option B below.

---

## 1. What the system does (short)

- **Core (70% of account):** holds SPY while SPY is above its 200-day average. It flips only on a 2% break, to avoid whipsaw. Otherwise it holds cash.
- **Satellite (30%):** a long-only swing desk on ~100 US large caps.
  - **Buy** at the next open when a stock closes above its 200-day average and its 3-day RSI closes below 15.
  - **Sell** at the next open when the close is above the 10-day average, or after 10 days.
  - **Protective stop:** 2.5 × ATR(14) below the fill. If the stock gaps below it, the fill is the open.
  - Idle satellite cash is parked in SPY while the regime is risk-on.
- **Risk:**
  - 0.75% of the satellite at risk per trade, max 20% of the satellite per position.
  - Max 6 positions and 3 new entries a day.
  - No entries when the regime is risk-off, or when the account is down 3% on the day.
  - Entries halt when the desk is 12% off its peak.
- **Costs:** 5 bps slippage per side on every fill, $0 commission.

All settings are in `config.toml`.

## 2. Environment

- Python 3.11 or newer (uses `tomllib`).
- `pip install pandas numpy requests flask tzdata yfinance`

`psycopg` is only needed for the live app, not for backtesting.

## 3. Run it

**Option A: the sandbox has internet**

```
pip install pandas numpy requests flask tzdata yfinance
python -m unittest discover -s tests -t . -v
python -m tradingbot backtest --source yfinance
```

**Option B: no internet (typical for ChatGPT)**

The user creates `price_data.zip` on a machine with internet by running `python tools/fetch_data.py`, then uploads it with the code. In the sandbox:

```
unzip swing-desk.zip && cd swing-desk
unzip ../price_data.zip          # creates data/csv/*.csv
pip install pandas numpy requests flask tzdata   # skip if offline; usually preinstalled
python -m unittest discover -s tests -t . -v
python -m tradingbot backtest --source csv
```

The full run takes about 1–3 minutes. That includes 28 backtests: the main run plus a 27-setting robustness grid.

**Outputs, in `reports/`:**

| File | What it is |
|---|---|
| `backtest_report.md` | Human-readable summary. Paste this into the verdict. |
| `backtest_report.json` | Same results, machine-readable |
| `backtest_trades.csv` | Every simulated trade |
| `backtest_equity.csv` | Daily values of core, satellite, desk, total and SPY |

**Exit code:** 0 if all gates pass, 1 if any gate fails. A failure is a valid result, not an error to fix.

## 4. The gates

Gates are measured on **2023–2025 (out-of-sample)**, with 2020–2022 as in-sample:

| Check | Rule |
|---|---|
| Symbols with full history | ≥ 80 |
| Trades | ≥ 60 |
| Profit factor | ≥ 1.15 |
| Win rate | ≥ 50% |
| Desk Sharpe (swing trades only, parked SPY excluded) | ≥ 0.5 |
| Desk max drawdown | ≤ 20% |
| Portfolio Sharpe shortfall vs SPY buy-and-hold | ≤ 0.15 |
| In-sample profit factor | ≥ 1.0 |
| Robustness: share of 27 nearby parameter sets profitable over 2020–2025 | ≥ 70% |

## 5. What to check in the code

Please verify, and say in the verdict whether each holds:

1. **No look-ahead.**
   - Signals use data only through day *t*'s close (`tradingbot/strategy.py`).
   - Orders fill at day *t+1*'s open (`tradingbot/backtest.py`, "OPEN" section).
   - `tests/test_core.py::Backtest::test_no_lookahead` scrambles all prices after a date and checks that no earlier trade changes.
2. **Realistic fills.**
   - Stops fill at min(open, stop), so gaps aren't filled at the stop price.
   - Slippage is applied on every buy and sell.
   - Entries are capped by available cash.
3. **Same logic live and in the backtest.**
   - `tradingbot/live.py` calls the same `add_features`, `candidates_on`, `size_position` and `gate_new_entry` functions.
   - `tests/test_core.py::LiveParity` checks this.
4. **Accounting.**
   - Total equals core plus satellite every day.
   - Sleeve returns are time-weighted, so the yearly core/satellite rebalance isn't counted as profit.
5. **Known limitations.** Don't treat these as bugs, but weigh them in the verdict:
   - **Survivorship bias:** the universe is today's large caps, so 2020–2025 results are somewhat optimistic.
   - **Data:** yfinance prices are split- and dividend-adjusted. Alpaca with `adjustment=all` is similar. Small differences between sources are expected.
   - **Fills:** daily bars can't show intraday order. A day that hits the stop and later the target counts as a stop, which is conservative.

## 6. Rules for the reviewer

- **Don't tune the parameters to make it pass.** Changing settings until the numbers look good is overfitting, and it defeats the out-of-sample test. The result with unchanged settings is the result.
- If you run experiments with other settings, label them **EXPERIMENT** and report them separately. They don't count toward the verdict.
- Report bugs with the file, the line, a description, and how they would change the numbers.

## 7. Verdict template

```
VERDICT: PASS / FAIL / CAN'T TELL (pick one)
Ran by: <AI name>   Data source: <yfinance/csv/alpaca>   Config hash: <from report>
Tests: <N passed / N failed>

Gate results (out-of-sample 2023-2025):
<paste the gates table from reports/backtest_report.md>

Key numbers:
- Desk: CAGR __, Sharpe __, max drawdown __
- Portfolio vs SPY: CAGR __ vs __, Sharpe __ vs __, max drawdown __ vs __
- Trades __, win rate __, profit factor __, average R __

Code review:
- Look-ahead: OK / problem (details)
- Fills and costs: OK / problem
- Live/backtest parity: OK / problem
- Accounting: OK / problem
- Bugs found: none / list with file:line

Biggest risks or weaknesses (max 5):
1.

Suggested changes (not applied; each needs its own re-test):
1.

EXPERIMENTS (if any, clearly separate):
```

---

## For the owner: how the result unlocks paper trading

Paper trading starts only if `reports/backtest_report.json` says `passed: true` **and** its config hash matches `config.toml`.

A reviewer's run gives you an independent verdict. The report that unlocks trading must be produced where the bot is deployed, so run this once in the Replit Shell before deploying:

```
python -m tradingbot backtest
```
