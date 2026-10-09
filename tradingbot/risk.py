"""Position sizing and the risk gate.

Sizing is pure arithmetic from the stop distance; nothing upstream (including
the optional LLM filter) can choose a size. The same gate runs in the backtest
and in paper/live trading, so what was tested is what trades.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class RiskState:
    satellite_equity: float          # size of the satellite sleeve (sets position sizes)
    satellite_cash: float            # what the satellite can spend now (cash + parked SPY)
    desk_equity: float               # swing-trade P&L track (excludes parked SPY)
    desk_peak: float                 # high-water mark of desk_equity
    account_equity: float
    day_start_equity: float
    open_symbols: set = field(default_factory=set)
    entries_today: int = 0
    regime_on: bool = True
    data_age_days: int = 0
    paused: bool = False             # manual kill switch
    halted: bool = False             # circuit breaker latched (needs manual reset)


def size_position(ref_price: float, atr: float, state: RiskState, risk_cfg, strat_cfg) -> int:
    stop_dist = strat_cfg.stop_atr_mult * atr
    if stop_dist <= 0 or ref_price <= 0:
        return 0
    by_risk = state.satellite_equity * risk_cfg.risk_per_trade_pct / stop_dist
    by_cap = state.satellite_equity * risk_cfg.max_position_pct / ref_price
    by_cash = state.satellite_cash / (ref_price * 1.01)  # small buffer for an opening gap
    return max(0, math.floor(min(by_risk, by_cap, by_cash)))


def gate_new_entry(symbol: str, shares: int, state: RiskState, risk_cfg) -> str | None:
    """Return None if allowed, else the rejection reason."""
    if state.paused:
        return "paused by user"
    if state.halted:
        return "circuit breaker latched (reset manually)"
    if not state.regime_on:
        return "regime risk-off: no new entries"
    if state.data_age_days > risk_cfg.max_data_age_days:
        return f"stale data ({state.data_age_days}d old)"
    if symbol in state.open_symbols:
        return "already holding"
    if len(state.open_symbols) >= risk_cfg.max_positions:
        return "max positions reached"
    if state.entries_today >= risk_cfg.max_new_entries_per_day:
        return "max new entries today"
    if drawdown_breached(state, risk_cfg):
        return "desk drawdown limit hit"
    if state.day_start_equity > 0 and state.account_equity < state.day_start_equity * (1 - risk_cfg.daily_loss_halt_pct):
        return "daily loss limit hit"
    if shares <= 0:
        return "size rounds to zero (not enough cash or risk budget)"
    return None


def drawdown_breached(state: RiskState, risk_cfg) -> bool:
    return state.desk_peak > 0 and state.desk_equity < state.desk_peak * (1 - risk_cfg.satellite_drawdown_halt_pct)
