"""Run the agent's current code against the tests it started with (C1 + T0).

The counterfactual overlays the baseline test surface (tests and pytest config)
onto the working tree, keeps the current code, and re-runs pytest. Before it
touches anything it writes the current files to an on-disk restore journal, so a
crash mid-run is recoverable on the next invocation instead of losing edits.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .discovery import stays_in_root
from .runner import RunResult, run_pytest
from .snapshot import baseline_file, load_manifest

SENTINEL = "ACTIVE.json"
LOCK_NAME = "LOCK"


class LockHeld(Exception):
    pass


@contextlib.contextmanager
def _exclusive_lock(restore_dir: Path):
    """Refuse to run a second counterfactual against the same baseline at once.

    Two overlapping runs would both overwrite the working tree with the
    baseline and then restore from their own journal, and whichever finishes
    its restore last wins, silently dropping the other run's version.

    The lock file lives next to restore_dir, not inside it: _begin_journal
    rmtree's restore_dir on every run, which would delete a lock file placed
    inside it and silently defeat the lock for the remainder of the run.
    """
    restore_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = restore_dir.parent / LOCK_NAME
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            holder = lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            holder = "unknown"
        raise LockHeld(
            f"another greenproof run (pid {holder}) is already using {restore_dir}. "
            f"if that process is gone, delete {lock_path} and retry."
        )
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        try:
            os.remove(lock_path)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise


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

    Returns (restored_paths, failures). Acquires the same lock run_counterfactual
    uses: if another run currently holds it, there's nothing to recover from
    (that run isn't interrupted, it's in progress), so this is a no-op rather
    than replaying a journal a live run might still be writing to.
    """
    restore_dir = _restore_dir(baseline_dir)
    try:
        with _exclusive_lock(restore_dir):
            return _replay_journal(restore_dir)
    except LockHeld:
        return [], []


def _read_current(path: Path, root: Path) -> bytes | None:
    # A test file replaced by a symlink, or an ancestor directory turned
    # into a symlink or NTFS junction, would otherwise have the link
    # target's content read straight into the local restore journal.
    # Treat anything that resolves outside root as absent rather than
    # following it. is_symlink() alone misses junctions and ancestor links.
    if not path.exists() or not stays_in_root(path, root):
        return None
    return path.read_bytes()


def run_counterfactual(baseline_dir: Path, root: Path) -> Counterfactual:
    root = Path(root)
    manifest = load_manifest(baseline_dir)
    all_files = [r["path"] for r in manifest["files"]]
    restore_dir = _restore_dir(baseline_dir)

    with _exclusive_lock(restore_dir):
        # claimed must run inside the lock too: if it ran before acquiring
        # the lock, a second run already mid-overlay could have the working
        # tree full of baseline content while this pytest run is reading it,
        # and claimed would silently reflect the wrong repo state.
        claimed = run_pytest(root)

        current = [((root / rel), rel) for rel in all_files]
        journal = [(rel, _read_current(p, root)) for (p, rel) in current]

        # a path that exists but escapes root is recorded as absent so we
        # never write through it; that means it won't come back on replay
        # either, since replay just unlinks whatever "absent" ends up at
        # that path afterward. warn now rather than let it vanish silently.
        escaped = [rel for (p, rel) in current if p.exists() and not stays_in_root(p, root)]
        if escaped:
            print(
                "greenproof will not restore these paths, they point outside "
                f"the repo and won't be written to or read back: {', '.join(escaped)}"
            )

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
