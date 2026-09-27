"""Trend signal.

For market i at close t (bars = the market's own sessions):

    dP_t      = price change of the contract held at t-1 (continuous series difference)
    sigma_t   = sqrt( EWMA_span( dP^2 ) )                       daily price-unit volatility
    floor_t   = vol_floor_ratio * median(sigma over trailing vol_floor_window bars)
    sigma*_t  = max(sigma_t, floor_t)
    z_{L,t}   = (A_t - A_{t-L}) / (sigma*_t * sqrt(L))          A = cumulative dP, L in lookbacks
    f_{L,t}   = sign(z_{L,t})                     (transform=sign)
              = clip(z_{L,t} / z_scale, -1, 1)    (transform=clipped_z)
    forecast_t = mean_L f_{L,t}  in [-1, 1]

Disagreement across horizons shrinks |forecast| (e.g. sign transform gives ±1/3 when 2 of 3
agree). Everything is computed from price *differences* so zero/negative prices are fine.
All operations are causal (rolling/EWMA over past data only); ``extra_lag_bars`` shifts the
forecast further for series whose construction (monthly averages) induces spurious
autocorrelation.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def price_vol(dp: pd.Series, span: int, min_periods: int) -> pd.Series:
    return np.sqrt((dp.fillna(0.0) ** 2).ewm(span=span, min_periods=min_periods, adjust=False).mean()).where(
        dp.notna().cumsum() >= min_periods)


def compute_signal_frame(cont: pd.DataFrame, sig: dict) -> pd.DataFrame:
    dp = cont["dP"]
    adj = cont["adj"]
    sigma_raw = price_vol(dp, int(sig["vol_span_bars"]), int(sig["vol_min_periods"]))
    floor = float(sig["vol_floor_ratio"]) * sigma_raw.rolling(
        int(sig["vol_floor_window_bars"]), min_periods=int(sig["vol_floor_min_periods"])).median()
    sigma = np.maximum(sigma_raw, floor.fillna(0.0)).where(sigma_raw.notna())
    out = pd.DataFrame(index=cont.index)
    out["sigma_raw"] = sigma_raw
    out["sigma_floor"] = floor
    out["sigma"] = sigma
    fs = []
    for L in sig["lookbacks_bars"]:
        L = int(L)
        z = (adj - adj.shift(L)) / (sigma * np.sqrt(L))
        out[f"z_{L}"] = z
        if sig["transform"] == "sign":
            f = np.sign(z)
        else:
            f = (z / float(sig["z_scale"])).clip(-1.0, 1.0)
        fs.append(f)
    fc = pd.concat(fs, axis=1).mean(axis=1, skipna=False)
    hist = dp.notna().cumsum()
    fc = fc.where(hist >= int(sig["min_history_bars"]))
    lag = int(sig.get("extra_lag_bars", 0))
    if lag:
        fc = fc.shift(lag)
    out["forecast"] = fc
    return out
