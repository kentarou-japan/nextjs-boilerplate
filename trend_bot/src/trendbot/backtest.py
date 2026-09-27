"""Daily event-loop futures backtest.

Order of events on each master date t:
  1. FX translation of yesterday's balances at today's FX
  2. Cash interest on balances for the calendar days since the previous date
  3. Mark-to-market of held contracts at today's settlement (markets with a session today)
  4. Fills of pending orders whose lag has elapsed, at today's settlement, if the contract is
     tradable today (status ok, price present); otherwise they stay pending
  5. Data-end exit: a market whose data ends today is closed at today's settlement
  6. Month-end sweep of foreign balances into JPY (configurable)
  7. Record NAV and attribution
  8. Decision using data up to today's close → new pending orders filled at t+lag sessions

No order is ever filled at the close used to decide it (execution.lag_bars >= 1).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .accounting import Ledger
from .contracts import Instrument
from .data.schema import MarketDataset
from .execution import Order, cost_per_contract
from .risk import DrawdownController
from .strategy import PreparedData, StrategyCore, orders_from_positions


@dataclass
class BacktestResult:
    label: str
    daily: pd.DataFrame
    positions: pd.DataFrame
    trades: pd.DataFrame
    market_pnl: pd.DataFrame
    market_cost: pd.DataFrame
    decisions: pd.DataFrame
    config: dict
    expiry_violations: list = field(default_factory=list)
    notes: dict = field(default_factory=dict)

    @property
    def returns(self) -> pd.Series:
        return self.daily["ret"]


def run_backtest(ds: MarketDataset, cfg: dict, instruments: dict[str, Instrument], assumptions: dict,
                 start=None, end=None, capital: float | None = None, prepared: PreparedData | None = None,
                 cost_multiplier: float = 1.0, adv_multiplier: float = 1.0, margin_multiplier: float = 1.0,
                 integer: bool | None = None, strict_expiry: bool = True) -> BacktestResult:
    if end is not None:
        ds = ds.restrict(end=end)
    pdata = prepared or PreparedData(ds, cfg, instruments, assumptions, adv_multiplier, margin_multiplier)
    core = StrategyCore(cfg)
    exe, acc = cfg["execution"], cfg["accounting"]
    lag = int(exe["lag_bars"])
    integer = cfg["run"].get("integer_contracts", True) if integer is None else integer
    capital = float(capital or cfg["run"]["initial_capital"])
    impact_coef = float(assumptions["cost_model"]["impact_coef"])
    fx_cost_bps = float(assumptions["cost_model"]["fx_conversion_cost_bps"])
    neg_spread = float(assumptions["cash_rates"].get("negative_balance_spread_bps", 0)) / 1e4
    dd_cfg = cfg["risk"]["drawdown_control"]
    ddc = DrawdownController([tuple(x) for x in dd_cfg["levels"]], float(dd_cfg["resume_hysteresis"]), bool(dd_cfg["enabled"]))
    specs = {m: (pdata.inst[m].currency, pdata.inst[m].multiplier) for m in pdata.markets}
    ltd = {}
    for _, r in ds.contracts.iterrows():
        ltd[(r["market"], r["contract"])] = r["last_trade_date"]

    dates = pdata.dates
    start_pos = 0 if start is None else int(np.searchsorted(dates.values, np.datetime64(pd.Timestamp(start)), side="left"))
    ledger = Ledger()
    ledger.deposit(capital, "JPY")
    ledger.fx_prev = pdata.fx_at(start_pos)
    pending: list[list] = []   # [Order, remaining_sessions]
    rows, pos_rows, trade_rows, dec_rows = [], [], [], []
    mkt_pnl_rows, mkt_cost_rows = [], []
    expiry_violations = []
    cash_rates_missing = pdata.rates is None
    prev_date = None
    jidx = {m: j for j, m in enumerate(pdata.markets)}
    last_stats = {}

    for pos in range(start_pos, len(dates)):
        date = dates[pos]
        fx = pdata.fx_at(pos)
        if any(not np.isfinite(fx.get(c, np.nan)) for c in {s[0] for s in specs.values()}):
            # FX not yet available (start of history): skip valuation until it is.
            prev_date = date
            ledger.fx_prev = fx
            continue
        nav_prev = ledger.nav(ledger.fx_prev) if ledger.fx_prev else capital
        fx_pnl = ledger.fx_translation(fx)
        days = (date - prev_date).days if prev_date is not None else 0
        rates = {} if cash_rates_missing else {c: float(v) for c, v in pdata.rates.iloc[pos].items() if np.isfinite(v)}
        interest = ledger.accrue_interest(days, rates, neg_spread, fx)
        prices = {}
        for (m, c) in list(ledger.positions):
            if pdata.session[pos, jidx[m]]:
                px = pdata.settle_at(m, c, date)
                if px is not None:
                    prices[(m, c)] = px
        vm_by_mkt = ledger.mark_to_market(prices, specs, fx)
        cost_by_mkt: dict = defaultdict(float)
        # Fills.
        still = []
        traded_notional = 0.0
        for item in pending:
            o, remaining = item
            j = jidx[o.market]
            if pdata.session[pos, j]:
                remaining -= 1
            if remaining > 0 or not pdata.tradable(o.market, o.contract, date):
                still.append([o, max(remaining, 0)])
                continue
            px = pdata.settle_at(o.market, o.contract, date)
            inst = pdata.inst[o.market]
            cp = pdata.cost[o.market]
            unit_cost = cost_per_contract(pdata.sigma[pos, j], o.qty, pdata.adv[pos, j], inst.tick_size,
                                          inst.multiplier, cp["spread_ticks"], cp["commission"], impact_coef)
            cost_ccy = unit_cost * abs(o.qty) * cost_multiplier
            vm_fill, cost_jpy = ledger.fill(o.market, o.contract, o.qty, px, px, inst.currency, inst.multiplier, cost_ccy, fx)
            vm_by_mkt[o.market] = vm_by_mkt.get(o.market, 0.0) + vm_fill
            cost_by_mkt[o.market] += cost_jpy
            notional = abs(o.qty) * abs(px) * inst.multiplier * fx[inst.currency]
            traded_notional += notional
            trade_rows.append({"date": date, "decision_date": o.date, "market": o.market, "contract": o.contract,
                               "qty": o.qty, "price": px, "reason": o.reason, "intent": o.intent,
                               "cost_jpy": -cost_jpy, "notional_jpy": notional, "client_order_id": o.client_order_id})
        pending = still
        # Data-end exit (proxy series that stop, e.g. S&P daily ends 2018-12-31).
        for (m, c), q in list(ledger.positions.items()):
            if date == pdata.last_session[m] and date < dates[-1] and q != 0 and pdata.settle_at(m, c, date) is not None:
                px = pdata.settle_at(m, c, date)
                inst = pdata.inst[m]
                cp = pdata.cost[m]
                unit_cost = cost_per_contract(pdata.sigma[pos, jidx[m]], q, pdata.adv[pos, jidx[m]], inst.tick_size,
                                              inst.multiplier, cp["spread_ticks"], cp["commission"], impact_coef)
                _, cost_jpy = ledger.fill(m, c, -q, px, px, inst.currency, inst.multiplier, unit_cost * abs(q) * cost_multiplier, fx)
                cost_by_mkt[m] += cost_jpy
                trade_rows.append({"date": date, "decision_date": date.strftime("%Y-%m-%d"), "market": m, "contract": c,
                                   "qty": -q, "price": px, "reason": "data_end", "intent": "reduce",
                                   "cost_jpy": -cost_jpy, "notional_jpy": abs(q * px) * inst.multiplier * fx[inst.currency],
                                   "client_order_id": ""})
                pending = [p for p in pending if p[0].market != m]
        # Expiry safety.
        for (m, c), q in ledger.positions.items():
            d = ltd.get((m, c))
            if d is not None and pd.notna(d) and date > d:
                expiry_violations.append((date, m, c, q))
                if strict_expiry:
                    raise RuntimeError(f"held {c} ({q}) past last trade date {d.date()} on {date.date()}")
        fx_conv = 0.0
        is_month_end = pos + 1 >= len(dates) or dates[pos + 1].month != date.month
        if acc["sweep_frequency"] == "daily" or (acc["sweep_frequency"] == "monthly" and is_month_end):
            nav_now = ledger.nav(fx)
            foreign = sum(abs(b * fx[c]) for c, b in ledger.balances.items() if c != "JPY")
            if nav_now > 0 and foreign / nav_now >= float(acc["sweep_threshold_nav_frac"]):
                fx_conv = ledger.sweep_to_base(fx, fx_cost_bps)
        nav = ledger.nav(fx)
        fut_pnl = float(sum(vm_by_mkt.values()))
        costs = float(sum(cost_by_mkt.values()))
        exposure = ledger.market_exposure()
        rows.append({
            "date": date, "nav": nav, "ret": nav / nav_prev - 1 if nav_prev > 0 else 0.0,
            "futures_pnl": fut_pnl, "costs": costs, "interest": interest, "fx_translation": fx_pnl,
            "fx_conversion_cost": fx_conv, "traded_notional": traded_notional,
            "n_positions": sum(1 for q in exposure.values() if q), "contracts_abs": sum(abs(q) for q in exposure.values()),
            "dd_scale": ddc.scale, **{f"stat_{k}": v for k, v in last_stats.items()},
        })
        pos_rows.append({"date": date, **exposure})
        mkt_pnl_rows.append({"date": date, **vm_by_mkt})
        mkt_cost_rows.append({"date": date, **cost_by_mkt})
        # Decision.
        dd_scale = ddc.update(nav)
        expected = dict(ledger.positions)
        for o, _ in pending:
            expected[(o.market, o.contract)] = expected.get((o.market, o.contract), 0) + o.qty
        cur_mkt: dict = defaultdict(int)
        for (m, _), q in expected.items():
            cur_mkt[m] += q
        dec = core.decide(pdata, pos, nav, dict(cur_mkt), dd_scale, integer=integer)
        active = {m: pdata.active[pos, jidx[m]] for m in pdata.markets}
        orders = orders_from_positions(date, dec.desired, expected, active)
        for o in orders:
            pending.append([o, lag])
        last_stats = {k: v for k, v in dec.stats_desired.items()}
        last_stats["idm"] = dec.idm
        last_stats["n_markets"] = len([m for m in dec.markets if dec.weights.get(m, 0) > 0])
        dec_rows.append({"date": date, "idm": dec.idm, "dd_scale": dd_scale, "n_orders": len(orders),
                         "binding": ";".join(sorted(dec.binding)), "unresolved": ";".join(dec.unresolved),
                         **{f"fc_{m}": v for m, v in dec.forecast.items()},
                         **{f"tgt_{m}": v for m, v in dec.target_int.items()}})
        prev_date = date

    daily = pd.DataFrame(rows).set_index("date")
    positions = pd.DataFrame(pos_rows).set_index("date").fillna(0) if pos_rows else pd.DataFrame()
    trades = pd.DataFrame(trade_rows)
    market_pnl = pd.DataFrame(mkt_pnl_rows).set_index("date").fillna(0.0)
    market_cost = pd.DataFrame(mkt_cost_rows).set_index("date").fillna(0.0)
    decisions = pd.DataFrame(dec_rows).set_index("date")
    return BacktestResult(pdata.label, daily, positions, trades, market_pnl, market_cost, decisions, cfg,
                          expiry_violations, notes={"cash_rates_missing": cash_rates_missing, "capital": capital,
                                                    "cost_multiplier": cost_multiplier, "adv_multiplier": adv_multiplier,
                                                    "margin_multiplier": margin_multiplier, "lag_bars": lag})
