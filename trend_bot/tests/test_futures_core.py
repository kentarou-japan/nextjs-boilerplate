"""Tests for the parts where a bug would cause losses or mis-evaluation."""
import numpy as np
import pandas as pd
import pytest

from conftest import make_dataset
from trendbot.accounting import Ledger
from trendbot.backtest import run_backtest
from trendbot.config import load_strategy_config, set_dotted
from trendbot.continuous import active_contracts, build_continuous
from trendbot.metrics import backtest_summary
from trendbot.signals import compute_signal_frame
from trendbot.strategy import PreparedData


# ----------------------------------------------------------------------------- look-ahead
def test_prepared_arrays_are_point_in_time(synth_small, cfg, instruments, assumptions):
    """Forecast/sigma/active computed on full history must equal those computed on data truncated at t."""
    full = PreparedData(synth_small, cfg, instruments, assumptions)
    rng = np.random.default_rng(0)
    cut_positions = sorted(rng.choice(np.arange(400, len(full.dates) - 5), size=4, replace=False))
    for pos in cut_positions:
        t = full.dates[pos]
        trunc = PreparedData(synth_small.restrict(end=t), cfg, instruments, assumptions)
        assert trunc.dates[-1] == t
        np.testing.assert_allclose(trunc.forecast[-1], full.forecast[pos], equal_nan=True)
        np.testing.assert_allclose(trunc.sigma[-1], full.sigma[pos], rtol=1e-12, equal_nan=True)
        assert list(trunc.active[-1]) == list(full.active[pos])
        c_full = full.corr_model.corr_at(pos)
        c_trunc = trunc.corr_model.corr_at(len(trunc.dates) - 1)
        # Correlation uses only completed blocks, so both see the same sample.
        np.testing.assert_allclose(c_trunc.values, c_full.values, equal_nan=True, atol=1e-12)


def test_no_edge_on_driftless_random_walk(synth_null, instruments, assumptions):
    """A causal strategy cannot systematically profit from driftless random walks."""
    cfg = load_strategy_config()
    cfg = set_dotted(cfg, "universe", ["ES", "ZN", "6E", "GC", "CL", "ZC"])
    res = run_backtest(synth_null, cfg, instruments, assumptions, start="2002-01-02")
    s = backtest_summary(res)
    # ~19 years: a true Sharpe of 0 gives |SR| > 0.5 with probability well below 5%.
    assert s["sharpe"] < 0.5, s["sharpe"]


def test_lag_zero_is_rejected():
    with pytest.raises(ValueError):
        load_strategy_config(dotted={"execution.lag_bars": 0})


def test_fills_never_on_decision_close(synth_small, cfg, instruments, assumptions):
    res = run_backtest(synth_small, cfg, instruments, assumptions, start="2016-01-04")
    t = res.trades[res.trades["reason"] != "data_end"]
    assert (pd.to_datetime(t["date"]) > pd.to_datetime(t["decision_date"])).all()


# ----------------------------------------------------------------------------- continuous / roll
def _two_contract_market(gap: float):
    dates = pd.bdate_range("2020-01-01", periods=60)
    rows = []
    for i, d in enumerate(dates):
        if i <= 45:
            rows.append({"date": d, "market": "ES", "contract": "ESH2020", "settle": 100.0})
        rows.append({"date": d, "market": "ES", "contract": "ESM2020", "settle": 100.0 + gap})
    contracts = pd.DataFrame({"market": "ES", "contract": ["ESH2020", "ESM2020"],
                              "last_trade_date": [dates[45], dates[59] + pd.Timedelta(days=90)],
                              "first_notice_date": pd.NaT})
    contracts["roll_reference_date"] = contracts["last_trade_date"]
    return pd.DataFrame(rows), contracts, dates


def test_roll_gap_is_not_pnl_or_signal():
    prices, contracts, dates = _two_contract_market(gap=10.0)
    settle = prices.pivot(index="date", columns="contract", values="settle")
    act = active_contracts(settle, contracts, None, 5)
    assert act.iloc[0] == "ESH2020" and act.iloc[-1] == "ESM2020"
    cont = build_continuous(settle, act)
    # Each contract's price is constant, so the continuous series must be flat despite the 10-point gap.
    assert np.allclose(cont["dP"].dropna(), 0.0)
    assert cont["rolled"].sum() == 1


def test_continuous_differences_invariant_to_later_data():
    prices, contracts, dates = _two_contract_market(gap=3.0)
    prices.loc[prices["contract"] == "ESH2020", "settle"] += np.arange((prices["contract"] == "ESH2020").sum()) * 0.5
    settle = prices.pivot(index="date", columns="contract", values="settle")
    full = build_continuous(settle, active_contracts(settle, contracts, None, 5))
    part_settle = settle.iloc[:30]
    part = build_continuous(part_settle, active_contracts(part_settle, contracts, None, 5))
    pd.testing.assert_series_equal(full["dP"].iloc[:30], part["dP"], check_names=False)


def test_volume_roll_uses_previous_day_volume_only():
    prices, contracts, dates = _two_contract_market(gap=1.0)
    vol = prices.pivot(index="date", columns="contract", values="settle") * 0 + 1.0
    vol["ESH2020"] = 100.0
    vol["ESM2020"] = 10.0
    settle = prices.pivot(index="date", columns="contract", values="settle")
    base = active_contracts(settle, contracts, None, 5, method="volume", volume=vol)
    k = 20
    vol2 = vol.copy()
    vol2.iloc[k, vol2.columns.get_loc("ESM2020")] = 1e9     # spike known only at close of day k
    alt = active_contracts(settle, contracts, None, 5, method="volume", volume=vol2)
    assert alt.iloc[k] == base.iloc[k] == "ESH2020"         # not used on day k itself
    assert alt.iloc[k + 1] == "ESM2020"                      # used from the next decision
    assert (alt.iloc[k + 1:] == "ESM2020").all()             # never rolls back


def test_roll_executes_both_legs_with_costs_and_no_gap_pnl(instruments, assumptions):
    """Constant prices in each contract + big contango gap: NAV may only change by costs."""
    dates = pd.bdate_range("2019-01-01", "2021-12-31")
    rng = np.random.default_rng(3)
    rows, contracts = [], []
    exps = pd.date_range("2019-03-15", "2022-06-15", freq="3MS") + pd.offsets.Day(14)
    for k, e in enumerate(exps):
        cid = f"ES{'HMUZ'[k % 4]}{e.year}"
        contracts.append({"market": "ES", "contract": cid, "last_trade_date": e, "first_notice_date": pd.NaT,
                          "roll_reference_date": e})
    # Underlying trends upward so the strategy holds a position; each contract carries +5*k points.
    trend = np.cumsum(rng.normal(0.8, 5, len(dates))) + 3000
    for i, d in enumerate(dates):
        for k, c in enumerate(contracts):
            if d <= c["last_trade_date"] and (c["last_trade_date"] - d).days < 200:
                rows.append({"date": d, "market": "ES", "contract": c["contract"], "settle": trend[i] + 50.0 * k})
    ds = make_dataset(pd.DataFrame(rows), pd.DataFrame(contracts),
                      pd.DataFrame({"USD": 100.0, "JPY": 1.0}, index=dates))
    cfg = load_strategy_config(dotted={"universe": ["ES"]})
    res = run_backtest(ds, cfg, instruments, assumptions, start="2020-01-02")
    rolls = res.trades[res.trades["reason"].isin(["roll_close", "roll_open"])]
    assert len(rolls[rolls["reason"] == "roll_close"]) >= 3
    assert (rolls["cost_jpy"] > 0).all()
    # Sum of roll-day P&L from the gap must be zero: VM equals Σ qty*mult*Δsettle of the same contract.
    t = res.trades.sort_values("date")
    for _, r in t[t["reason"] == "roll_close"].iterrows():
        same_day_open = t[(t["date"] == r["date"]) & (t["reason"] == "roll_open")]
        assert len(same_day_open) == 1
        assert same_day_open["contract"].iloc[0] != r["contract"]
    # Reconstruct NAV from contract-level VM only (no gap) and compare.
    specs_mult = instruments["ES"].multiplier
    vm_total = 0.0
    held = {}
    last = {}
    settle = ds.prices.pivot(index="date", columns="contract", values="settle")
    trades_by_date = t.groupby("date")
    for d in res.daily.index:
        for c, q in held.items():
            if q and not np.isnan(settle.at[d, c]):
                vm_total += q * specs_mult * (settle.at[d, c] - last[c]) * 100.0
                last[c] = settle.at[d, c]
        if d in trades_by_date.groups:
            for _, tr in trades_by_date.get_group(d).iterrows():
                held[tr["contract"]] = held.get(tr["contract"], 0) + tr["qty"]
                last[tr["contract"]] = tr["price"]
    nav_change = res.daily["nav"].iloc[-1] - res.notes["capital"]
    costs = res.daily["costs"].sum() + res.daily["fx_conversion_cost"].sum()
    fx = res.daily["fx_translation"].sum()
    assert nav_change == pytest.approx(vm_total + costs + fx, rel=1e-9, abs=1e-3)


# ----------------------------------------------------------------------------- accounting
def test_variation_margin_multiplier_and_fx():
    led = Ledger()
    led.deposit(1e8)
    fx = {"JPY": 1.0, "USD": 150.0}
    led.fx_prev = dict(fx)
    led.fill("ES", "ESZ2025", 3, 5000.0, 5000.0, "USD", 50.0, cost_ccy=7.5, fx=fx)
    assert led.balances["USD"] == pytest.approx(-7.5)
    vm = led.mark_to_market({("ES", "ESZ2025"): 5010.0}, {"ES": ("USD", 50.0)}, fx)
    assert vm["ES"] == pytest.approx(3 * 50 * 10 * 150.0)
    assert led.balances["USD"] == pytest.approx(1500 - 7.5)
    fx2 = {"JPY": 1.0, "USD": 160.0}
    tr = led.fx_translation(fx2)
    assert tr == pytest.approx((1500 - 7.5) * 10.0)
    assert led.nav(fx2) == pytest.approx(1e8 + (1500 - 7.5) * 160.0)


def test_negative_price_pnl_sign():
    led = Ledger()
    led.deposit(1e8)
    fx = {"JPY": 1.0, "USD": 100.0}
    led.fill("CL", "CLK2020", 2, 18.0, 18.0, "USD", 1000.0, 0.0, fx)
    vm = led.mark_to_market({("CL", "CLK2020"): -37.63}, {"CL": ("USD", 1000.0)}, fx)
    assert vm["CL"] == pytest.approx(2 * 1000 * (-37.63 - 18.0) * 100.0)


def test_signal_finite_through_negative_prices():
    s = pd.Series(np.r_[np.linspace(60, 5, 300), [-37.63, 10.0], np.linspace(12, 40, 100)])
    cont = pd.DataFrame({"dP": s.diff(), "adj": s - s.iloc[0]})
    cfg = load_strategy_config()
    sig = compute_signal_frame(cont, cfg["signal"])
    assert np.isfinite(sig["forecast"].iloc[260:]).all()
    assert (sig["sigma"].dropna() > 0).all()


def test_attribution_identity_every_day(synth_small, cfg, instruments, assumptions):
    res = run_backtest(synth_small, cfg, instruments, assumptions, start="2016-01-04")
    d = res.daily
    dnav = d["nav"].diff().iloc[1:]
    comp = (d["futures_pnl"] + d["costs"] + d["interest"] + d["fx_translation"] + d["fx_conversion_cost"]).iloc[1:]
    np.testing.assert_allclose(dnav.values, comp.values, rtol=1e-9, atol=1e-2)
    assert d["interest"].sum() > 0          # synthetic dataset has cash rates
    assert res.label == "SYNTHETIC_TEST_ONLY"


def test_interest_only_on_cash_not_notional(instruments, assumptions):
    led = Ledger()
    led.deposit(1e8)
    fx = {"JPY": 1.0, "USD": 100.0}
    led.fill("ES", "ESZ2025", 100, 5000.0, 5000.0, "USD", 50.0, 0.0, fx)   # notional 2.5bn JPY
    got = led.accrue_interest(365, {"JPY": 0.01, "USD": 0.05}, 0.005, fx)
    assert got == pytest.approx(1e8 * 0.01)                                   # only the JPY cash


def test_never_held_past_last_trade(synth_small, cfg, instruments, assumptions):
    res = run_backtest(synth_small, cfg, instruments, assumptions, start="2015-01-05", strict_expiry=True)
    assert res.expiry_violations == []


def test_halt_and_limit_days_defer_fills(instruments, assumptions):
    from trendbot.data.synthetic import generate_synthetic

    ds = generate_synthetic(markets=["ZC", "ES"], start="2009-01-02", end="2013-12-31", trend_strength=1.0)
    cfg = load_strategy_config(dotted={"universe": ["ZC", "ES"]})
    res = run_backtest(ds, cfg, instruments, assumptions, start="2011-01-03")
    zc_days = pd.to_datetime(res.trades.loc[res.trades["market"] == "ZC", "date"])
    assert not zc_days.isin([pd.Timestamp("2012-07-18"), pd.Timestamp("2012-07-19")]).any()
