"""Data quality checks.

Distinguishes:
- holiday:  exchange calendar closed and no row                    → not an issue
- missing:  calendar open but no row for the market                → WARN (FAIL if at decision date)
- halt/limit: row present with status halt|limit                   → INFO; market not tradable that day
- stale:    identical settlement for n consecutive sessions         → WARN (FAIL at decision date)
- anomaly:  |ΔP| > k × robust σ (trailing, causal)                  → WARN (FAIL at decision date)
- duplicate (date, contract) rows                                   → FAIL
- non-positive price on a market that does not allow it             → FAIL
- data age: latest row older than allowed at decision time          → FAIL

All checks are causal (use only data up to each date) so the same code gates live decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .calendars import get_calendar
from .schema import MarketDataset

SEVERITY_ORDER = {"INFO": 0, "WARN": 1, "FAIL": 2}


@dataclass
class QualityReport:
    issues: pd.DataFrame
    status: dict[str, str]                       # market -> OK | WARN | FAIL
    summary: pd.DataFrame
    as_of: pd.Timestamp | None = None
    notes: list[str] = field(default_factory=list)

    def failed_markets(self) -> list[str]:
        return [m for m, s in self.status.items() if s == "FAIL"]


def _issue(market, date, contract, check, severity, detail):
    return {"market": market, "date": date, "contract": contract, "check": check, "severity": severity, "detail": detail}


def check_market(ds: MarketDataset, market: str, dq: dict, allow_negative: bool,
                 as_of: pd.Timestamp | None = None) -> list[dict]:
    p = ds.market_prices(market)
    if as_of is not None:
        p = p[p["date"] <= as_of]
    issues: list[dict] = []
    if p.empty:
        issues.append(_issue(market, as_of, None, "no_data", "FAIL", "no rows"))
        return issues

    dups = p[p.duplicated(["date", "contract"], keep=False)]
    for (d, c), _ in dups.groupby(["date", "contract"]):
        issues.append(_issue(market, d, c, "duplicate", "FAIL", "duplicate (date, contract) rows"))
    p = p.drop_duplicates(["date", "contract"], keep="first")

    bad = p[p["settle"].isna()]
    for _, r in bad.iterrows():
        issues.append(_issue(market, r["date"], r["contract"], "missing_settle", "WARN", "settle is NaN"))
    nonpos = p[p["settle"] <= 0]
    for _, r in nonpos.iterrows():
        sev = "WARN" if allow_negative else "FAIL"
        issues.append(_issue(market, r["date"], r["contract"], "non_positive_price", sev, f"settle={r['settle']}"))

    ohlc = p.dropna(subset=["high", "low"])
    if len(ohlc):
        incons = ohlc[(ohlc["low"] > ohlc[["open", "settle"]].min(axis=1) + 1e-12) |
                      (ohlc["high"] < ohlc[["open", "settle"]].max(axis=1) - 1e-12)]
        for _, r in incons.head(50).iterrows():
            issues.append(_issue(market, r["date"], r["contract"], "ohlc_inconsistent", "WARN", "low/high do not bracket open/settle"))

    for _, r in p[p["status"].isin(["halt", "limit"])].iterrows():
        issues.append(_issue(market, r["date"], r["contract"], f"status_{r['status']}", "INFO",
                             "trading halted / limit — not tradable at settle, not a holiday"))

    # Calendar completeness.
    cal_name = ds.calendars.get(market)
    dates = pd.DatetimeIndex(sorted(p["date"].unique()))
    if cal_name:
        cal = get_calendar(cal_name)
        expected = cal.sessions_in_range(dates[0], dates[-1])
        expected = expected[[not cal.is_fallback(d) for d in expected]]
        missing = expected.difference(dates)
        for d in missing:
            issues.append(_issue(market, d, None, "missing_session", "WARN", "calendar open, no data"))
        extra = [d for d in dates if not cal.is_session(d) and not cal.is_fallback(d)]
        for d in extra:
            issues.append(_issue(market, d, None, "data_on_holiday", "WARN", "row on a calendar holiday"))
    else:
        gaps = pd.Series(dates[1:] - dates[:-1], index=dates[1:])
        limit = pd.Timedelta(days=10) if ds.bars_per_year >= 200 else pd.Timedelta(days=62)
        for d, g in gaps[gaps > limit].items():
            issues.append(_issue(market, d, None, "gap", "WARN", f"gap of {g.days} days (no calendar available)"))

    # Per-contract anomaly and stale checks (causal trailing windows).
    k = float(dq["anomaly_mad_k"])
    win = int(dq["anomaly_window"])
    stale_n = int(dq["stale_run_days"])
    for contract, g in p.sort_values("date").groupby("contract"):
        s = g.set_index("date")["settle"].astype(float)
        dp = s.diff()
        robust = 1.4826 * dp.abs().rolling(win, min_periods=20).median().shift(1)
        flag = (dp.abs() > k * robust) & robust.gt(0)
        for d in dp[flag].index:
            issues.append(_issue(market, d, contract, "anomaly", "WARN",
                                 f"|dP|={abs(dp[d]):.6g} > {k}×robustσ={robust[d]:.6g}"))
        same = (dp == 0).astype(int)
        run = same.groupby((same == 0).cumsum()).cumsum()
        for d in run[run >= stale_n].index:
            if run[d] == stale_n:  # report once per run
                issues.append(_issue(market, d, contract, "stale", "WARN", f"settle unchanged for {stale_n}+ sessions"))
    return issues


def run_quality(ds: MarketDataset, dq: dict, instruments: dict | None = None,
                as_of: pd.Timestamp | None = None, recent_sessions: int = 3) -> QualityReport:
    """Full quality report. If ``as_of`` is given, issues within the last ``recent_sessions`` of each
    market and data age are escalated to FAIL (they would affect the decision at ``as_of``)."""
    rows: list[dict] = []
    status: dict[str, str] = {}
    for m in ds.markets:
        allow_neg = bool(instruments[m].allow_negative_prices) if instruments and m in instruments else False
        iss = check_market(ds, m, dq, allow_neg, as_of)
        if as_of is not None:
            mp = ds.market_prices(m)
            mp = mp[mp["date"] <= as_of]
            last = mp["date"].max() if len(mp) else pd.NaT
            recent = sorted(mp["date"].unique())[-recent_sessions:] if len(mp) else []
            for it in iss:
                if it["date"] in recent and it["check"] in ("anomaly", "stale", "missing_session", "missing_settle"):
                    it["severity"] = "FAIL"
                    it["detail"] += " (at decision window)"
            cal = get_calendar(ds.calendars.get(m))
            max_age = int(dq["max_data_age_business_days"])
            if pd.isna(last):
                age = 10 ** 6
            elif ds.bars_per_year >= 200:
                age = cal.sessions_between(last, as_of)
            else:
                age = (as_of - last).days
            if age > max_age:
                iss.append(_issue(m, as_of, None, "data_age", "FAIL", f"latest data {last} is {age} units old > {max_age}"))
        rows.extend(iss)
        worst = max((SEVERITY_ORDER[i["severity"]] for i in iss), default=0)
        status[m] = {0: "OK", 1: "WARN", 2: "FAIL"}[worst]
    issues = pd.DataFrame(rows, columns=["market", "date", "contract", "check", "severity", "detail"])
    summary = (issues.groupby(["market", "check", "severity"]).size().rename("count").reset_index()
               if len(issues) else pd.DataFrame(columns=["market", "check", "severity", "count"]))
    return QualityReport(issues, status, summary, as_of)


def fx_quality(fx: pd.DataFrame, dq: dict) -> list[dict]:
    issues = []
    for c in fx.columns:
        if c == "JPY":
            continue
        s = fx[c].dropna()
        if (s <= 0).any():
            issues.append(_issue(f"FX:{c}", s[s <= 0].index[0], None, "non_positive_fx", "FAIL", "fx <= 0"))
        r = np.log(s).diff()
        robust = 1.4826 * r.abs().rolling(int(dq["anomaly_window"]), min_periods=20).median().shift(1)
        for d in r[(r.abs() > dq["anomaly_mad_k"] * robust) & robust.gt(0)].index:
            issues.append(_issue(f"FX:{c}", d, None, "anomaly", "WARN", f"log return {r[d]:.4f}"))
    return issues
