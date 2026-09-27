"""Run manifest: records code version, config hash, data hashes, seed and library versions."""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .config import PROJECT_ROOT, config_hash


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def frame_sha256(df) -> str:
    """Deterministic hash of a DataFrame's content (index + values)."""
    import pandas as pd

    hashed = pd.util.hash_pandas_object(df, index=True).values
    return hashlib.sha256(hashed.tobytes()).hexdigest()


def git_info() -> dict:
    def run(*args):
        try:
            return subprocess.check_output(["git", *args], cwd=PROJECT_ROOT, stderr=subprocess.DEVNULL).decode().strip()
        except Exception:
            return None

    status = run("status", "--porcelain", "--", ".")
    return {"commit": run("rev-parse", "HEAD"), "dirty": bool(status) if status is not None else None}


def lib_versions() -> dict:
    import numpy, pandas, yaml, exchange_calendars  # noqa: E401

    return {
        "python": platform.python_version(),
        "numpy": numpy.__version__,
        "pandas": pandas.__version__,
        "pyyaml": yaml.__version__,
        "exchange_calendars": exchange_calendars.__version__,
    }


def build_manifest(run_name: str, cfg: dict, data_label: str, data_hashes: dict, extra: dict | None = None) -> dict:
    return {
        "run_name": run_name,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_label": data_label,
        "config_hash": config_hash(cfg),
        "config": cfg,
        "data_hashes": data_hashes,
        "code": git_info(),
        "libraries": lib_versions(),
        "seed": cfg.get("run", {}).get("seed"),
        **(extra or {}),
    }


def write_manifest(path: str | Path, manifest: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
