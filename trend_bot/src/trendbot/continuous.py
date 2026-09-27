"""Point-in-time contract selection and continuous (signal-only) series.

Separation of concerns:
- The *continuous series* is used ONLY for signals and volatility. It is the cumulative sum
  of daily price changes of the contract that was active at the previous close, so the
  price gap between contracts at a roll never enters the series (no fictitious P&L) and a
  later back-adjustment cannot change any past difference (no look-ahead).
- P&L is always computed from actual contract settlements in ``accounting``.

Roll decision at date t uses only information available at t's close:
- calendar method: active(t) = earliest listed contract whose roll date (roll_reference_date
  minus N business days) is strictly after t and which has a settlement at t.
- volume method: switch from the current contract to the next when the next contract's
  volume at t-1 exceeds the current one's (one-day lag = conservative about publication
  time), never switching back, and always by the calendar roll date at the latest.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data.calendars import get_calendar
from .data.schema import PERPETUAL


def roll_dates(contracts: pd.DataFrame, calendar_name: str | None, business_days_before: int) -> pd.Series:
    cal = get_calendar(calendar_name)
    out = {}
    for _, r in contracts.iterrows():
        ref = r["roll_reference_date"]
        out[r["contract"]] = pd.NaT if pd.isna(ref) else cal.offset(ref, -business_days_before)
    return pd.Series(out, dtype="datetime64[ns]")


def active_contracts(settle: pd.DataFrame, contracts: pd.DataFrame, calendar_name: str | None,
                     business_days_before: int, method: str = "calendar",
                     volume: pd.DataFrame | None = None) -> pd.Series:
    """Series indexed by the market's dates -> contract to hold after the close of that date."""
    dates = settle.index
    if list(settle.columns) == [PERPETUAL]:
        return pd.Series(PERPETUAL, index=dates)
    c = contracts[contracts["contract"].isin(settle.columns)].sort_values("roll_reference_date")
    rd = roll_dates(c, calendar_name, business_days_before)
    order = list(c["contract"])
    rd_arr = np.array([rd[k] for k in order], dtype="datetime64[ns]")
    has_px = settle[order].notna().values
    date_arr = dates.values
    active = []
    current_idx = 0
    for i, d in enumerate(date_arr):
        # Calendar floor: first contract whose roll date is after d.
        floor_idx = int(np.searchsorted(rd_arr, d, side="right"))
        # searchsorted over sorted roll dates gives the earliest contract with roll_date > d
        idx = max(current_idx, floor_idx)
        if method == "volume" and volume is not None and i > 0:
            vol_prev = volume[order].iloc[i - 1].values
            j = idx
            while j + 1 < len(order) and has_px[i, j + 1] and np.nan_to_num(vol_prev[j + 1]) > np.nan_to_num(vol_prev[j]):
                j += 1
            idx = j
        # Must have a price today; if the scheduled contract has none (data gap), keep the previous.
        if idx < len(order) and not has_px[i, idx] and active:
            idx = current_idx
        if idx >= len(order):
            active.append(None)
            continue
        current_idx = idx
        active.append(order[idx])
    return pd.Series(active, index=dates, dtype=object)


def build_continuous(settle: pd.DataFrame, active: pd.Series, status: pd.DataFrame | None = None) -> pd.DataFrame:
    """Continuous frame for one market (index = market dates).

    Columns: active, price (settle of active contract), dP (change of the contract active at
    t-1, measured t-1 -> t), adj (cumulative dP; level has no meaning, only differences),
    rolled (active changed at t), tradable (status ok and price present for active contract).
    """
    vals = settle.values
    col_idx = {c: j for j, c in enumerate(settle.columns)}
    n = len(settle)
    price = np.full(n, np.nan)
    dp = np.full(n, np.nan)
    tradable = np.zeros(n, dtype=bool)
    st = status.reindex(index=settle.index, columns=settle.columns).values if status is not None else None
    act = active.values
    for i in range(n):
        a = act[i]
        if a is None:
            continue
        j = col_idx[a]
        price[i] = vals[i, j]
        tradable[i] = not np.isnan(vals[i, j]) and (st is None or st[i, j] in (None, "ok") or
                                                    (isinstance(st[i, j], float) and np.isnan(st[i, j])))
        if i > 0 and act[i - 1] is not None:
            jp = col_idx[act[i - 1]]
            dp[i] = vals[i, jp] - vals[i - 1, jp]
    out = pd.DataFrame({"active": act, "price": price, "dP": dp, "tradable": tradable}, index=settle.index)
    out["adj"] = out["dP"].fillna(0.0).cumsum()
    out["rolled"] = out["active"].ne(out["active"].shift(1)) & out["active"].shift(1).notna()
    return out
