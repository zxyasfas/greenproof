"""Copy the test surface into a baseline dir before an agent runs."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from .discovery import find_test_surface, stays_in_root

MANIFEST = "manifest.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def take_snapshot(root: Path, baseline_dir: Path) -> dict:
    root = Path(root).resolve()
    baseline_dir = Path(baseline_dir).resolve()
    # rmtree(baseline_dir) must never hit the repo itself or an ancestor of it.
    # A subdirectory of the repo (the default .greenproof/baseline) is fine.
    if baseline_dir == root or baseline_dir in root.parents:
        raise ValueError(f"baseline dir {baseline_dir} would delete the repo or a parent; pick another")
    files_dir = baseline_dir / "files"

    if baseline_dir.exists():
        if not (baseline_dir / MANIFEST).exists() and any(baseline_dir.iterdir()):
            raise ValueError(f"{baseline_dir} is not a greenproof baseline; refusing to overwrite it")
        shutil.rmtree(baseline_dir)
    files_dir.mkdir(parents=True)

    records = []
    for rel in find_test_surface(root):
        src = root / rel
        # find_test_surface already filters these out; re-checking here
        # is defense in depth against the file changing between that call
        # returning and this copy actually running.
        if not stays_in_root(src, root):
            continue
        dst = files_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        records.append({"path": rel, "sha256": _sha256(src)})

    manifest = {"root": str(root), "files": records}
    (baseline_dir / MANIFEST).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def load_manifest(baseline_dir: Path) -> dict:
    path = Path(baseline_dir) / MANIFEST
    if not path.exists():
        raise FileNotFoundError(
            f"no snapshot at {baseline_dir}. run `greenproof snapshot` before the agent."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def baseline_file(baseline_dir: Path, rel_path: str) -> Path:
    return Path(baseline_dir) / "files" / rel_path
