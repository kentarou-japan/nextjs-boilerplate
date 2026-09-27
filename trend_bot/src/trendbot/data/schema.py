"""Canonical in-memory data model and CSV specifications.

``MarketDataset`` is the only object the strategy/backtest/paper layers consume, so real,
proxy and synthetic data all go through the same code path. Every dataset carries a
``label`` that is propagated into every result:

- ``REAL_FUTURES``          contract-level futures settlements (none available in this environment)
- ``PROXY_PRELIMINARY``     spot / index / FX-spot substitutes — preliminary research only
- ``SYNTHETIC_TEST_ONLY``   generated data for software verification; results are meaningless
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

LABELS = ("REAL_FUTURES", "PROXY_PRELIMINARY", "SYNTHETIC_TEST_ONLY")
PERPETUAL = "PERP"  # contract id used for proxy series that have no expiry

PRICE_COLUMNS = ["date", "market", "contract", "open", "high", "low", "settle", "volume", "open_interest", "status"]
STATUS_VALUES = ("ok", "halt", "limit")

# CSV specifications for user-supplied real data (data/user/). See docs/02_data_spec.md.
CSV_SPECS = {
    "futures_prices.csv": {
        "required": ["date", "market", "contract", "settle"],
        "optional": ["open", "high", "low", "volume", "open_interest", "status"],
        "notes": "date = 取引所の取引日(YYYY-MM-DD)。settle = 清算値（建値単位）。status: ok|halt|limit。"
                 "1行 = 1市場×1限月×1取引日。休場日は行を作らない。取引停止日は status=halt で行を作る。",
    },
    "contracts.csv": {
        "required": ["market", "contract", "last_trade_date"],
        "optional": ["first_notice_date"],
        "notes": "contract は <market><月コード><西暦4桁>（例 ESZ2025）。日付は取引所公表値。",
    },
    "fx.csv": {
        "required": ["date", "ccy", "jpy_per_unit"],
        "optional": ["fixing"],
        "notes": "1外貨あたりの円。fixing に参照レート（例 WMR 16:00 London）を記録。",
    },
    "rates.csv": {
        "required": ["date", "ccy", "rate_annual_pct"],
        "optional": ["tenor", "source"],
        "notes": "短期安全資産の年率（%）。例: JPY は無担保コール O/N や TONA、国庫短期証券。",
    },
}


@dataclass
class MarketDataset:
    label: str
    prices: pd.DataFrame              # long format, PRICE_COLUMNS
    contracts: pd.DataFrame           # market, contract, last_trade_date, first_notice_date, roll_reference_date
    fx: pd.DataFrame                  # index=date, columns=ccy, values=JPY per unit (JPY column == 1.0)
    rates: pd.DataFrame | None        # index=date, columns=ccy, annual decimal rates; None = missing
    calendars: dict[str, str | None]  # market -> exchange calendar name (None = unknown/proxy)
    bars_per_year: int = 252
    notes: dict = field(default_factory=dict)
    source_hashes: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.label not in LABELS:
            raise ValueError(f"unknown data label {self.label}")
        missing = [c for c in PRICE_COLUMNS if c not in self.prices.columns]
        if missing:
            raise ValueError(f"prices missing columns {missing}")
        self.prices = self.prices[PRICE_COLUMNS].sort_values(["market", "date", "contract"]).reset_index(drop=True)
        self.prices["date"] = pd.to_datetime(self.prices["date"]).dt.normalize()
        if "JPY" not in self.fx.columns:
            raise ValueError("fx table must include JPY column (=1.0)")

    @property
    def markets(self) -> list[str]:
        return sorted(self.prices["market"].unique().tolist())

    def market_prices(self, market: str) -> pd.DataFrame:
        return self.prices[self.prices["market"] == market]

    def settle_matrix(self, market: str) -> pd.DataFrame:
        """date x contract matrix of settlements for one market."""
        p = self.market_prices(market)
        return p.pivot(index="date", columns="contract", values="settle").sort_index()

    def restrict(self, markets: list[str] | None = None, start=None, end=None) -> "MarketDataset":
        p = self.prices
        if markets is not None:
            p = p[p["market"].isin(markets)]
        if start is not None:
            p = p[p["date"] >= pd.Timestamp(start)]
        if end is not None:
            p = p[p["date"] <= pd.Timestamp(end)]
        c = self.contracts if markets is None else self.contracts[self.contracts["market"].isin(markets)]
        return MarketDataset(self.label, p.copy(), c.copy(), self.fx, self.rates,
                             {k: v for k, v in self.calendars.items() if markets is None or k in markets},
                             self.bars_per_year, dict(self.notes), dict(self.source_hashes))


def empty_prices() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in PRICE_COLUMNS})
