"""Durable state: trade plans, journal, equity history, settings.

Uses PostgreSQL when DATABASE_URL is set (recommended on Replit: deployments
do not keep local files across redeploys), otherwise a local SQLite file.
The broker remains the source of truth for what is actually held.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

from .config import ROOT

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)""",
    """CREATE TABLE IF NOT EXISTS plans (
        symbol TEXT PRIMARY KEY, entry_date TEXT, entry_price REAL, shares INTEGER,
        atr REAL, stop REAL, risk_per_share REAL, entry_order_id TEXT, stop_order_id TEXT,
        stop_adjusted INTEGER DEFAULT 0, status TEXT)""",
    """CREATE TABLE IF NOT EXISTS trades (
        id TEXT PRIMARY KEY, symbol TEXT, entry_date TEXT, exit_date TEXT, entry_price REAL,
        exit_price REAL, shares INTEGER, pnl REAL, r_multiple REAL, exit_reason TEXT)""",
    """CREATE TABLE IF NOT EXISTS journal (
        ts TEXT, kind TEXT, symbol TEXT, message TEXT, data TEXT)""",
    """CREATE TABLE IF NOT EXISTS equity (
        date TEXT PRIMARY KEY, equity REAL, spy_close REAL, satellite_value REAL, risk_on INTEGER)""",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, url: str | None = None):
        url = url if url is not None else os.environ.get("DATABASE_URL", "")
        self.lock = threading.Lock()
        if url.startswith("postgres"):
            import psycopg

            self.pg = True
            self.conn = psycopg.connect(url, autocommit=True)
        else:
            self.pg = False
            path = url or str(ROOT / "data" / "state.db")
            if path != ":memory:":
                os.makedirs(os.path.dirname(path), exist_ok=True)
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
        for stmt in SCHEMA:
            self.execute(stmt)

    # ----------------------------------------------------------------- core
    def execute(self, sql: str, params=()):
        if self.pg:
            sql = sql.replace("?", "%s")
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(sql, params)
            if not self.pg:
                self.conn.commit()
            return cur

    def query(self, sql: str, params=()) -> list[dict]:
        cur = self.execute(sql, params)
        rows = cur.fetchall()
        if self.pg:
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, r)) for r in rows]
        return [dict(r) for r in rows]

    def _upsert(self, table: str, key: str, row: dict):
        cols = list(row)
        ph = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != key)
        self.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph}) "
                     f"ON CONFLICT ({key}) DO UPDATE SET {updates}", tuple(row.values()))

    # ------------------------------------------------------------------ kv
    def get(self, key: str, default=None):
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key: str, value):
        self._upsert("kv", "key", {"key": key, "value": json.dumps(value)})

    # --------------------------------------------------------------- plans
    def plans(self) -> dict[str, dict]:
        return {r["symbol"]: r for r in self.query("SELECT * FROM plans WHERE status != 'closed'")}

    def save_plan(self, plan: dict):
        self._upsert("plans", "symbol", {**plan, "status": plan.get("status", "pending")})

    def close_plan(self, symbol: str):
        self.execute("UPDATE plans SET status='closed' WHERE symbol=?", (symbol,))

    # -------------------------------------------------------------- trades
    def add_trade(self, t: dict):
        self._upsert("trades", "id", t)

    def trades(self, limit=200) -> list[dict]:
        return self.query("SELECT * FROM trades ORDER BY exit_date DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------- journal
    def log(self, kind: str, message: str, symbol: str = "", data=None):
        self.execute("INSERT INTO journal (ts, kind, symbol, message, data) VALUES (?,?,?,?,?)",
                     (now_iso(), kind, symbol, message, json.dumps(data, default=str) if data is not None else None))

    def journal(self, limit=100) -> list[dict]:
        return self.query("SELECT * FROM journal ORDER BY ts DESC LIMIT ?", (limit,))

    # -------------------------------------------------------------- equity
    def record_equity(self, date: str, equity: float, spy_close: float, sat_value: float, risk_on: bool):
        self._upsert("equity", "date", {"date": date, "equity": equity, "spy_close": spy_close,
                                         "satellite_value": sat_value, "risk_on": int(risk_on)})

    def equity_history(self) -> list[dict]:
        return self.query("SELECT * FROM equity ORDER BY date")
