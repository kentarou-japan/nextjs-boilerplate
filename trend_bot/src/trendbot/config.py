"""Configuration loading, overriding and hashing.

All run-affecting parameters live in YAML files under ``config/``. A run's effective
configuration is the deep merge of the base file and optional override files plus
dotted-key overrides (used by parameter search). The SHA-256 of the canonical JSON of
the effective config is recorded in every run manifest.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


def load_yaml(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return data


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def set_dotted(cfg: dict, dotted: str, value: Any) -> dict:
    out = copy.deepcopy(cfg)
    node = out
    parts = dotted.split(".")
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            raise KeyError(f"unknown config section '{part}' in '{dotted}'")
        node = node[part]
    if parts[-1] not in node:
        raise KeyError(f"unknown config key '{dotted}'")
    node[parts[-1]] = copy.deepcopy(value)
    return out


def get_dotted(cfg: dict, dotted: str) -> Any:
    node = cfg
    for part in dotted.split("."):
        node = node[part]
    return node


def config_hash(cfg: dict) -> str:
    canonical = json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


REQUIRED_SECTIONS = ("run", "universe", "signal", "portfolio", "risk", "execution", "accounting", "data_quality")


def validate_strategy_config(cfg: dict) -> None:
    missing = [s for s in REQUIRED_SECTIONS if s not in cfg]
    if missing:
        raise ValueError(f"strategy config missing sections: {missing}")
    sig, port, risk, exe = cfg["signal"], cfg["portfolio"], cfg["risk"], cfg["execution"]
    if sig["transform"] not in ("sign", "clipped_z"):
        raise ValueError("signal.transform must be sign|clipped_z")
    if not sig["lookbacks_bars"] or any(int(x) <= 0 for x in sig["lookbacks_bars"]):
        raise ValueError("signal.lookbacks_bars must be positive")
    if not 0 < port["target_vol_annual"] < 0.5:
        raise ValueError("portfolio.target_vol_annual out of range")
    if abs(sum(port["class_budget"].values()) - 1.0) > 1e-9:
        raise ValueError("portfolio.class_budget must sum to 1")
    if exe["lag_bars"] < 1:
        # A lag of 0 would mean trading at the same close used for the decision.
        raise ValueError("execution.lag_bars must be >= 1 (no same-close fills)")
    if exe["trade_to"] not in ("edge", "target"):
        raise ValueError("execution.trade_to must be edge|target")
    for key in ("market_vol_cap", "class_vol_cap", "sector_vol_cap", "margin_usage_cap"):
        if not 0 < risk[key] <= 1.0:
            raise ValueError(f"risk.{key} out of range")
    levels = risk["drawdown_control"]["levels"]
    if any(levels[i][0] >= levels[i + 1][0] for i in range(len(levels) - 1)):
        raise ValueError("drawdown levels must be increasing")


def load_strategy_config(path: str | Path | None = None, overrides: list[str | Path] | None = None,
                         dotted: dict[str, Any] | None = None) -> dict:
    cfg = load_yaml(path or CONFIG_DIR / "strategy.yaml")
    for ov in overrides or []:
        cfg = deep_merge(cfg, load_yaml(ov))
    for key, value in (dotted or {}).items():
        cfg = set_dotted(cfg, key, value)
    validate_strategy_config(cfg)
    return cfg


def load_assumptions(path: str | Path | None = None) -> dict:
    return load_yaml(path or CONFIG_DIR / "assumptions.yaml")
