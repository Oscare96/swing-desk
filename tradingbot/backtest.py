"""Daily event-driven backtest of the core + satellite portfolio.

Timeline for each trading day d:
  open  : execute orders decided at the previous close (exits, core regime
          trade, satellite entries, SPY parking), all with slippage
  intra : protective stops fire if the day's low touches them
          (gap-downs fill at the open, not the stop)
  close : mark to market, decide exits / entries for tomorrow's open

Signals only ever see data through the close they are computed on.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .risk import RiskState, gate_new_entry, size_position, drawdown_breached
from .strategy import add_features, regime_series

HALT_COOLDOWN_DAYS = 20   # backtest stand-in for "you review and reset the breaker"


@dataclass
class Position:
    symbol: str
    shares: int
    entry_date: pd.Timestamp
    entry_price: float
    stop: float
    risk_per_share: float
    held_days: int = 0


@dataclass
class Result:
    equity: pd.DataFrame   # core, satellite (incl. parked SPY), desk (trades only), total, spy_bh, risk_on
    trades: pd.DataFrame
    rejections: dict = field(default_factory=dict)
    halts: int = 0
    symbols_used: list = field(default_factory=list)


def _panel(feats: dict[str, pd.DataFrame], col: str, dates: pd.DatetimeIndex) -> np.ndarray:
    return np.column_stack([feats[s][col].reindex(dates).to_numpy(dtype=float) for s in feats])


def run_backtest(bars: dict[str, pd.DataFrame], cfg, start: str, end: str) -> Result:
    bench = cfg.regime.benchmark
    spy = bars[bench]
    syms = [s for s in bars if s != bench]
    sp, rp, kp, ck = cfg.strategy, cfg.risk, cfg.costs, cfg.account
    slip = kp.slippage_bps / 1e4

    feats = {s: add_features(bars[s], sp) for s in syms}
    risk_on_all = regime_series(spy["close"], cfg.regime)
    dates = spy.loc[start:end].index

    O, H, L, C = (_panel(feats, c, dates) for c in ("open", "high", "low", "close"))
    ENTRY = np.nan_to_num(_panel(feats, "entry_signal", dates)).astype(bool)
    EXIT = np.nan_to_num(_panel(feats, "exit_signal", dates)).astype(bool)
    RSI, ATR, ADV = (_panel(feats, c, dates) for c in ("rsi", "atr", "adv"))
    so, sc = spy["open"].reindex(dates).to_numpy(), spy["close"].reindex(dates).to_numpy()
    risk_on = risk_on_all.reindex(dates).fillna(False).to_numpy()
    idx = {s: j for j, s in enumerate(syms)}

    cap = ck.starting_capital
    core_cash, core_spy = cap * ck.core_fraction, 0
    sat_cash, sat_spy = cap * ck.satellite_fraction, 0
    positions: dict[str, Position] = {}
    pending_exit: dict[str, str] = {}
    pending_entry: list[tuple[str, int, float]] = []   # (symbol, shares, atr)
    core_target_on = False
    trades, rows, rejections = [], [], {}
    prev_total = cap
    # Sleeve performance is tracked time-weighted, so the yearly money moved
    # between core and satellite is not counted as profit or loss.
    sat_idx = desk_idx = desk_peak = sat_cash
    prev_sat = prev_desk = sat_cash
    park_spent = 0.0   # net cash put into parked SPY; parked P&L = value - park_spent
    halted_until, halts = -1, 0
    last_close = np.full(len(syms), np.nan)
    year = dates[0].year

    def sell_spy(n, px):
        return n * px * (1 - slip)  # ETF commission is 0 at Alpaca

    for i, d in enumerate(dates):
        flow = 0.0   # money moved into the satellite today (annual rebalance)
        # ----- annual sleeve rebalance to target split (at the first open of the year)
        if d.year != year:
            year = d.year
            total = core_cash + core_spy * so[i] + sat_cash + sat_spy * so[i] + sum(
                p.shares * (O[i, idx[p.symbol]] if not np.isnan(O[i, idx[p.symbol]]) else last_close[idx[p.symbol]]) for p in positions.values())
            sat_val = total - core_cash - core_spy * so[i]
            shift = total * ck.satellite_fraction - sat_val   # >0: move money core -> satellite
            if shift > 0:
                if core_cash < shift and core_spy > 0:
                    n = min(core_spy, math.ceil((shift - core_cash) / (so[i] * (1 - slip))))
                    core_cash += sell_spy(n, so[i])
                    core_spy -= n
                shift = min(shift, core_cash)
            else:
                shift = -min(-shift, max(sat_cash, 0) + sat_spy * so[i] * (1 - slip))
                if sat_cash < -shift and sat_spy > 0:
                    n = min(sat_spy, math.ceil((-shift - sat_cash) / (so[i] * (1 - slip))))
                    sat_cash += sell_spy(n, so[i])
                    park_spent -= sell_spy(n, so[i])
                    sat_spy -= n
                shift = -min(-shift, sat_cash)
            core_cash -= shift
            sat_cash += shift
            flow = shift

        # ----- OPEN: exits first
        for sym, reason in list(pending_exit.items()):
            j = idx[sym]
            px = O[i, j]
            if np.isnan(px) or sym not in positions:
                continue
            p = positions.pop(sym)
            fill = px * (1 - slip)
            sat_cash += p.shares * fill - p.shares * kp.commission_per_share
            trades.append(_trade(p, d, fill, reason))
            del pending_exit[sym]

        # ----- OPEN: core sleeve follows yesterday's regime call
        if core_target_on and core_cash > 0.02 * (core_cash + core_spy * so[i]):
            n = math.floor(core_cash / (so[i] * (1 + slip)))
            core_cash -= n * so[i] * (1 + slip)
            core_spy += n
        elif not core_target_on and core_spy > 0:
            core_cash += sell_spy(core_spy, so[i])
            core_spy = 0

        # ----- OPEN: satellite entries (sell parked SPY to fund them)
        for sym, shares, a in pending_entry:
            j = idx[sym]
            px = O[i, j]
            if np.isnan(px):
                rejections["no open price"] = rejections.get("no open price", 0) + 1
                continue
            fill = px * (1 + slip)
            need = shares * fill + shares * kp.commission_per_share
            if sat_cash < need and sat_spy > 0:
                n = min(sat_spy, math.ceil((need - sat_cash) / (so[i] * (1 - slip))))
                sat_cash += sell_spy(n, so[i])
                park_spent -= sell_spy(n, so[i])
                sat_spy -= n
            if sat_cash < need:   # opening gap made it too expensive: buy what we can
                shares = math.floor(sat_cash / (fill + kp.commission_per_share))
                if shares <= 0:
                    continue
                need = shares * fill + shares * kp.commission_per_share
            sat_cash -= need
            stop = fill - sp.stop_atr_mult * a
            positions[sym] = Position(sym, shares, d, fill, stop, fill - stop)
        pending_entry = []

        # ----- OPEN: park idle satellite cash in SPY while risk-on
        if ck.park_idle_cash_in_spy and core_target_on:
            sat_eq_open = sat_cash + sat_spy * so[i]
            if sat_cash > 0.05 * max(sat_eq_open, 1):
                n = math.floor(sat_cash / (so[i] * (1 + slip)))
                sat_cash -= n * so[i] * (1 + slip)
                park_spent += n * so[i] * (1 + slip)
                sat_spy += n
        elif sat_spy > 0:
            sat_cash += sell_spy(sat_spy, so[i])
            park_spent -= sell_spy(sat_spy, so[i])
            sat_spy = 0

        # ----- INTRADAY: protective stops
        for sym in list(positions):
            p, j = positions[sym], idx[sym]
            if np.isnan(L[i, j]):
                continue
            if L[i, j] <= p.stop:
                fill = min(O[i, j], p.stop) * (1 - slip)
                sat_cash += p.shares * fill - p.shares * kp.commission_per_share
                trades.append(_trade(positions.pop(sym), d, fill, "stop"))
                pending_exit.pop(sym, None)

        # ----- CLOSE: mark to market
        ok = ~np.isnan(C[i])
        last_close[ok] = C[i, ok]
        pos_val = sum(p.shares * last_close[idx[p.symbol]] for p in positions.values())
        core_val = core_cash + core_spy * sc[i]
        sat_val = sat_cash + sat_spy * sc[i] + pos_val
        total = core_val + sat_val
        desk_val = sat_val - (sat_spy * sc[i] - park_spent)   # satellite minus parked-SPY P&L
        if i > 0:
            sat_idx *= (sat_val - flow) / prev_sat
            desk_idx *= (desk_val - flow) / prev_desk
        prev_sat, prev_desk = sat_val, desk_val
        desk_peak = max(desk_peak, desk_idx)
        rows.append((d, core_val, sat_val, sat_idx, desk_idx, total, bool(risk_on[i])))

        # ----- CLOSE: exits for tomorrow
        for sym, p in positions.items():
            j = idx[sym]
            if np.isnan(C[i, j]):
                continue
            p.held_days += 1
            if EXIT[i, j]:
                pending_exit[sym] = "target: close above exit SMA"
            elif p.held_days >= sp.max_hold_days:
                pending_exit[sym] = "time stop"

        # ----- CLOSE: circuit breaker bookkeeping
        state = RiskState(
            satellite_equity=sat_val, desk_equity=desk_idx, desk_peak=desk_peak,
            satellite_cash=sat_cash + sat_spy * sc[i] + sum(
                positions[s].shares * last_close[idx[s]] for s in pending_exit),
            account_equity=total, day_start_equity=prev_total,
            open_symbols=set(positions) - set(pending_exit),
            regime_on=bool(risk_on[i]),
        )
        if i < halted_until:
            state.halted = True
        elif halted_until >= 0 and i == halted_until:
            desk_peak = desk_idx          # "manual reset" after the cooldown
            state.desk_peak = desk_idx
            halted_until = -1
        elif drawdown_breached(state, rp):
            halts += 1
            halted_until = i + HALT_COOLDOWN_DAYS
            state.halted = True

        # ----- CLOSE: entries for tomorrow
        core_target_on = bool(risk_on[i])
        cand = np.nonzero(ENTRY[i])[0]
        cand = sorted(cand, key=lambda j: (RSI[i, j], -ADV[i, j]))
        for j in cand:
            sym = syms[j]
            shares = size_position(C[i, j], ATR[i, j], state, rp, sp)
            why = gate_new_entry(sym, shares, state, rp)
            if why:
                rejections[why] = rejections.get(why, 0) + 1
                if why.startswith(("regime", "circuit", "desk drawdown", "daily loss", "max new", "max pos")):
                    break
                continue
            pending_entry.append((sym, shares, ATR[i, j]))
            state.open_symbols.add(sym)
            state.entries_today += 1
            state.satellite_cash -= shares * C[i, j] * 1.01
        prev_total = total

    eq = pd.DataFrame(rows, columns=["date", "core", "sat_value", "satellite", "desk", "total", "risk_on"]).set_index("date")
    eq["spy_bh"] = cap * sc / sc[0]
    tr = pd.DataFrame(trades)
    return Result(eq, tr, rejections, halts, syms)


def _trade(p: Position, d, fill: float, reason: str) -> dict:
    pnl = (fill - p.entry_price) * p.shares
    return {
        "symbol": p.symbol, "entry_date": p.entry_date, "exit_date": d,
        "entry_price": round(p.entry_price, 4), "exit_price": round(fill, 4),
        "shares": p.shares, "pnl": pnl, "ret": fill / p.entry_price - 1,
        "r_multiple": (fill - p.entry_price) / p.risk_per_share if p.risk_per_share > 0 else 0.0,
        "held_days": p.held_days, "exit_reason": reason,
    }
