"""Probability of Backtest Overfitting via Combinatorially Symmetric Cross-Validation
(Bailey, Borwein, López de Prado, Zhu, "The Probability of Backtest Overfitting").

M: T x N matrix of per-period returns for N configurations. The T rows are split into S
contiguous blocks; for every choice of S/2 blocks as in-sample, the configuration with the
best in-sample Sharpe is located and its out-of-sample relative rank ω is converted to a
logit λ = ln(ω / (1-ω)). PBO = share of splits with λ <= 0 (the IS winner is at or below the
OOS median).
"""
from __future__ import annotations

from itertools import combinations

import numpy as np
import pandas as pd


def _sharpe(x: np.ndarray) -> np.ndarray:
    sd = x.std(axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(sd > 0, x.mean(axis=0) / sd, -np.inf)


def pbo_cscv(M: pd.DataFrame, S: int = 10) -> dict:
    X = M.dropna().values
    T, N = X.shape
    if N < 2 or T < S * 2:
        return {"pbo": np.nan, "n_splits": 0, "note": "insufficient data"}
    blocks = np.array_split(np.arange(T), S)
    logits, degradation = [], []
    for is_blocks in combinations(range(S), S // 2):
        is_idx = np.concatenate([blocks[b] for b in is_blocks])
        oos_idx = np.concatenate([blocks[b] for b in range(S) if b not in is_blocks])
        sr_is = _sharpe(X[is_idx])
        sr_oos = _sharpe(X[oos_idx])
        best = int(np.argmax(sr_is))
        rank = (sr_oos < sr_oos[best]).sum() + 0.5 * ((sr_oos == sr_oos[best]).sum() - 1) + 1
        omega = rank / (N + 1)
        logits.append(np.log(omega / (1 - omega)))
        degradation.append((sr_is[best], sr_oos[best]))
    logits = np.array(logits)
    deg = np.array(degradation)
    slope = np.polyfit(deg[:, 0], deg[:, 1], 1)[0] if len(deg) > 2 else np.nan
    return {"pbo": float((logits <= 0).mean()), "n_splits": len(logits), "S": S, "N": N, "T": T,
            "median_logit": float(np.median(logits)), "is_oos_slope": float(slope),
            "mean_is_sharpe_of_winner": float(deg[:, 0].mean()), "mean_oos_sharpe_of_winner": float(deg[:, 1].mean())}
