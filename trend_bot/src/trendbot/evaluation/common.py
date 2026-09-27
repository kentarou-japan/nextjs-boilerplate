"""Shared helpers for evaluation: dataset registry, parallel backtest runs, holdout lock, trial log."""
from __future__ import annotations

import json
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..backtest import BacktestResult, run_backtest
from ..config import CONFIG_DIR, PROJECT_ROOT, config_hash, load_assumptions, load_strategy_config, load_yaml, set_dotted
from ..contracts import load_instruments
from ..manifest import frame_sha256
from ..metrics import backtest_summary

REPORTS = PROJECT_ROOT / "reports"

DATASETS = {
    # key: (loader, config overrides, trading start, holdout key)
    "proxy_daily": {"overrides": [], "start": "2000-01-03", "holdout": "daily_proxy_start"},
    "proxy_monthly": {"overrides": [CONFIG_DIR / "strategy_monthly_proxy.yaml"], "start": "1973-01-31",
                      "holdout": "monthly_proxy_start"},
    "synthetic": {"overrides": [], "start": "2002-01-02", "holdout": "synthetic_start"},
}

_CACHE: dict = {}


def load_dataset(key: str):
    if key in _CACHE:
        return _CACHE[key]
    if key == "proxy_daily":
        from ..data.loaders import load_proxy_daily
        ds = load_proxy_daily()
    elif key == "proxy_monthly":
        from ..data.loaders import load_proxy_monthly
        ds = load_proxy_monthly()
    elif key == "synthetic":
        from ..data.synthetic import generate_synthetic
        ds = generate_synthetic(start="2000-01-03", end="2026-09-25", trend_strength=0.0)
    elif key == "real":
        from ..data.loaders import load_real_futures
        ds = load_real_futures()
    else:
        raise KeyError(key)
    _CACHE[key] = ds
    return ds


def base_config(key: str, dotted: dict | None = None) -> dict:
    spec = DATASETS[key]
    cfg = load_strategy_config(overrides=spec["overrides"], dotted=dotted)
    ds = load_dataset(key)
    cfg = set_dotted(cfg, "universe", [m for m in cfg["universe"] if m in ds.markets])
    return cfg


def holdout_start(key: str) -> pd.Timestamp:
    ss = load_yaml(CONFIG_DIR / "search_space.yaml")
    return pd.Timestamp(ss["holdout"][DATASETS[key]["holdout"]])


def dev_end(key: str) -> pd.Timestamp:
    return holdout_start(key) - pd.Timedelta(days=1)


@dataclass
class RunSpec:
    name: str
    dataset: str
    dotted: dict
    start: str
    end: str
    capital: float | None = None
    cost_multiplier: float = 1.0
    adv_multiplier: float = 1.0
    margin_multiplier: float = 1.0
    integer: bool = True
    universe: list | None = None


def execute(spec: RunSpec) -> dict:
    ds = load_dataset(spec.dataset)
    cfg = base_config(spec.dataset, spec.dotted)
    if spec.universe is not None:
        cfg = set_dotted(cfg, "universe", spec.universe)
    res: BacktestResult = run_backtest(ds, cfg, load_instruments(), load_assumptions(), start=spec.start, end=spec.end,
                                       capital=spec.capital, cost_multiplier=spec.cost_multiplier,
                                       adv_multiplier=spec.adv_multiplier, margin_multiplier=spec.margin_multiplier,
                                       integer=spec.integer)
    summ = backtest_summary(res)
    return {"name": spec.name, "spec": spec.__dict__, "config_hash": config_hash(cfg), "summary": summ,
            "returns": res.daily["ret"], "daily": res.daily, "positions": res.positions, "trades": res.trades,
            "market_pnl": res.market_pnl, "market_cost": res.market_cost, "decisions": res.decisions,
            "label": res.label, "notes": res.notes, "config": cfg}


def run_many(specs: list[RunSpec], workers: int | None = None, keep_detail: bool = False) -> dict[str, dict]:
    workers = workers or min(4, os.cpu_count() or 1)
    out = {}
    if workers <= 1 or len(specs) == 1:
        results = [execute(s) for s in specs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(execute, specs))
    for r in results:
        if not keep_detail:
            for k in ("positions", "trades", "decisions"):
                r.pop(k, None)
        out[r["name"]] = r
    return out


def append_trials(rows: list[dict], path: Path | None = None) -> None:
    """Every configuration evaluated is recorded, including rejected ones."""
    path = path or REPORTS / "trials_log.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    df = pd.DataFrame([{**r, "logged_utc": stamp} for r in rows])
    df.to_csv(path, mode="a", header=not path.exists(), index=False)


def record_holdout(key: str, cfg_hash: str, data_hash: str, summary: dict) -> dict:
    path = REPORTS / "holdout_log.json"
    log = json.loads(path.read_text()) if path.exists() else []
    prior = [e for e in log if e["dataset"] == key]
    entry = {"dataset": key, "config_hash": cfg_hash, "data_hash": data_hash,
             "evaluated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "n_prior_evaluations_same_dataset": len(prior), "summary": summary}
    log.append(entry)
    path.write_text(json.dumps(log, indent=2, ensure_ascii=False, default=str))
    return entry


def dataset_hash(key: str) -> str:
    ds = load_dataset(key)
    return frame_sha256(ds.prices[["date", "market", "contract", "settle"]])
