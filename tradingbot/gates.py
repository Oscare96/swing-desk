"""The pass/fail gate that decides whether paper trading may start.

Steps:
  1. load data (with 200-day warm-up before the start date)
  2. full backtest; split stats into in-sample and out-of-sample
  3. robustness grid: nearby parameter sets must mostly stay profitable
  4. check every gate on the OUT-OF-SAMPLE period
  5. write reports/backtest_report.json (+ trades and equity CSVs)

Paper trading reads the report and starts only if `passed` is true AND the
report's config hash matches the running config.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
from datetime import datetime, timezone

import pandas as pd

from .backtest import run_backtest
from .config import ROOT, Config
from .data import coverage_filter, load_bars
from .metrics import period_report
from .universe import UNIVERSE

log = logging.getLogger(__name__)
REPORT_DIR = ROOT / "reports"
REPORT_PATH = REPORT_DIR / "backtest_report.json"
MIN_SYMBOLS = 80


def _check(name, value, op, threshold):
    ok = value >= threshold if op == ">=" else value <= threshold
    return {"check": name, "value": round(float(value), 4), "rule": f"{op} {threshold}", "passed": bool(ok)}


def robustness(bars, cfg, start, end) -> dict:
    s = cfg.strategy
    grid = {
        "rsi_entry_below": sorted({s.rsi_entry_below - 5, s.rsi_entry_below, s.rsi_entry_below + 5}),
        "exit_sma": sorted({max(3, s.exit_sma - 5), s.exit_sma, s.exit_sma + 5}),
        "stop_atr_mult": sorted({s.stop_atr_mult - 0.5, s.stop_atr_mult, s.stop_atr_mult + 0.5}),
    }
    runs = []
    for combo in itertools.product(*grid.values()):
        params = dict(zip(grid, combo))
        r = run_backtest(bars, cfg.with_overrides("strategy", **params), start, end)
        tr = r.trades
        pnl = float(tr["pnl"].sum()) if not tr.empty else 0.0
        runs.append({**params, "trades": int(len(tr)), "pnl": round(pnl, 2)})
    profitable = sum(1 for x in runs if x["pnl"] > 0 and x["trades"] > 0)
    return {"grid_size": len(runs), "profitable_share": profitable / len(runs), "runs": runs}


def run_gate(cfg: Config, source: str | None = None, skip_robustness: bool = False) -> dict:
    source = source or os.environ.get("DATA_SOURCE", "alpaca")
    bt = cfg.backtest
    warm_start = (pd.Timestamp(bt.start) - pd.Timedelta(days=420)).strftime("%Y-%m-%d")
    symbols = UNIVERSE + [cfg.regime.benchmark]
    bars = load_bars(symbols, warm_start, bt.end, source=source)
    if cfg.regime.benchmark not in bars:
        raise RuntimeError(f"No data for benchmark {cfg.regime.benchmark}")
    bars, dropped = coverage_filter(bars, warm_start, bt.end)
    n_syms = len(bars) - 1

    res = run_backtest(bars, cfg, bt.start, bt.end)
    oos_start = (pd.Timestamp(bt.in_sample_end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    ins = period_report(res, bt.start, bt.in_sample_end)
    oos = period_report(res, oos_start, bt.end)
    full = period_report(res, bt.start, bt.end)
    rob = None if skip_robustness else robustness(bars, cfg, bt.start, bt.end)

    g, ts = cfg.gates, oos["trade_stats"]
    checks = [
        _check("symbols with full history", n_syms, ">=", MIN_SYMBOLS),
        _check("OOS trades", ts["trades"], ">=", g.min_trades),
        _check("OOS profit factor", min(ts["profit_factor"], 99), ">=", g.min_profit_factor),
        _check("OOS win rate", ts["win_rate"], ">=", g.min_win_rate),
        _check("OOS desk Sharpe (trades only, no parked SPY)", oos["desk"]["sharpe"], ">=", g.min_sharpe),
        _check("OOS desk max drawdown", oos["desk"]["max_drawdown"], "<=", g.max_drawdown),
        _check("OOS portfolio Sharpe shortfall vs SPY",
               oos["spy_buy_hold"]["sharpe"] - oos["portfolio"]["sharpe"], "<=", g.max_sharpe_shortfall_vs_spy),
        _check("in-sample profit factor (consistency)", min(ins["trade_stats"]["profit_factor"], 99), ">=", 1.0),
    ]
    if rob is not None:
        checks.append(_check("robust parameter share", rob["profitable_share"], ">=", g.min_robust_share))
    else:
        checks.append({"check": "robust parameter share", "value": None, "rule": "skipped", "passed": False})

    passed = all(c["passed"] for c in checks)
    notes = []
    if source == "synthetic":
        passed = False
        notes.append("Synthetic data: this run only tests the plumbing and can never unlock trading.")
    notes.append("Universe is today's large caps, so results carry survivorship bias; treat them as optimistic.")
    notes.append("The optional LLM veto is not part of this backtest; its value is measured in paper trading.")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config_hash": cfg.hash(),
        "data_source": source,
        "passed": passed,
        "checks": checks,
        "notes": notes,
        "symbols_dropped": dropped,
        "rejections": res.rejections,
        "circuit_breaker_halts": res.halts,
        "in_sample": ins,
        "out_of_sample": oos,
        "full_period": full,
        "robustness": rob,
    }
    REPORT_DIR.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str))
    res.trades.to_csv(REPORT_DIR / "backtest_trades.csv", index=False)
    res.equity.to_csv(REPORT_DIR / "backtest_equity.csv")
    return report


def load_report() -> dict | None:
    if not REPORT_PATH.exists():
        return None
    return json.loads(REPORT_PATH.read_text())


def trading_unlocked(cfg: Config) -> tuple[bool, str]:
    rep = load_report()
    if rep is None:
        return False, "no backtest report: run `python -m tradingbot backtest` first"
    if rep.get("config_hash") != cfg.hash():
        return False, "config changed since the last backtest: re-run the backtest"
    if not rep.get("passed"):
        failed = [c["check"] for c in rep.get("checks", []) if not c.get("passed")]
        return False, "last backtest failed: " + ", ".join(failed or rep.get("notes", []))
    return True, "backtest passed for this config"
