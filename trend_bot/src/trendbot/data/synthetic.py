"""Synthetic contract-level futures data — SYNTHETIC_TEST_ONLY.

Purpose: exercise every futures-specific code path (multiple listed contracts, calendar and
volume rolls, term-structure gaps at rolls, integer contracts, multi-currency accounting,
negative prices, trading halts / limit days, exchange holidays) without real data.

Results computed on this data say NOTHING about the strategy's merit. With
``trend_strength=0`` prices are driftless random walks, which doubles as a look-ahead test:
a causal strategy must not earn a material positive Sharpe on them after costs.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

from ..contracts import Instrument, contract_calendar, load_instruments
from .calendars import get_calendar
from .schema import PRICE_COLUMNS, MarketDataset

# (initial level, annual log-vol, annual carry/basis rate b in F = S*exp(b*tau))
_PARAMS = {
    "ES": (1400.0, 0.18, 0.01), "NK225M": (18000.0, 0.22, 0.00), "FESX": (4000.0, 0.20, -0.01),
    "ZN": (110.0, 0.055, -0.01), "FGBL": (110.0, 0.055, -0.005), "JGB10": (135.0, 0.025, -0.003),
    "GILT": (110.0, 0.065, -0.005),
    "6J": (0.0085, 0.10, 0.02), "6E": (1.00, 0.09, 0.005), "6B": (1.50, 0.09, -0.005),
    "6A": (0.55, 0.11, -0.02), "6C": (0.65, 0.07, -0.005),
    "GC": (280.0, 0.16, 0.02), "HG": (0.85, 0.22, 0.01), "CL": (25.0, 0.35, 0.03),
    "NG": (2.3, 0.50, 0.08), "ZC": (220.0, 0.25, 0.04), "ZS": (500.0, 0.20, 0.02),
}
_ADV = {"ES": 1_000_000, "NK225M": 500_000, "FESX": 500_000, "ZN": 1_000_000, "FGBL": 500_000,
        "JGB10": 20_000, "GILT": 100_000, "6J": 100_000, "6E": 150_000, "6B": 60_000, "6A": 60_000,
        "6C": 50_000, "GC": 150_000, "HG": 60_000, "CL": 250_000, "NG": 150_000, "ZC": 200_000, "ZS": 120_000}


def _subseed(seed: int, name: str) -> int:
    return int(hashlib.sha256(f"{seed}:{name}".encode()).hexdigest()[:8], 16)


def _underlying(rng: np.random.Generator, n: int, s0: float, vol: float, trend_strength: float) -> np.ndarray:
    daily_vol = vol / np.sqrt(252)
    drift = np.zeros(n)
    if trend_strength > 0:
        i = 0
        while i < n:
            dur = int(rng.geometric(1 / 150))
            direction = rng.choice([-1.0, 0.0, 1.0], p=[0.35, 0.3, 0.35])
            drift[i:i + dur] = direction * trend_strength * 0.08 * daily_vol
            i += dur
    shocks = rng.standard_t(df=5, size=n) * np.sqrt(3 / 5) * daily_vol
    logp = np.log(s0) + np.cumsum(drift + shocks - 0.5 * daily_vol ** 2)
    return np.exp(logp)


def _market_frame(inst: Instrument, sessions: pd.DatetimeIndex, rng: np.random.Generator,
                  trend_strength: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    s0, vol, carry = _PARAMS[inst.key]
    n = len(sessions)
    spot = _underlying(rng, n, s0, vol, trend_strength)
    cal = contract_calendar(inst, sessions[0].year, sessions[-1].year + 2)
    ltd = cal["last_trade_date"].values.astype("datetime64[D]")
    ref = cal["roll_reference_date"].values.astype("datetime64[D]")
    days = sessions.values.astype("datetime64[D]")
    rows = []
    adv = _ADV[inst.key]
    for i, d in enumerate(days):
        live = np.where(ltd >= d)[0][:3]  # front three listed contracts still trading
        dtr = np.busday_count(d, ref[live[0]])
        w_front = 1.0 / (1.0 + np.exp(-(dtr - 8) / 2.0))
        weights = [w_front * 0.95, (1 - w_front) * 0.95 + 0.04, 0.01]
        day_vol = adv * float(np.exp(rng.normal(0, 0.3)))
        for j, idx in enumerate(live):
            tau = max((ltd[idx] - d).astype(int), 0) / 365.0
            f = spot[i] * np.exp(carry * tau)
            rows.append((sessions[i], cal.at[idx, "contract"], f, weights[j] * day_vol))
    df = pd.DataFrame(rows, columns=["date", "contract", "settle", "volume"])
    tick = inst.tick_size
    df["settle"] = np.round(df["settle"] / tick) * tick
    df["volume"] = np.floor(df["volume"])
    df["open_interest"] = np.floor(df["volume"] * 3)
    noise = np.abs(rng.normal(0, vol / np.sqrt(252) * 0.5, size=len(df)))
    df["open"] = df["settle"] * (1 + rng.normal(0, vol / np.sqrt(252) * 0.5, size=len(df)))
    df["high"] = np.maximum(df["open"], df["settle"]) * (1 + noise)
    df["low"] = np.minimum(df["open"], df["settle"]) * (1 - noise)
    df["status"] = "ok"
    df["market"] = inst.key
    return df, cal


def _inject_special_events(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # (1) WTI-style negative front-month settlement: the contract closest to expiry on 2020-04-20.
    day = pd.Timestamp("2020-04-20")
    cl = df[(df["market"] == "CL") & (df["date"] == day)]
    if len(cl):
        front = cl.sort_values("volume").index  # front is the one with least remaining volume near expiry
        expiring = cl.loc[cl["contract"].map(lambda c: (int(c[-4:]), "FGHJKMNQUVXZ".index(c[-5]))).idxmin()]
        df.loc[expiring.name, ["settle", "open", "high", "low"]] = [-37.63, 10.0, 12.0, -40.0]
    # (2) Trading halt: NK225M on 2011-03-15 (settle row present but not tradable).
    df.loc[(df["market"] == "NK225M") & (df["date"] == pd.Timestamp("2011-03-15")), "status"] = "halt"
    # (3) Limit days for corn.
    for d in ("2012-07-18", "2012-07-19"):
        df.loc[(df["market"] == "ZC") & (df["date"] == pd.Timestamp(d)), "status"] = "limit"
    return df


def inject_data_errors(df: pd.DataFrame, market: str, rng_seed: int = 7) -> pd.DataFrame:
    """Deliberately corrupt one market (duplicate row, spike, stale run, missing session) for QA tests."""
    rng = np.random.default_rng(rng_seed)
    m = df[df["market"] == market]
    dates = sorted(m["date"].unique())
    k = len(dates) // 2
    front_contract = m[m["date"] == dates[k]].sort_values("volume").iloc[-1]["contract"]
    out = df.copy()
    dup = out[(out["market"] == market) & (out["date"] == dates[k])].iloc[[0]]
    out = pd.concat([out, dup], ignore_index=True)                           # duplicate
    sel = (out["market"] == market) & (out["date"] == dates[k + 10]) & (out["contract"] == front_contract)
    out.loc[sel, "settle"] = out.loc[sel, "settle"] * 1.6                     # spike
    stale_days = dates[k + 30:k + 38]
    for d in stale_days:                                                      # stale run
        sel = (out["market"] == market) & (out["date"] == d) & (out["contract"] == front_contract)
        ref = out.loc[(out["market"] == market) & (out["date"] == stale_days[0]) & (out["contract"] == front_contract), "settle"]
        out.loc[sel, "settle"] = float(ref.iloc[0])
    out = out[~((out["market"] == market) & (out["date"] == dates[k + 50]))]  # missing session
    _ = rng
    return out.reset_index(drop=True)


def generate_synthetic(markets: list[str] | None = None, start: str = "2000-01-03", end: str = "2026-09-25",
                       seed: int = 20260927, trend_strength: float = 0.0,
                       instruments: dict[str, Instrument] | None = None) -> MarketDataset:
    instruments = instruments or load_instruments()
    markets = markets or list(instruments)
    frames, cals = [], []
    for key in markets:
        inst = instruments[key]
        sessions = get_calendar(inst.calendar).sessions_in_range(start, end)
        rng = np.random.default_rng(_subseed(seed, key))
        df, cal = _market_frame(inst, sessions, rng, trend_strength)
        frames.append(df)
        cals.append(cal)
    prices = _inject_special_events(pd.concat(frames, ignore_index=True))[PRICE_COLUMNS]
    contracts = pd.concat(cals, ignore_index=True)
    # Synthetic FX (JPY per unit) and cash rates.
    days = pd.bdate_range(start, end)
    rng = np.random.default_rng(_subseed(seed, "FX"))
    fx = pd.DataFrame(index=days)
    for ccy, s0, vol in (("USD", 110.0, 0.10), ("EUR", 130.0, 0.10), ("GBP", 150.0, 0.11),
                         ("AUD", 80.0, 0.12), ("CAD", 85.0, 0.09)):
        fx[ccy] = s0 * np.exp(np.cumsum(rng.normal(0, vol / np.sqrt(252), len(days))))
    fx["JPY"] = 1.0
    rates = pd.DataFrame({"JPY": 0.001, "USD": 0.02, "EUR": 0.01, "GBP": 0.015}, index=days)
    return MarketDataset(
        "SYNTHETIC_TEST_ONLY", prices, contracts, fx, rates,
        {k: instruments[k].calendar for k in markets}, 252,
        notes={"_dataset": f"合成データ（動作確認専用）。seed={seed}, trend_strength={trend_strength}。成績に意味はない。"},
    )
