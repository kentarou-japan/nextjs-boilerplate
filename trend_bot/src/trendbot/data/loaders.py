"""Loaders for user-supplied real futures CSVs and public proxy datasets."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ..config import CONFIG_DIR, PROJECT_ROOT, load_yaml
from ..contracts import Instrument, load_instruments, parse_contract_id
from .fetch_public import RAW_DIR, verify_manifest
from .schema import CSV_SPECS, PERPETUAL, PRICE_COLUMNS, STATUS_VALUES, MarketDataset

USER_DIR = PROJECT_ROOT / "data" / "user"


# ---------------------------------------------------------------------------
# Real futures data from CSV (the interface a data vendor feed must satisfy)
# ---------------------------------------------------------------------------

def _check_columns(df: pd.DataFrame, name: str) -> None:
    spec = CSV_SPECS[name]
    missing = [c for c in spec["required"] if c not in df.columns]
    if missing:
        raise ValueError(f"{name}: missing required columns {missing}")


def load_real_futures(directory: str | Path = USER_DIR, instruments: dict[str, Instrument] | None = None) -> MarketDataset:
    d = Path(directory)
    instruments = instruments or load_instruments()
    prices = pd.read_csv(d / "futures_prices.csv")
    _check_columns(prices, "futures_prices.csv")
    for col in PRICE_COLUMNS:
        if col not in prices.columns:
            prices[col] = "ok" if col == "status" else np.nan
    prices["status"] = prices["status"].fillna("ok").str.lower()
    bad_status = set(prices["status"]) - set(STATUS_VALUES)
    if bad_status:
        raise ValueError(f"futures_prices.csv: invalid status values {bad_status}")
    unknown = set(prices["market"]) - set(instruments)
    if unknown:
        raise ValueError(f"futures_prices.csv: markets not in instruments.yaml {unknown}")
    contracts = pd.read_csv(d / "contracts.csv", parse_dates=["last_trade_date"])
    _check_columns(contracts, "contracts.csv")
    if "first_notice_date" not in contracts.columns:
        contracts["first_notice_date"] = pd.NaT
    contracts["first_notice_date"] = pd.to_datetime(contracts["first_notice_date"])
    contracts["roll_reference_date"] = contracts[["last_trade_date", "first_notice_date"]].min(axis=1)
    for cid in contracts["contract"]:
        parse_contract_id(cid)  # validates the identifier format
    fx = pd.read_csv(d / "fx.csv", parse_dates=["date"])
    _check_columns(fx, "fx.csv")
    fx_wide = fx.pivot(index="date", columns="ccy", values="jpy_per_unit").sort_index()
    fx_wide["JPY"] = 1.0
    rates = None
    if (d / "rates.csv").exists():
        r = pd.read_csv(d / "rates.csv", parse_dates=["date"])
        _check_columns(r, "rates.csv")
        rates = r.pivot(index="date", columns="ccy", values="rate_annual_pct").sort_index() / 100.0
    cals = {m: instruments[m].calendar for m in prices["market"].unique()}
    return MarketDataset("REAL_FUTURES", prices, contracts, fx_wide, rates, cals, 252,
                         notes={"source": str(d)})


# ---------------------------------------------------------------------------
# Public proxies (PROXY_PRELIMINARY)
# ---------------------------------------------------------------------------

def _read_fx_raw() -> pd.DataFrame:
    fx = pd.read_csv(RAW_DIR / "fx_daily.csv")
    fx["Date"] = pd.to_datetime(fx["Date"])
    wide = fx.pivot_table(index="Date", columns="Country", values="Exchange rate", aggfunc="first").sort_index()
    return wide


# Documented expectation: every series is "currency units per 1 USD". The dataset README
# claims AUD/EUR/GBP are USD per currency; the data contradicts it. We verify with loose
# plausibility anchors so that a future silent convention flip would fail loudly.
_FX_ANCHORS = {  # (date, currency-per-USD plausible range)
    "Japan": ("2020-01-02", 90.0, 130.0),
    "Euro": ("2020-01-02", 0.80, 1.00),
    "United Kingdom": ("2020-01-02", 0.65, 0.90),
    "Australia": ("2020-01-02", 1.20, 1.70),
    "Canada": ("2020-01-02", 1.15, 1.45),
}


def verify_fx_convention(wide: pd.DataFrame) -> None:
    for country, (day, lo, hi) in _FX_ANCHORS.items():
        v = wide.loc[pd.Timestamp(day), country]
        if not lo <= v <= hi:
            raise ValueError(f"FX convention check failed for {country} on {day}: {v} not in [{lo},{hi}] (currency per USD)")


def fx_table_from_raw(wide: pd.DataFrame) -> pd.DataFrame:
    """JPY per unit of each currency. Rows only where USDJPY is observed."""
    usdjpy = wide["Japan"]
    out = pd.DataFrame(index=wide.index)
    out["USD"] = usdjpy
    out["EUR"] = usdjpy / wide["Euro"]
    out["GBP"] = usdjpy / wide["United Kingdom"]
    out["AUD"] = usdjpy / wide["Australia"]
    out["CAD"] = usdjpy / wide["Canada"]
    out["JPY"] = 1.0
    out = out[usdjpy.notna()]
    return out


def _series_from_source(name: str, spec: dict, fx_wide: pd.DataFrame) -> pd.Series:
    if name == "arch_sp500_daily":
        from arch.data import sp500

        s = sp500.load()["Close"].astype(float)
        s.index = pd.to_datetime(s.index).normalize()
        return s
    if name == "fx_daily":
        s = fx_wide[spec["series"]].dropna()
        return s
    path = RAW_DIR / f"{name}.csv"
    df = pd.read_csv(path)
    date_col = df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col].astype(str).str.slice(0, 10), format="mixed")
    col = spec.get("column") or ("Rate" if "Rate" in df.columns else "Price")
    s = df.set_index(date_col)[col].astype(float)
    if name == "sp500_monthly":
        # Trailing rows with SP500 present but other fields 0 are fine; SP500 itself must be > 0.
        s = s[s > 0]
    return s.dropna().sort_index()


def _apply_transform(s: pd.Series, spec: dict) -> pd.Series:
    t = spec.get("transform", "identity")
    if t == "reciprocal":
        s = 1.0 / s
    elif t == "duration_price":
        # Price proxy from yield changes: P_t = P_{t-1} * (1 - D * dy), dy in decimal. Duration-only;
        # carry/coupon, convexity and CTD switches are excluded (documented bias).
        y = s / 100.0
        d = float(spec["modified_duration"])
        ret = -d * y.diff().fillna(0.0)
        s = 100.0 * (1.0 + ret).cumprod()
    if "unit_factor" in spec:
        s = s * float(spec["unit_factor"])
    return s


def _month_end(s: pd.Series) -> pd.Series:
    """Last observation within each calendar month, stamped at month end."""
    s = s.dropna()
    grouped = s.groupby(s.index.to_period("M"))
    out = grouped.last()
    last_obs = pd.Series(s.index, index=s.index).groupby(s.index.to_period("M")).max()
    out.index = out.index.to_timestamp(how="end").normalize()
    # Drop an incomplete trailing month (last observation > 7 days before month end); otherwise a
    # mid-month value would be stamped at a future month-end date.
    if len(out) and (out.index[-1] - last_obs.iloc[-1]).days > 7:
        out = out.iloc[:-1]
    return out


def _monthly_stamp(s: pd.Series) -> pd.Series:
    """Monthly-average series are stamped on the first of the month; move to month end
    because the average is only known once the month is over."""
    idx = s.index.to_period("M").to_timestamp(how="end").normalize()
    return pd.Series(s.values, index=idx)


def load_proxy_daily(markets: list[str] | None = None, verify_hashes: bool = True) -> MarketDataset:
    hashes = verify_manifest() if verify_hashes else {}
    cfg = load_yaml(CONFIG_DIR / "datasets.yaml")
    fx_wide = _read_fx_raw()
    verify_fx_convention(fx_wide)
    rows, notes = [], {}
    for mkt, spec in cfg["proxy_map_daily"].items():
        if markets is not None and mkt not in markets:
            continue
        s = _apply_transform(_series_from_source(spec["source"], spec, fx_wide), spec)
        rows.append(pd.DataFrame({"date": s.index, "market": mkt, "contract": PERPETUAL, "settle": s.values}))
        notes[mkt] = {"source": spec["source"], "note": spec.get("note", "")}
    prices = pd.concat(rows, ignore_index=True)
    for col in ("open", "high", "low", "volume", "open_interest"):
        prices[col] = np.nan
    prices["status"] = "ok"
    contracts = pd.DataFrame({"market": sorted(prices["market"].unique()), "contract": PERPETUAL,
                              "last_trade_date": pd.NaT, "first_notice_date": pd.NaT, "roll_reference_date": pd.NaT})
    fx = fx_table_from_raw(fx_wide)
    cals = {m: None for m in prices["market"].unique()}
    notes["_dataset"] = ("現物・指数・為替スポットによる代替。キャリー、ロールイールド、配当−金利を含まない。"
                         "先物BOTの運用実績ではない。")
    return MarketDataset("PROXY_PRELIMINARY", prices, contracts, fx, None, cals, 252, notes=notes,
                         source_hashes=hashes)


def load_proxy_monthly(markets: list[str] | None = None, verify_hashes: bool = True) -> MarketDataset:
    hashes = verify_manifest() if verify_hashes else {}
    cfg = load_yaml(CONFIG_DIR / "datasets.yaml")
    fx_wide = _read_fx_raw()
    verify_fx_convention(fx_wide)
    rows, notes = [], {}
    for mkt, spec in cfg["proxy_map_monthly"].items():
        if markets is not None and mkt not in markets:
            continue
        s = _series_from_source(spec["source"], spec, fx_wide)
        if spec.get("month_end"):
            s = _month_end(s)
        else:
            s = _monthly_stamp(s)
        if "start" in spec:
            s = s[s.index >= pd.Timestamp(spec["start"])]
        s = _apply_transform(s, spec)
        rows.append(pd.DataFrame({"date": s.index, "market": mkt, "contract": PERPETUAL, "settle": s.values}))
        notes[mkt] = {"source": spec["source"], "averaged": bool(spec.get("averaged")), "note": spec.get("note", "")}
    prices = pd.concat(rows, ignore_index=True)
    for col in ("open", "high", "low", "volume", "open_interest"):
        prices[col] = np.nan
    prices["status"] = "ok"
    contracts = pd.DataFrame({"market": sorted(prices["market"].unique()), "contract": PERPETUAL,
                              "last_trade_date": pd.NaT, "first_notice_date": pd.NaT, "roll_reference_date": pd.NaT})
    fx_full = fx_table_from_raw(fx_wide)
    fx = pd.DataFrame({c: _month_end(fx_full[c].dropna()) for c in fx_full.columns})
    cals = {m: None for m in prices["market"].unique()}
    notes["_dataset"] = ("月次代替データ。月平均系列を含むため1カ月の追加ラグを適用。"
                         "キャリー・ロール・配当−金利を含まない。先物BOTの運用実績ではない。")
    return MarketDataset("PROXY_PRELIMINARY", prices, contracts, fx, None, cals, 12, notes=notes,
                         source_hashes=hashes)


def load_equity_benchmark_monthly() -> pd.DataFrame:
    """Monthly US equity (Shiller, price & dividend), US 10y yield, gold, USD RF (Fama-French, to 2018-11)
    for benchmark B and the equity-overlay analysis. All PROXY."""
    fx_wide = _read_fx_raw()
    sp = pd.read_csv(RAW_DIR / "sp500_monthly.csv", parse_dates=["Date"]).set_index("Date")
    sp.index = sp.index.to_period("M").to_timestamp(how="end").normalize()
    gold = _monthly_stamp(_series_from_source("gold_monthly", {}, fx_wide))
    ust = _monthly_stamp(_series_from_source("ust10y_monthly", {"column": "Rate"}, fx_wide))
    from arch.data import frenchdata

    ff = frenchdata.load()
    # arch stores the YYYYMM code as the integer payload of a datetime index (e.g. 192607 ns).
    yyyymm = ff.index.asi8 if isinstance(ff.index, pd.DatetimeIndex) else ff.index.astype(int)
    ff.index = pd.to_datetime(pd.Series(yyyymm).astype(str), format="%Y%m").dt.to_period("M").dt.to_timestamp(how="end").dt.normalize().values
    usdjpy = _month_end(fx_wide["Japan"].dropna())
    out = pd.DataFrame({
        "sp_price": sp["SP500"].where(sp["SP500"] > 0),
        "sp_dividend": sp["Dividend"].where(sp["Dividend"] > 0),
        "gold": gold,
        "ust10y_pct": ust,
        "usd_rf_pct_month": ff["RF"],
        "ff_mkt_rf_pct": ff["Mkt-RF"],
        "usdjpy": usdjpy,
    }).sort_index()
    return out
