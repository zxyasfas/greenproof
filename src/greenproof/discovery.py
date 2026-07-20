"""Find the test surface: the files pytest actually collects, plus config."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CONFIG_NAMES = ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml")

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".venv", "venv", "dist", "build", ".greenproof",
}


def stays_in_root(path: Path, root: Path) -> bool:
    """True if path resolves to somewhere under root.

    A leaf-level symlink check (is_symlink()) misses a symlinked or junction
    ancestor directory, and misses NTFS junctions entirely (they don't set
    the reparse tag is_symlink() looks for, and junctions need no elevated
    privilege to create on Windows). Resolving the full path and checking
    containment catches both, since resolve() follows the link either way.
    """
    root = root.resolve()
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _collect_test_files(root: Path) -> set:
    """Ask pytest which files it collects, so project config decides, not a guess."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
             "--collect-only", "-q", "--co"],
            cwd=str(root), capture_output=True, encoding="utf-8", errors="replace",
            env=dict(os.environ, NO_COLOR="1", PY_COLORS="0"), timeout=300,
        )
    except (subprocess.TimeoutExpired, OSError):
        return set()
    files = set()
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if "::" not in line:
            continue
        head = line.split("::", 1)[0]
        if head.endswith(".py"):
            files.add(Path(head).as_posix())
    return files


def _looks_like_test(rel: Path) -> bool:
    name = rel.name
    if name == "conftest.py":
        return True
    if not name.endswith(".py"):
        return False
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if name in ("test.py", "tests.py"):
        return True
    return any(part in ("tests", "test") for part in rel.parts)


def find_test_surface(root: Path) -> list[str]:
    root = Path(root).resolve()
    found = {
        f for f in _collect_test_files(root)
        if (root / f).is_file() and stays_in_root(root / f, root)
    }
    used_pytest = bool(found)

    for path in root.rglob("*"):
        if not path.is_file() or not stays_in_root(path, root):
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if rel.name in CONFIG_NAMES or rel.name == "conftest.py":
            found.add(rel.as_posix())  # config and conftest are always in scope
        elif not used_pytest and _looks_like_test(rel):
            found.add(rel.as_posix())  # only guess by name when pytest gave nothing

    return sorted(found)


def is_config_file(rel_path: str) -> bool:
    return Path(rel_path).name in CONFIG_NAMES
