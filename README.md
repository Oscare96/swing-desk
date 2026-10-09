# Swing Desk v1

A long-only stock swing-trading desk built so that **what gets paper traded is exactly what was backtested**, and so it **cannot trade until the backtest passes**.

## How it works

```
completed daily bars ──► deterministic signals ──► (optional LLM veto) ──► code sizing ──► risk gate ──► Alpaca
                                                                                                   │
           dashboard ◄── journal / trades / equity ◄── reconcile (broker = truth) ◄── broker-side stops
```

**Portfolio: core + satellite**
- **Core (70%)**: SPY while SPY is above its 200-day average (with a 2% band to avoid whipsaw); cash when it isn't.
- **Satellite (30%)**: the swing desk. Idle satellite cash is parked in SPY while the regime is risk-on.

**Swing strategy (pullback in an uptrend)**
- Buy when the stock closes above its 200-day average and its 3-day RSI closes below 15 (sharp short-term dip in a long-term uptrend). The most oversold names go first.
- Sell when it closes back above its 10-day average, after 10 days, or if the protective stop (2.5 × ATR below the fill) is hit.
- Decisions use completed daily closes; orders go in at the next open.

**Risk rules (enforced by code, identical in backtest and paper)**
- 0.75% of the satellite at risk per trade, sized from the stop distance. Max 20% of the satellite per position, 6 positions, 3 new entries a day.
- No new entries when the regime is risk-off, the data is stale, the account is down 3% on the day, or the desk is 12% below its peak. The drawdown breaker **latches** until you reset it on the dashboard.
- Every position has a **stop order sitting at Alpaca**, so it works even if this app is down.
- Orders carry fixed IDs, so a retry or a restart can't double-buy.

**Optional LLM veto** (`[llm] enabled = true`): it reviews candidates against recent headlines and can only *remove* a trade, for example a dip caused by an earnings miss. It can't add trades, size them or move stops. If it fails, the tested strategy runs unchanged. Every call is journaled. It's off by default because it can't be backtested honestly; turn it on later and compare paper results with it on and off.

## The gate: nothing trades until this passes

`python -m tradingbot backtest` runs 2020–2025 with costs (5 bps slippage per side). It treats **2020–2022 as in-sample and 2023–2025 as out-of-sample**, then checks the out-of-sample period:

| Check | Rule |
|---|---|
| Trades | ≥ 60 |
| Profit factor | ≥ 1.15 |
| Win rate | ≥ 50% |
| Desk Sharpe (trades only, excluding parked SPY) | ≥ 0.5 |
| Desk max drawdown | ≤ 20% |
| Portfolio Sharpe vs SPY | no more than 0.15 below |
| In-sample profit factor | ≥ 1.0 (consistency) |
| Robustness | ≥ 70% of 27 nearby parameter sets profitable |
| Data coverage | ≥ 80 symbols with full history |

The report records a hash of `config.toml`. **Changing any setting locks trading again** until you re-run the backtest and it passes. That is the rule for every "improvement": change, re-test, then trade.

Honest caveats, also printed in the report:
- The universe is today's large caps, so the backtest has survivorship bias and will look somewhat better than reality.
- Simulated fills at the open with fixed slippage are an approximation.
- A pass means "worth paper trading," not "will make money." Paper results are the real test.

## Setup on Replit

1. **Get Alpaca paper keys**: sign up at alpaca.markets (free), switch to the Paper account, and generate API keys.
2. **Import the repo**: Replit → Create → Import from GitHub → this repo.
3. **Add Secrets** (padlock icon): `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `DASHBOARD_PASSWORD`. Optional: `NTFY_TOPIC` for phone alerts. See `.env.example`.
4. **Add a database**: Replit → Database → PostgreSQL. It sets `DATABASE_URL`. Without it, the journal lives in a local file that is wiped on every redeploy. Positions are always safe at Alpaca either way.
5. **Install and test** in the Shell:
   ```
   pip install -r requirements.txt
   python -m unittest discover -s tests -t . -v
   ```
6. **Run the real backtest**:
   ```
   python -m tradingbot backtest
   ```
   This takes a few minutes the first time while it downloads data. If it says **FAILED**, stop here. Paper trading stays locked, and that's the system working. Send me the output and we'll look at why.
7. **Check what it would do today** (sends nothing):
   ```
   python -m tradingbot run --dry-run
   ```
8. **Deploy**: Deploy → **Reserved VM** (it must stay on 24/7; Autoscale would put the scheduler to sleep). Reserved VMs are a paid Replit feature. The run command comes from `.replit`. The deployment includes `reports/backtest_report.json` from step 6, which is what unlocks trading.
9. **Optional uptime alert**: point a free monitor such as UptimeRobot at `https://<your-app>/health`. It returns 503 if the scheduler stops.

## Daily routine (US Eastern)

| Time | What happens |
|---|---|
| 09:31 | Reconcile with Alpaca, compute signals from yesterday's close, sell exits, buy entries with attached stops, rebalance SPY |
| After 10:00 | If the app started late, it runs exits only that day (the backtest assumes fills at the open) |
| Every 5 min | Reconcile; reset stops from the actual fill price; replace any missing stop |
| 16:10 | Equity snapshot, drawdown breaker check, daily summary alert |

## Dashboard

Shows gate status, regime, heartbeat, account and desk equity vs SPY, positions with stops, open orders, backtest checks, closed trades and the journal.

Controls are separate actions:
- **Pause new entries**
- **Cancel pending entries**
- **Reset circuit breaker**
- **Close all positions** (asks you to type FLATTEN)

Controls only work when `DASHBOARD_PASSWORD` is set.

## Commands

```
python -m tradingbot backtest [--source alpaca|yfinance|csv|synthetic] [--quick]
python -m tradingbot status          # unlocked or locked, and why
python -m tradingbot run --dry-run   # today's orders, nothing sent
python -m tradingbot serve           # scheduler + dashboard
```

## How to evaluate the paper period

Run it for at least 2–3 months, or about 30+ closed trades. Then compare paper win rate, profit factor and average R with the out-of-sample backtest. Big gaps usually mean fill assumptions or data problems, not bad luck.

Live trading is disabled in code in v1 (`broker.py`). Turning it on should be a deliberate decision after a reviewed paper period.

## Layout

```
config.toml              every tunable setting (hashed into the gate)
tradingbot/strategy.py   indicators, signals, regime
tradingbot/risk.py       sizing + risk gate (shared by backtest and live)
tradingbot/backtest.py   daily simulator
tradingbot/gates.py      in/out-of-sample split, robustness grid, pass/fail report
tradingbot/live.py       paper desk: reconcile, morning run, monitor, end of day
tradingbot/broker.py     Alpaca client (paper only) + dry-run broker
tradingbot/llm_filter.py optional veto
tradingbot/dashboard.py  web UI and controls
tests/                   unit tests incl. look-ahead and backtest/live parity checks
```

## Next steps after v1 proves itself

- Earnings-drift (PEAD) desk as a second satellite, needs an earnings-surprise data source.
- Day-trading desk (check current pattern-day-trader rules for accounts near $25k first).
- Point-in-time index membership data to remove survivorship bias.
