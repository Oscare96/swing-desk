"""Run with:  python -m unittest discover -s tests -v"""
import unittest
from datetime import date

import numpy as np
import pandas as pd

from tradingbot.backtest import run_backtest
from tradingbot.config import Config
from tradingbot.data import synthetic
from tradingbot.risk import RiskState, gate_new_entry, size_position
from tradingbot.strategy import add_features, candidates_on, regime_series, rsi, sma

CFG = Config.load()
SYMS = [f"S{i:02d}" for i in range(30)]
START, END = "2020-01-01", "2023-12-29"


def bars():
    b = synthetic(SYMS + ["SPY"], "2019-01-01", END, seed=11)
    return b


class Indicators(unittest.TestCase):
    def test_rsi_bounds_and_extremes(self):
        up = pd.Series(np.arange(1, 50, dtype=float))
        down = up[::-1].reset_index(drop=True)
        self.assertAlmostEqual(rsi(up, 3).iloc[-1], 100.0)
        self.assertLess(rsi(down, 3).iloc[-1], 1.0)
        r = rsi(pd.Series(np.random.default_rng(1).normal(0, 1, 500).cumsum() + 100), 3).dropna()
        self.assertTrue(((r >= 0) & (r <= 100)).all())

    def test_sma_needs_full_window(self):
        s = sma(pd.Series([1.0, 2, 3, 4]), 3)
        self.assertTrue(np.isnan(s.iloc[1]))
        self.assertEqual(s.iloc[3], 3.0)

    def test_regime_hysteresis(self):
        c = pd.Series([100.0] * 200 + [97.0] * 5 + [101.0] * 5 + [104.0] * 5)
        r = regime_series(c, CFG.regime)
        self.assertFalse(r.iloc[199 - 1])          # not enough history yet -> defensive
        # 200-day SMA ~ 100: 97 is >2% below -> off; 101 is inside the band -> stays off
        self.assertFalse(r.iloc[204])
        self.assertFalse(r.iloc[209])


class Risk(unittest.TestCase):
    def state(self, **kw):
        base = dict(satellite_equity=7500, satellite_cash=7500, desk_equity=7500, desk_peak=7500,
                    account_equity=25000, day_start_equity=25000)
        base.update(kw)
        return RiskState(**base)

    def test_size_by_risk(self):
        # risk 0.75% of 7500 = 56.25; stop 2.5*2 = 5 -> 11 shares; cap 20% = 1500/50 = 30
        self.assertEqual(size_position(50.0, 2.0, self.state(), CFG.risk, CFG.strategy), 11)

    def test_size_by_cash(self):
        self.assertEqual(size_position(50.0, 2.0, self.state(satellite_cash=200), CFG.risk, CFG.strategy), 3)

    def test_gate_rules(self):
        r = CFG.risk
        self.assertIsNone(gate_new_entry("AAA", 10, self.state(), r))
        self.assertIn("regime", gate_new_entry("AAA", 10, self.state(regime_on=False), r))
        self.assertIn("paused", gate_new_entry("AAA", 10, self.state(paused=True), r))
        self.assertIn("stale", gate_new_entry("AAA", 10, self.state(data_age_days=9), r))
        self.assertIn("drawdown", gate_new_entry("AAA", 10, self.state(desk_equity=6000), r))
        self.assertIn("daily loss", gate_new_entry("AAA", 10, self.state(account_equity=24000), r))
        self.assertIn("max positions", gate_new_entry("AAA", 10, self.state(open_symbols=set("abcdef")), r))
        self.assertIn("zero", gate_new_entry("AAA", 0, self.state(), r))


class Backtest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = bars()
        cls.res = run_backtest(cls.bars, CFG, START, END)

    def test_trades_happen_and_accounting_is_consistent(self):
        eq, tr = self.res.equity, self.res.trades
        self.assertGreater(len(tr), 20)
        np.testing.assert_allclose(eq["total"], eq["core"] + eq["sat_value"], rtol=1e-9)
        self.assertTrue((eq["total"] > 0).all())
        self.assertTrue((tr["exit_date"] >= tr["entry_date"]).all())

    def test_never_more_than_max_positions(self):
        tr = self.res.trades
        for d in self.res.equity.index[::5]:
            live = ((tr["entry_date"] <= d) & (tr["exit_date"] > d)).sum()
            self.assertLessEqual(live, CFG.risk.max_positions)

    def test_stops_respect_gaps(self):
        stops = self.res.trades[self.res.trades["exit_reason"] == "stop"]
        self.assertTrue((stops["r_multiple"] <= -0.99).all() or stops.empty)

    def test_no_lookahead(self):
        """Scrambling prices AFTER a cutoff must not change any trade entered before it."""
        cut = pd.Timestamp("2022-06-01")
        changed = {}
        rng = np.random.default_rng(99)
        for s, df in self.bars.items():
            df = df.copy()
            m = df.index > cut
            df.loc[m, ["open", "high", "low", "close"]] *= rng.uniform(0.5, 1.5, (m.sum(), 1))
            df.loc[m, "high"] = df.loc[m, ["open", "high", "low", "close"]].max(axis=1)
            df.loc[m, "low"] = df.loc[m, ["open", "high", "low", "close"]].min(axis=1)
            changed[s] = df
        res2 = run_backtest(changed, CFG, START, END)
        a = self.res.trades[self.res.trades["exit_date"] <= cut].reset_index(drop=True)
        b = res2.trades[res2.trades["exit_date"] <= cut].reset_index(drop=True)
        pd.testing.assert_frame_equal(a, b)
        pd.testing.assert_frame_equal(self.res.equity.loc[:cut], res2.equity.loc[:cut])


class Gate(unittest.TestCase):
    def test_config_hash_changes_with_strategy(self):
        self.assertNotEqual(CFG.hash(), CFG.with_overrides("strategy", rsi_entry_below=20.0).hash())
        self.assertEqual(CFG.hash(), CFG.with_overrides("llm", enabled=True).hash())


class LiveParity(unittest.TestCase):
    """The live desk must pick the same entries the backtest logic would on that day."""

    def test_morning_matches_signals(self):
        import tradingbot.live as live
        from tradingbot.broker import DryRunBroker
        from tradingbot.store import Store

        b = bars()
        spy_dates = b["SPY"].index
        feats = {s: add_features(b[s], CFG.strategy) for s in SYMS}
        risk_on = regime_series(b["SPY"]["close"], CFG.regime)
        # find a risk-on day with at least one signal
        day = next(d for d in spy_dates[300:] if risk_on[d] and candidates_on(d, feats, set()))
        today = spy_dates[spy_dates.get_loc(day) + 1].date()
        expected = [c.symbol for c in candidates_on(day, feats, set())][: CFG.risk.max_new_entries_per_day]

        orig_unlock, orig_univ = live.trading_unlocked, live.UNIVERSE
        live.trading_unlocked = lambda cfg: (True, "test")
        live.UNIVERSE = SYMS
        try:
            broker, store = DryRunBroker(equity=25000), Store(":memory:")
            desk = live.Desk(CFG, broker, store, bars_loader=lambda s, a, z: b, today_fn=lambda: today)
            out = desk.morning()
        finally:
            live.trading_unlocked, live.UNIVERSE = orig_unlock, orig_univ
        self.assertEqual(out["signal_date"], str(day.date()))
        got = [e["symbol"] for e in out["entries"]]
        self.assertEqual(got, expected[: len(got)])
        self.assertTrue(got)
        # every entry is an OTO order carrying a protective stop, with a saved plan
        buys = [o for o in broker.sent if o.get("side") == "buy" and o["symbol"] != "SPY"]
        self.assertTrue(all(o.get("stop_loss") for o in buys))
        self.assertEqual(set(store.plans()), set(got))
        # running again the same day sends nothing new (idempotent client ids)
        n = len(broker.sent)
        live.trading_unlocked = lambda cfg: (True, "test")
        live.UNIVERSE = SYMS
        try:
            desk.morning()
        finally:
            live.trading_unlocked, live.UNIVERSE = orig_unlock, orig_univ
        self.assertEqual(len([o for o in broker.sent[n:] if o.get("side") == "buy" and o["symbol"] != "SPY"]), 0)

    def test_locked_gate_blocks_orders(self):
        import tradingbot.live as live
        from tradingbot.broker import DryRunBroker
        from tradingbot.store import Store

        orig = live.trading_unlocked
        live.trading_unlocked = lambda cfg: (False, "no report")
        try:
            broker = DryRunBroker()
            out = live.Desk(CFG, broker, Store(":memory:"), today_fn=lambda: date(2024, 1, 2)).morning()
        finally:
            live.trading_unlocked = orig
        self.assertIn("blocked", out)
        self.assertEqual(broker.sent, [])


if __name__ == "__main__":
    unittest.main()


class Reconcile(unittest.TestCase):
    def test_fill_stop_adjust_then_stop_out(self):
        from tradingbot.broker import DryRunBroker
        from tradingbot.live import Desk
        from tradingbot.store import Store

        store, broker = Store(":memory:"), DryRunBroker()
        desk = Desk(CFG, broker, store, today_fn=lambda: date(2024, 3, 1))
        store.set("desk_base", 7500.0)
        store.save_plan({"symbol": "AAA", "entry_date": "2024-03-01", "entry_price": 100.0, "shares": 10,
                         "atr": 2.0, "stop": 95.0, "risk_per_share": 5.0, "entry_order_id": "e-2024-03-01-AAA",
                         "stop_order_id": None, "stop_adjusted": 0, "status": "pending"})
        # entry filled at 101; OTO stop leg resting at the broker
        broker._pos = {"AAA": {"qty": "10", "avg_entry_price": "101", "market_value": "1010",
                               "unrealized_pl": "0", "current_price": "101"}}
        broker._orders = [{"id": "stop1", "symbol": "AAA", "side": "sell", "type": "stop", "legs": []}]
        desk.reconcile()
        plan = store.plans()["AAA"]
        self.assertEqual(plan["status"], "open")
        self.assertAlmostEqual(plan["stop"], 96.0)          # 101 - 2.5 * 2
        self.assertTrue(any(o.get("replace") == "stop1" and o["stop_price"] == 96.0 for o in broker.sent))

        # stop fires: position gone, filled stop order in history
        broker._pos, broker._orders = {}, []
        broker.closed = [{"symbol": "AAA", "side": "sell", "status": "filled", "type": "stop",
                          "filled_avg_price": "95.9", "filled_at": "2024-03-05T15:00:00Z"}]
        desk.reconcile()
        self.assertNotIn("AAA", store.plans())
        t = store.trades()[0]
        self.assertEqual(t["exit_reason"], "stop")
        self.assertAlmostEqual(t["pnl"], -51.0)
        self.assertAlmostEqual(desk._desk_equity({}), 7500 - 51.0)

    def test_missing_stop_gets_placed(self):
        from tradingbot.broker import DryRunBroker
        from tradingbot.live import Desk
        from tradingbot.store import Store

        store, broker = Store(":memory:"), DryRunBroker()
        desk = Desk(CFG, broker, store, today_fn=lambda: date(2024, 3, 1))
        store.save_plan({"symbol": "BBB", "entry_date": "2024-02-27", "entry_price": 50.0, "shares": 20,
                         "atr": 1.0, "stop": 47.5, "risk_per_share": 2.5, "entry_order_id": "e-x",
                         "stop_order_id": None, "stop_adjusted": 1, "status": "open"})
        broker._pos = {"BBB": {"qty": "20", "avg_entry_price": "50", "market_value": "1000",
                               "unrealized_pl": "0", "current_price": "50"}}
        desk.reconcile()
        stops = [o for o in broker.sent if o.get("type_") == "stop"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["stop_price"], 47.5)


class DataLoader(unittest.TestCase):
    """fetch_alpaca must fail loudly when the API keeps rate-limiting."""

    def test_raises_after_repeated_429(self):
        import requests
        from unittest import mock
        from tradingbot import data as data_mod

        resp = mock.Mock()
        resp.status_code = 429
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError("429 Too Many Requests")
        with mock.patch("requests.get", return_value=resp), \
             mock.patch.object(data_mod, "alpaca_headers", return_value={}), \
             mock.patch("time.sleep", return_value=None):
            with self.assertRaises(requests.exceptions.HTTPError):
                data_mod.fetch_alpaca(["AAA"], "2020-01-01", "2020-01-10")

    def test_retries_then_succeeds(self):
        from unittest import mock
        from tradingbot import data as data_mod

        bad = mock.Mock()
        bad.status_code = 429
        good = mock.Mock()
        good.status_code = 200
        good.json.return_value = {"bars": {"AAA": []}}
        good.raise_for_status.return_value = None
        with mock.patch("requests.get", side_effect=[bad, good]), \
             mock.patch.object(data_mod, "alpaca_headers", return_value={}), \
             mock.patch("time.sleep", return_value=None):
            out = data_mod.fetch_alpaca(["AAA"], "2020-01-01", "2020-01-10")
        self.assertEqual(out, {})


class BreakerAutoReset(unittest.TestCase):
    """The live breaker must reset itself after 20 sessions, like the backtest."""

    def _desk(self, store):
        import tradingbot.live as live
        from tradingbot.broker import DryRunBroker
        live.trading_unlocked = lambda cfg: (True, "test")
        live.UNIVERSE = SYMS
        broker = DryRunBroker(equity=25000)
        return live.Desk(CFG, broker, store, bars_loader=lambda s, a, z: bars(),
                         today_fn=lambda: date(2024, 1, 2))

    def test_auto_resets_after_20_sessions(self):
        import tradingbot.live as live
        from tradingbot.store import Store
        orig_unlock, orig_univ = live.trading_unlocked, live.UNIVERSE
        try:
            store = Store(":memory:")
            store.set("halted", True)
            store.set("halt_sessions_left", 1)
            self._desk(store).morning()
        finally:
            live.trading_unlocked, live.UNIVERSE = orig_unlock, orig_univ
        self.assertFalse(store.get("halted"))

    def test_counts_down_while_halted(self):
        import tradingbot.live as live
        from tradingbot.store import Store
        orig_unlock, orig_univ = live.trading_unlocked, live.UNIVERSE
        try:
            store = Store(":memory:")
            store.set("halted", True)
            store.set("halt_sessions_left", 5)
            self._desk(store).morning()
        finally:
            live.trading_unlocked, live.UNIVERSE = orig_unlock, orig_univ
        self.assertTrue(store.get("halted"))
        self.assertEqual(store.get("halt_sessions_left"), 4)

    def test_manual_reset_still_works(self):
        import tradingbot.live as live
        from tradingbot.broker import DryRunBroker
        from tradingbot.store import Store
        store, broker = Store(":memory:"), DryRunBroker(equity=25000)
        store.set("halted", True)
        store.set("halt_sessions_left", 20)
        desk = live.Desk(CFG, broker, store, today_fn=lambda: date(2024, 1, 2))
        desk.reset_breaker()
        self.assertFalse(store.get("halted"))
        self.assertEqual(store.get("halt_sessions_left"), 0)

    def test_dry_run_does_not_mutate_breaker_state(self):
        """python -m tradingbot run --dry-run is documented as side-effect free."""
        import tradingbot.live as live
        from tradingbot.store import Store
        orig_unlock, orig_univ = live.trading_unlocked, live.UNIVERSE
        try:
            store = Store(":memory:")
            store.set("halted", True)
            store.set("halt_sessions_left", 1)
            self._desk(store).morning(dry_run=True)
        finally:
            live.trading_unlocked, live.UNIVERSE = orig_unlock, orig_univ
        self.assertTrue(store.get("halted"))
        self.assertEqual(store.get("halt_sessions_left"), 1)

    def test_dry_run_breach_saves_nothing(self):
        """A dry run that detects a breach must not persist halted, the counter,
        or a new peak -- it only reports."""
        import tradingbot.live as live
        from tradingbot.store import Store
        orig_unlock, orig_univ = live.trading_unlocked, live.UNIVERSE
        try:
            store = Store(":memory:")
            store.set("desk_peak", 100000.0)  # force a 12% drawdown breach
            self._desk(store).morning(dry_run=True)
        finally:
            live.trading_unlocked, live.UNIVERSE = orig_unlock, orig_univ
        self.assertFalse(store.get("halted"))
        self.assertFalse(store.get("halt_sessions_left"))
        self.assertEqual(store.get("desk_peak"), 100000.0)

    def _run_until_clear(self, store, desk, limit=40):
        n = 0
        while store.get("halted") and n < limit:
            desk.morning()
            n += 1
        return n

    def test_morning_halt_blocks_20_opens(self):
        """A breach found in the morning blocks that morning + 19 more opens,
        matching a backtest breach at the prior close."""
        import tradingbot.live as live
        from tradingbot.store import Store
        orig_unlock, orig_univ = live.trading_unlocked, live.UNIVERSE
        try:
            store = Store(":memory:")
            desk = self._desk(store)
            store.set("desk_peak", 100000.0)  # force a 12% drawdown breach
            desk.morning()  # halt latches here; this morning's entries blocked
            self.assertTrue(store.get("halted"))
            n = 1 + self._run_until_clear(store, desk)
            # halt morning + 20 countdown mornings, the last of which resets:
            # 20 blocked opens, matching a backtest breach at the prior close
            self.assertEqual(n, 21)
        finally:
            live.trading_unlocked, live.UNIVERSE = orig_unlock, orig_univ

    def test_eod_halt_blocks_20_opens(self):
        """A breach found after the close blocks the next 20 opens,
        matching a backtest breach at that close."""
        import tradingbot.live as live
        from tradingbot.store import Store
        orig_unlock, orig_univ = live.trading_unlocked, live.UNIVERSE
        try:
            store = Store(":memory:")
            desk = self._desk(store)
            store.set("desk_peak", 100000.0)  # force a 12% drawdown breach
            desk.end_of_day()  # halt latches here; next morning is the first blocked open
            self.assertTrue(store.get("halted"))
            self.assertEqual(store.get("halt_sessions_left"), 21)
            n = self._run_until_clear(store, desk)
            self.assertEqual(n, 21)  # 20 blocked opens, the 21st morning resets
            self.assertFalse(store.get("halted"))
        finally:
            live.trading_unlocked, live.UNIVERSE = orig_unlock, orig_univ


class Spy50Filter(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(CFG.strategy.entry_spy_sma50_filter)

    def test_filter_blocks_only_allowed_days(self):
        """With the filter on, every trade's signal day must have SPY above its 50d SMA."""
        b = bars()
        res = run_backtest(b, CFG.with_overrides("strategy", entry_spy_sma50_filter=True), START, END)
        self.assertGreater(len(res.trades), 0)
        spy = b["SPY"]["close"]
        sma50 = spy.rolling(50).mean()
        sessions = spy.index
        for _, t in res.trades.iterrows():
            sig = sessions[sessions < pd.Timestamp(t["entry_date"])][-1]
            self.assertTrue(sma50.loc[sig] > 0)
            self.assertGreater(float(spy.loc[sig]), float(sma50.loc[sig]))

    def test_live_sends_no_buys_when_spy_below_50d(self):
        import tradingbot.live as live
        from tradingbot.broker import DryRunBroker
        from tradingbot.store import Store

        b = bars()
        spy = b["SPY"]["close"]
        sma50 = spy.rolling(50).mean()
        sessions = spy.index
        day = next(d for d in sessions[300:] if float(spy.loc[d]) < float(sma50.loc[d]))
        today = sessions[sessions.get_loc(day) + 1].date()
        cfg2 = CFG.with_overrides("strategy", entry_spy_sma50_filter=True)
        orig_unlock, orig_univ = live.trading_unlocked, live.UNIVERSE
        live.trading_unlocked = lambda cfg: (True, "test")
        live.UNIVERSE = SYMS
        try:
            broker, store = DryRunBroker(equity=25000), Store(":memory:")
            desk = live.Desk(cfg2, broker, store, bars_loader=lambda s, a, z: b,
                             today_fn=lambda: today)
            desk.morning()
        finally:
            live.trading_unlocked, live.UNIVERSE = orig_unlock, orig_univ
        buys = [o for o in broker.sent if o.get("side") == "buy" and o.get("symbol") != "SPY"]
        self.assertEqual(buys, [])
