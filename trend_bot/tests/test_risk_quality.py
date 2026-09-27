import numpy as np
import pandas as pd
import pytest

from trendbot.config import load_strategy_config
from trendbot.data.quality import run_quality
from trendbot.data.synthetic import generate_synthetic, inject_data_errors
from trendbot.execution import Order, buffered, limit_changes, split_increase_reduce
from trendbot.portfolio import diversification_multiplier, risk_weights
from trendbot.risk import DrawdownController, RiskInputs, cap_continuous, enforce_integer, violations


def _inputs(rng, k=8, nav=3e8):
    vol = rng.uniform(2e6, 2e7, k)
    corr = np.full((k, k), 0.3)
    np.fill_diagonal(corr, 1.0)
    notional = vol / rng.uniform(0.03, 0.3, k)
    return RiskInputs([f"M{i}" for i in range(k)], nav, vol, notional, notional * 0.05,
                      corr * np.outer(vol, vol), [f"s{i % 3}" for i in range(k)], [f"c{i % 2}" for i in range(k)],
                      rng.uniform(1e3, 1e5, k), 0.09, 252)


@pytest.mark.parametrize("seed", range(20))
def test_caps_hold_after_integer_rounding(seed):
    cfg = load_strategy_config()
    rng = np.random.default_rng(seed)
    ri = _inputs(rng)
    target = rng.normal(0, 1, 8) * ri.nav * 0.05 / ri.vol_pc * 3   # deliberately oversized
    capped, _ = cap_continuous(target, ri, cfg["risk"], cfg["execution"])
    assert np.all(np.abs(capped) <= np.abs(target) + 1e-9)             # caps only reduce
    assert np.all(np.sign(capped) * np.sign(target) >= 0)               # never flip direction
    n = enforce_integer(np.round(capped), ri, cfg["risk"], cfg["execution"])
    assert violations(n, ri, cfg["risk"], cfg["execution"]) == []
    assert np.all(n == np.round(n))


def test_low_margin_is_not_low_risk():
    """A market with tiny margin but high vol is still limited by the vol cap."""
    cfg = load_strategy_config()
    rng = np.random.default_rng(1)
    ri = _inputs(rng, k=2)
    ri.margin_pc[:] = 1.0            # almost no margin
    big = np.array([1e6, 0.0])
    capped, binding = cap_continuous(big, ri, cfg["risk"], cfg["execution"])
    assert abs(capped[0]) * ri.vol_pc[0] <= cfg["risk"]["market_vol_cap"] * ri.nav + 1e-6
    assert "market" in binding


def test_margin_cap_binds_when_margin_raised():
    cfg = load_strategy_config()
    rng = np.random.default_rng(2)
    ri = _inputs(rng)
    ri.margin_pc[:] = ri.notional_pc * 0.5      # stress: margins x10
    t = np.sign(rng.normal(size=8)) * cfg["risk"]["market_vol_cap"] * ri.nav / ri.vol_pc
    capped, binding = cap_continuous(t, ri, cfg["risk"], cfg["execution"])
    assert np.abs(capped) @ ri.margin_pc <= cfg["risk"]["margin_usage_cap"] * ri.nav * (1 + 1e-9)
    assert "margin" in binding


def test_no_scale_up_to_reach_target_vol():
    cfg = load_strategy_config()
    ri = _inputs(np.random.default_rng(3))
    tiny = np.ones(8) * 0.1
    capped, _ = cap_continuous(tiny, ri, cfg["risk"], cfg["execution"])
    np.testing.assert_allclose(capped, tiny)


def test_buffer_and_change_limits():
    exe = load_strategy_config()["execution"]
    # Inside buffer: no trade.
    assert buffered(np.array([10.0]), np.array([9.0]), np.array([20.0]), 0.1, "edge")[0] == 9.0
    # Outside buffer: trade to edge (10 - 2 = 8 from above... current 20 -> edge 12).
    assert buffered(np.array([10.0]), np.array([20.0]), np.array([20.0]), 0.1, "edge")[0] == 12.0
    # Increase limited to 50% of full per day; reduction not limited by it.
    out = limit_changes(np.array([0.0, 20.0]), np.array([20.0, 0.0]), np.array([20.0, 20.0]),
                        np.array([1e6, 1e6]), exe, np.array([True, True]))
    assert out[0] == 10.0 and out[1] == 0.0
    # Increase blocked when not allowed (halt), reduction still allowed.
    out = limit_changes(np.array([5.0, 5.0]), np.array([9.0, 1.0]), np.array([20.0, 20.0]),
                        np.array([1e6, 1e6]), exe, np.array([False, False]))
    assert out[0] == 5.0 and out[1] == 1.0
    # Sign flip under halt: can reduce to zero but not open the opposite side.
    out = limit_changes(np.array([5.0]), np.array([-5.0]), np.array([20.0]), np.array([1e6]), exe, np.array([False]))
    assert out[0] == 0.0
    # Liquidity: order size capped at max_adv_frac * ADV.
    out = limit_changes(np.array([0.0]), np.array([100.0]), np.array([1000.0]), np.array([1000.0]), exe, np.array([True]))
    assert out[0] == np.floor(exe["max_adv_frac"] * 1000)
    assert split_increase_reduce(5, -3) == (5, 3)


def test_risk_weights_class_then_sector():
    ac = {"ES": "equity", "CL": "commodity", "NG": "commodity", "GC": "commodity", "6J": "fx"}
    sec = {"ES": "eq", "CL": "energy", "NG": "energy", "GC": "metals", "6J": "fx"}
    w = risk_weights(list(ac), ac, sec, {"equity": 0.25, "rates": 0.25, "fx": 0.25, "commodity": 0.25}, True)
    assert sum(w.values()) == pytest.approx(1.0)
    assert w["ES"] == pytest.approx(1 / 3)                 # rates absent → renormalised
    assert w["GC"] == pytest.approx(1 / 6)                 # half of commodities (metals sector)
    assert w["CL"] == pytest.approx(1 / 12)                # energy sector shared by CL and NG
    idm = diversification_multiplier(np.array([0.5, 0.5]), np.array([[1, -0.9], [-0.9, 1]]), 2.5, True)
    assert idm == pytest.approx(np.sqrt(2))                # negative corr floored at 0


def test_drawdown_controller_hysteresis():
    c = DrawdownController([(0.10, 0.75), (0.15, 0.5)], 0.025)
    assert c.update(100) == 1.0
    assert c.update(89) == 0.75            # dd 11%
    assert c.update(84) == 0.5             # dd 16%
    assert c.update(86) == 0.5             # dd 14% — not below 15-2.5
    assert c.update(88) == 0.75            # dd 12% < 12.5% → one level back
    assert c.update(91) == 0.75            # dd 9% — not below 7.5%
    assert c.update(93) == 1.0


def test_order_ids_deterministic_and_distinct():
    a = Order("2025-01-06", "ES", "ESH2025", 3, "rebalance", "increase")
    b = Order("2025-01-06", "ES", "ESH2025", 3, "rebalance", "increase")
    c = Order("2025-01-06", "ES", "ESH2025", 4, "rebalance", "increase")
    assert a.client_order_id == b.client_order_id != c.client_order_id


# ----------------------------------------------------------------------------- data quality
def test_quality_detects_injected_errors(instruments):
    ds = generate_synthetic(markets=["ES", "6E"], start="2015-01-02", end="2018-12-31")
    bad = ds.prices.copy()
    bad = inject_data_errors(bad, "ES")
    ds.prices = bad
    dq = load_strategy_config()["data_quality"]
    rep = run_quality(ds, dq, instruments)
    checks = set(rep.issues.loc[rep.issues["market"] == "ES", "check"])
    assert {"duplicate", "anomaly", "stale", "missing_session"} <= checks
    assert rep.status["ES"] == "FAIL"                       # duplicates are fatal
    assert rep.status["6E"] in ("OK", "WARN")


def test_quality_halt_is_not_holiday_and_age_check(instruments):
    ds = generate_synthetic(markets=["NK225M"], start="2010-06-01", end="2011-06-30")
    dq = load_strategy_config()["data_quality"]
    rep = run_quality(ds, dq, instruments)
    iss = rep.issues
    halted = iss[iss["check"] == "status_halt"]
    assert len(halted) >= 1 and (halted["date"] == pd.Timestamp("2011-03-15")).all()
    assert not ((iss["check"] == "missing_session") & (iss["date"] == pd.Timestamp("2011-03-15"))).any()
    # 2011-01-03 is a Tokyo holiday (calendar closed): no missing_session issue
    assert not ((iss["check"] == "missing_session") & (iss["date"] == pd.Timestamp("2011-01-03"))).any()
    # Decision on a date far after the last row → data age FAIL.
    rep2 = run_quality(ds, dq, instruments, as_of=pd.Timestamp("2011-07-15"))
    assert rep2.status["NK225M"] == "FAIL"
    assert "data_age" in set(rep2.issues["check"])


def test_negative_price_allowed_only_where_configured(instruments):
    ds = generate_synthetic(markets=["CL"], start="2019-06-03", end="2020-06-30")
    dq = load_strategy_config()["data_quality"]
    rep = run_quality(ds, dq, instruments)
    neg = rep.issues[rep.issues["check"] == "non_positive_price"]
    assert len(neg) == 1 and (neg["severity"] == "WARN").all()
    ds.prices.loc[ds.prices.index[5], "settle"] = -1.0
    ds.prices.loc[ds.prices.index[5], "market"] = "CL"
    from trendbot.data.quality import check_market
    assert any(i["severity"] == "FAIL" for i in check_market(ds, "CL", dq, allow_negative=False)
               if i["check"] == "non_positive_price")


def test_fx_convention_check_rejects_flipped_series():
    from trendbot.data.loaders import verify_fx_convention

    idx = pd.to_datetime(["2020-01-02"])
    good = pd.DataFrame({"Japan": [108.4], "Euro": [0.896], "United Kingdom": [0.76], "Australia": [1.43], "Canada": [1.30]}, index=idx)
    verify_fx_convention(good)
    flipped = good.copy()
    flipped["Euro"] = 1 / flipped["Euro"]
    with pytest.raises(ValueError):
        verify_fx_convention(flipped)


def test_config_validation():
    with pytest.raises(ValueError):
        load_strategy_config(dotted={"portfolio.class_budget": {"equity": 0.5, "rates": 0.6}})
    with pytest.raises(KeyError):
        load_strategy_config(dotted={"signal.not_a_key": 1})
