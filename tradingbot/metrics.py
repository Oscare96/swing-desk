"""Performance statistics."""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def equity_stats(eq: pd.Series) -> dict:
    eq = eq.dropna()
    if len(eq) < 2:
        return {"cagr": 0.0, "sharpe": 0.0, "vol": 0.0, "max_drawdown": 0.0, "total_return": 0.0}
    rets = eq.pct_change().dropna()
    years = len(rets) / TRADING_DAYS
    total = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else -1.0
    sd = rets.std()
    sharpe = float(rets.mean() / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else 0.0
    dd = float((eq / eq.cummax() - 1).min())
    return {
        "total_return": float(total), "cagr": float(cagr), "sharpe": sharpe,
        "vol": float(sd * np.sqrt(TRADING_DAYS)), "max_drawdown": -dd,
    }


def trade_stats(trades: pd.DataFrame) -> dict:
    if trades is None or trades.empty:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "avg_r": 0.0,
                "avg_hold_days": 0.0, "exit_reasons": {}}
    wins = trades.loc[trades["pnl"] > 0, "pnl"].sum()
    losses = -trades.loc[trades["pnl"] < 0, "pnl"].sum()
    return {
        "trades": int(len(trades)),
        "win_rate": float((trades["pnl"] > 0).mean()),
        "profit_factor": float(wins / losses) if losses > 0 else float("inf") if wins > 0 else 0.0,
        "avg_r": float(trades["r_multiple"].mean()),
        "avg_hold_days": float(trades["held_days"].mean()),
        "total_pnl": float(trades["pnl"].sum()),
        "exit_reasons": trades["exit_reason"].value_counts().to_dict(),
    }


def period_report(res, start: str, end: str) -> dict:
    eq = res.equity.loc[start:end]
    tr = res.trades
    if not tr.empty:
        tr = tr[(tr["entry_date"] >= pd.Timestamp(start)) & (tr["entry_date"] <= pd.Timestamp(end))]
    return {
        "start": str(eq.index[0].date()) if len(eq) else start,
        "end": str(eq.index[-1].date()) if len(eq) else end,
        "desk": equity_stats(eq["desk"]),            # swing trades only: the edge being tested
        "satellite": equity_stats(eq["satellite"]),  # desk + idle cash parked in SPY
        "portfolio": equity_stats(eq["total"]),
        "spy_buy_hold": equity_stats(eq["spy_bh"]),
        "risk_on_share": float(eq["risk_on"].mean()) if len(eq) else 0.0,
        "trade_stats": trade_stats(tr),
    }
