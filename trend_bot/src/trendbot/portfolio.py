"""Target position sizing.

    w_i       risk weight: class budget split equally among sectors present (if enabled), then
              equally among markets in the sector; renormalised over markets with a valid signal
    IDM       = min(idm_cap, 1 / sqrt(w' C+ w)),  C+ = correlation with negatives floored at 0
    v_i       = sigma_i * sqrt(bars_per_year) * multiplier_i * FX_i     (JPY annual vol per contract)
    N*_i      = w_i * IDM * target_vol * NAV / v_i                      (position at |forecast| = 1)
    T_i       = forecast_i * N*_i * dd_scale                            (continuous target, pre-caps)
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np


def risk_weights(markets: list[str], asset_class: dict[str, str], sector: dict[str, str],
                 class_budget: dict[str, float], sector_equal: bool) -> dict[str, float]:
    by_class: dict[str, list[str]] = defaultdict(list)
    for m in markets:
        by_class[asset_class[m]].append(m)
    present = {c: class_budget.get(c, 0.0) for c in by_class if class_budget.get(c, 0.0) > 0}
    total = sum(present.values())
    if total <= 0:
        return {}
    w: dict[str, float] = {}
    for c, members in by_class.items():
        if c not in present:
            continue
        cb = present[c] / total
        if sector_equal:
            secs: dict[str, list[str]] = defaultdict(list)
            for m in members:
                secs[sector[m]].append(m)
            for s_members in secs.values():
                for m in s_members:
                    w[m] = cb / len(secs) / len(s_members)
        else:
            for m in members:
                w[m] = cb / len(members)
    return w


def diversification_multiplier(weights: np.ndarray, corr: np.ndarray, cap: float, floor_negative: bool) -> float:
    c = np.clip(corr, 0.0, None) if floor_negative else corr
    denom = float(weights @ c @ weights)
    if denom <= 0:
        return 1.0
    return float(min(cap, 1.0 / np.sqrt(denom)))


def full_positions(weights: np.ndarray, idm: float, target_vol: float, nav: float, vol_per_contract: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        n = weights * idm * target_vol * nav / vol_per_contract
    return np.where(np.isfinite(n), n, 0.0)
