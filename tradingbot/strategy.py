"""Deterministic signal layer. The same functions drive the backtest and live trading.

Everything here uses data up to and including day t's close only. Orders based
on day t are filled at day t+1's open, so there is no look-ahead.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- indicators
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rsi(close: pd.Series, n: int) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / down.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(down != 0, 100.0)


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


# ------------------------------------------------------------------ features
def add_features(df: pd.DataFrame, p) -> pd.DataFrame:
    """p is the [strategy] config section."""
    out = df.copy()
    out["sma_trend"] = sma(out["close"], p.trend_sma)
    out["sma_exit"] = sma(out["close"], p.exit_sma)
    out["rsi"] = rsi(out["close"], p.rsi_days)
    out["atr"] = atr(out, p.atr_days)
    out["adv"] = (out["close"] * out["volume"]).rolling(20, min_periods=20).mean()
    out["entry_signal"] = (
        (out["close"] > out["sma_trend"])
        & (out["rsi"] < p.rsi_entry_below)
        & (out["close"] >= p.min_price)
        & (out["adv"] >= p.min_dollar_volume)
        & out["atr"].notna()
    )
    out["exit_signal"] = out["close"] > out["sma_exit"]
    return out


def regime_series(spy_close: pd.Series, r) -> pd.Series:
    """True = risk-on. Hysteresis band: flips on only above SMA*(1+band),
    off only below SMA*(1-band). r is the [regime] config section."""
    m = sma(spy_close, r.sma_days)
    state, out = True, []
    for c, s in zip(spy_close.values, m.values):
        if np.isnan(s):
            out.append(False)  # no regime call without enough history -> defensive
            continue
        if state and c < s * (1 - r.band_pct):
            state = False
        elif not state and c > s * (1 + r.band_pct):
            state = True
        out.append(state)
    return pd.Series(out, index=spy_close.index, name="risk_on")


@dataclass
class Candidate:
    symbol: str
    date: pd.Timestamp     # signal date (close)
    ref_price: float       # signal-day close, used for sizing and stop
    atr: float
    rsi: float
    adv: float

    def stop_price(self, mult: float, fill_price: float | None = None) -> float:
        base = fill_price if fill_price is not None else self.ref_price
        return round(base - mult * self.atr, 2)


def candidates_on(day: pd.Timestamp, feats: dict[str, pd.DataFrame], exclude: set[str]) -> list[Candidate]:
    """Entry candidates from day's close, most oversold first."""
    out = []
    for sym, f in feats.items():
        if sym in exclude or day not in f.index:
            continue
        row = f.loc[day]
        if bool(row["entry_signal"]):
            out.append(Candidate(sym, day, float(row["close"]), float(row["atr"]), float(row["rsi"]), float(row["adv"])))
    out.sort(key=lambda c: (c.rsi, -c.adv))
    return out


def should_exit(row: pd.Series, held_days: int, p) -> str | None:
    if bool(row["exit_signal"]):
        return "target: close above exit SMA"
    if held_days >= p.max_hold_days:
        return "time stop"
    return None
