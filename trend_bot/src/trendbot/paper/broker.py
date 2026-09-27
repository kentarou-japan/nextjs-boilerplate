"""Broker interface, a persistent simulated broker, and a live stub that is always disabled.

The simulated broker stands in for an external FCM/exchange account: it keeps its own SQLite
file (separate from the bot's state DB) so that restart reconciliation compares two
independently persisted books. It supports fault injection for testing halts.
"""
from __future__ import annotations

import json
import os
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np
import pandas as pd

from ..accounting import Ledger


class BrokerError(Exception):
    pass


class BrokerDisconnected(BrokerError):
    pass


class BrokerTimeout(BrokerError):
    """Request may or may not have reached the broker → order status unknown."""


class LiveTradingDisabled(BrokerError):
    pass


class BrokerInterface(ABC):
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def submit(self, order: dict) -> dict: ...

    @abstractmethod
    def get_order(self, client_order_id: str) -> dict | None: ...

    @abstractmethod
    def open_orders(self) -> list[dict]: ...

    @abstractmethod
    def cancel(self, client_order_id: str) -> dict: ...

    @abstractmethod
    def positions(self) -> dict[tuple[str, str], float]: ...

    @abstractmethod
    def balances(self) -> dict[str, float]: ...

    @abstractmethod
    def fills_since(self, fill_id: int) -> list[dict]: ...


class LiveBrokerStub(BrokerInterface):
    """Placeholder for a real broker API. Always refuses to trade in this release.

    Enabling would require BOTH config ``live_trading.enabled: true`` AND environment variable
    ``TRENDBOT_LIVE_TRADING=I_UNDERSTAND_REAL_MONEY`` — and even then this stub raises, because
    no real broker adapter has been implemented or reviewed. Credentials must come from the
    environment/secret store, never from code or config files.
    """

    def __init__(self, config: dict):
        self.enabled_by_config = bool(config.get("live_trading", {}).get("enabled", False))
        self.enabled_by_env = os.environ.get("TRENDBOT_LIVE_TRADING") == "I_UNDERSTAND_REAL_MONEY"

    def _refuse(self, *_a, **_k):
        raise LiveTradingDisabled(
            "live trading is disabled: no reviewed broker adapter exists "
            f"(config_enabled={self.enabled_by_config}, env_enabled={self.enabled_by_env})")

    connect = submit = get_order = open_orders = cancel = positions = balances = fills_since = _refuse


_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (client_order_id TEXT PRIMARY KEY, body TEXT, status TEXT, broker_order_id TEXT,
                                   submitted_date TEXT, filled_qty REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS fills (fill_id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT, date TEXT,
                                  market TEXT, contract TEXT, qty REAL, price REAL, cost_ccy REAL, ccy TEXT);
CREATE TABLE IF NOT EXISTS positions (market TEXT, contract TEXT, qty REAL, last_price REAL, PRIMARY KEY (market, contract));
CREATE TABLE IF NOT EXISTS balances (ccy TEXT PRIMARY KEY, amount REAL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class SimBroker(BrokerInterface):
    """Simulated account. Fills WORKING orders at the settlement of the next processed session if the
    contract is tradable (status ok, price present). Performs daily variation margin and interest."""

    def __init__(self, path: str | Path, cost_fn=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(_SCHEMA)
        self.db.commit()
        self.cost_fn = cost_fn
        self.faults: set[str] = set()
        self.connected = False

    # --- fault injection ------------------------------------------------------------------
    def inject(self, fault: str) -> None:
        self.faults.add(fault)

    def clear_faults(self) -> None:
        self.faults.clear()

    def _check(self):
        if "disconnected" in self.faults:
            self.connected = False
            raise BrokerDisconnected("simulated API disconnect")
        if not self.connected:
            raise BrokerDisconnected("not connected")

    # --- account setup --------------------------------------------------------------------
    def fund(self, amount: float, ccy: str = "JPY") -> None:
        cur = self.balances_raw().get(ccy, 0.0)
        self.db.execute("INSERT OR REPLACE INTO balances VALUES (?, ?)", (ccy, cur + amount))
        self.db.commit()

    def balances_raw(self) -> dict:
        return {c: a for c, a in self.db.execute("SELECT ccy, amount FROM balances")}

    def _ledger(self) -> Ledger:
        led = Ledger()
        for c, a in self.db.execute("SELECT ccy, amount FROM balances"):
            led.balances[c] = a
        for m, c, q, p in self.db.execute("SELECT market, contract, qty, last_price FROM positions"):
            led.positions[(m, c)] = q
            led.last_price[(m, c)] = p
        return led

    def _save_ledger(self, led: Ledger) -> None:
        self.db.execute("DELETE FROM positions")
        for (m, c), q in led.positions.items():
            self.db.execute("INSERT INTO positions VALUES (?,?,?,?)", (m, c, q, led.last_price[(m, c)]))
        self.db.execute("DELETE FROM balances")
        for c, a in led.balances.items():
            self.db.execute("INSERT INTO balances VALUES (?,?)", (c, a))

    # --- interface ------------------------------------------------------------------------
    def connect(self) -> None:
        if "disconnected" in self.faults:
            raise BrokerDisconnected("simulated API disconnect")
        self.connected = True

    def submit(self, order: dict) -> dict:
        self._check()
        coid = order["client_order_id"]
        existing = self.get_order(coid)
        if existing is not None:
            return {**existing, "status": "DUPLICATE_REJECTED", "original_status": existing["status"]}
        self.db.execute("INSERT INTO orders VALUES (?,?,?,?,?,0)",
                        (coid, json.dumps(order), "WORKING", "SIM-" + coid[-10:], order["date"]))
        self.db.commit()
        if "timeout_on_submit" in self.faults:
            self.faults.discard("timeout_on_submit")
            raise BrokerTimeout("simulated timeout after order reached broker")
        return self.get_order(coid)

    def get_order(self, client_order_id: str) -> dict | None:
        if "query_fails" in self.faults:
            raise BrokerTimeout("simulated order query failure")
        row = self.db.execute("SELECT body, status, broker_order_id, filled_qty FROM orders WHERE client_order_id=?",
                              (client_order_id,)).fetchone()
        if row is None:
            return None
        body = json.loads(row[0])
        return {**body, "status": row[1], "broker_order_id": row[2], "filled_qty": row[3]}

    def open_orders(self) -> list[dict]:
        self._check()
        out = []
        for (coid,) in self.db.execute("SELECT client_order_id FROM orders WHERE status='WORKING'"):
            out.append(self.get_order(coid))
        return out

    def cancel(self, client_order_id: str) -> dict:
        self._check()
        o = self.get_order(client_order_id)
        if o is None:
            return {"client_order_id": client_order_id, "status": "UNKNOWN_ORDER"}
        if o["status"] == "WORKING":
            self.db.execute("UPDATE orders SET status='CANCELLED' WHERE client_order_id=?", (client_order_id,))
            self.db.commit()
        return self.get_order(client_order_id)

    def positions(self) -> dict:
        self._check()
        return {(m, c): q for m, c, q in self.db.execute("SELECT market, contract, qty FROM positions") if q != 0}

    def balances(self) -> dict:
        self._check()
        return self.balances_raw()

    def fills_since(self, fill_id: int) -> list[dict]:
        self._check()
        cols = ["fill_id", "client_order_id", "date", "market", "contract", "qty", "price", "cost_ccy", "ccy"]
        return [dict(zip(cols, r)) for r in self.db.execute(
            "SELECT fill_id, client_order_id, date, market, contract, qty, price, cost_ccy, ccy FROM fills WHERE fill_id > ? ORDER BY fill_id",
            (fill_id,))]

    def convert_to_base(self, fx: dict, cost_bps: float) -> float:
        self._check()
        led = self._ledger()
        cost = led.sweep_to_base(fx, cost_bps)
        self._save_ledger(led)
        self.db.commit()
        return cost

    # --- simulation of an exchange session --------------------------------------------------
    def process_session(self, date: pd.Timestamp, settle_lookup, tradable_lookup, specs: dict, fx: dict,
                        rates: dict, neg_spread: float) -> None:
        """Run the exchange day ``date``: interest, daily MTM at settlement, then fills."""
        last = self.db.execute("SELECT value FROM meta WHERE key='last_session'").fetchone()
        if last is not None and pd.Timestamp(last[0]) >= date:
            return  # idempotent
        days = (date - pd.Timestamp(last[0])).days if last is not None else 0
        led = self._ledger()
        led.accrue_interest(days, rates, neg_spread, fx)
        prices = {k: settle_lookup(k[0], k[1], date) for k in led.positions}
        prices = {k: v for k, v in prices.items() if v is not None}
        led.mark_to_market(prices, specs, fx)
        for (coid,) in list(self.db.execute("SELECT client_order_id FROM orders WHERE status='WORKING'")):
            o = self.get_order(coid)
            if pd.Timestamp(o["date"]) >= date or not tradable_lookup(o["market"], o["contract"], date):
                continue
            px = settle_lookup(o["market"], o["contract"], date)
            ccy, mult = specs[o["market"]]
            cost = self.cost_fn(o, date) if self.cost_fn else 0.0
            led.fill(o["market"], o["contract"], o["qty"], px, px, ccy, mult, cost, fx)
            self.db.execute("UPDATE orders SET status='FILLED', filled_qty=? WHERE client_order_id=?", (o["qty"], coid))
            self.db.execute("INSERT INTO fills (client_order_id, date, market, contract, qty, price, cost_ccy, ccy) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (coid, date.strftime("%Y-%m-%d"), o["market"], o["contract"], o["qty"], px, cost, ccy))
        self._save_ledger(led)
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('last_session', ?)", (date.strftime("%Y-%m-%d"),))
        self.db.commit()
        if "position_drift" in self.faults:
            self.faults.discard("position_drift")
            row = self.db.execute("SELECT market, contract, qty FROM positions LIMIT 1").fetchone()
            if row:
                self.db.execute("UPDATE positions SET qty=? WHERE market=? AND contract=?", (row[2] + 1, row[0], row[1]))
                self.db.commit()

    def close(self) -> None:
        self.db.close()


def _finite(x) -> bool:
    return x is not None and np.isfinite(x)
