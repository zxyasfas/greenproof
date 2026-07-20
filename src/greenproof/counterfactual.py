"""Run the agent's current code against the tests it started with (C1 + T0).

The counterfactual overlays the baseline test surface (tests and pytest config)
onto the working tree, keeps the current code, and re-runs pytest. Before it
touches anything it writes the current files to an on-disk restore journal, so a
crash mid-run is recoverable on the next invocation instead of losing edits.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .runner import RunResult, run_pytest
from .snapshot import baseline_file, load_manifest

SENTINEL = "ACTIVE.json"


@dataclass
class Counterfactual:
    claimed: RunResult    # C1 + T1
    original: RunResult   # C1 + T0


def _restore_dir(baseline_dir: Path) -> Path:
    return Path(baseline_dir).resolve().parent / "restore"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".gp-tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _safe_unlink(path: Path) -> None:
    if path.exists():
        if not os.access(path, os.W_OK):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        path.unlink()


def _begin_journal(root: Path, restore_dir: Path, files: list) -> None:
    if restore_dir.exists():
        shutil.rmtree(restore_dir)
    (restore_dir / "files").mkdir(parents=True)
    records = []
    for rel, content in files:
        existed = content is not None
        if existed:
            dst = restore_dir / "files" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(content)
        records.append({"path": rel, "existed": existed})
    (restore_dir / SENTINEL).write_text(
        json.dumps({"root": str(root), "files": records}), encoding="utf-8"
    )


def _replay_journal(restore_dir: Path) -> tuple:
    """Returns (restored_paths, failures). failures is (path, error) pairs."""
    sentinel = restore_dir / SENTINEL
    if not sentinel.exists():
        return [], []
    data = json.loads(sentinel.read_text(encoding="utf-8"))
    root = Path(data["root"])
    restored = []
    failures = []
    for rec in data["files"]:
        rel = rec["path"]
        target = root / rel
        try:
            if rec["existed"]:
                _atomic_write(target, (restore_dir / "files" / rel).read_bytes())
            else:
                _safe_unlink(target)
            restored.append(rel)
        except OSError as exc:
            failures.append((rel, str(exc)))
    if not failures:
        shutil.rmtree(restore_dir, ignore_errors=True)
    return restored, failures


def recover_if_interrupted(baseline_dir: Path) -> tuple:
    """Called at startup; restores a working tree left overwritten by a crash.

    Returns (restored_paths, failures).
    """
    return _replay_journal(_restore_dir(baseline_dir))


def run_counterfactual(baseline_dir: Path, root: Path) -> Counterfactual:
    root = Path(root)
    manifest = load_manifest(baseline_dir)
    all_files = [r["path"] for r in manifest["files"]]
    restore_dir = _restore_dir(baseline_dir)

    claimed = run_pytest(root)

    current = [((root / rel), rel) for rel in all_files]
    journal = [(rel, p.read_bytes() if p.exists() else None) for (p, rel) in current]

    _begin_journal(root, restore_dir, journal)
    try:
        for rel in all_files:
            _atomic_write(root / rel, baseline_file(baseline_dir, rel).read_bytes())
        original = run_pytest(root)
    finally:
        _restored, failures = _replay_journal(restore_dir)
        if failures:
            lines = "\n".join(f"  {rel}: {err}" for rel, err in failures)
            print(
                "greenproof could not restore these files. your versions are kept in\n"
                f"{restore_dir / 'files'} and will be restored on the next run:\n{lines}"
            )
    return Counterfactual(claimed=claimed, original=original)
