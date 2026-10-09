"""Download the daily price history the backtest needs into data/csv/ and zip it.

Use this on any machine WITH internet (your Mac, Replit shell, Google Colab).
Then upload price_data.zip to an AI sandbox that has no internet (e.g. ChatGPT),
unzip it into data/csv/, and run:  python -m tradingbot backtest --source csv

    python tools/fetch_data.py                 # yfinance, no keys needed
    python tools/fetch_data.py --source alpaca # needs ALPACA_API_KEY / ALPACA_SECRET_KEY
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tradingbot.config import Config  # noqa: E402
from tradingbot.data import CSV_DIR, fetch_alpaca, fetch_yfinance  # noqa: E402
from tradingbot.universe import UNIVERSE  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["yfinance", "alpaca"], default="yfinance")
    args = ap.parse_args()

    cfg = Config.load()
    start = (pd.Timestamp(cfg.backtest.start) - pd.Timedelta(days=420)).strftime("%Y-%m-%d")
    end = cfg.backtest.end
    symbols = UNIVERSE + [cfg.regime.benchmark]
    print(f"Downloading {len(symbols)} symbols, {start} -> {end}, from {args.source} ...")
    frames = (fetch_alpaca if args.source == "alpaca" else fetch_yfinance)(symbols, start, end)

    CSV_DIR.mkdir(parents=True, exist_ok=True)
    for sym, df in frames.items():
        df.to_csv(CSV_DIR / f"{sym}.csv")
    missing = sorted(set(symbols) - set(frames))
    print(f"Saved {len(frames)} files to {CSV_DIR}")
    if missing:
        print("No data for:", ", ".join(missing))

    out = ROOT / "price_data.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(CSV_DIR.glob("*.csv")):
            z.write(p, f"data/csv/{p.name}")
        z.writestr("data/csv/SOURCE.txt", f"source={args.source}\nstart={start}\nend={end}\n")
    print(f"Wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
