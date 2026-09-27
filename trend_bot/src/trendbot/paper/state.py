"""Persistent bot state (SQLite). All multi-row updates are done in a single transaction so a
crash leaves either the old or the new state, never a mix."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..accounting import Ledger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY, decision_date TEXT, market TEXT, contract TEXT, qty REAL,
    reason TEXT, intent TEXT, status TEXT, broker_order_id TEXT, created_utc TEXT, updated_utc TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS fills (
    broker_fill_id INTEGER PRIMARY KEY, client_order_id TEXT, date TEXT, market TEXT, contract TEXT,
    qty REAL, price REAL, cost_ccy REAL, ccy TEXT);
CREATE TABLE IF NOT EXISTS positions (market TEXT, contract TEXT, qty REAL, last_price REAL, PRIMARY KEY (market, contract));
CREATE TABLE IF NOT EXISTS balances (ccy TEXT PRIMARY KEY, amount REAL);
CREATE TABLE IF NOT EXISTS nav (date TEXT PRIMARY KEY, nav REAL, futures_pnl REAL, costs REAL, interest REAL,
    fx_translation REAL, fx_conversion_cost REAL, dd_scale REAL, exante_vol REAL, margin_usage REAL,
    gross_notional REAL, halted INTEGER);
CREATE TABLE IF NOT EXISTS halts (id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, reason TEXT, detail TEXT,
    created_utc TEXT, created_date TEXT, cleared_utc TEXT, cleared_by TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, date TEXT, level TEXT, event TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS decisions (date TEXT PRIMARY KEY, body TEXT);
CREATE TABLE IF NOT EXISTS quality (date TEXT, market TEXT, status TEXT, detail TEXT, PRIMARY KEY (date, market));
"""

ACTIVE_ORDER_STATUSES = ("PENDING_SUBMIT", "SUBMITTED", "WORKING", "UNKNOWN")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)  # explicit transactions
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)

    @contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    # meta ---------------------------------------------------------------------------------
    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value, db=None) -> None:
        (db or self.db).execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value, default=str)))

    # ledger -------------------------------------------------------------------------------
    def load_ledger(self) -> Ledger:
        led = Ledger()
        for c, a in self.db.execute("SELECT ccy, amount FROM balances"):
            led.balances[c] = a
        for m, c, q, p in self.db.execute("SELECT market, contract, qty, last_price FROM positions"):
            led.positions[(m, c)] = q
            led.last_price[(m, c)] = p
        led.fx_prev = self.get("fx_prev", {}) or {}
        return led

    def save_ledger(self, led: Ledger, db) -> None:
        db.execute("DELETE FROM positions")
        for (m, c), q in led.positions.items():
            db.execute("INSERT INTO positions VALUES (?,?,?,?)", (m, c, q, led.last_price[(m, c)]))
        db.execute("DELETE FROM balances")
        for c, a in led.balances.items():
            db.execute("INSERT INTO balances VALUES (?,?)", (c, a))
        self.set("fx_prev", led.fx_prev, db)

    # orders -------------------------------------------------------------------------------
    def insert_order(self, o: dict, status: str, db) -> bool:
        cur = db.execute("INSERT OR IGNORE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (o["client_order_id"], o["date"], o["market"], o["contract"], o["qty"], o["reason"],
                          o["intent"], status, None, now(), now(), o.get("note")))
        return cur.rowcount == 1

    def update_order(self, coid: str, status: str, broker_order_id=None, note=None, db=None) -> None:
        (db or self.db).execute(
            "UPDATE orders SET status=?, broker_order_id=COALESCE(?, broker_order_id), updated_utc=?, "
            "note=COALESCE(?, note) WHERE client_order_id=?", (status, broker_order_id, now(), note, coid))

    def orders(self, statuses=None) -> list[dict]:
        cols = ["client_order_id", "decision_date", "market", "contract", "qty", "reason", "intent", "status",
                "broker_order_id", "created_utc", "updated_utc", "note"]
        q = "SELECT * FROM orders"
        args: tuple = ()
        if statuses:
            q += f" WHERE status IN ({','.join('?' * len(statuses))})"
            args = tuple(statuses)
        return [dict(zip(cols, r)) for r in self.db.execute(q + " ORDER BY created_utc, client_order_id", args)]

    # halts --------------------------------------------------------------------------------
    def add_halt(self, scope: str, reason: str, detail: str, date: str, db=None) -> None:
        d = db or self.db
        exists = d.execute("SELECT 1 FROM halts WHERE scope=? AND reason=? AND cleared_utc IS NULL", (scope, reason)).fetchone()
        if not exists:
            d.execute("INSERT INTO halts (scope, reason, detail, created_utc, created_date) VALUES (?,?,?,?,?)",
                      (scope, reason, detail, now(), date))

    def active_halts(self) -> list[dict]:
        cols = ["id", "scope", "reason", "detail", "created_utc", "created_date"]
        return [dict(zip(cols, r)) for r in self.db.execute(
            "SELECT id, scope, reason, detail, created_utc, created_date FROM halts WHERE cleared_utc IS NULL ORDER BY id")]

    def clear_halt(self, halt_id: int, by: str, db=None) -> None:
        (db or self.db).execute("UPDATE halts SET cleared_utc=?, cleared_by=? WHERE id=?", (now(), by, halt_id))

    def event(self, date: str | None, level: str, event: str, body: dict | None = None, db=None) -> None:
        (db or self.db).execute("INSERT INTO events (ts, date, level, event, body) VALUES (?,?,?,?,?)",
                                (now(), date, level, event, json.dumps(body or {}, default=str, ensure_ascii=False)))

    def close(self) -> None:
        self.db.close()
