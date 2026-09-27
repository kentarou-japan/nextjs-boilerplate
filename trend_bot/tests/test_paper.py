"""Paper trading: equivalence with the backtest, idempotency, halts, restart recovery."""
import copy

import numpy as np
import pandas as pd
import pytest

from trendbot.backtest import run_backtest
from trendbot.config import load_strategy_config
from trendbot.paper.broker import LiveBrokerStub, LiveTradingDisabled, SimBroker
from trendbot.paper.runner import PaperRunner, load_paper_config

START, END = "2017-01-03", "2017-03-31"
MARKETS = ["ES", "FGBL", "CL", "NK225M"]


@pytest.fixture()
def pcfg():
    return load_paper_config()


@pytest.fixture()
def scfg():
    return load_strategy_config(dotted={"universe": MARKETS})


def make_runner(tmp_path, ds, pcfg, scfg, name="run", **kw):
    broker = SimBroker(tmp_path / name / "broker.db")
    r = PaperRunner(pcfg, ds, broker, tmp_path / name, strategy_cfg=scfg, **kw)
    return r, broker


def session_dates(ds, start, end):
    d = pd.DatetimeIndex(sorted(ds.prices["date"].unique()))
    return d[(d >= start) & (d <= end)]


def test_paper_matches_backtest(tmp_path, synth_small, pcfg, scfg, instruments, assumptions):
    bt = run_backtest(synth_small.restrict(end=END), scfg, instruments, assumptions, start=START)
    r, _ = make_runner(tmp_path, synth_small, pcfg, scfg)
    r.initialize(scfg["run"]["initial_capital"])
    for d in session_dates(synth_small, START, END):
        rep = r.run_day(d)
        assert rep.status == "ok", (d, rep)
    nav = pd.read_sql("SELECT date, nav FROM nav", r.store.db, index_col="date")["nav"]
    nav.index = pd.to_datetime(nav.index)
    np.testing.assert_allclose(nav.values, bt.daily["nav"].reindex(nav.index).values, rtol=1e-10)
    pos = dict(r.store.load_ledger().market_exposure())
    last = bt.positions.iloc[-1]
    assert {k: v for k, v in pos.items() if v} == {k: v for k, v in last.items() if v}


def test_rerun_same_day_is_noop_and_duplicate_rejected(tmp_path, synth_small, pcfg, scfg):
    r, broker = make_runner(tmp_path, synth_small, pcfg, scfg)
    r.initialize(3e8)
    days = session_dates(synth_small, START, END)
    for d in days[:5]:
        r.run_day(d)
    n_orders = len(r.store.orders())
    assert r.run_day(days[4]).status == "skipped_already_processed"
    assert len(r.store.orders()) == n_orders
    # Simulate a bug that re-submits yesterday's working order: broker must reject by client_order_id.
    working = r.store.orders(("SUBMITTED",))
    if working:
        o = working[0]
        body = {"client_order_id": o["client_order_id"], "date": o["decision_date"], "market": o["market"],
                "contract": o["contract"], "qty": o["qty"], "reason": o["reason"], "intent": o["intent"]}
        res = broker.submit(body)
        assert res["status"] == "DUPLICATE_REJECTED"
        assert len(broker.open_orders()) == len(working)


def test_unknown_order_status_halts_and_blocks(tmp_path, synth_small, pcfg, scfg):
    r, broker = make_runner(tmp_path, synth_small, pcfg, scfg)
    r.initialize(3e8)
    days = session_dates(synth_small, START, END)
    for d in days[:3]:
        r.run_day(d)
    broker.inject("timeout_on_submit")
    i = 3
    while not any(h["reason"] == "unknown_order_status" for h in r.store.active_halts()):
        r.run_day(days[i]); i += 1
    unknown = r.store.orders(("UNKNOWN",))
    assert len(unknown) == 1
    before = len(r.store.orders())
    rep = r.run_day(days[i])            # blocked: no new orders; the unknown order is resolved by query
    assert rep.orders_sent == 0 and len(r.store.orders()) == before
    assert r.store.orders(("UNKNOWN",)) == []   # resolved via broker query (it had reached the broker)
    res = r.resume("tester")
    assert res["reconcile"]["ok"] and r.store.active_halts() == []


def test_restart_recovers_and_continues_identically(tmp_path, synth_small, pcfg, scfg):
    days = session_dates(synth_small, START, END)
    ref, _ = make_runner(tmp_path, synth_small, pcfg, scfg, name="ref")
    ref.initialize(3e8)
    for d in days:
        ref.run_day(d)
    a, broker = make_runner(tmp_path, synth_small, pcfg, scfg, name="crash")
    a.initialize(3e8)
    for d in days[:20]:
        a.run_day(d)
    a.store.close(); broker.close()
    # "Process restart": fresh objects on the same files.
    b, broker2 = make_runner(tmp_path, synth_small, pcfg, scfg, name="crash")
    chk = b.startup_reconcile()
    assert chk["ok"], chk
    for d in days[20:]:
        b.run_day(d)
    n1 = pd.read_sql("SELECT nav FROM nav ORDER BY date", ref.store.db)["nav"].values
    n2 = pd.read_sql("SELECT nav FROM nav ORDER BY date", b.store.db)["nav"].values
    np.testing.assert_allclose(n1, n2, rtol=1e-12)


def test_crash_between_persist_and_submit(tmp_path, synth_small, pcfg, scfg):
    """Orders written as PENDING_SUBMIT but never sent must not be sent twice nor lost silently."""
    r, broker = make_runner(tmp_path, synth_small, pcfg, scfg)
    r.initialize(3e8)
    days = session_dates(synth_small, START, END)
    real_submit = r._submit
    r._submit = lambda orders, date: 0      # crash right after the write-ahead commit
    i = 0
    while not r.store.orders(("PENDING_SUBMIT",)):
        r.run_day(days[i]); i += 1
    r._submit = real_submit
    res = r.startup_reconcile()
    assert res["ok"]
    assert r.store.orders(("PENDING_SUBMIT",)) == []
    assert len(r.store.orders(("NOT_SENT",))) >= 1
    assert broker.open_orders() == []


def test_position_mismatch_halts_until_resolved(tmp_path, synth_small, pcfg, scfg):
    r, broker = make_runner(tmp_path, synth_small, pcfg, scfg)
    r.initialize(3e8)
    days = session_dates(synth_small, START, END)
    i = 0
    while not r.store.load_ledger().positions:
        r.run_day(days[i]); i += 1
    broker.inject("position_drift")
    rep = r.run_day(days[i]); i += 1
    assert any(h["reason"] == "reconciliation_mismatch" for h in r.store.active_halts())
    rep = r.run_day(days[i])
    assert rep.orders_sent == 0
    res = r.resume("tester")
    assert res["refused"] and any(h["reason"] == "reconciliation_mismatch" for h in r.store.active_halts())


def test_api_disconnect_no_state_change_then_catch_up(tmp_path, synth_small, pcfg, scfg):
    days = session_dates(synth_small, START, END)
    ref, _ = make_runner(tmp_path, synth_small, pcfg, scfg, name="ref")
    ref.initialize(3e8)
    r, broker = make_runner(tmp_path, synth_small, pcfg, scfg, name="dc")
    r.initialize(3e8)
    for d in days[:10]:
        ref.run_day(d); r.run_day(d)
    broker.inject("disconnected")
    rep = r.run_day(days[10])
    assert rep.status == "halted_api_disconnect"
    assert r.store.get("last_processed_date") == days[9].strftime("%Y-%m-%d")
    broker.clear_faults()
    rep = r.run_day(days[11])
    assert rep.orders_sent == 0             # still halted (block_all) until an operator resumes
    res = r.resume("tester")
    assert res["reconcile"]["ok"] and not r.store.active_halts()
    # NAV after the gap equals the reference NAV evaluated on the same day (positions valued over the gap).
    ref.run_day(days[10]); ref.run_day(days[11])
    n_ref = ref.store.db.execute("SELECT nav FROM nav WHERE date=?", (days[11].strftime("%Y-%m-%d"),)).fetchone()[0]
    n_dc = r.store.db.execute("SELECT nav FROM nav WHERE date=?", (days[11].strftime("%Y-%m-%d"),)).fetchone()[0]
    assert n_dc == pytest.approx(n_ref, rel=1e-3)   # differs only by interest-day/fx-path effects


def test_stale_market_data_freezes_only_that_market(tmp_path, synth_small, pcfg, scfg):
    ds = copy.deepcopy(synth_small)
    days = session_dates(ds, START, END)
    gap = days[30:34]
    ds.prices = ds.prices[~((ds.prices["market"] == "ES") & ds.prices["date"].isin(gap))].reset_index(drop=True)
    r, _ = make_runner(tmp_path, ds, pcfg, scfg)
    r.initialize(3e8)
    for d in days[:32]:
        r.run_day(d)
    halts = r.store.active_halts()
    assert any(h["scope"] == "market:ES" for h in halts)
    assert not any(h["scope"] == "global" for h in halts)
    es_orders = [o for o in r.store.orders() if o["market"] == "ES" and o["decision_date"] >= gap[0].strftime("%Y-%m-%d")]
    assert es_orders == []
    for d in days[32:40]:
        r.run_day(d)
    assert not any(h["scope"] == "market:ES" for h in r.store.active_halts())   # auto-cleared when data returns


def test_margin_shortfall_reduce_only_with_approval(tmp_path, synth_small, pcfg, scfg):
    days = session_dates(synth_small, START, END)
    r, broker = make_runner(tmp_path, synth_small, pcfg, scfg)
    r.initialize(3e8)
    for d in days[:15]:
        r.run_day(d)
    # Exchange raises margins 20x from here on.
    r.margin_multiplier = 20.0
    rep = r.run_day(days[15])
    assert any(h["reason"] == "margin_shortfall" for h in r.store.active_halts())
    rep = r.run_day(days[16])
    held = r.store.orders(("AWAITING_APPROVAL",))
    assert rep.orders_sent == 0 and len(held) >= 1
    assert all(o["intent"] == "reduce" for o in held)
    sent = r.approve("risk-officer")
    assert sent == len([o for o in held if o["decision_date"] == days[16].strftime("%Y-%m-%d")])


def test_live_broker_is_disabled(monkeypatch):
    monkeypatch.setenv("TRENDBOT_LIVE_TRADING", "I_UNDERSTAND_REAL_MONEY")
    b = LiveBrokerStub({"live_trading": {"enabled": True}})
    with pytest.raises(LiveTradingDisabled):
        b.submit({"client_order_id": "x"})
    with pytest.raises(LiveTradingDisabled):
        b.connect()
