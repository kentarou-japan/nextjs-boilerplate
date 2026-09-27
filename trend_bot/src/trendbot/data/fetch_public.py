"""Download pinned public proxy datasets and record their hashes.

Only sources listed in config/datasets.yaml with a ``url`` are downloaded. URLs are pinned
to git commit SHAs so a re-download yields identical bytes; the manifest stores sha256 and
subsequent loads verify it (``verify_manifest``).
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ..config import CONFIG_DIR, PROJECT_ROOT, load_yaml
from ..manifest import file_sha256

RAW_DIR = PROJECT_ROOT / "data" / "raw"
MANIFEST = RAW_DIR / "manifest.json"


def _download(url: str, dest: Path, retries: int = 4) -> None:
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 (pinned https URL)
                data = resp.read()
            dest.write_bytes(data)
            return
        except Exception:
            if attempt == retries:
                raise
            time.sleep(delay)
            delay *= 2


def fetch_all(force: bool = False) -> dict:
    cfg = load_yaml(CONFIG_DIR / "datasets.yaml")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    for name, src in cfg["sources"].items():
        if "url" not in src:
            continue
        dest = RAW_DIR / f"{name}.csv"
        if dest.exists() and not force and name in manifest:
            continue
        _download(src["url"], dest)
        manifest[name] = {
            "url": src["url"],
            "file": dest.name,
            "sha256": file_sha256(dest),
            "bytes": dest.stat().st_size,
            "downloaded_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "primary_origin": src.get("primary_origin"),
            "license": src.get("license"),
        }
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    return manifest


def verify_manifest() -> dict[str, str]:
    """Return {source: sha256}; raise if a file changed since it was downloaded."""
    if not MANIFEST.exists():
        raise FileNotFoundError("data/raw/manifest.json not found. Run: trendbot fetch-data")
    manifest = json.loads(MANIFEST.read_text())
    out = {}
    for name, meta in manifest.items():
        path = RAW_DIR / meta["file"]
        digest = file_sha256(path)
        if digest != meta["sha256"]:
            raise ValueError(f"raw data {path} hash mismatch: manifest {meta['sha256']} vs file {digest}")
        out[name] = digest
    return out
