"""Performance and risk metrics."""
from __future__ import annotations

import numpy as np
import pandas as pd


def drawdown_series(nav: pd.Series) -> pd.Series:
    return nav / nav.cummax() - 1.0


def drawdown_episodes(nav: pd.Series) -> pd.DataFrame:
    """Peak → trough → recovery episodes (recovery NaT if not recovered)."""
    dd = drawdown_series(nav)
    eps, in_dd, peak_date = [], False, nav.index[0]
    trough_date, trough = None, 0.0
    for d, v in dd.items():
        if v < 0 and not in_dd:
            in_dd, trough, trough_date = True, v, d
        elif v < 0 and in_dd:
            if v < trough:
                trough, trough_date = v, d
        elif v >= 0 and in_dd:
            eps.append({"peak": peak_date, "trough": trough_date, "recovery": d, "depth": trough})
            in_dd = False
        if v >= 0:
            peak_date = d
    if in_dd:
        eps.append({"peak": peak_date, "trough": trough_date, "recovery": pd.NaT, "depth": trough})
    df = pd.DataFrame(eps, columns=["peak", "trough", "recovery", "depth"])
    if len(df):
        end = nav.index[-1]
        df["days_peak_to_recovery"] = [((r if pd.notna(r) else end) - p).days for p, r in zip(df["peak"], df["recovery"])]
        df["days_trough_to_recovery"] = [((r - t).days if pd.notna(r) else np.nan) for t, r in zip(df["trough"], df["recovery"])]
        df["recovered"] = df["recovery"].notna()
    return df.sort_values("depth").reset_index(drop=True)


def period_returns(ret: pd.Series, freq: str) -> pd.Series:
    return (1 + ret).groupby(ret.index.to_period(freq)).prod() - 1


def expected_shortfall(ret: pd.Series, q: float = 0.05) -> float:
    r = ret.dropna().sort_values()
    if r.empty:
        return np.nan
    k = max(1, int(np.floor(len(r) * q)))
    return float(r.iloc[:k].mean())


def summarize(ret: pd.Series, bars_per_year: int, rf: pd.Series | None = None) -> dict:
    ret = ret.dropna()
    if len(ret) < 2:
        return {}
    excess = ret - (rf.reindex(ret.index).fillna(0.0) if rf is not None else 0.0)
    nav = (1 + ret).cumprod()
    years = len(ret) / bars_per_year
    cagr = nav.iloc[-1] ** (1 / years) - 1 if nav.iloc[-1] > 0 else -1.0
    vol = ret.std(ddof=1) * np.sqrt(bars_per_year)
    sharpe = excess.mean() / excess.std(ddof=1) * np.sqrt(bars_per_year) if excess.std(ddof=1) > 0 else np.nan
    eps = drawdown_episodes(nav)
    maxdd = float(drawdown_series(nav).min())
    worst = eps.iloc[0] if len(eps) else None
    monthly = period_returns(ret, "M")
    yearly = period_returns(ret, "Y")
    underwater = eps[~eps["recovered"]] if len(eps) else eps
    out = {
        "start": ret.index[0].date().isoformat(), "end": ret.index[-1].date().isoformat(), "years": round(years, 2),
        "cagr": cagr, "ann_mean_excess": excess.mean() * bars_per_year, "ann_vol": vol, "sharpe": sharpe,
        "max_drawdown": maxdd,
        "maxdd_peak": worst["peak"].date().isoformat() if worst is not None else None,
        "maxdd_trough": worst["trough"].date().isoformat() if worst is not None else None,
        "maxdd_recovery": (worst["recovery"].date().isoformat() if worst is not None and pd.notna(worst["recovery"]) else "未回復"),
        "maxdd_days_peak_to_recovery": int(worst["days_peak_to_recovery"]) if worst is not None else 0,
        "longest_dd_days": int(eps["days_peak_to_recovery"].max()) if len(eps) else 0,
        "current_underwater_days": int(underwater["days_peak_to_recovery"].iloc[0]) if len(underwater) else 0,
        "worst_month": float(monthly.min()), "worst_month_date": str(monthly.idxmin()),
        "worst_year": float(yearly.min()), "worst_year_date": str(yearly.idxmin()),
        "es95_bar": expected_shortfall(ret, 0.05), "es95_month": expected_shortfall(monthly, 0.05),
        "skew_month": float(monthly.skew()) if len(monthly) > 2 else np.nan,
        "pct_positive_years": float((yearly > 0).mean()),
    }
    return out


def backtest_summary(res, rf: pd.Series | None = None) -> dict:
    bpy = int(res.config["run"]["bars_per_year"])
    d = res.daily
    s = summarize(d["ret"], bpy, rf)
    avg_nav = d["nav"].mean()
    years = len(d) / bpy
    s.update({
        "turnover_notional_per_year": d["traded_notional"].sum() / avg_nav / years if years else np.nan,
        "costs_pct_nav_per_year": -d["costs"].sum() / avg_nav / years if years else np.nan,
        "avg_contracts_abs": d["contracts_abs"].mean(),
        "avg_positions": d["n_positions"].mean(),
        "margin_usage_mean": d.get("stat_margin_usage", pd.Series(dtype=float)).mean(),
        "margin_usage_max": d.get("stat_margin_usage", pd.Series(dtype=float)).max(),
        "gross_notional_mean": d.get("stat_gross_notional", pd.Series(dtype=float)).mean(),
        "exante_vol_mean": d.get("stat_exante_vol", pd.Series(dtype=float)).mean(),
        "data_label": res.label,
        "cash_rates_missing": res.notes.get("cash_rates_missing"),
    })
    return s


def attribution(res, instruments: dict) -> pd.DataFrame:
    """Contribution to NAV change, as % of initial capital, by market / class / other components."""
    cap = res.notes["capital"]
    pnl = res.market_pnl.sum()
    cost = res.market_cost.sum()
    rows = []
    for m in sorted(set(pnl.index) | set(cost.index)):
        rows.append({"item": m, "type": "market", "asset_class": instruments[m].asset_class,
                     "gross_pnl_pct": pnl.get(m, 0.0) / cap * 100, "cost_pct": cost.get(m, 0.0) / cap * 100})
    df = pd.DataFrame(rows)
    df["net_pct"] = df["gross_pnl_pct"] + df["cost_pct"]
    cls = df.groupby("asset_class")[["gross_pnl_pct", "cost_pct", "net_pct"]].sum().reset_index()
    cls = cls.rename(columns={"asset_class": "item"}).assign(type="asset_class", asset_class=cls["asset_class"])
    d = res.daily
    other = pd.DataFrame([
        {"item": "fx_translation", "type": "other", "net_pct": d["fx_translation"].sum() / cap * 100},
        {"item": "fx_conversion_cost", "type": "other", "net_pct": d["fx_conversion_cost"].sum() / cap * 100},
        {"item": "cash_interest", "type": "other", "net_pct": d["interest"].sum() / cap * 100},
        {"item": "TOTAL_NAV_CHANGE", "type": "total", "net_pct": (d["nav"].iloc[-1] - cap) / cap * 100},
    ])
    return pd.concat([df, cls, other], ignore_index=True)
