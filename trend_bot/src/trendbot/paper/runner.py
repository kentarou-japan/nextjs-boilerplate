"""Paper-trading daily cycle, halts, reconciliation, restart recovery.

Uses exactly the same PreparedData / StrategyCore / orders_from_positions as the backtest,
rebuilt each day from data truncated at the decision date.

Halt handling (see docs/06_operations_runbook.md for the operator procedure):

| trigger                     | scope  | new orders            | unfilled orders           | positions |
|-----------------------------|--------|-----------------------|---------------------------|-----------|
| market data FAIL            | market | none in that market   | cancel that market's      | hold      |
| data FAIL on many markets/FX| global | none (block_all)      | cancel risk-increasing    | hold      |
| duplicate order             | global | none                  | cancel risk-increasing    | hold      |
| unknown order status        | global | none, never resend    | query only                | hold      |
| reconciliation mismatch     | global | none                  | cancel risk-increasing    | hold, manual review |
| API disconnect              | global | none (cannot)         | query after reconnect     | hold      |
| margin shortfall            | global | reduce-only plan, approval required by default | cancel risk-increasing | reduce |

Positions are never flattened automatically: a blanket liquidation during a data or broker
fault can be more dangerous than holding hedged, risk-limited positions.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import PROJECT_ROOT, config_hash, load_assumptions, load_strategy_config, load_yaml
from ..contracts import load_instruments
from ..data.quality import run_quality
from ..data.schema import MarketDataset
from ..execution import cost_per_contract
from ..logutil import get_logger, log_event
from ..risk import DrawdownController, portfolio_stats
from ..strategy import PreparedData, StrategyCore, orders_from_positions
from .broker import BrokerDisconnected, BrokerError, BrokerInterface, BrokerTimeout, SimBroker
from .state import ACTIVE_ORDER_STATUSES, StateStore

GLOBAL_REASONS = ("data_quality_global", "duplicate_order", "unknown_order_status", "reconciliation_mismatch",
                  "api_disconnect", "margin_shortfall")


@dataclass
class CycleReport:
    date: str
    status: str
    nav: float | None = None
    orders_sent: int = 0
    orders_held: int = 0
    halts: list = field(default_factory=list)
    frozen_markets: list = field(default_factory=list)
    notes: list = field(default_factory=list)


class PaperRunner:
    def __init__(self, paper_cfg: dict, dataset: MarketDataset, broker: BrokerInterface, state_dir: str | Path,
                 strategy_cfg: dict | None = None, assumptions: dict | None = None, margin_multiplier: float = 1.0):
        self.pcfg = paper_cfg["paper"]
        self.full_cfg = paper_cfg
        self.cfg = strategy_cfg or load_strategy_config(PROJECT_ROOT / self.pcfg["strategy_config"])
        self.assumptions = assumptions or load_assumptions()
        self.instruments = load_instruments()
        self.dataset = dataset
        self.broker = broker
        self.state_dir = Path(state_dir)
        self.store = StateStore(self.state_dir / "bot.db")
        self.log = get_logger("trendbot.paper", self.state_dir / "paper.log.jsonl")
        self.core = StrategyCore(self.cfg)
        self.margin_multiplier = margin_multiplier
        dd = self.cfg["risk"]["drawdown_control"]
        self.ddc = DrawdownController([tuple(x) for x in dd["levels"]], float(dd["resume_hysteresis"]), bool(dd["enabled"]))
        st = self.store.get("dd_state")
        if st:
            self.ddc.load(st)
        cfg_hash = config_hash(self.cfg)
        prev = self.store.get("config_hash")
        if prev is not None and prev != cfg_hash:
            self.store.event(None, "WARN", "config_changed", {"old": prev, "new": cfg_hash})
        self.store.set("config_hash", cfg_hash)
        self.store.set("data_label", dataset.label)

    # ------------------------------------------------------------------ setup
    def initialize(self, capital: float) -> None:
        if self.store.get("initialized"):
            raise RuntimeError("state already initialized")
        with self.store.tx() as db:
            db.execute("INSERT OR REPLACE INTO balances VALUES ('JPY', ?)", (float(capital),))
            self.store.set("initialized", True, db)
            self.store.set("initial_capital", float(capital), db)
            self.store.set("last_fill_id", 0, db)
            self.store.event(None, "INFO", "initialized", {"capital": capital, "label": self.dataset.label}, db)
        if isinstance(self.broker, SimBroker):
            self.broker.fund(float(capital), "JPY")

    # ------------------------------------------------------------------ helpers
    def _halt(self, scope: str, reason: str, detail: str, date: str, db=None) -> None:
        self.store.add_halt(scope, reason, detail, date, db)
        log_event(self.log, "halt", 30, scope=scope, reason=reason, detail=detail, date=date)

    def _policy(self) -> str:
        """Most restrictive active global policy: none | reduce_only | block_all."""
        pol = "none"
        for h in self.store.active_halts():
            if h["scope"] != "global":
                continue
            p = self.pcfg["halt_policy"].get(h["reason"], "block_all")
            if p == "block_all":
                return "block_all"
            pol = "reduce_only"
        return pol

    def _frozen(self) -> set:
        return {h["scope"].split(":", 1)[1] for h in self.store.active_halts() if h["scope"].startswith("market:")}

    def _specs(self, pdata: PreparedData) -> dict:
        return {m: (pdata.inst[m].currency, pdata.inst[m].multiplier) for m in pdata.markets}

    def _cost_fn(self, pdata: PreparedData, pos: int):
        impact = float(self.assumptions["cost_model"]["impact_coef"])
        jidx = {m: j for j, m in enumerate(pdata.markets)}

        def fn(order: dict, date) -> float:
            m = order["market"]
            j = jidx[m]
            inst, cp = pdata.inst[m], pdata.cost[m]
            return cost_per_contract(pdata.sigma[pos, j], order["qty"], pdata.adv[pos, j], inst.tick_size,
                                     inst.multiplier, cp["spread_ticks"], cp["commission"], impact) * abs(order["qty"])
        return fn

    def simulate_exchange(self, date: pd.Timestamp, pdata: PreparedData, pos: int) -> None:
        """Only for SimBroker: the exchange/broker processes session ``date`` independently of the bot."""
        if not isinstance(self.broker, SimBroker):
            return
        self.broker.cost_fn = self._cost_fn(pdata, pos)
        rates = {} if pdata.rates is None else {c: float(v) for c, v in pdata.rates.iloc[pos].items() if np.isfinite(v)}
        neg = float(self.assumptions["cash_rates"].get("negative_balance_spread_bps", 0)) / 1e4
        self.broker.process_session(date, pdata.settle_at, pdata.tradable, self._specs(pdata), pdata.fx_at(pos), rates, neg)

    # ------------------------------------------------------------------ reconciliation
    def sync_orders(self, date: str) -> list[str]:
        problems = []
        for o in self.store.orders(ACTIVE_ORDER_STATUSES):
            try:
                b = self.broker.get_order(o["client_order_id"])
            except BrokerTimeout as e:
                self.store.update_order(o["client_order_id"], "UNKNOWN", note=str(e))
                problems.append(f"unknown:{o['client_order_id']}")
                continue
            if b is None:
                if o["status"] == "PENDING_SUBMIT":
                    self.store.update_order(o["client_order_id"], "NOT_SENT", note="not found at broker after restart")
                    self.store.event(date, "WARN", "order_not_sent", o)
                elif o["status"] == "UNKNOWN":
                    self.store.update_order(o["client_order_id"], "NOT_SENT", note="unknown resolved: not at broker")
                else:
                    problems.append(f"missing_at_broker:{o['client_order_id']}")
            else:
                status = {"WORKING": "SUBMITTED"}.get(b["status"], b["status"])
                self.store.update_order(o["client_order_id"], status, b.get("broker_order_id"))
        return problems

    def reconcile_positions(self, led, pending_fills: list[dict]) -> list[str]:
        mism = []
        broker_pos = self.broker.positions()
        expect = dict(led.positions)
        for f in pending_fills:
            k = (f["market"], f["contract"])
            expect[k] = expect.get(k, 0) + f["qty"]
        keys = {k for k, v in expect.items() if v} | set(broker_pos)
        for k in sorted(keys):
            if abs(expect.get(k, 0) - broker_pos.get(k, 0)) > 1e-9:
                mism.append(f"{k[0]}/{k[1]}: internal={expect.get(k, 0)} broker={broker_pos.get(k, 0)}")
        return mism

    def reconcile_balances(self, led) -> list[str]:
        tol = float(self.pcfg["reconcile"]["balance_tolerance"])
        b = self.broker.balances()
        out = []
        for c in set(b) | set(led.balances):
            x, y = led.balances.get(c, 0.0), b.get(c, 0.0)
            if abs(x - y) > max(1.0, tol * max(abs(x), abs(y))):
                out.append(f"{c}: internal={x:.2f} broker={y:.2f}")
        return out

    def check_duplicates(self) -> list[str]:
        out = []
        opens = self.broker.open_orders()
        known = {o["client_order_id"] for o in self.store.orders()}
        seen = defaultdict(list)
        for o in opens:
            seen[(o["market"], o["contract"], o["date"])].append(o["client_order_id"])
            if o["client_order_id"] not in known:
                out.append(f"unknown open order at broker {o['client_order_id']} {o['market']} {o['qty']}")
        for k, ids in seen.items():
            if len(ids) > 1:
                out.append(f"multiple open orders for {k}: {ids}")
        return out

    def startup_reconcile(self) -> dict:
        """Run after a restart before any new decision: resolve in-flight orders and compare books."""
        date = self.store.get("last_processed_date")
        try:
            self.broker.connect()
        except BrokerDisconnected as e:
            self._halt("global", "api_disconnect", str(e), date)
            return {"ok": False, "reason": "api_disconnect"}
        problems = self.sync_orders(date)
        led = self.store.load_ledger()
        fills = self.broker.fills_since(int(self.store.get("last_fill_id", 0)))
        mism = self.reconcile_positions(led, fills)
        with self.store.tx() as db:
            for p in problems:
                reason = "unknown_order_status" if p.startswith("unknown") else "reconciliation_mismatch"
                self._halt("global", reason, p, date, db)
            if mism:
                self._halt("global", "reconciliation_mismatch", "; ".join(mism), date, db)
            self.store.event(date, "INFO", "startup_reconcile", {"problems": problems, "mismatch": mism,
                                                                   "unapplied_fills": len(fills)}, db)
        return {"ok": not problems and not mism, "problems": problems, "mismatch": mism, "unapplied_fills": len(fills)}

    # ------------------------------------------------------------------ daily cycle
    def run_day(self, date) -> CycleReport:
        date = pd.Timestamp(date).normalize()
        ds_str = date.strftime("%Y-%m-%d")
        if not self.store.get("initialized"):
            raise RuntimeError("run `paper init` first")
        last = self.store.get("last_processed_date")
        if last is not None and date <= pd.Timestamp(last):
            return CycleReport(ds_str, "skipped_already_processed")
        data = self.dataset.restrict(end=date)                    # nothing after the decision date
        pdata = PreparedData(data, self.cfg, self.instruments, self.assumptions, margin_multiplier=self.margin_multiplier)
        if len(pdata.dates) == 0 or pdata.dates[-1] != date:
            return CycleReport(ds_str, "no_session")
        pos = len(pdata.dates) - 1
        fx = pdata.fx_at(pos)
        self.simulate_exchange(date, pdata, pos)
        rep = CycleReport(ds_str, "ok")
        # 1. broker connectivity
        try:
            self.broker.connect()
            new_fills = self.broker.fills_since(int(self.store.get("last_fill_id", 0)))
        except BrokerDisconnected as e:
            self._halt("global", "api_disconnect", str(e), ds_str)
            rep.status, rep.halts = "halted_api_disconnect", self.store.active_halts()
            # No state change: the next successful cycle values positions over the whole gap and
            # applies any fills that happened meanwhile.
            return rep
        # 2. internal book: FX translation → interest → MTM → broker fills
        led = self.store.load_ledger()
        if not led.fx_prev:
            led.fx_prev = dict(fx)
        fx_pnl = led.fx_translation(fx)
        days = (date - pd.Timestamp(last)).days if last else 0
        rates = {} if pdata.rates is None else {c: float(v) for c, v in pdata.rates.iloc[pos].items() if np.isfinite(v)}
        neg = float(self.assumptions["cash_rates"].get("negative_balance_spread_bps", 0)) / 1e4
        interest = led.accrue_interest(days, rates, neg, fx)
        specs = self._specs(pdata)
        jidx = {m: j for j, m in enumerate(pdata.markets)}
        prices = {}
        for (m, c) in led.positions:
            if m in jidx and pdata.session[pos, jidx[m]]:
                px = pdata.settle_at(m, c, date)
                if px is not None:
                    prices[(m, c)] = px
        vm = led.mark_to_market(prices, specs, fx)
        fut_pnl, costs = float(sum(vm.values())), 0.0
        for f in new_fills:
            settle_now = pdata.settle_at(f["market"], f["contract"], date)
            settle_now = f["price"] if settle_now is None else settle_now
            v, c = led.fill(f["market"], f["contract"], f["qty"], f["price"], settle_now, f["ccy"], specs[f["market"]][1],
                            f["cost_ccy"], fx)
            fut_pnl += v
            costs += c
        # 3. order status sync, reconciliation, duplicates
        problems = self.sync_orders(ds_str)
        mism = self.reconcile_positions(led, [])
        bal = self.reconcile_balances(led)
        dups = self.check_duplicates()
        # 4. data quality at the decision date
        qrep = run_quality(data, self.cfg["data_quality"], self.instruments, as_of=date)
        failed = [m for m in pdata.markets if qrep.status.get(m) == "FAIL"]
        fx_needed = {pdata.inst[m].currency for m in pdata.markets}
        fx_missing = [c for c in fx_needed if not np.isfinite(fx.get(c, np.nan))]
        # 5. margin
        nav = led.nav(fx)
        cur = led.market_exposure()
        ri = self.core.risk_inputs(pdata, pos, nav, [m for m in pdata.markets if np.isfinite(pdata.sigma[pos, jidx[m]])])
        n_cur = np.array([cur.get(m, 0) for m in ri.markets], dtype=float)
        stats_now = portfolio_stats(n_cur, ri, self.cfg["risk"]) if len(ri.markets) else {}
        margin_trigger = self.cfg["risk"]["margin_usage_cap"] * float(self.pcfg["margin_shortfall_trigger_mult"])
        with self.store.tx() as db:
            for p in problems:
                self._halt("global", "unknown_order_status" if p.startswith("unknown") else "reconciliation_mismatch", p, ds_str, db)
            if mism or bal:
                self._halt("global", "reconciliation_mismatch", "; ".join(mism + bal), ds_str, db)
            if dups:
                self._halt("global", "duplicate_order", "; ".join(dups), ds_str, db)
            for m in failed:
                detail = "; ".join(qrep.issues[(qrep.issues["market"] == m) & (qrep.issues["severity"] == "FAIL")]["check"].unique())
                self._halt(f"market:{m}", "data_quality", detail, ds_str, db)
            if self.pcfg.get("auto_clear_market_data_halt", True):
                for h in self.store.active_halts():
                    if h["scope"].startswith("market:") and h["scope"].split(":", 1)[1] not in failed:
                        self.store.clear_halt(h["id"], "auto:data_ok", db)
            if len(failed) >= int(self.cfg["data_quality"]["global_halt_if_failed_markets_ge"]) or fx_missing:
                self._halt("global", "data_quality_global", f"failed={failed} fx_missing={fx_missing}", ds_str, db)
            if stats_now and stats_now["margin_usage"] > margin_trigger:
                self._halt("global", "margin_shortfall",
                           f"margin_usage={stats_now['margin_usage']:.3f} > {margin_trigger:.3f}", ds_str, db)
            for m, st in qrep.status.items():
                db.execute("INSERT OR REPLACE INTO quality VALUES (?,?,?,?)", (ds_str, m, st, ""))
        # 6. decision (drawdown state is updated after the sweep, as in the backtest)
        policy = self._policy()
        frozen = self._frozen()
        rep.frozen_markets = sorted(frozen)
        allow_inc = {m: policy == "none" and m not in frozen for m in pdata.markets}
        # Unfilled orders: cancel before re-deciding (all if normal; risk-increasing ones if halted).
        for o in self.store.orders(("SUBMITTED",)):
            if policy == "none" and self.pcfg.get("cancel_unfilled_each_cycle", True) or o["intent"] == "increase" \
                    or o["market"] in frozen:
                try:
                    r = self.broker.cancel(o["client_order_id"])
                    self.store.update_order(o["client_order_id"], r.get("status", "CANCELLED"))
                except BrokerError as e:
                    self._halt("global", "unknown_order_status", f"cancel failed {o['client_order_id']}: {e}", ds_str)
        policy = self._policy()
        # Sweep foreign balances (month end), mirrored at the broker.
        fx_conv = 0.0
        acc = self.cfg["accounting"]
        next_day = self.dataset.prices["date"][self.dataset.prices["date"] > date].min()
        is_month_end = pd.isna(next_day) or next_day.month != date.month
        if acc["sweep_frequency"] == "daily" or (acc["sweep_frequency"] == "monthly" and is_month_end):
            if policy != "block_all":
                bps = float(self.assumptions["cost_model"]["fx_conversion_cost_bps"])
                fx_conv = led.sweep_to_base(fx, bps)
                if isinstance(self.broker, SimBroker):
                    self.broker.convert_to_base(fx, bps)
        nav = led.nav(fx)
        dd_scale = self.ddc.update(nav)
        orders = []
        if policy != "block_all":
            dec = self.core.decide(pdata, pos, nav, cur, dd_scale, allow_increase=allow_inc, frozen=frozen)
            active = {m: pdata.active[pos, jidx[m]] for m in pdata.markets}
            orders = orders_from_positions(date, dec.desired, dict(led.positions), active, allow_inc, frozen)
            if policy == "reduce_only":
                orders = [o for o in orders if o.intent == "reduce"]
            dec_body = {"forecast": dec.forecast, "target": dec.target_int, "desired": dec.desired, "idm": dec.idm,
                        "binding": dec.binding, "stats": dec.stats_desired, "policy": policy, "frozen": sorted(frozen)}
        else:
            dec_body = {"policy": policy, "note": "block_all: no decision executed"}
            rep.notes.append("block_all halt active: no orders generated")
        hold_for_approval = policy == "reduce_only" and self.pcfg.get("reduce_orders_require_approval", True)
        # 7. persist the day atomically (write-ahead orders as PENDING_SUBMIT / AWAITING_APPROVAL)
        max_fill = max([f["fill_id"] for f in new_fills], default=int(self.store.get("last_fill_id", 0)))
        with self.store.tx() as db:
            for f in new_fills:
                db.execute("INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?)",
                           (f["fill_id"], f["client_order_id"], f["date"], f["market"], f["contract"], f["qty"],
                            f["price"], f["cost_ccy"], f["ccy"]))
            self.store.save_ledger(led, db)
            db.execute("INSERT OR REPLACE INTO nav VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (ds_str, nav, fut_pnl, costs, interest, fx_pnl, fx_conv, dd_scale,
                        stats_now.get("exante_vol"), stats_now.get("margin_usage"), stats_now.get("gross_notional"),
                        int(policy != "none")))
            db.execute("INSERT OR REPLACE INTO decisions VALUES (?, ?)", (ds_str, json.dumps(dec_body, default=float)))
            inserted = []
            for o in orders:
                if self.store.insert_order(o.to_dict(), "AWAITING_APPROVAL" if hold_for_approval else "PENDING_SUBMIT", db):
                    inserted.append(o)
                else:
                    self.store.event(ds_str, "WARN", "duplicate_order_suppressed", o.to_dict(), db)
            self.store.set("last_processed_date", ds_str, db)
            self.store.set("last_fill_id", max_fill, db)
            self.store.set("dd_state", self.ddc.state(), db)
        rep.nav = nav
        if hold_for_approval:
            rep.orders_held = len(inserted)
        else:
            rep.orders_sent = self._submit([o.to_dict() for o in inserted], ds_str)
        rep.halts = self.store.active_halts()
        if rep.halts:
            rep.status = "ok_with_halts"
        log_event(self.log, "cycle", date=ds_str, nav=nav, orders=rep.orders_sent, held=rep.orders_held,
                  policy=policy, frozen=sorted(frozen), halts=[h["reason"] for h in rep.halts])
        return rep

    def _submit(self, orders: list[dict], date: str) -> int:
        sent = 0
        for o in orders:
            try:
                r = self.broker.submit(o)
            except BrokerTimeout as e:
                self.store.update_order(o["client_order_id"], "UNKNOWN", note=str(e))
                self._halt("global", "unknown_order_status", f"{o['client_order_id']}: {e}", date)
                break                      # never keep sending while an order's fate is unknown
            except BrokerDisconnected as e:
                self._halt("global", "api_disconnect", str(e), date)
                break
            if r["status"] == "DUPLICATE_REJECTED":
                self.store.update_order(o["client_order_id"], {"WORKING": "SUBMITTED"}.get(r["original_status"], r["original_status"]),
                                        r.get("broker_order_id"), note="duplicate submission rejected by broker")
                self.store.event(date, "WARN", "duplicate_submission_rejected", o)
                continue
            self.store.update_order(o["client_order_id"], "SUBMITTED", r.get("broker_order_id"))
            sent += 1
        return sent

    # ------------------------------------------------------------------ operator actions
    def approve(self, operator: str) -> int:
        last = self.store.get("last_processed_date")
        waiting = [o for o in self.store.orders(("AWAITING_APPROVAL",)) if o["decision_date"] == last]
        stale = [o for o in self.store.orders(("AWAITING_APPROVAL",)) if o["decision_date"] != last]
        for o in stale:
            self.store.update_order(o["client_order_id"], "EXPIRED", note="approval window passed")
        for o in waiting:
            self.store.update_order(o["client_order_id"], "PENDING_SUBMIT", note=f"approved by {operator}")
        self.store.event(last, "INFO", "orders_approved", {"operator": operator, "n": len(waiting)})
        body = [{"client_order_id": o["client_order_id"], "date": o["decision_date"], "market": o["market"],
                 "contract": o["contract"], "qty": o["qty"], "reason": o["reason"], "intent": o["intent"], "seq": 0}
                for o in waiting]
        return self._submit(body, last)

    def resume(self, operator: str, halt_ids: list[int] | None = None) -> dict:
        """Clear halts after the operator has resolved the cause. Global halts are only cleared if a fresh
        reconciliation is clean and no order is in an unknown state."""
        check = self.startup_reconcile()
        cleared, refused = [], []
        for h in self.store.active_halts():
            if halt_ids is not None and h["id"] not in halt_ids:
                continue
            if h["scope"] == "global" and h["reason"] in ("reconciliation_mismatch", "unknown_order_status",
                                                          "duplicate_order", "api_disconnect") and not check["ok"]:
                refused.append(h)
                continue
            if h["created_utc"] and h in self.store.active_halts():
                self.store.clear_halt(h["id"], operator)
                cleared.append(h)
        # startup_reconcile may have re-raised halts; report them.
        self.store.event(self.store.get("last_processed_date"), "INFO", "resume",
                         {"operator": operator, "cleared": cleared, "refused": refused})
        return {"cleared": cleared, "refused": refused, "reconcile": check, "active": self.store.active_halts()}


def load_paper_config(path: str | Path | None = None) -> dict:
    return load_yaml(path or PROJECT_ROOT / "config" / "paper.yaml")
