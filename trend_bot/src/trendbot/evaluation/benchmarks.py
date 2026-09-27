"""Comparison benchmarks (all PROXY data).

A  cash / short-term safe asset: excess return 0 by definition. A JPY short-rate series is not
   available, so a JPY cash *level* cannot be shown; USD T-bill (Fama-French RF) is shown to 2018-11.
B  40% US equity / 40% US 10y Treasury / 20% gold, monthly rebalanced (ETF-like, 10 bps per unit
   turnover). Equity total return = Shiller price + dividend/12; bond total return is modelled from
   the 10y yield (carry y/12, modified duration of a 10y par bond, convexity ≈ D²); gold = World
   Bank monthly price. These are monthly *averages* and the bond is a model → PROXY, preliminary.
C  single-horizon trend (12 months only) run through the same engine as D.
D  the multi-horizon, multi-asset strategy.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..data.loaders import load_equity_benchmark_monthly


def par_bond_duration(y: pd.Series, years: int = 10) -> pd.Series:
    y = y.clip(lower=1e-4)
    return (1 - (1 + y / 2) ** (-2 * years)) / y


def monthly_asset_returns() -> pd.DataFrame:
    b = load_equity_benchmark_monthly()
    eq = b["sp_price"].pct_change() + (b["sp_dividend"] / 12) / b["sp_price"].shift(1)
    y = b["ust10y_pct"] / 100
    d = par_bond_duration(y.shift(1))
    dy = y.diff()
    bond = y.shift(1) / 12 - d * dy + 0.5 * d ** 2 * dy ** 2
    gold = b["gold"].pct_change()
    rf = b["usd_rf_pct_month"] / 100
    fx = b["usdjpy"].pct_change()
    return pd.DataFrame({"equity_usd": eq, "bond_usd": bond, "gold_usd": gold, "usd_rf": rf, "usdjpy_ret": fx})


def benchmark_b(weights=(0.4, 0.4, 0.2), cost_bps: float = 10.0) -> pd.DataFrame:
    r = monthly_asset_returns()
    assets = r[["equity_usd", "bond_usd", "gold_usd"]].dropna()
    w0 = np.array(weights)
    out, w = [], w0.copy()
    for d, row in assets.iterrows():
        gross = float(w @ row.values)
        drift = w * (1 + row.values) / (1 + gross)
        turnover = np.abs(w0 - drift).sum()
        out.append((d, gross - turnover * cost_bps / 1e4))
        w = w0.copy()
    s = pd.Series(dict(out)).sort_index()
    df = pd.DataFrame({"B_usd_total": s})
    df["usd_rf"] = r["usd_rf"].reindex(df.index)
    df["B_usd_excess"] = df["B_usd_total"] - df["usd_rf"]
    df["B_jpy_unhedged_total"] = (1 + df["B_usd_total"]) * (1 + r["usdjpy_ret"].reindex(df.index)) - 1
    df["equity_jpy_unhedged_total"] = (1 + r["equity_usd"].reindex(df.index)) * (1 + r["usdjpy_ret"].reindex(df.index)) - 1
    df["equity_usd_excess"] = r["equity_usd"].reindex(df.index) - df["usd_rf"]
    return df


def to_monthly(ret: pd.Series) -> pd.Series:
    m = (1 + ret).groupby(ret.index.to_period("M")).prod() - 1
    m.index = m.index.to_timestamp(how="end").normalize()
    return m


def overlay_analysis(bot_monthly_excess: pd.Series, equity_monthly: pd.Series, weights=(0.0, 0.1, 0.2)) -> pd.DataFrame:
    """Existing equity portfolio with a carve-out w to the BOT. The cash return on the BOT's collateral is
    omitted (JPY short-rate data missing), which understates the mixed portfolio by w × JPY cash rate."""
    from ..metrics import summarize

    df = pd.concat([bot_monthly_excess.rename("bot"), equity_monthly.rename("eq")], axis=1).dropna()
    rows = []
    for w in weights:
        mix = (1 - w) * df["eq"] + w * df["bot"]
        s = summarize(mix, 12)
        rows.append({"bot_weight": w, "cagr": s["cagr"], "ann_vol": s["ann_vol"], "max_drawdown": s["max_drawdown"],
                     "worst_year": s["worst_year"], "es95_month": s["es95_month"],
                     "sharpe_like(no rf)": s["sharpe"], "start": s["start"], "end": s["end"]})
    out = pd.DataFrame(rows)
    out.attrs["corr_bot_equity"] = float(df["bot"].corr(df["eq"]))
    down = df["eq"] < df["eq"].quantile(0.1)
    out.attrs["bot_mean_in_worst_equity_decile"] = float(df.loc[down, "bot"].mean())
    out.attrs["eq_mean_in_worst_equity_decile"] = float(df.loc[down, "eq"].mean())
    return out
