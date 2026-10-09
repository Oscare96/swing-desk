"""Thin Alpaca Trading API client (paper by default).

The broker is the source of truth for positions and orders. Every order gets
a deterministic client_order_id so a retry can never create a duplicate.
"""
from __future__ import annotations

import logging
import os
import time

import requests

from .data import alpaca_headers

log = logging.getLogger(__name__)
PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


class BrokerError(RuntimeError):
    pass


class Alpaca:
    def __init__(self, paper: bool = True):
        if not paper:
            # v1 is paper-only on purpose. Going live should be a deliberate
            # code change after a long, reviewed paper period.
            if os.environ.get("ALLOW_LIVE_TRADING") != "I_ACCEPT_REAL_MONEY_RISK":
                raise BrokerError("Live trading is disabled in v1. Use paper.")
        self.base = (PAPER_URL if paper else LIVE_URL) + "/v2"
        self.paper = paper
        self.s = requests.Session()

    def _req(self, method: str, path: str, **kw):
        kw.setdefault("timeout", 20)
        for attempt in range(4):
            try:
                r = self.s.request(method, self.base + path, headers=alpaca_headers(), **kw)
            except requests.RequestException as e:
                if attempt == 3:
                    raise BrokerError(f"{method} {path}: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 404 and method == "GET":
                return None
            if r.status_code >= 400:
                raise BrokerError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else None
        raise BrokerError(f"{method} {path}: gave up after retries")

    # ---------------------------------------------------------------- reads
    def account(self) -> dict:
        return self._req("GET", "/account")

    def clock(self) -> dict:
        return self._req("GET", "/clock")

    def positions(self) -> dict[str, dict]:
        return {p["symbol"]: p for p in self._req("GET", "/positions") or []}

    def open_orders(self) -> list[dict]:
        # nested=false lists OTO stop legs as their own orders, so they are never hidden
        # under an already-filled parent
        return self._req("GET", "/orders", params={"status": "open", "limit": 500, "nested": "false"}) or []

    def orders(self, status="all", after=None, limit=200) -> list[dict]:
        params = {"status": status, "limit": limit, "nested": "true", "direction": "desc"}
        if after:
            params["after"] = after
        return self._req("GET", "/orders", params=params) or []

    def order_by_client_id(self, client_id: str) -> dict | None:
        return self._req("GET", "/orders:by_client_order_id", params={"client_order_id": client_id})

    # --------------------------------------------------------------- writes
    def submit(self, symbol: str, qty: int, side: str, client_id: str, *, type_="market",
               tif="day", stop_loss: float | None = None, stop_price: float | None = None) -> dict:
        existing = self.order_by_client_id(client_id)
        if existing:                       # idempotent: never send the same order twice
            return existing
        body = {"symbol": symbol, "qty": str(int(qty)), "side": side, "type": type_,
                "time_in_force": tif, "client_order_id": client_id}
        if stop_price is not None:
            body["stop_price"] = f"{stop_price:.2f}"
        if stop_loss is not None:
            body["order_class"] = "oto"
            body["stop_loss"] = {"stop_price": f"{stop_loss:.2f}"}
        return self._req("POST", "/orders", json=body)

    def replace_stop(self, order_id: str, stop_price: float) -> dict:
        return self._req("PATCH", f"/orders/{order_id}", json={"stop_price": f"{stop_price:.2f}"})

    def cancel(self, order_id: str):
        try:
            self._req("DELETE", f"/orders/{order_id}")
        except BrokerError as e:
            if "422" not in str(e):        # already filled / cancelled is fine
                raise

    def close_position(self, symbol: str):
        return self._req("DELETE", f"/positions/{symbol}")


class DryRunBroker:
    """Prints orders instead of sending them. Used by `run --dry-run` and tests."""

    paper = True

    def __init__(self, equity: float = 25000.0, positions=None, orders=None, is_open=True):
        self._equity, self._pos, self._orders, self._open = equity, positions or {}, orders or [], is_open
        self.sent: list[dict] = []
        self.closed: list[dict] = []      # tests put filled orders here

    def account(self):
        return {"equity": str(self._equity), "last_equity": str(self._equity), "cash": str(self._equity),
                "buying_power": str(self._equity), "status": "ACTIVE"}

    def clock(self):
        return {"is_open": self._open}

    def positions(self):
        return self._pos

    def open_orders(self):
        return self._orders

    def orders(self, **kw):
        return self.closed

    def order_by_client_id(self, cid):
        return next((o for o in self.sent if o["client_order_id"] == cid), None)

    def submit(self, symbol, qty, side, client_id, **kw):
        existing = self.order_by_client_id(client_id)
        if existing:
            return existing
        o = {"id": f"dry-{len(self.sent)}", "symbol": symbol, "qty": str(qty), "side": side,
             "client_order_id": client_id, "status": "accepted", "legs": [], **kw}
        self.sent.append(o)
        log.info("DRY RUN order: %s", o)
        return o

    def replace_stop(self, order_id, stop_price):
        self.sent.append({"replace": order_id, "stop_price": stop_price, "client_order_id": f"rp-{order_id}"})
        return {}

    def cancel(self, order_id):
        self.sent.append({"cancel": order_id, "client_order_id": f"cx-{order_id}"})

    def close_position(self, symbol):
        self.sent.append({"close": symbol, "client_order_id": f"cl-{symbol}"})
