"""Order generation (buffering, change limits, liquidity limits) and transaction costs.

Only *risk-increasing* quantity is subject to the no-trade buffer's edge rule, the daily
change limit and the "increase allowed" gate; reducing quantity is always allowed (subject to
the liquidity participation cap) so that limit breaches and halts can be resolved.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

import numpy as np


@dataclass
class Order:
    date: str                  # decision date (YYYY-MM-DD)
    market: str
    contract: str
    qty: float                 # signed; integer contracts except in fractional (ideal) capacity runs
    reason: str                # rebalance | roll_close | roll_open | risk_reduce | expiry_close
    intent: str                # increase | reduce | roll
    seq: int = 0

    @property
    def client_order_id(self) -> str:
        """Deterministic id: the same decision always yields the same id (idempotent resubmission)."""
        raw = f"{self.date}|{self.market}|{self.contract}|{self.qty}|{self.reason}|{self.seq}"
        return "TB-" + hashlib.sha256(raw.encode()).hexdigest()[:20]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["client_order_id"] = self.client_order_id
        return d


def split_increase_reduce(current: float, desired: float) -> tuple[float, float]:
    """Return (reduce_qty, increase_qty) as non-negative magnitudes."""
    if current == 0:
        return 0.0, abs(desired)
    if desired == 0 or np.sign(desired) != np.sign(current):
        return abs(current), abs(desired)
    if abs(desired) <= abs(current):
        return abs(current) - abs(desired), 0.0
    return 0.0, abs(desired) - abs(current)


def buffered(target: np.ndarray, current: np.ndarray, full: np.ndarray, buffer_frac: float, trade_to: str,
             integer: bool = True) -> np.ndarray:
    out = current.astype(float).copy()
    for i in range(len(target)):
        b = buffer_frac * abs(full[i])
        if abs(target[i] - current[i]) <= b:
            continue
        if trade_to == "target" or b == 0:
            out[i] = target[i]
            continue
        edge = target[i] + np.sign(current[i] - target[i]) * b
        d = edge if not integer else (np.floor(edge) if current[i] > target[i] else np.ceil(edge))
        lo, hi = sorted((target[i], current[i]))
        out[i] = float(np.clip(d, lo, hi))
    return out


def limit_changes(current: np.ndarray, desired: np.ndarray, full: np.ndarray, adv: np.ndarray, exe: dict,
                  allow_increase: np.ndarray) -> np.ndarray:
    out = current.astype(float).copy()
    for i in range(len(current)):
        red, inc = split_increase_reduce(current[i], desired[i])
        liq = np.inf if np.isnan(adv[i]) else max(0.0, np.floor(exe["max_adv_frac"] * adv[i]))
        red_lim = max(1.0, liq) if np.isfinite(liq) else np.inf
        inc_lim = 0.0 if not allow_increase[i] else min(max(1.0, np.floor(exe["max_daily_change_frac"] * abs(full[i]))), liq)
        red = min(red, red_lim)
        inc = min(inc, inc_lim)
        c = current[i]
        if c == 0:
            out[i] = np.sign(desired[i]) * inc
        elif desired[i] == 0 or np.sign(desired[i]) != np.sign(c):
            if red < abs(c):
                out[i] = np.sign(c) * (abs(c) - red)       # could not get through zero today
            else:
                out[i] = np.sign(desired[i]) * inc if desired[i] != 0 else 0.0
        elif abs(desired[i]) < abs(c):
            out[i] = np.sign(c) * (abs(c) - red)
        else:
            out[i] = np.sign(c) * (abs(c) + inc)
    return out


def cost_per_contract(price_sigma_daily: float, qty: float, adv: float, tick: float, multiplier: float,
                      spread_ticks: float, commission: float, impact_coef: float) -> float:
    """One-way cost per contract in the contract currency."""
    spread = 0.5 * spread_ticks * tick * multiplier
    impact = 0.0
    if not np.isnan(adv) and adv > 0 and np.isfinite(price_sigma_daily):
        impact = impact_coef * price_sigma_daily * np.sqrt(abs(qty) / adv) * multiplier
    return commission + spread + impact
