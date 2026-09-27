"""Command-line entry point: ``python -m trendbot.cli <command>`` (or ``trendbot`` if installed)."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .config import PROJECT_ROOT, load_assumptions


def cmd_fetch(args):
    from .data.fetch_public import fetch_all
    m = fetch_all(force=args.force)
    for k, v in m.items():
        print(f"{k:28s} {v['bytes']:>9d} bytes sha256={v['sha256'][:16]}")


def cmd_quality(args):
    from .contracts import load_instruments
    from .data.quality import fx_quality, run_quality
    from .evaluation.common import base_config, load_dataset
    ds = load_dataset(args.dataset)
    cfg = base_config(args.dataset)
    rep = run_quality(ds, cfg["data_quality"], load_instruments(),
                      as_of=pd.Timestamp(args.as_of) if args.as_of else None)
    out = PROJECT_ROOT / "reports" / args.dataset
    out.mkdir(parents=True, exist_ok=True)
    rep.issues.to_csv(out / "data_quality_issues.csv", index=False)
    rep.summary.to_csv(out / "data_quality_summary.csv", index=False)
    fxi = pd.DataFrame(fx_quality(ds.fx, cfg["data_quality"]))
    fxi.to_csv(out / "data_quality_fx.csv", index=False)
    print(f"label={ds.label}")
    print(json.dumps(rep.status, indent=1))
    print(rep.summary.to_string(index=False))
    print(f"FX issues: {len(fxi)}  → {out}")


def cmd_backtest(args):
    from .backtest import run_backtest
    from .contracts import load_instruments
    from .evaluation.common import base_config, dataset_hash, load_dataset
    from .manifest import build_manifest, write_manifest
    from .metrics import attribution, backtest_summary
    ds = load_dataset(args.dataset)
    cfg = base_config(args.dataset)
    ins = load_instruments()
    res = run_backtest(ds, cfg, ins, load_assumptions(), start=args.start, end=args.end, capital=args.capital)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = PROJECT_ROOT / "runs" / f"{stamp}_{args.dataset}"
    out.mkdir(parents=True, exist_ok=True)
    res.daily.to_csv(out / "daily.csv")
    res.positions.to_csv(out / "positions.csv")
    res.trades.to_csv(out / "trades.csv", index=False)
    s = backtest_summary(res)
    attribution(res, ins).to_csv(out / "attribution.csv", index=False)
    write_manifest(out / "manifest.json", build_manifest(cfg["run"]["name"], cfg, ds.label,
                                                          {"prices": dataset_hash(args.dataset), **ds.source_hashes},
                                                          {"start": args.start, "end": args.end, "summary": s}))
    print(json.dumps(s, indent=1, default=str))
    print(f"→ {out}")


def cmd_evaluate(args):
    from .evaluation.suite import evaluate_dataset
    from .report.html import evaluation_report
    r = evaluate_dataset(args.dataset, unlock_holdout=args.unlock_holdout, workers=args.workers, quick=args.quick)
    p = evaluation_report(PROJECT_ROOT / "reports" / args.dataset)
    print(json.dumps({"base_dev_sharpe": r["base_dev"].get("sharpe"), "walk_forward": r["walk_forward"]["decision"],
                      "pbo": r["walk_forward"]["pbo"].get("pbo"),
                      "holdout": r.get("holdout", {}).get("summary", "locked")}, indent=1, default=str, ensure_ascii=False))
    print(f"→ {p}")


def cmd_report(args):
    from .report.html import evaluation_report
    print(evaluation_report(PROJECT_ROOT / "reports" / args.dataset))


def cmd_blockers(args):
    from .contracts import load_instruments, production_blockers
    from .config import load_strategy_config
    cfg = load_strategy_config()
    b = production_blockers(load_instruments(), cfg["universe"], args.data_label, load_assumptions())
    df = pd.DataFrame(b)
    print(df.to_string(index=False))
    print(f"\n{len(b)} blockers — live trading must stay disabled while any remain.")


# ------------------------------------------------------------------------------ paper
def _paper(args):
    from .evaluation.common import load_dataset
    from .paper.broker import SimBroker
    from .paper.runner import PaperRunner, load_paper_config
    pcfg = load_paper_config(args.config)
    state = PROJECT_ROOT / (args.state_dir or pcfg["paper"]["state_dir"])
    key = pcfg["paper"]["dataset"]
    if key == "synthetic":
        from .data.synthetic import generate_synthetic
        sc = pcfg["paper"]["synthetic"]
        ds = generate_synthetic(markets=sc["markets"], start=sc["start"], end=sc["end"], seed=sc["seed"],
                                trend_strength=sc["trend_strength"])
    else:
        ds = load_dataset(key)
    from .config import load_strategy_config
    from .config import set_dotted
    scfg = load_strategy_config(PROJECT_ROOT / pcfg["paper"]["strategy_config"])
    scfg = set_dotted(scfg, "universe", [m for m in scfg["universe"] if m in ds.markets])
    broker = SimBroker(state / "broker.db")
    return PaperRunner(pcfg, ds, broker, state, strategy_cfg=scfg), broker, pcfg, ds


def cmd_paper(args):
    runner, broker, pcfg, ds = _paper(args)
    if args.action == "init":
        runner.initialize(float(pcfg["paper"]["initial_capital"]))
        print("initialized", runner.state_dir)
        return
    if args.action == "reconcile":
        print(json.dumps(runner.startup_reconcile(), indent=1, default=str, ensure_ascii=False))
        return
    if args.action == "run":
        chk = runner.startup_reconcile()
        print("startup_reconcile:", json.dumps(chk, default=str, ensure_ascii=False))
        dates = pd.DatetimeIndex(sorted(ds.prices["date"].unique()))
        dates = dates[(dates >= pd.Timestamp(args.start)) & (dates <= pd.Timestamp(args.end))]
        inject = dict(x.split("@") for x in (args.inject or []))   # fault@YYYY-MM-DD
        for d in dates:
            ds_ = d.strftime("%Y-%m-%d")
            for fault, day in inject.items():
                if day == ds_:
                    broker.inject(fault)
                    print(f"[inject] {fault} on {ds_}")
            rep = runner.run_day(d)
            if "disconnected" in broker.faults and args.auto_reconnect:
                broker.clear_faults()
            nav_s = "—" if rep.nav is None else f"{rep.nav:,.0f}"
            print(f"{rep.date} {rep.status:26s} nav={nav_s:>14} "
                  f"sent={rep.orders_sent} held={rep.orders_held} halts={[h['scope'] + ':' + h['reason'] for h in rep.halts]}")
        return
    if args.action == "status":
        print(json.dumps({"last_processed_date": runner.store.get("last_processed_date"),
                          "active_halts": runner.store.active_halts(),
                          "positions": {f"{m}/{c}": q for (m, c), q in runner.store.load_ledger().positions.items()},
                          "open_orders": runner.store.orders(("PENDING_SUBMIT", "SUBMITTED", "UNKNOWN", "AWAITING_APPROVAL"))},
                         indent=1, default=str, ensure_ascii=False))
        return
    if args.action == "approve":
        print("sent", runner.approve(args.operator))
        return
    if args.action == "resume":
        ids = [int(x) for x in args.halt_ids.split(",")] if args.halt_ids else None
        print(json.dumps(runner.resume(args.operator, ids), indent=1, default=str, ensure_ascii=False))
        return
    if args.action == "halt":
        runner.store.add_halt("global", "manual", args.reason or "manual halt", runner.store.get("last_processed_date"))
        print("halted")
        return
    if args.action == "dashboard":
        from .report.html import paper_dashboard
        print(paper_dashboard(runner.state_dir))
        return


def main(argv=None):
    p = argparse.ArgumentParser(prog="trendbot")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch-data"); f.add_argument("--force", action="store_true"); f.set_defaults(func=cmd_fetch)
    q = sub.add_parser("quality"); q.add_argument("--dataset", default="proxy_daily"); q.add_argument("--as-of")
    q.set_defaults(func=cmd_quality)
    b = sub.add_parser("backtest"); b.add_argument("--dataset", default="proxy_daily"); b.add_argument("--start")
    b.add_argument("--end"); b.add_argument("--capital", type=float); b.set_defaults(func=cmd_backtest)
    e = sub.add_parser("evaluate"); e.add_argument("--dataset", default="proxy_daily")
    e.add_argument("--unlock-holdout", action="store_true", help="evaluate the final holdout period (logged)")
    e.add_argument("--quick", action="store_true"); e.add_argument("--workers", type=int, default=4)
    e.set_defaults(func=cmd_evaluate)
    r = sub.add_parser("report"); r.add_argument("--dataset", default="proxy_daily"); r.set_defaults(func=cmd_report)
    bl = sub.add_parser("blockers"); bl.add_argument("--data-label", default="PROXY_PRELIMINARY"); bl.set_defaults(func=cmd_blockers)
    pp = sub.add_parser("paper")
    pp.add_argument("action", choices=["init", "run", "status", "reconcile", "approve", "resume", "halt", "dashboard"])
    pp.add_argument("--config"); pp.add_argument("--state-dir"); pp.add_argument("--start"); pp.add_argument("--end")
    pp.add_argument("--inject", nargs="*", help="fault@YYYY-MM-DD: disconnected, timeout_on_submit, position_drift, query_fails")
    pp.add_argument("--auto-reconnect", action="store_true", help="clear a simulated disconnect after one day")
    pp.add_argument("--operator", default="operator"); pp.add_argument("--halt-ids"); pp.add_argument("--reason")
    pp.set_defaults(func=cmd_paper)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
