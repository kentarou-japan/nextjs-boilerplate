"""Correlation / covariance estimation.

Markets close at different times (Tokyo, Frankfurt/London, Chicago), which biases daily
correlations toward zero. We therefore estimate correlations on non-overlapping sums of
``aggregation`` bars of *volatility-normalised* price changes (dP_t / sigma_{t-1}), using
only blocks completed at or before the decision date.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


class CorrelationModel:
    def __init__(self, norm_returns: pd.DataFrame, aggregation: int, window: int, min_periods: int,
                 update_every: int):
        """norm_returns: master-date x market matrix of dP/sigma_{t-1}; NaN before a market starts,
        0 on dates the market did not trade."""
        self.markets = list(norm_returns.columns)
        self.aggregation = max(1, int(aggregation))
        self.window = int(window)
        self.min_periods = int(min_periods)
        self.update_every = max(1, int(update_every))
        n = len(norm_returns)
        block_id = np.arange(n) // self.aggregation
        grouped = norm_returns.groupby(block_id)
        # A block is valid for a market only if the market had data for the whole block.
        blocks = grouped.sum(min_count=self.aggregation)
        sizes = pd.Series(np.ones(n)).groupby(block_id).sum().values
        end_pos = pd.Series(np.arange(n)).groupby(block_id).max().values
        # Only full-length blocks are ever used; an in-progress trailing block is excluded so that the
        # sample at date t is identical whether or not later data exist.
        full = sizes == self.aggregation
        self.blocks = blocks[full]
        self.block_end_pos = end_pos[full]
        self._cache_pos = -10 ** 9
        self._cache: pd.DataFrame | None = None

    def corr_at(self, pos: int) -> pd.DataFrame:
        """Correlation matrix as of ``pos``.

        Re-estimated on a fixed schedule: the anchor is the latest position <= pos that is a multiple
        of ``update_every``, and only full blocks ending at or before the anchor are used. The result
        therefore depends only on (data up to pos, pos) — not on where a loop started — so backtest and
        paper trading see the same matrix.
        """
        anchor = (pos // self.update_every) * self.update_every
        if self._cache is not None and anchor == self._cache_pos:
            return self._cache
        nb = int(np.searchsorted(self.block_end_pos, anchor, side="right"))
        sample = self.blocks.iloc[max(0, nb - self.window):nb]
        c = sample.corr(min_periods=self.min_periods)
        c = c.reindex(index=self.markets, columns=self.markets)
        self._cache, self._cache_pos = c, anchor
        return c


def fill_correlation(corr: pd.DataFrame, markets: list[str], default_same_class: float = 0.5,
                     classes: dict[str, str] | None = None) -> np.ndarray:
    """Return a PSD correlation matrix for ``markets``; unknown pairs get a conservative default
    (0.5 within the same asset class, 0.0 otherwise)."""
    c = corr.reindex(index=markets, columns=markets).values.astype(float)
    n = len(markets)
    for i in range(n):
        for j in range(n):
            if i == j:
                c[i, j] = 1.0
            elif np.isnan(c[i, j]):
                same = classes is not None and classes.get(markets[i]) == classes.get(markets[j])
                c[i, j] = default_same_class if same else 0.0
    c = (c + c.T) / 2
    # Project to PSD (clip negative eigenvalues) and re-normalise the diagonal.
    w, v = np.linalg.eigh(c)
    if w.min() < 1e-8:
        w = np.clip(w, 1e-8, None)
        c = (v * w) @ v.T
        d = np.sqrt(np.diag(c))
        c = c / np.outer(d, d)
    return c
