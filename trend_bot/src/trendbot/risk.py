"""Risk limits, integer-safe enforcement and drawdown control.

Limits (all configurable in strategy.yaml ``risk``; values are fractions of NAV):
  market_vol     |N_i| v_i                          <= market_vol_cap * NAV
  market_notional|N_i| notional_i                   <= market_notional_cap * NAV
  position_adv   |N_i|                              <= max_position_adv_frac * ADV_i
  sector_vol     sqrt(N_s' Σ_s N_s)                 <= sector_vol_cap * NAV
  class_vol      sqrt(N_c' Σ_c N_c)                 <= class_vol_cap * NAV
  portfolio_vol  sqrt(N' Σ N)                       <= target_vol * portfolio_vol_cap_mult * NAV
  gross_notional Σ |N_i| notional_i                 <= gross_notional_cap * NAV
  margin         Σ |N_i| IM_i                       <= margin_usage_cap * NAV
  liquidity      NAV - Σ|N_i| IM_i - k σ_daily      >= min_liquidity_frac * NAV

Caps only ever *reduce* exposure. Low margin is never treated as low risk: margin is a
separate constraint on top of volatility-based limits. The target is never scaled *up* to
reach target volatility.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class RiskInputs:
    markets: list[str]
    nav: float
    vol_pc: np.ndarray          # annual JPY vol per contract
    notional_pc: np.ndarray     # JPY notional per contract (|price| * multiplier * fx)
    margin_pc: np.ndarray       # JPY initial margin per contract
    cov: np.ndarray             # annual JPY covariance per contract
    sector: list[str]
    asset_class: list[str]
    adv: np.ndarray             # contracts/day (NaN = unknown)
    target_vol: float
    bars_per_year: int


@dataclass
class Violation:
    limit: str
    group: list[int]
    value: float
    cap: float


def _group_vol(n: np.ndarray, cov: np.ndarray, idx: list[int]) -> float:
    if not idx:
        return 0.0
    x = n[idx]
    return float(np.sqrt(max(x @ cov[np.ix_(idx, idx)] @ x, 0.0)))


def market_caps(ri: RiskInputs, rc: dict, exe: dict) -> np.ndarray:
    """Per-market max |contracts| from the market-level limits."""
    with np.errstate(divide="ignore", invalid="ignore"):
        c1 = rc["market_vol_cap"] * ri.nav / ri.vol_pc
        c2 = rc["market_notional_cap"] * ri.nav / ri.notional_pc
    c3 = np.where(np.isnan(ri.adv), np.inf, exe["max_position_adv_frac"] * ri.adv)
    caps = np.minimum(np.minimum(np.nan_to_num(c1, nan=0.0, posinf=np.inf), np.nan_to_num(c2, nan=0.0, posinf=np.inf)), c3)
    return caps


def portfolio_stats(n: np.ndarray, ri: RiskInputs, rc: dict) -> dict:
    vol = _group_vol(n, ri.cov, list(range(len(n))))
    im = float(np.abs(n) @ ri.margin_pc)
    gross = float(np.abs(n) @ ri.notional_pc)
    daily = vol / np.sqrt(ri.bars_per_year)
    return {
        "exante_vol": vol / ri.nav,
        "gross_notional": gross / ri.nav,
        "margin_usage": im / ri.nav,
        "liquidity": (ri.nav - im - rc["stress_sigma"] * daily) / ri.nav,
    }


def violations(n: np.ndarray, ri: RiskInputs, rc: dict, exe: dict, tol: float = 1e-9) -> list[Violation]:
    out: list[Violation] = []
    caps = market_caps(ri, rc, exe)
    for i in range(len(n)):
        if abs(n[i]) > caps[i] + tol:
            out.append(Violation("market", [i], abs(n[i]), caps[i]))
    for key, labels, cap in (("sector_vol", ri.sector, rc["sector_vol_cap"]),
                             ("class_vol", ri.asset_class, rc["class_vol_cap"])):
        for g in sorted(set(labels)):
            idx = [i for i, l in enumerate(labels) if l == g]
            v = _group_vol(n, ri.cov, idx)
            if v > cap * ri.nav * (1 + tol):
                out.append(Violation(key, idx, v / ri.nav, cap))
    st = portfolio_stats(n, ri, rc)
    all_idx = list(range(len(n)))
    if st["exante_vol"] > ri.target_vol * rc["portfolio_vol_cap_mult"] * (1 + tol):
        out.append(Violation("portfolio_vol", all_idx, st["exante_vol"], ri.target_vol * rc["portfolio_vol_cap_mult"]))
    if st["gross_notional"] > rc["gross_notional_cap"] * (1 + tol):
        out.append(Violation("gross_notional", all_idx, st["gross_notional"], rc["gross_notional_cap"]))
    if st["margin_usage"] > rc["margin_usage_cap"] * (1 + tol):
        out.append(Violation("margin", all_idx, st["margin_usage"], rc["margin_usage_cap"]))
    if st["liquidity"] < rc["min_liquidity_frac"] - tol:
        out.append(Violation("liquidity", all_idx, st["liquidity"], rc["min_liquidity_frac"]))
    return out


def cap_continuous(t: np.ndarray, ri: RiskInputs, rc: dict, exe: dict) -> tuple[np.ndarray, dict[str, float]]:
    """Scale a continuous target down until every limit holds. Returns (capped, binding scales)."""
    t = t.copy()
    binding: dict[str, float] = {}
    caps = market_caps(ri, rc, exe)
    clipped = np.clip(t, -caps, caps)
    if np.any(np.abs(clipped) < np.abs(t) - 1e-12):
        binding["market"] = 1.0
    t = clipped
    for key, labels, cap in (("sector_vol", ri.sector, rc["sector_vol_cap"]),
                             ("class_vol", ri.asset_class, rc["class_vol_cap"])):
        for g in sorted(set(labels)):
            idx = [i for i, l in enumerate(labels) if l == g]
            v = _group_vol(t, ri.cov, idx)
            if v > cap * ri.nav:
                s = cap * ri.nav / v
                t[idx] *= s
                binding[f"{key}:{g}"] = s
    st = portfolio_stats(t, ri, rc)
    scale = 1.0
    lim = ri.target_vol * rc["portfolio_vol_cap_mult"]
    if st["exante_vol"] > lim:
        scale = min(scale, lim / st["exante_vol"]); binding["portfolio_vol"] = lim / st["exante_vol"]
    if st["gross_notional"] > rc["gross_notional_cap"]:
        s = rc["gross_notional_cap"] / st["gross_notional"]; scale = min(scale, s); binding["gross_notional"] = s
    if st["margin_usage"] > rc["margin_usage_cap"]:
        s = rc["margin_usage_cap"] / st["margin_usage"]; scale = min(scale, s); binding["margin"] = s
    need = st["margin_usage"] + rc["stress_sigma"] * st["exante_vol"] / np.sqrt(ri.bars_per_year)
    if need > 0 and 1 - need < rc["min_liquidity_frac"]:
        s = (1 - rc["min_liquidity_frac"]) / need; scale = min(scale, s); binding["liquidity"] = s
    return t * scale, binding


def _reduce_one(n: np.ndarray, v: Violation, ri: RiskInputs, fixed: np.ndarray | None = None) -> bool:
    cand = [i for i in v.group if n[i] != 0 and (fixed is None or not fixed[i])]
    if not cand:
        return False
    if v.limit in ("gross_notional",):
        score = {i: abs(n[i]) * ri.notional_pc[i] for i in cand}
    elif v.limit in ("margin",):
        score = {i: abs(n[i]) * ri.margin_pc[i] for i in cand}
    else:
        contrib = n * (ri.cov @ n)
        score = {i: contrib[i] if v.limit != "market" else abs(n[i]) for i in cand}
    i = max(score, key=score.get)
    n[i] -= np.sign(n[i])
    return True


def enforce_integer(n: np.ndarray, ri: RiskInputs, rc: dict, exe: dict, fixed: np.ndarray | None = None,
                    strict: bool = True, max_iter: int = 100000) -> np.ndarray:
    """Reduce integer positions (toward zero, one contract at a time) until all limits hold.

    ``fixed`` marks markets that cannot trade now (halted / no price); they are never changed.
    With strict=False an unresolvable breach (only fixed markets left) is returned as-is and must
    be reported by the caller; with strict=True it raises.
    """
    n = n.astype(float).copy()
    caps = np.floor(market_caps(ri, rc, exe) + 1e-9)
    capped = np.sign(n) * np.minimum(np.abs(n), caps)
    if fixed is not None:
        capped = np.where(fixed, n, capped)
    n = capped
    for _ in range(max_iter):
        v = violations(n, ri, rc, exe)
        v = [x for x in v if any(fixed is None or not fixed[i] for i in x.group if n[i] != 0)]
        if not v:
            break
        if not _reduce_one(n, v[0], ri, fixed):
            break
    if strict and violations(n, ri, rc, exe) and fixed is None:
        raise RuntimeError("could not satisfy risk limits by reducing positions")
    return n


@dataclass
class DrawdownController:
    levels: list[tuple[float, float]]
    hysteresis: float
    enabled: bool = True
    peak: float = 0.0
    level: int = 0
    history: list = field(default_factory=list)

    def update(self, nav: float) -> float:
        if not self.enabled:
            return 1.0
        self.peak = max(self.peak, nav)
        dd = 1.0 - nav / self.peak if self.peak > 0 else 0.0
        while self.level < len(self.levels) and dd >= self.levels[self.level][0]:
            self.level += 1
        while self.level > 0 and dd < self.levels[self.level - 1][0] - self.hysteresis:
            self.level -= 1
        return self.scale

    @property
    def scale(self) -> float:
        return 1.0 if self.level == 0 or not self.enabled else float(self.levels[self.level - 1][1])

    def state(self) -> dict:
        return {"peak": self.peak, "level": self.level}

    def load(self, st: dict) -> None:
        self.peak, self.level = float(st["peak"]), int(st["level"])
