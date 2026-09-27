"""Strategy core shared by the backtest and the paper-trading runner.

``PreparedData`` turns a MarketDataset into point-in-time arrays on a master timeline.
``StrategyCore.decide`` turns those arrays plus the current book into market-level desired
positions, and ``orders_from_positions`` converts them into contract-level orders (including
roll legs). The paper runner rebuilds ``PreparedData`` from data truncated at the decision
date, so any look-ahead in the vectorised precomputation would show up as a backtest/paper
mismatch (tested).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .contracts import Instrument
from .continuous import active_contracts, build_continuous
from .data.schema import MarketDataset
from .execution import Order, buffered, limit_changes
from .portfolio import diversification_multiplier, full_positions, risk_weights
from .risk import RiskInputs, cap_continuous, enforce_integer, portfolio_stats, violations
from .riskmodel import CorrelationModel, fill_correlation
from .signals import compute_signal_frame


def market_cost_params(key: str, inst: Instrument, assumptions: dict) -> dict:
    cls = assumptions["class_defaults"][inst.asset_class]
    m = assumptions["markets"].get(key, {})
    return {
        "commission": float(m.get("commission", 0.0)),
        "spread_ticks": float(m.get("spread_ticks", cls["spread_ticks"])),
        "adv_assumed": float(m["adv_contracts"]) if m.get("adv_contracts") else np.nan,
        "margin_frac": float(m.get("initial_margin_frac", cls["initial_margin_frac"])),
    }


class PreparedData:
    def __init__(self, ds: MarketDataset, cfg: dict, instruments: dict[str, Instrument], assumptions: dict,
                 adv_multiplier: float = 1.0, margin_multiplier: float = 1.0):
        self.ds = ds
        self.cfg = cfg
        self.label = ds.label
        self.bpy = int(cfg["run"]["bars_per_year"])
        universe = [m for m in cfg["universe"] if m in ds.markets]
        self.markets = universe
        self.inst = {m: instruments[m] for m in universe}
        self.cost = {m: market_cost_params(m, instruments[m], assumptions) for m in universe}
        for m in universe:
            self.cost[m]["margin_frac"] *= margin_multiplier
        self.dates = pd.DatetimeIndex(sorted(ds.prices.loc[ds.prices["market"].isin(universe), "date"].unique()))
        n, k = len(self.dates), len(universe)
        self.forecast = np.full((n, k), np.nan)
        self.sigma = np.full((n, k), np.nan)
        self.price = np.full((n, k), np.nan)
        self.session = np.zeros((n, k), dtype=bool)     # market had a row that day
        self.active = np.empty((n, k), dtype=object)
        self.adv = np.full((n, k), np.nan)
        self.last_session = {}
        self.settle: dict[str, pd.DataFrame] = {}
        self.status: dict[str, pd.DataFrame] = {}
        self.cont: dict[str, pd.DataFrame] = {}
        self.signal_frames: dict[str, pd.DataFrame] = {}
        norm = pd.DataFrame(np.nan, index=self.dates, columns=universe)
        for j, m in enumerate(universe):
            p = ds.market_prices(m)
            settle = p.pivot(index="date", columns="contract", values="settle").sort_index()
            status = p.pivot(index="date", columns="contract", values="status").sort_index()
            volume = p.pivot(index="date", columns="contract", values="volume").sort_index()
            inst = self.inst[m]
            act = active_contracts(settle, ds.contracts[ds.contracts["market"] == m], inst.calendar,
                                   inst.roll_business_days_before, inst.roll_method, volume)
            cont = build_continuous(settle, act, status)
            sig = compute_signal_frame(cont, cfg["signal"])
            self.settle[m], self.status[m], self.cont[m], self.signal_frames[m] = settle, status, cont, sig
            pos = self.dates.get_indexer(cont.index)
            self.session[pos, j] = True
            self.last_session[m] = cont.index[-1]
            # As-of alignment onto the master timeline (forward fill from the market's last session).
            aligned = pd.DataFrame({"forecast": sig["forecast"], "sigma": sig["sigma"], "price": cont["price"],
                                    "active": cont["active"]}).reindex(self.dates).ffill()
            self.forecast[:, j] = aligned["forecast"].values
            self.sigma[:, j] = aligned["sigma"].values
            self.price[:, j] = aligned["price"].values
            self.active[:, j] = aligned["active"].values
            # Liquidity: 20-session mean volume of the active contract, lagged one session; else assumption.
            act_vol = pd.Series([volume.at[d, a] if a is not None and a in volume.columns else np.nan
                                 for d, a in zip(cont.index, cont["active"])], index=cont.index, dtype=float)
            adv_obs = act_vol.rolling(20, min_periods=5).mean().shift(1)
            adv = adv_obs.fillna(self.cost[m]["adv_assumed"]) * adv_multiplier
            self.adv[:, j] = adv.reindex(self.dates).ffill().values
            nr = (cont["dP"] / sig["sigma"].shift(1)).reindex(self.dates)
            first = sig["sigma"].first_valid_index()
            if first is not None:
                # After the first valid sigma, a missing bar is a non-session (holiday/other market's day)
                # and counts as a zero move. We deliberately do not treat "after the last row" differently,
                # because at decision time a holiday and the end of a data series look the same.
                started = self.dates > first
                nr = nr.where(~started, nr.fillna(0.0))
            norm[m] = nr
        self.norm_returns = norm
        pc = cfg["portfolio"]
        self.corr_model = CorrelationModel(norm, pc["corr_aggregation_bars"], pc["corr_window_periods"],
                                           pc["corr_min_periods"], pc["corr_update_every_bars"])
        fx = ds.fx.sort_index()
        self.fx = fx.reindex(fx.index.union(self.dates)).ffill().reindex(self.dates)
        if ds.rates is not None:
            r = ds.rates.sort_index()
            self.rates = r.reindex(r.index.union(self.dates)).ffill().reindex(self.dates)
        else:
            self.rates = None

    def fx_at(self, pos: int) -> dict:
        row = self.fx.iloc[pos]
        return {c: float(v) for c, v in row.items()}

    def settle_at(self, market: str, contract: str, date) -> float | None:
        s = self.settle[market]
        if date in s.index and contract in s.columns:
            v = s.at[date, contract]
            return None if pd.isna(v) else float(v)
        return None

    def tradable(self, market: str, contract: str, date) -> bool:
        if self.settle_at(market, contract, date) is None:
            return False
        st = self.status[market]
        v = st.at[date, contract] if (date in st.index and contract in st.columns) else None
        return v in (None, "ok") or (isinstance(v, float) and np.isnan(v))


@dataclass
class Decision:
    date: pd.Timestamp
    markets: list[str]
    target_int: dict = field(default_factory=dict)     # after caps and integer rounding
    desired: dict = field(default_factory=dict)         # after buffer and trade limits (what we send)
    full: dict = field(default_factory=dict)
    forecast: dict = field(default_factory=dict)
    weights: dict = field(default_factory=dict)
    idm: float = 1.0
    stats_target: dict = field(default_factory=dict)
    stats_desired: dict = field(default_factory=dict)
    binding: dict = field(default_factory=dict)
    unresolved: list = field(default_factory=list)
    inputs: RiskInputs | None = None


class StrategyCore:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def risk_inputs(self, pd_: PreparedData, pos: int, nav: float, markets: list[str]) -> RiskInputs:
        fx = pd_.fx_at(pos)
        j = [pd_.markets.index(m) for m in markets]
        inst = [pd_.inst[m] for m in markets]
        fxv = np.array([fx.get(i.currency, np.nan) for i in inst])
        mult = np.array([i.multiplier for i in inst])
        sigma = pd_.sigma[pos, j]
        price = pd_.price[pos, j]
        vol_pc = sigma * np.sqrt(pd_.bpy) * mult * fxv
        notional = np.abs(price) * mult * fxv
        margin = np.array([pd_.cost[m]["margin_frac"] for m in markets]) * notional
        corr_df = pd_.corr_model.corr_at(pos)
        classes = {m: pd_.inst[m].asset_class for m in markets}
        corr = fill_correlation(corr_df, markets, classes=classes)
        cov = corr * np.outer(vol_pc, vol_pc)
        return RiskInputs(markets, nav, vol_pc, notional, margin, cov,
                          [pd_.inst[m].sector for m in markets], [pd_.inst[m].asset_class for m in markets],
                          pd_.adv[pos, j], float(self.cfg["portfolio"]["target_vol_annual"]), pd_.bpy)

    def decide(self, pd_: PreparedData, pos: int, nav: float, current: dict[str, float], dd_scale: float,
               allow_increase: dict[str, bool] | None = None, frozen: set | None = None,
               integer: bool = True) -> Decision:
        cfg, pc, rc, exe = self.cfg, self.cfg["portfolio"], self.cfg["risk"], self.cfg["execution"]
        frozen = frozen or set()
        allow_increase = allow_increase or {}
        date = pd_.dates[pos]
        fx = pd_.fx_at(pos)
        valid = []
        for j, m in enumerate(pd_.markets):
            ok = (np.isfinite(pd_.forecast[pos, j]) and np.isfinite(pd_.sigma[pos, j]) and pd_.sigma[pos, j] > 0
                  and np.isfinite(pd_.price[pos, j]) and np.isfinite(fx.get(pd_.inst[m].currency, np.nan))
                  and pd_.active[pos, j] is not None and date <= pd_.last_session[m])
            if ok or current.get(m, 0) != 0 and np.isfinite(pd_.sigma[pos, j]) and np.isfinite(pd_.price[pos, j]):
                valid.append(m)
        dec = Decision(date=date, markets=valid)
        if not valid or nav <= 0:
            return dec
        j = [pd_.markets.index(m) for m in valid]
        fc = np.nan_to_num(pd_.forecast[pos, j])
        # Markets whose data ended or which lack a forecast are targeted at zero.
        for k, m in enumerate(valid):
            if not np.isfinite(pd_.forecast[pos, pd_.markets.index(m)]) or date > pd_.last_session[m]:
                fc[k] = 0.0
        signal_markets = [m for k, m in enumerate(valid) if np.isfinite(pd_.forecast[pos, pd_.markets.index(m)])]
        w_map = risk_weights(signal_markets, {m: pd_.inst[m].asset_class for m in valid},
                             {m: pd_.inst[m].sector for m in valid}, pc["class_budget"], pc["sector_equal_within_class"])
        w = np.array([w_map.get(m, 0.0) for m in valid])
        ri = self.risk_inputs(pd_, pos, nav, valid)
        corr = ri.cov / np.outer(ri.vol_pc, ri.vol_pc)
        idm = diversification_multiplier(w, corr, pc["idm_cap"], pc["idm_floor_negative_corr"])
        full = full_positions(w, idm, pc["target_vol_annual"], nav, ri.vol_pc)
        target = fc * full * dd_scale
        capped, binding = cap_continuous(target, ri, rc, exe)
        fixed = np.array([m in frozen for m in valid])
        cur = np.array([float(current.get(m, 0)) for m in valid])
        allow = np.array([allow_increase.get(m, True) and m not in frozen for m in valid])
        if integer:
            tgt_int = enforce_integer(np.round(capped), ri, rc, exe)
            tgt_int = np.where(fixed, cur, tgt_int)
            desired = buffered(tgt_int, cur, full, exe["buffer_frac"], exe["trade_to"])
            final = limit_changes(cur, desired, full, ri.adv, exe, allow)
            final = np.where(fixed, cur, final)
            final = enforce_integer(final, ri, rc, exe, fixed=fixed, strict=False)
            cast = lambda a: a.astype(int).tolist()  # noqa: E731
        else:
            # Fractional ("ideal") book for capacity analysis only: no rounding, same caps and buffer.
            tgt_int = np.where(fixed, cur, capped)
            desired = buffered(tgt_int, cur, full, exe["buffer_frac"], exe["trade_to"], integer=False)
            final = np.where(fixed | (~allow & (np.abs(desired) > np.abs(cur))), cur, desired)
            final, _ = cap_continuous(final, ri, rc, exe)
            cast = lambda a: a.tolist()  # noqa: E731
        dec.unresolved = [v.limit for v in violations(final, ri, rc, exe)]
        dec.target_int = dict(zip(valid, cast(tgt_int)))
        dec.desired = dict(zip(valid, cast(final)))
        dec.full = dict(zip(valid, full.tolist()))
        dec.forecast = dict(zip(valid, fc.tolist()))
        dec.weights = dict(zip(valid, w.tolist()))
        dec.idm = idm
        dec.binding = binding
        dec.stats_target = portfolio_stats(tgt_int, ri, rc)
        dec.stats_desired = portfolio_stats(final, ri, rc)
        dec.inputs = ri
        return dec


def orders_from_positions(date: pd.Timestamp, desired: dict[str, int], holdings: dict[tuple, int],
                          active: dict[str, str | None], allow_increase: dict[str, bool] | None = None,
                          frozen: set | None = None) -> list[Order]:
    """Contract-level orders moving ``holdings`` (expected, incl. pending) to market-level ``desired``.

    Contracts other than the active one are closed (roll_close); the active contract receives
    the remainder (roll_open if a roll leg was generated, else rebalance). When increases are
    not allowed (halt), roll_open is suppressed so that a halt never opens a new contract.
    """
    allow_increase = allow_increase or {}
    frozen = frozen or set()
    ds = date.strftime("%Y-%m-%d")
    orders: list[Order] = []
    by_market: dict[str, dict[str, int]] = {}
    for (m, c), q in holdings.items():
        if q:
            by_market.setdefault(m, {})[c] = q
    for m in sorted(set(desired) | set(by_market)):
        if m in frozen:
            continue
        held = by_market.get(m, {})
        act = active.get(m)
        tgt = desired.get(m, sum(held.values()))
        total = sum(held.values())
        rolled = False
        for c, q in sorted(held.items()):
            if c != act:
                orders.append(Order(ds, m, c, -q, "roll_close", "reduce"))
                rolled = True
        if act is None:
            continue
        in_act = held.get(act, 0)
        qty = tgt - in_act
        if qty == 0:
            continue
        increases = abs(tgt) > abs(total) or (np.sign(tgt) != np.sign(total) and tgt != 0)
        if rolled and not allow_increase.get(m, True):
            # Halt: do not open the new contract; only a reduction of the active leg is allowed.
            if abs(tgt) <= abs(in_act) and np.sign(tgt) == np.sign(in_act):
                orders.append(Order(ds, m, act, qty, "risk_reduce", "reduce"))
            continue
        intent = "roll" if rolled and not increases else ("increase" if increases else "reduce")
        reason = "roll_open" if rolled else "rebalance"
        orders.append(Order(ds, m, act, qty, reason, intent))
    return orders
