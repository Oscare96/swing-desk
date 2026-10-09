"""Market data: daily OHLCV bars, split- and dividend-adjusted.

Sources:
  alpaca     - Alpaca market data API (free account works; needs API keys)
  yfinance   - Yahoo via the yfinance package (no keys; fine for backtests)
  csv        - your own files: data/csv/<SYMBOL>.csv with date,open,high,low,close,volume
  synthetic  - generated prices for testing the plumbing ONLY (never for decisions)

Downloaded bars are cached under data/cache so re-runs are fast.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .config import ROOT

log = logging.getLogger(__name__)
CACHE_DIR = ROOT / "data" / "cache"
CSV_DIR = ROOT / "data" / "csv"
COLS = ["open", "high", "low", "close", "volume"]


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df[COLS].astype(float).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df.dropna()
    df = df[(df["close"] > 0) & (df["high"] >= df["low"])]
    df.index = pd.DatetimeIndex(df.index).tz_localize(None).normalize()
    df.index.name = "date"
    return df


# --------------------------------------------------------------------- alpaca
def alpaca_headers() -> dict:
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY (Replit Secrets).")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def fetch_alpaca(symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    import requests

    url = "https://data.alpaca.markets/v2/stocks/bars"
    feed = os.environ.get("ALPACA_DATA_FEED", "sip")
    out: dict[str, list] = {s: [] for s in symbols}
    for i in range(0, len(symbols), 50):
        chunk = symbols[i : i + 50]
        params = {
            "symbols": ",".join(chunk), "timeframe": "1Day", "start": start, "end": end,
            "adjustment": "all", "feed": feed, "limit": 10000,
        }
        while True:
            for attempt in range(5):
                r = requests.get(url, params=params, headers=alpaca_headers(), timeout=30)
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                break
            body = r.json()
            for sym, bars in (body.get("bars") or {}).items():
                out[sym].extend(bars)
            token = body.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
    frames = {}
    for sym, bars in out.items():
        if not bars:
            continue
        df = pd.DataFrame(bars).rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        df.index = pd.to_datetime(df["t"]).dt.tz_convert("America/New_York").dt.tz_localize(None)
        frames[sym] = _clean(df)
    return frames


# ------------------------------------------------------------------- yfinance
def fetch_yfinance(symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    end_excl = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    frames = {}
    for sym in symbols:
        df = yf.download(sym, start=start, end=end_excl, auto_adjust=True, progress=False)
        if df is None or df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)
        frames[sym] = _clean(df)
    return frames


# ------------------------------------------------------------------------ csv
def load_csv(symbols: list[str]) -> dict[str, pd.DataFrame]:
    frames = {}
    for sym in symbols:
        p = CSV_DIR / f"{sym}.csv"
        if p.exists():
            df = pd.read_csv(p, parse_dates=["date"], index_col="date")
            df.columns = [c.lower() for c in df.columns]
            frames[sym] = _clean(df)
    return frames


# ------------------------------------------------------------------ synthetic
def synthetic(symbols: list[str], start: str, end: str, seed: int = 7) -> dict[str, pd.DataFrame]:
    """Random prices with trends, regimes and short-term mean reversion.

    Used only to exercise the code paths. Results on this data mean nothing.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, end)
    n = len(dates)
    market = rng.normal(0.0004, 0.011, n)
    # a bear stretch so the regime filter gets exercised
    market[n // 3 : n // 3 + 120] -= 0.003
    frames = {}
    for sym in symbols:
        beta = rng.uniform(0.7, 1.4)
        drift = rng.normal(0.0002, 0.0003)
        idio = rng.normal(0, rng.uniform(0.008, 0.02), n)
        r = drift + beta * market + idio
        # short-term mean reversion: partially undo yesterday's idiosyncratic move
        r[1:] -= 0.25 * idio[:-1]
        close = 100 * np.exp(np.cumsum(r)) * rng.uniform(0.3, 3)
        gap = rng.normal(0, 0.004, n)
        open_ = close * np.exp(-r + gap)  # open near previous close
        open_[0] = close[0]
        hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
        lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
        vol = rng.uniform(2e6, 2e7, n)
        frames[sym] = pd.DataFrame(
            {"open": open_, "high": hi, "low": lo, "close": close, "volume": vol}, index=dates
        ).rename_axis("date")
    return frames


# ---------------------------------------------------------------- entrypoint
def load_bars(symbols: list[str], start: str, end: str, source: str | None = None,
              use_cache: bool = True) -> dict[str, pd.DataFrame]:
    source = source or os.environ.get("DATA_SOURCE", "alpaca")
    if source == "synthetic":
        return synthetic(symbols, start, end)
    if source == "csv":
        return load_csv(symbols)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    frames, missing = {}, []
    for sym in symbols:
        p = CACHE_DIR / f"{source}_{sym}_{start}_{end}.csv"
        if use_cache and p.exists():
            frames[sym] = _clean(pd.read_csv(p, parse_dates=["date"], index_col="date"))
        else:
            missing.append(sym)
    if missing:
        log.info("downloading %d symbols from %s", len(missing), source)
        fetched = (fetch_alpaca if source == "alpaca" else fetch_yfinance)(missing, start, end)
        for sym, df in fetched.items():
            df.to_csv(CACHE_DIR / f"{source}_{sym}_{start}_{end}.csv")
            frames[sym] = df
    return frames


def coverage_filter(frames: dict[str, pd.DataFrame], start: str, end: str,
                    min_share: float = 0.95) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Drop symbols missing more than 5% of trading days in the window."""
    expected = len(pd.bdate_range(start, end)) * 0.96  # ~holidays
    kept, dropped = {}, []
    for sym, df in frames.items():
        if len(df) >= expected * min_share:
            kept[sym] = df
        else:
            dropped.append(sym)
    return kept, dropped
