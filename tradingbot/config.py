"""Configuration loading and hashing.

The config hash ties a backtest report to the exact settings it tested. Paper
trading compares the hash of the running config with the hash recorded in the
passing report, so any unreviewed change blocks trading until re-tested.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.toml"
# Sections that change trading behaviour. [llm] is excluded because the veto
# filter is never part of the backtest.
HASHED_SECTIONS = ("account", "regime", "strategy", "risk", "costs", "backtest", "gates")


def _ns(d):
    if isinstance(d, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in d.items()})
    return d


class Config:
    def __init__(self, raw: dict):
        self.raw = raw
        for k, v in raw.items():
            setattr(self, k, _ns(v))

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        path = Path(path or os.environ.get("TRADINGBOT_CONFIG", DEFAULT_CONFIG))
        with open(path, "rb") as f:
            return cls(tomllib.load(f))

    def with_overrides(self, section: str, **values) -> "Config":
        raw = copy.deepcopy(self.raw)
        raw[section].update(values)
        return Config(raw)

    def hash(self) -> str:
        subset = {k: self.raw.get(k) for k in HASHED_SECTIONS}
        blob = json.dumps(subset, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]
