"""Full evaluation suite for one dataset.

Development period = trading start .. day before holdout start. Everything except the final
holdout evaluation uses the development period only. The holdout is evaluated only when
``unlock_holdout=True`` and each evaluation is appended to reports/holdout_log.json.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import CONFIG_DIR, load_yaml
from ..contracts import load_instruments
from ..metrics import attribution, summarize
from .benchmarks import benchmark_b, overlay_analysis, to_monthly
from .common import (DATASETS, REPORTS, RunSpec, append_trials, base_config, dataset_hash, dev_end, holdout_start,
                     load_dataset, record_holdout, run_many)
from .pbo import pbo_cscv

HIST_WINDOWS = {
    "2008_GFC (2008-09..2009-03)": ("2008-09-01", "2009-03-31"),
    "2020_COVID (2020-02..2020-04)": ("2020-02-01", "2020-04-30"),
    "2022_rates_inflation (2022)": ("2022-01-01", "2022-12-31"),
    "2025 (2025-01..2025-12)": ("2025-01-01", "2025-12-31"),
}


def _metrics_row(name: str, r: dict, bpy: int) -> dict:
    s = r["summary"]
    keys = ["cagr", "ann_mean_excess", "ann_vol", "sharpe", "max_drawdown", "maxdd_days_peak_to_recovery",
            "longest_dd_days", "current_underwater_days", "worst_month", "worst_year", "es95_bar", "es95_month",
            "turnover_notional_per_year", "costs_pct_nav_per_year", "avg_contracts_abs", "margin_usage_mean",
            "margin_usage_max", "gross_notional_mean", "exante_vol_mean"]
    return {"name": name, **{k: s.get(k) for k in keys}, "start": s.get("start"), "end": s.get("end")}


def _vol_matched(ret: pd.Series, bpy: int, target: float) -> pd.Series:
    vol = ret.std() * np.sqrt(bpy)
    return ret * (target / vol) if vol > 0 else ret


def shock_losses(daily_positions: pd.DataFrame, res_detail: dict, ds, cfg, instruments, ks=(3.0, 5.0)) -> dict:
    """Instantaneous loss if every held market moves k daily-σ against the position simultaneously
    (no diversification — a deliberately harsh 'everything reverses at once' scenario)."""
    from ..strategy import PreparedData
    from ..config import load_assumptions

    pdata = PreparedData(ds, cfg, instruments, load_assumptions())
    nav = res_detail["daily"]["nav"]
    out = {}
    pos = daily_positions.reindex(columns=pdata.markets).fillna(0.0)
    idx = pdata.dates.get_indexer(pos.index)
    fx_ccy = {m: pdata.inst[m].currency for m in pdata.markets}
    loss = np.zeros(len(pos))
    for j, m in enumerate(pdata.markets):
        fxs = pdata.fx[fx_ccy[m]].values[idx]
        loss += np.nan_to_num(np.abs(pos[m].values) * pdata.sigma[idx, j] * pdata.inst[m].multiplier * fxs)
    loss = pd.Series(np.nan_to_num(loss), index=pos.index) / nav.reindex(pos.index)
    for k in ks:
        l = loss * k
        out[f"{k:.0f}sigma_all_adverse"] = {"max_loss_pct_nav": float(l.max() * 100),
                                           "p99_loss_pct_nav": float(l.quantile(0.99) * 100),
                                           "median_loss_pct_nav": float(l.median() * 100),
                                           "worst_date": str(l.idxmax().date())}
    return out


def evaluate_dataset(key: str, unlock_holdout: bool = False, workers: int = 4, out_dir: Path | None = None,
                     quick: bool = False) -> dict:
    out_dir = out_dir or REPORTS / key
    out_dir.mkdir(parents=True, exist_ok=True)
    ds = load_dataset(key)
    instruments = load_instruments()
    spec_ds = DATASETS[key]
    ss = load_yaml(CONFIG_DIR / "search_space.yaml")
    cfg0 = base_config(key)
    bpy = int(cfg0["run"]["bars_per_year"])
    freq = "daily" if bpy >= 200 else "monthly"
    start, dend, hstart = spec_ds["start"], dev_end(key), holdout_start(key)
    dend_s = dend.strftime("%Y-%m-%d")
    universe = cfg0["universe"]
    results: dict = {"dataset": key, "data_label": ds.label, "frequency": freq, "universe": universe,
                     "dev_period": [start, dend_s], "holdout_start": hstart.strftime("%Y-%m-%d"),
                     "data_hash": dataset_hash(key), "notes": ds.notes}

    # ---------------------------------------------------------------- 1. grid (pre-registered)
    grid_keys = list(ss["grid"].keys())
    combos = list(itertools.product(*[ss["grid"][k] for k in grid_keys]))
    base_dotted = {"signal.transform": cfg0["signal"]["transform"], "signal.vol_span_bars": cfg0["signal"]["vol_span_bars"],
                   "execution.buffer_frac": cfg0["execution"]["buffer_frac"]}
    specs = []
    for c in combos:
        dotted = dict(zip(grid_keys, c))
        if freq == "monthly" and "signal.vol_span_bars" in dotted:
            dotted["signal.vol_span_bars"] = {30: 12, 60: 24}[dotted["signal.vol_span_bars"]]
            base_dotted["signal.vol_span_bars"] = cfg0["signal"]["vol_span_bars"]
        name = "grid:" + ",".join(f"{k.split('.')[-1]}={v}" for k, v in dotted.items())
        specs.append(RunSpec(name, key, dotted, start, dend_s))
    base_name = "grid:" + ",".join(f"{k.split('.')[-1]}={v}" for k, v in base_dotted.items())
    # ---------------------------------------------------------------- 2. benchmark C (single horizon)
    single = [252] if freq == "daily" else [12]
    specs.append(RunSpec("C_single_12m", key, {"signal.lookbacks_bars": single}, start, dend_s))
    if not quick:
        # ------------------------------------------------------------ 3. sensitivity
        for lb in ss["sensitivity"]["signal.lookbacks_bars"][freq]:
            specs.append(RunSpec(f"sens:lookbacks={lb}", key, {"signal.lookbacks_bars": lb}, start, dend_s))
        for v in ss["sensitivity"]["signal.vol_floor_ratio"]:
            specs.append(RunSpec(f"sens:vol_floor={v}", key, {"signal.vol_floor_ratio": v}, start, dend_s))
        for v in ss["sensitivity"]["portfolio.idm_cap"]:
            specs.append(RunSpec(f"sens:idm_cap={v}", key, {"portfolio.idm_cap": v}, start, dend_s))
        specs.append(RunSpec("sens:dd_control=off", key, {"risk.drawdown_control.enabled": False}, start, dend_s))
        # ------------------------------------------------------------ 4. robustness: leave-one-out
        for m in universe:
            specs.append(RunSpec(f"lomo:-{m}", key, {}, start, dend_s, universe=[x for x in universe if x != m]))
        classes = sorted({instruments[m].asset_class for m in universe})
        for c in classes:
            rest = [x for x in universe if instruments[x].asset_class != c]
            if rest:
                specs.append(RunSpec(f"loco:-{c}", key, {}, start, dend_s, universe=rest))
        # ------------------------------------------------------------ 5. stress
        specs += [RunSpec("stress:cost_x2", key, {}, start, dend_s, cost_multiplier=2.0),
                  RunSpec("stress:cost_x3", key, {}, start, dend_s, cost_multiplier=3.0),
                  RunSpec("stress:lag_2", key, {"execution.lag_bars": 2}, start, dend_s),
                  RunSpec("stress:adv_x0.25", key, {}, start, dend_s, adv_multiplier=0.25),
                  RunSpec("stress:margin_x2", key, {}, start, dend_s, margin_multiplier=2.0),
                  RunSpec("stress:margin_x4", key, {}, start, dend_s, margin_multiplier=4.0)]
        # ------------------------------------------------------------ 6. capacity
        for cap in (1e8, 3e8, 1e9):
            specs.append(RunSpec(f"cap:{cap:.0e}:int", key, {}, start, dend_s, capital=cap, integer=True))
            specs.append(RunSpec(f"cap:{cap:.0e}:frac", key, {}, start, dend_s, capital=cap, integer=False))
    runs = run_many(specs, workers=workers)
    base = run_many([RunSpec("D_base", key, {}, start, dend_s)], workers=1, keep_detail=True)["D_base"]

    # trials log (every configuration, adopted or not)
    append_trials([{"dataset": key, "data_label": ds.label, "name": n, "config_hash": r["config_hash"],
                    "period": f"{start}..{dend_s}", "sharpe": r["summary"].get("sharpe"),
                    "cagr": r["summary"].get("cagr"), "max_drawdown": r["summary"].get("max_drawdown"),
                    "role": n.split(":")[0]} for n, r in runs.items()])

    # ---------------------------------------------------------------- walk-forward on the grid
    grid_names = [s.name for s in specs if s.name.startswith("grid:")]
    grid_ret = pd.DataFrame({n: runs[n]["returns"] for n in grid_names})
    years = sorted(set(grid_ret.index.year))
    first = years[0] + int(ss["walk_forward"]["first_train_years"])
    wf_rows, wf_ret, base_ret_oos = [], [], []
    for y in [y for y in years if y >= first]:
        train = grid_ret[grid_ret.index.year < y]
        test = grid_ret[grid_ret.index.year == y]
        sr = train.mean() / train.std() * np.sqrt(bpy)
        pick = sr.idxmax()
        wf_ret.append(test[pick])
        base_ret_oos.append(test[base_name])
        wf_rows.append({"test_year": y, "selected": pick, "train_sharpe_selected": sr[pick],
                        "train_sharpe_base": sr[base_name], "test_return_selected": (1 + test[pick]).prod() - 1,
                        "test_return_base": (1 + test[base_name]).prod() - 1})
    wf = pd.DataFrame(wf_rows)
    wf_oos = pd.concat(wf_ret) if wf_ret else pd.Series(dtype=float)
    base_oos = pd.concat(base_ret_oos) if base_ret_oos else pd.Series(dtype=float)
    s_wf, s_base = summarize(wf_oos, bpy), summarize(base_oos, bpy)
    monthly_grid = grid_ret.apply(lambda c: to_monthly(c))
    pbo = pbo_cscv(monthly_grid, S=10)
    improvement = (s_wf.get("sharpe", np.nan) or np.nan) - (s_base.get("sharpe", np.nan) or np.nan)
    rule = ss["adoption_rule"]
    adopt_alternative = bool(improvement >= rule["min_sharpe_improvement_oos"] and pbo.get("pbo", 1) < rule["max_pbo"])
    results["walk_forward"] = {"oos_years": [int(x) for x in wf["test_year"]] if len(wf) else [],
                               "wf_selected_oos": s_wf, "baseline_oos": s_base,
                               "sharpe_improvement": improvement, "pbo": pbo,
                               "adopt_alternative": adopt_alternative,
                               "decision": ("代替案を採用" if adopt_alternative else "基準案を維持（採用規則を満たさない）")}
    wf.to_csv(out_dir / "walk_forward.csv", index=False)

    # ---------------------------------------------------------------- tables
    table = [_metrics_row("D_base (dev)", base, bpy)]
    table += [_metrics_row(n, r, bpy) for n, r in runs.items()]
    tdf = pd.DataFrame(table)
    tdf.to_csv(out_dir / "runs_dev.csv", index=False)
    results["base_dev"] = base["summary"]
    results["attribution_dev"] = attribution(_as_result(base), instruments).to_dict(orient="records")

    # leave-one-year-out on base
    br = base["returns"]
    loyo = []
    for y in sorted(set(br.index.year)):
        s = summarize(br[br.index.year != y], bpy)
        loyo.append({"excluded_year": y, "sharpe": s["sharpe"], "cagr": s["cagr"], "max_drawdown": s["max_drawdown"],
                     "year_return": float((1 + br[br.index.year == y]).prod() - 1)})
    pd.DataFrame(loyo).to_csv(out_dir / "leave_one_year_out.csv", index=False)
    results["leave_one_year_out_sharpe_range"] = [float(pd.DataFrame(loyo)["sharpe"].min()),
                                                 float(pd.DataFrame(loyo)["sharpe"].max())]
    # yearly returns
    yearly = (1 + br).groupby(br.index.year).prod() - 1
    results["yearly_returns_dev"] = {int(k): float(v) for k, v in yearly.items()}
    # shocks and historical windows
    results["shock_scenarios_dev"] = shock_losses(base["positions"], base, ds.restrict(end=dend), base["config"], instruments)

    # capacity table
    if not quick:
        cap_rows = []
        for cap in (1e8, 3e8, 1e9):
            ri, rf = runs[f"cap:{cap:.0e}:int"], runs[f"cap:{cap:.0e}:frac"]
            te = (ri["returns"] - rf["returns"]).std() * np.sqrt(bpy)
            cap_rows.append({"capital_jpy": cap, "sharpe_int": ri["summary"]["sharpe"], "sharpe_frac": rf["summary"]["sharpe"],
                             "cagr_int": ri["summary"]["cagr"], "cagr_frac": rf["summary"]["cagr"],
                             "tracking_error_int_vs_frac": te, "costs_pct_nav_per_year": ri["summary"]["costs_pct_nav_per_year"],
                             "avg_contracts_abs": ri["summary"]["avg_contracts_abs"], "avg_positions": ri["summary"]["avg_positions"],
                             "ann_vol_int": ri["summary"]["ann_vol"]})
        pd.DataFrame(cap_rows).to_csv(out_dir / "capacity.csv", index=False)
        results["capacity"] = cap_rows

    # ---------------------------------------------------------------- benchmarks A/B and overlay (monthly)
    bmk = benchmark_b()
    base_m = to_monthly(br)
    common = bmk.index.intersection(base_m.index)
    comp = {}
    if len(common) > 24:
        d_m = base_m.reindex(common)
        comp["D_monthly"] = summarize(d_m, 12)
        comp["D_vol_matched_to_B_usd_excess"] = None
        b_ex = bmk["B_usd_excess"].reindex(common).dropna()
        if len(b_ex) > 24:
            comp["B_usd_excess"] = summarize(b_ex, 12)
            vm = _vol_matched(d_m.reindex(b_ex.index), 12, b_ex.std() * np.sqrt(12))
            comp["D_vol_matched_to_B_usd_excess"] = summarize(vm, 12)
            comp["period_B_excess"] = [str(b_ex.index[0].date()), str(b_ex.index[-1].date())]
        comp["B_jpy_unhedged_total"] = summarize(bmk["B_jpy_unhedged_total"].reindex(common).dropna(), 12)
        c_m = to_monthly(runs["C_single_12m"]["returns"]).reindex(common)
        comp["C_single_12m_monthly"] = summarize(c_m, 12)
        comp["A_cash"] = {"note": "超過収益ベースでは定義上0。JPY短期金利データ未入手のため水準は表示不可"}
        usd_rf = bmk["usd_rf"].reindex(common).dropna()
        if len(usd_rf):
            comp["A_usd_tbill_reference"] = {"ann_return": float((1 + usd_rf).prod() ** (12 / len(usd_rf)) - 1),
                                             "period": [str(usd_rf.index[0].date()), str(usd_rf.index[-1].date())]}
        ov = overlay_analysis(base_m, bmk["equity_jpy_unhedged_total"])
        comp["overlay"] = {"table": ov.to_dict(orient="records"), **ov.attrs}
        corr_ex = pd.concat([base_m, bmk["equity_usd_excess"]], axis=1).dropna()
        comp["corr_D_vs_equity_usd_excess"] = float(corr_ex.iloc[:, 0].corr(corr_ex.iloc[:, 1])) if len(corr_ex) > 12 else None
    results["comparison_dev"] = comp

    # historical windows (dev part)
    hw = {}
    for name, (a, b) in HIST_WINDOWS.items():
        seg = br[(br.index >= a) & (br.index <= b)]
        if len(seg) >= (10 if bpy >= 200 else 2):
            hw[name] = {"return": float((1 + seg).prod() - 1), "max_dd": float(((1 + seg).cumprod() / (1 + seg).cumprod().cummax() - 1).min()),
                        "n_bars": int(len(seg)), "markets_with_data": _markets_with_data(ds, a, b)}
        else:
            hw[name] = {"note": "開発期間外または該当データなし"}
    results["historical_windows_dev"] = hw

    # ---------------------------------------------------------------- holdout
    if unlock_holdout:
        hold_specs = [RunSpec("D_holdout_full", key, {}, start, ds.prices["date"].max().strftime("%Y-%m-%d")),
                      RunSpec("C_holdout_full", key, {"signal.lookbacks_bars": single}, start,
                              ds.prices["date"].max().strftime("%Y-%m-%d"))]
        hr = run_many(hold_specs, workers=2, keep_detail=True)
        d_h = hr["D_holdout_full"]
        seg = d_h["returns"][d_h["returns"].index >= hstart]
        c_seg = hr["C_holdout_full"]["returns"][hr["C_holdout_full"]["returns"].index >= hstart]
        s_h = summarize(seg, bpy)
        entry = record_holdout(key, d_h["config_hash"], results["data_hash"], s_h)
        hw_h = {}
        for name, (a, b) in HIST_WINDOWS.items():
            sg = seg[(seg.index >= a) & (seg.index <= b)]
            if len(sg) >= (10 if bpy >= 200 else 2):
                hw_h[name] = {"return": float((1 + sg).prod() - 1), "markets_with_data": _markets_with_data(ds, a, b)}
        results["holdout"] = {"summary": s_h, "C_summary": summarize(c_seg, bpy),
                              "yearly": {int(k): float(v) for k, v in ((1 + seg).groupby(seg.index.year).prod() - 1).items()},
                              "historical_windows": hw_h, "log_entry": entry,
                              "attribution_full": attribution(_as_result(d_h), instruments).to_dict(orient="records"),
                              "markets_in_holdout": _markets_with_data(ds, hstart, ds.prices["date"].max())}
        seg.to_frame("ret").to_csv(out_dir / "holdout_returns.csv")
        d_h["daily"].to_csv(out_dir / "full_period_daily.csv")
        d_h["positions"].to_csv(out_dir / "full_period_positions.csv")
        d_h["trades"].to_csv(out_dir / "full_period_trades.csv", index=False)
    # save base detail for the report
    base["daily"].to_csv(out_dir / "base_dev_daily.csv")
    base["positions"].to_csv(out_dir / "base_dev_positions.csv")
    base["trades"].to_csv(out_dir / "base_dev_trades.csv", index=False)
    base["market_pnl"].to_csv(out_dir / "base_dev_market_pnl.csv")
    pd.DataFrame({n: r["returns"] for n, r in runs.items() if n.startswith(("grid:", "C_"))}).to_csv(out_dir / "grid_returns.csv")
    (out_dir / "summary.json").write_text(json.dumps(results, indent=2, ensure_ascii=False, default=_json_default))
    return results


def _markets_with_data(ds, a, b) -> list[str]:
    p = ds.prices
    sel = p[(p["date"] >= pd.Timestamp(a)) & (p["date"] <= pd.Timestamp(b))]
    return sorted(sel["market"].unique().tolist())


def _as_result(r: dict):
    class _R:  # minimal adapter for metrics.attribution
        pass
    x = _R()
    x.market_pnl, x.market_cost, x.daily, x.notes = r["market_pnl"], r["market_cost"], r["daily"], r["notes"]
    return x


def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, (pd.Timestamp,)):
        return o.isoformat()
    if isinstance(o, float) and np.isnan(o):
        return None
    return str(o)
