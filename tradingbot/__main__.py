"""Command line.

  python -m tradingbot backtest            # full gate (needs data access); writes reports/
  python -m tradingbot backtest --source synthetic --quick   # plumbing test only
  python -m tradingbot status              # is paper trading unlocked?
  python -m tradingbot run --dry-run       # compute today's orders without sending them
  python -m tradingbot serve               # scheduler + dashboard (what the deployment runs)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import Config


def _print_report(r: dict):
    oos, ins = r["out_of_sample"], r["in_sample"]
    def row(name, d):
        return (f"  {name:<22} CAGR {d['cagr']:>7.1%}  Sharpe {d['sharpe']:>5.2f}  "
                f"MaxDD {d['max_drawdown']:>6.1%}")
    for label, p in (("IN-SAMPLE", ins), ("OUT-OF-SAMPLE", oos)):
        ts = p["trade_stats"]
        print(f"\n{label}  {p['start']} -> {p['end']}")
        print(row("Desk (trades only)", p["desk"]))
        print(row("Satellite (+parked SPY)", p["satellite"]))
        print(row("Portfolio", p["portfolio"]))
        print(row("SPY buy & hold", p["spy_buy_hold"]))
        print(f"  trades {ts['trades']}, win rate {ts['win_rate']:.0%}, profit factor {ts['profit_factor']:.2f}, "
              f"avg R {ts['avg_r']:.2f}, avg hold {ts['avg_hold_days']:.1f}d")
    print("\nGATES")
    for c in r["checks"]:
        print(f"  [{'PASS' if c['passed'] else 'FAIL'}] {c['check']}: {c['value']} ({c['rule']})")
    if r.get("symbols_dropped"):
        print(f"\nDropped for missing history: {', '.join(r['symbols_dropped'])}")
    for n in r["notes"]:
        print(f"Note: {n}")
    print(f"\nRESULT: {'PASSED - paper trading unlocked' if r['passed'] else 'FAILED - paper trading stays locked'}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tradingbot")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backtest")
    b.add_argument("--source", choices=["alpaca", "yfinance", "csv", "synthetic"], default=None)
    b.add_argument("--quick", action="store_true", help="skip the robustness grid (report cannot pass)")
    sub.add_parser("status")
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    sub.add_parser("serve")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.load(args.config)

    if args.cmd == "backtest":
        from .gates import run_gate
        rep = run_gate(cfg, source=args.source, skip_robustness=args.quick)
        _print_report(rep)
        return 0 if rep["passed"] else 1

    if args.cmd == "status":
        from .gates import trading_unlocked
        ok, why = trading_unlocked(cfg)
        print(("UNLOCKED: " if ok else "LOCKED: ") + why, f"(config hash {cfg.hash()})")
        return 0 if ok else 1

    from .broker import Alpaca
    from .live import Desk
    from .store import Store
    desk = Desk(cfg, Alpaca(paper=True), Store())

    if args.cmd == "run":
        print(json.dumps(desk.morning(dry_run=args.dry_run), indent=2, default=str))
        return 0

    if args.cmd == "serve":
        from .dashboard import create_app
        from .scheduler import Scheduler
        Scheduler(desk).start()
        port = int(os.environ.get("PORT", "8000"))
        create_app(desk, cfg).run(host="0.0.0.0", port=port)
        return 0


if __name__ == "__main__":
    sys.exit(main())
