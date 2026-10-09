"""Trading universe: ~100 liquid US large caps.

Known bias: this is TODAY's list of large caps, so a 2020-2025 backtest
excludes companies that shrank or were delisted (survivorship bias). That
flatters results somewhat. The gates are set conservatively partly for this
reason; the forward paper period is the real test.

Tickers whose history can't be loaded for the full window are dropped and
listed in the backtest report.
"""

UNIVERSE = [
    # Tech / communication
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "ORCL", "CRM", "ADBE",
    "AMD", "INTC", "CSCO", "QCOM", "TXN", "IBM", "NOW", "INTU", "AMAT", "MU",
    "LRCX", "KLAC", "ADI", "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW", "BLK", "AXP", "SPGI",
    "CB", "PGR", "MMC", "V", "MA", "PYPL",
    # Health care
    "UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "TMO", "ABT", "DHR", "BMY",
    "AMGN", "GILD", "CVS", "MDT", "ISRG", "VRTX", "REGN", "ZTS", "SYK",
    # Consumer
    "WMT", "COST", "HD", "LOW", "MCD", "SBUX", "NKE", "TGT", "PG", "KO",
    "PEP", "PM", "MO", "MDLZ", "CL", "TJX", "BKNG", "TSLA",
    # Industrials / energy / materials / utilities / real estate
    "CAT", "DE", "HON", "GE", "UPS", "RTX", "LMT", "BA", "UNP", "MMM",
    "XOM", "CVX", "COP", "SLB", "LIN", "NEE", "DUK", "SO", "PLD", "AMT",
]

assert len(set(UNIVERSE)) == len(UNIVERSE), "duplicate tickers in universe"
