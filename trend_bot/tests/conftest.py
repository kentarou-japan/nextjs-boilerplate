import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trendbot.config import load_assumptions, load_strategy_config  # noqa: E402
from trendbot.contracts import load_instruments  # noqa: E402
from trendbot.data.schema import MarketDataset, PRICE_COLUMNS  # noqa: E402
from trendbot.data.synthetic import generate_synthetic  # noqa: E402


@pytest.fixture(scope="session")
def instruments():
    return load_instruments()


@pytest.fixture(scope="session")
def assumptions():
    return load_assumptions()


@pytest.fixture()
def cfg():
    return load_strategy_config()


@pytest.fixture(scope="session")
def synth_small():
    """Four markets, three currencies, ~6 years, with trends. Includes the CL negative day."""
    return generate_synthetic(markets=["ES", "FGBL", "CL", "NK225M"], start="2014-01-02", end="2020-12-31",
                              trend_strength=1.0)


@pytest.fixture(scope="session")
def synth_null():
    """Driftless random walks (no trend) for the look-ahead / no-edge test."""
    return generate_synthetic(markets=["ES", "ZN", "6E", "GC", "CL", "ZC"], start="2000-01-03", end="2020-12-31",
                              trend_strength=0.0, seed=11)


def make_dataset(prices: pd.DataFrame, contracts: pd.DataFrame, fx: pd.DataFrame, rates=None,
                 calendars=None, label="SYNTHETIC_TEST_ONLY") -> MarketDataset:
    p = prices.copy()
    for c in PRICE_COLUMNS:
        if c not in p.columns:
            p[c] = "ok" if c == "status" else np.nan
    cals = calendars or {m: None for m in p["market"].unique()}
    return MarketDataset(label, p, contracts, fx, rates, cals, 252)
