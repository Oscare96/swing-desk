"""Paper-trading desk: the backtested rules, run against a real broker account.

Daily cycle (US/Eastern):
  09:31  morning(): reconcile, signals from yesterday's close, exits, entries, SPY core
  every 5 min while open: monitor(): reconcile, make sure every position has a broker stop
  16:10  end_of_day(): reconcile, equity snapshot, drawdown breaker, daily summary

Mirrors the backtest: decisions use completed daily bars only, orders go in at
the open, sizing and gating use the same functions, and stops sit at the broker
(so they work even if this process is down).
"""
from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import llm_filter
from .config import Config
from .data import load_bars
from .gates import trading_unlocked
from .notify import notify
from .risk import RiskState, drawdown_breached, gate_new_entry, size_position
from .strategy import add_features, candidates_on, regime_series, should_exit
from .universe import UNIVERSE

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")


def ny_today() -> date:
    return datetime.now(NY).date()


class Desk:
    def __init__(self, cfg: Config, broker, store, bars_loader=None, today_fn=ny_today):
        self.cfg, self.broker, self.store = cfg, broker, store
        self.bench = cfg.regime.benchmark
        self.today_fn = today_fn
        self.bars_loader = bars_loader or (lambda syms, start, end: load_bars(syms, start, end, source="alpaca", use_cache=False))

    # =========================================================== market data
    def _signals(self):
        today = self.today_fn()
        start = (today - timedelta(days=450)).isoformat()
        # end at yesterday: free Alpaca data can't serve the most recent 15 minutes of SIP data
        bars = self.bars_loader(UNIVERSE + [self.bench], start, (today - timedelta(days=1)).isoformat())
        # never let today's partial bar in: decisions use completed days only
        bars = {s: df[df.index.date < today] for s, df in bars.items() if len(df)}
        spy = bars[self.bench]
        last = spy.index[-1]
        age = int(np.busday_count(last.date(), today)) - 1
        feats = {s: add_features(df, self.cfg.strategy) for s, df in bars.items() if s != self.bench and len(df) > 210}
        risk_on = bool(regime_series(spy["close"], self.cfg.regime).iloc[-1])
        return feats, spy, last, max(age, 0), risk_on

    # ============================================================ accounting
    def _desk_equity(self, positions: dict) -> float:
        base = self.store.get("desk_base")
        if base is None:
            base = float(self.broker.account()["equity"]) * self.cfg.account.satellite_fraction
            self.store.set("desk_base", base)
        realized = sum(t["pnl"] or 0 for t in self.store.trades(limit=100000))
        plans = self.store.plans()
        unreal = sum(float(p.get("unrealized_pl", 0)) for s, p in positions.items() if s in plans)
        return base + realized + unreal

    # ============================================================= reconcile
    def reconcile(self) -> dict:
        """Bring local plans in line with what the broker actually holds."""
        positions = self.broker.positions()
        open_orders = self.broker.open_orders()
        orders_by_sym: dict[str, list] = {}
        for o in open_orders:
            orders_by_sym.setdefault(o["symbol"], []).append(o)
            for leg in o.get("legs") or []:
                if leg.get("status") in ("new", "accepted", "held", "pending_new", "partially_filled"):
                    orders_by_sym.setdefault(leg["symbol"], []).append(leg)
        mult = self.cfg.strategy.stop_atr_mult
        events = []

        for sym, plan in self.store.plans().items():
            pos = positions.get(sym)
            if pos:
                qty = int(float(pos["qty"]))
                entry = float(pos["avg_entry_price"])
                if plan["status"] == "pending":
                    plan.update(status="open", entry_price=entry, shares=qty)
                    events.append(f"{sym}: entry filled {qty} @ {entry:.2f}")
                    self.store.log("fill", f"entry filled {qty} @ {entry:.2f}", sym)
                plan["shares"] = qty
                stops = [o for o in orders_by_sym.get(sym, []) if o.get("side") == "sell" and o.get("type") == "stop"]
                want = round(entry - mult * plan["atr"], 2) if not plan["stop_adjusted"] else plan["stop"]
                if not stops and plan["status"] != "exiting":
                    try:
                        o = self.broker.submit(sym, qty, "sell", f"s-{sym}-{plan['entry_date']}-{int(time.time())}",
                                               type_="stop", tif="gtc", stop_price=want)
                        plan.update(stop=want, stop_order_id=o["id"], stop_adjusted=1)
                        events.append(f"{sym}: placed missing stop @ {want:.2f}")
                        self.store.log("stop", f"placed missing broker stop @ {want:.2f}", sym)
                    except Exception as e:
                        self.store.log("error", f"COULD NOT PLACE STOP: {e}", sym)
                        notify("Stop missing", f"{sym}: could not place a protective stop ({e}). Check the account.", urgent=True)
                elif stops and not plan["stop_adjusted"]:
                    st = stops[0]
                    try:
                        o = self.broker.replace_stop(st["id"], want) or {}
                        plan.update(stop=want, stop_order_id=o.get("id", st["id"]), stop_adjusted=1)
                        self.store.log("stop", f"stop set from fill: {want:.2f}", sym)
                    except Exception as e:  # keep the original stop; it is still protective
                        plan.update(stop_order_id=st["id"])
                        self.store.log("warn", f"could not adjust stop: {e}", sym)
                elif stops:
                    plan["stop_order_id"] = stops[0]["id"]
                self.store.save_plan(plan)
                continue

            # no position at the broker
            if plan["status"] == "pending":
                o = self.broker.order_by_client_id(plan["entry_order_id"]) if plan.get("entry_order_id") else None
                st = (o or {}).get("status", "unknown")
                if st in ("canceled", "expired", "rejected", "unknown", "done_for_day") or o is None:
                    self.store.close_plan(sym)
                    self.store.log("entry", f"entry not filled ({st}); plan dropped", sym)
                    events.append(f"{sym}: entry not filled ({st})")
                elif st == "filled":   # filled and already stopped out the same day
                    self._record_close(sym, plan, orders_by_sym)
                continue
            # it was open/exiting and is gone: it was sold (stop or exit)
            self._record_close(sym, plan, orders_by_sym)
            events.append(f"{sym}: position closed")

        plans = self.store.plans()
        for sym in positions:
            if sym != self.bench and sym not in plans:
                key = f"unmanaged_{sym}"
                if not self.store.get(key):
                    self.store.set(key, True)
                    self.store.log("warn", "position at broker with no plan: left untouched", sym)
                    notify("Unmanaged position", f"{sym} is held but the desk has no plan for it. It is left alone.")
        self.store.set("heartbeat", datetime.now(NY).isoformat(timespec="seconds"))
        return {"positions": positions, "open_orders": open_orders, "events": events}

    def _record_close(self, sym: str, plan: dict, orders_by_sym: dict):
        for o in orders_by_sym.get(sym, []):          # leftover stop etc.
            try:
                self.broker.cancel(o["id"])
            except Exception as e:
                log.warning("cancel %s: %s", o["id"], e)
        fills = [o for o in self.broker.orders(status="closed", limit=100)
                 if o.get("symbol") == sym and o.get("side") == "sell" and o.get("status") == "filled"]
        price, reason, when = None, "exit", self.today_fn().isoformat()
        if fills:
            f = fills[0]
            price = float(f["filled_avg_price"])
            reason = "stop" if f.get("type") == "stop" else (self.store.get(f"exit_reason_{sym}") or "exit")
            when = (f.get("filled_at") or when)[:10]
        entry = plan.get("entry_price") or 0
        shares = plan.get("shares") or 0
        pnl = (price - entry) * shares if price else 0.0
        rps = plan.get("risk_per_share") or 0
        self.store.add_trade({
            "id": f"{sym}-{plan['entry_date']}", "symbol": sym, "entry_date": plan["entry_date"],
            "exit_date": when, "entry_price": entry, "exit_price": price, "shares": shares,
            "pnl": pnl, "r_multiple": (price - entry) / rps if price and rps else None, "exit_reason": reason,
        })
        self.store.close_plan(sym)
        self.store.log("exit", f"closed ({reason}) @ {price}, P&L {pnl:+.2f}", sym)
        notify(f"{sym} closed", f"{reason} @ {price}, P&L {pnl:+.2f}")

    # =============================================================== morning
    def morning(self, dry_run: bool = False, allow_entries: bool = True) -> dict:
        """allow_entries=False when the open was missed: exits still run, but no new
        entries, because the backtest assumed fills at the opening price."""
        today = self.today_fn().isoformat()
        cfg, rp, sp = self.cfg, self.cfg.risk, self.cfg.strategy
        ok, why = trading_unlocked(cfg)
        if not ok and not dry_run:
            self.store.log("blocked", why)
            notify("Trading blocked", why, urgent=True)
            return {"blocked": why}

        # a dry run must not touch the account, so it skips reconcile (which can place stops)
        positions = self.broker.positions() if dry_run else self.reconcile()["positions"]
        feats, spy, last, age, risk_on = self._signals()
        acct = self.broker.account()
        equity, last_equity = float(acct["equity"]), float(acct.get("last_equity") or acct["equity"])
        plans = self.store.plans()
        summary = {"date": today, "signal_date": str(last.date()), "regime_on": risk_on, "data_age": age,
                   "exits": [], "entries": [], "rejected": {}, "vetoed": {}, "spy": None}
        if not ok:
            summary["gate"] = f"LOCKED ({why}); dry run only"

        # ---------------- exits (decided on yesterday's close, sold at the open)
        exiting = set()
        for sym, plan in plans.items():
            if plan["status"] != "open" or sym not in positions or sym not in feats:
                continue
            f = feats[sym]
            held = int((f.index.date >= date.fromisoformat(plan["entry_date"])).sum())
            reason = should_exit(f.iloc[-1], held, sp)
            if not reason:
                continue
            exiting.add(sym)
            summary["exits"].append({"symbol": sym, "reason": reason})
            if dry_run:
                continue
            if plan.get("stop_order_id"):
                self.broker.cancel(plan["stop_order_id"])
                self._wait_cancelled(plan["stop_order_id"])
            self.broker.submit(sym, int(float(positions[sym]["qty"])), "sell", f"x-{today}-{sym}")
            plan["status"] = "exiting"
            self.store.save_plan(plan)
            self.store.set(f"exit_reason_{sym}", reason)
            self.store.log("exit", f"selling at open: {reason}", sym)

        # ---------------- risk state (same gate as the backtest)
        sat_eq = equity * cfg.account.satellite_fraction
        held_val = sum(float(positions[s]["market_value"]) for s in plans if s in positions and s not in exiting)
        spy_val = float(positions[self.bench]["market_value"]) if self.bench in positions else 0.0
        desk_eq = self._desk_equity(positions)
        desk_peak = max(self.store.get("desk_peak", desk_eq), desk_eq)
        self.store.set("desk_peak", desk_peak)
        state = RiskState(
            satellite_equity=sat_eq, satellite_cash=max(0.0, sat_eq - held_val),
            desk_equity=desk_eq, desk_peak=desk_peak, account_equity=equity, day_start_equity=last_equity,
            open_symbols={s for s in plans if s not in exiting}, regime_on=risk_on, data_age_days=age,
            entries_today=sum(1 for p in plans.values() if p["entry_date"] == today),
            paused=bool(self.store.get("paused", False)), halted=bool(self.store.get("halted", False)),
        )
        if drawdown_breached(state, rp) and not state.halted:
            self.store.set("halted", True)
            state.halted = True
            notify("Circuit breaker", "Desk drawdown limit hit. New entries halted until you reset it.", urgent=True)

        # ---------------- entries
        cands = candidates_on(last, feats, exclude=set(plans))
        cands = cands[: rp.max_new_entries_per_day * 3]
        vetoes = self._llm_vetoes(cands, feats) if cands and cfg.llm.enabled and allow_entries else {}
        entries_value = 0.0
        if not allow_entries:
            summary["rejected"] = {c.symbol: "missed the opening window" for c in cands}
            cands = []
        for c in cands:
            if c.symbol in vetoes:
                summary["vetoed"][c.symbol] = vetoes[c.symbol]
                continue
            shares = size_position(c.ref_price, c.atr, state, rp, sp)
            why = gate_new_entry(c.symbol, shares, state, rp)
            if why:
                summary["rejected"][c.symbol] = why
                continue
            stop = c.stop_price(sp.stop_atr_mult)
            cid = f"e-{today}-{c.symbol}"
            summary["entries"].append({"symbol": c.symbol, "shares": shares, "ref_price": c.ref_price,
                                       "stop": stop, "rsi": round(c.rsi, 1)})
            state.open_symbols.add(c.symbol)
            state.entries_today += 1
            state.satellite_cash -= shares * c.ref_price * 1.01
            entries_value += shares * c.ref_price
            if dry_run:
                continue
            # save the plan BEFORE sending, so a crash can never leave an untracked position
            self.store.save_plan({"symbol": c.symbol, "entry_date": today, "entry_price": c.ref_price,
                                  "shares": shares, "atr": c.atr, "stop": stop,
                                  "risk_per_share": sp.stop_atr_mult * c.atr, "entry_order_id": cid,
                                  "stop_order_id": None, "stop_adjusted": 0, "status": "pending"})
            self.broker.submit(c.symbol, shares, "buy", cid, tif="day", stop_loss=stop)
            self.store.log("entry", f"buy {shares} at open, stop {stop:.2f}, RSI {c.rsi:.1f}", c.symbol,
                           {"ref_price": c.ref_price, "atr": c.atr})

        # ---------------- SPY: core sleeve + parked idle satellite cash
        target = 0.0
        if risk_on:
            target = equity * cfg.account.core_fraction
            if cfg.account.park_idle_cash_in_spy:
                target += max(0.0, sat_eq - held_val - entries_value)
        spy_px = float(spy["close"].iloc[-1])
        delta_val = target - spy_val
        if (target == 0 and spy_val > 0) or abs(delta_val) > 0.03 * equity:
            qty = math.floor(abs(delta_val) / spy_px)
            if target == 0 and self.bench in positions:
                qty = int(float(positions[self.bench]["qty"]))
            if qty > 0:
                side = "buy" if delta_val > 0 else "sell"
                summary["spy"] = {"side": side, "qty": qty, "target_value": round(target, 2)}
                if not dry_run:
                    self.broker.submit(self.bench, qty, side, f"spy-{today}-{side}")
                    self.store.log("core", f"{side} {qty} SPY (target ${target:,.0f}, regime {'on' if risk_on else 'off'})", self.bench)

        summary["rejected_count"] = len(summary["rejected"])
        if not dry_run:
            self.store.set("morning_done", today)
            self.store.set("last_summary", summary)
            self.store.set("regime_on", risk_on)
            self.store.log("summary", self._summary_text(summary), data=summary)
            notify("Morning run", self._summary_text(summary))
        return summary

    def _wait_cancelled(self, order_id: str, timeout: float = 10.0):
        end = time.time() + timeout
        while time.time() < end:
            if not any(o["id"] == order_id for o in self.broker.open_orders()):
                return
            time.sleep(0.5)

    def _llm_vetoes(self, cands, feats) -> dict:
        used = self.store.get(f"llm_calls_{self.today_fn()}", 0)
        if used >= self.cfg.llm.max_calls_per_day:
            return {}
        data = []
        for c in cands:
            f = feats[c.symbol]
            hi20 = float(f["high"].iloc[-20:].max())
            data.append({"symbol": c.symbol, "close": round(c.ref_price, 2), "rsi3": round(c.rsi, 1),
                         "pct_from_20d_high": round(c.ref_price / hi20 - 1, 4),
                         "pct_above_200d_sma": round(c.ref_price / float(f["sma_trend"].iloc[-1]) - 1, 4)})
        try:
            res = llm_filter.review(data, self.cfg.llm.model)
        except Exception as e:
            self.store.log("llm", f"filter failed, no vetoes applied: {e}")
            return {}
        self.store.set(f"llm_calls_{self.today_fn()}", used + 1)
        self.store.log("llm", "veto review", data={"model": self.cfg.llm.model, **res})
        return {s: d["reason"] for s, d in (res.get("decisions") or {}).items() if d["veto"]}

    @staticmethod
    def _summary_text(s: dict) -> str:
        parts = [f"Regime {'ON' if s['regime_on'] else 'OFF'} (signals from {s['signal_date']})"]
        parts.append("Exits: " + (", ".join(f"{e['symbol']} ({e['reason']})" for e in s["exits"]) or "none"))
        parts.append("Entries: " + (", ".join(f"{e['symbol']} x{e['shares']}" for e in s["entries"]) or "none"))
        if s["vetoed"]:
            parts.append("Vetoed: " + ", ".join(s["vetoed"]))
        if s["spy"]:
            parts.append(f"SPY: {s['spy']['side']} {s['spy']['qty']}")
        return " | ".join(parts)

    # =============================================================== monitor
    def monitor(self) -> dict:
        return self.reconcile()

    # ============================================================ end of day
    def end_of_day(self) -> dict:
        today = self.today_fn().isoformat()
        rec = self.reconcile()
        acct = self.broker.account()
        equity = float(acct["equity"])
        positions = rec["positions"]
        desk_eq = self._desk_equity(positions)
        peak = max(self.store.get("desk_peak", desk_eq), desk_eq)
        self.store.set("desk_peak", peak)
        spy_px = float(positions[self.bench]["current_price"]) if self.bench in positions else None
        self.store.record_equity(today, equity, spy_px, desk_eq, bool(self.store.get("regime_on", False)))
        if desk_eq < peak * (1 - self.cfg.risk.satellite_drawdown_halt_pct) and not self.store.get("halted"):
            self.store.set("halted", True)
            notify("Circuit breaker", f"Desk down {1 - desk_eq / peak:.1%} from peak. New entries halted.", urgent=True)
        self.store.set("eod_done", today)
        msg = f"Equity ${equity:,.0f} | desk ${desk_eq:,.0f} (peak ${peak:,.0f}) | {len(self.store.plans())} swing positions"
        self.store.log("eod", msg)
        notify("End of day", msg)
        return {"equity": equity, "desk_equity": desk_eq}

    # ============================================================== controls
    def pause(self, on: bool):
        self.store.set("paused", on)
        self.store.log("control", "new entries paused" if on else "new entries resumed")

    def reset_breaker(self):
        desk_eq = self._desk_equity(self.broker.positions())
        self.store.set("halted", False)
        self.store.set("desk_peak", desk_eq)
        self.store.log("control", f"circuit breaker reset; new peak ${desk_eq:,.0f}")

    def cancel_pending_entries(self) -> int:
        n = 0
        for o in self.broker.open_orders():
            if o.get("side") == "buy" and str(o.get("client_order_id", "")).startswith("e-"):
                self.broker.cancel(o["id"])
                n += 1
        self.store.log("control", f"cancelled {n} pending entry orders")
        return n

    def close_all(self) -> int:
        self.store.set("paused", True)
        for o in self.broker.open_orders():
            self.broker.cancel(o["id"])
        n = 0
        for sym in list(self.broker.positions()):
            self.broker.close_position(sym)
            n += 1
        self.store.log("control", f"FLATTEN: cancelled all orders, closed {n} positions, entries paused")
        notify("Flattened", f"Closed {n} positions; entries paused.", urgent=True)
        return n
