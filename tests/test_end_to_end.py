"""End-to-end: snapshot a tiny project, apply a change, check the verdict.

Each case builds a real project in a tmp dir and runs pytest in a subprocess
through the counterfactual, so these exercise the real mechanism.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from greenproof.counterfactual import LockHeld, _exclusive_lock, _restore_dir, run_counterfactual
from greenproof.report import build_verdict
from greenproof.snapshot import take_snapshot

CODE = "def add(a, b):\n    return a + b\n"
TESTS = textwrap.dedent(
    """
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from calc import add

    def test_add():
        assert add(2, 2) == 4

    def test_add_neg():
        assert add(-1, -1) == -2
    """
)


def _project(tmp: Path) -> Path:
    (tmp / "src").mkdir(parents=True)
    (tmp / "tests").mkdir(parents=True)
    (tmp / "src" / "calc.py").write_text(CODE, encoding="utf-8")
    (tmp / "tests" / "test_calc.py").write_text(TESTS, encoding="utf-8")
    return tmp


def _verdict(repo: Path, baseline: Path) -> str:
    return build_verdict(run_counterfactual(baseline, repo))


def test_unearned(tmp_path):
    repo = _project(tmp_path / "r")
    bl = tmp_path / "bl"
    take_snapshot(repo, bl)
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        TESTS.replace("assert add(2, 2) == 4", "assert True").replace(
            "assert add(-1, -1) == -2", "assert True"
        ),
        encoding="utf-8",
    )
    assert _verdict(repo, bl) == "unearned"
    assert not (repo / "tests" / "test_calc.py").read_text().count("assert add")  # T1 restored (still weakened)


def test_earned(tmp_path):
    repo = _project(tmp_path / "r")
    bl = tmp_path / "bl"
    take_snapshot(repo, bl)
    with (repo / "tests" / "test_calc.py").open("a", encoding="utf-8") as f:
        f.write("\ndef test_add_zero():\n    assert add(0, 0) == 0\n")
    assert _verdict(repo, bl) == "earned"


def test_inconclusive_on_import_break(tmp_path):
    repo = _project(tmp_path / "r")
    bl = tmp_path / "bl"
    take_snapshot(repo, bl)
    (repo / "src" / "calc.py").write_text("def plus(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        TESTS.replace("from calc import add", "from calc import plus as add"), encoding="utf-8"
    )
    assert _verdict(repo, bl) == "inconclusive"


def test_config_tampering_is_caught(tmp_path):
    # baseline treats warnings as errors; a test relies on that. the agent
    # loosens the config instead of fixing the code.
    repo = _project(tmp_path / "r")
    (repo / "pytest.ini").write_text("[pytest]\nfilterwarnings =\n    error\n", encoding="utf-8")
    (repo / "src" / "calc.py").write_text(
        "import warnings\n\ndef add(a, b):\n    warnings.warn('deprecated')\n    return a + b\n",
        encoding="utf-8",
    )
    bl = tmp_path / "bl"
    take_snapshot(repo, bl)
    # agent green: silence the warning gate in config, code still warns
    (repo / "pytest.ini").write_text("[pytest]\nfilterwarnings =\n    ignore\n", encoding="utf-8")
    assert _verdict(repo, bl) in ("unearned", "inconclusive")  # must not be 'earned'


def test_suppressed_summary_still_unearned(tmp_path):
    # a real cheat, with the project trying to hide the summary lines
    repo = _project(tmp_path / "r")
    (repo / "pytest.ini").write_text("[pytest]\naddopts = --no-summary\n", encoding="utf-8")
    bl = tmp_path / "bl"
    take_snapshot(repo, bl)
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        TESTS.replace("assert add(2, 2) == 4", "assert True").replace(
            "assert add(-1, -1) == -2", "assert True"
        ),
        encoding="utf-8",
    )
    assert _verdict(repo, bl) == "unearned"


def test_source_under_test_named_dir_is_not_touched(tmp_path):
    # repo path contains a 'test' segment; production code must not be treated
    # as test surface (regression for the absolute-path misclassification)
    repo = _project(tmp_path / "test" / "proj")
    bl = tmp_path / "bl"
    take_snapshot(repo, bl)
    from greenproof.discovery import find_test_surface
    surface = find_test_surface(repo)
    assert "src/calc.py" not in surface
    assert "tests/test_calc.py" in surface


def test_concurrent_run_is_rejected(tmp_path):
    repo = _project(tmp_path / "r")
    bl = tmp_path / "bl"
    take_snapshot(repo, bl)
    restore_dir = _restore_dir(bl)
    with _exclusive_lock(restore_dir):
        try:
            run_counterfactual(bl, repo)
            raised = False
        except LockHeld:
            raised = True
    assert raised


def test_lock_survives_begin_journal_clearing_restore_dir(tmp_path):
    # _begin_journal rmtree's restore_dir on every run; the lock must live
    # outside it or a second run's LockHeld check would silently stop firing
    # partway through the first run.
    repo = _project(tmp_path / "r")
    bl = tmp_path / "bl"
    take_snapshot(repo, bl)
    restore_dir = _restore_dir(bl)
    with _exclusive_lock(restore_dir):
        lock_path = restore_dir.parent / "LOCK"
        assert lock_path.exists()
        from greenproof.counterfactual import _begin_journal
        _begin_journal(repo, restore_dir, [("tests/test_calc.py", b"x")])
        assert lock_path.exists(), "lock file must not be deleted by _begin_journal's rmtree"


def test_recover_is_noop_while_lock_held(tmp_path):
    repo = _project(tmp_path / "r")
    bl = tmp_path / "bl"
    take_snapshot(repo, bl)
    restore_dir = _restore_dir(bl)
    from greenproof.counterfactual import _begin_journal, recover_if_interrupted
    _begin_journal(repo, restore_dir, [("tests/test_calc.py", b"stale-journal-content")])
    with _exclusive_lock(restore_dir):
        restored, failures = recover_if_interrupted(bl)
    assert restored == [] and failures == []


def test_read_current_rejects_a_path_that_resolves_outside_root(tmp_path):
    # a leaf-level is_symlink() check misses an ancestor directory that's a
    # symlink or an NTFS junction (junctions don't set the reparse tag
    # is_symlink() looks for, and need no elevated privilege to create).
    # this drives the actual containment check (stays_in_root) with real
    # paths rather than mocking is_symlink(), so it also proves the guard
    # catches escapes that a symlink-only check would miss.
    from greenproof.counterfactual import _read_current

    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("do-not-leak-me", encoding="utf-8")

    inside_path = root / "tests" / "test_x.py"
    assert _read_current(inside_path, root) is None  # doesn't exist, not a leak

    escaped_path = tmp_path / "outside.py"  # resolves outside root entirely
    assert _read_current(escaped_path, root) is None

    real_test = root / "tests" / "test_real.py"
    real_test.parent.mkdir(parents=True)
    real_test.write_text("legit-content", encoding="utf-8")
    assert _read_current(real_test, root) == b"legit-content"


def test_junction_ancestor_does_not_leak_into_journal(tmp_path):
    # real integration test: create an actual NTFS junction (no elevated
    # privilege needed on Windows, unlike symlinks) and drive it through
    # run_counterfactual end to end, not just the containment helper in
    # isolation.
    import subprocess

    repo = _project(tmp_path / "r")
    bl = tmp_path / "bl"
    take_snapshot(repo, bl)

    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "secret.py").write_text("do-not-leak-me", encoding="utf-8")

    junctioned = repo / "tests" / "junctioned"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junctioned), str(outside)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return  # not on Windows or mklink unavailable; nothing to test here

    run_counterfactual(bl, repo)
    restore_dir = _restore_dir(bl)
    for f in (restore_dir / "files").rglob("*"):
        if f.is_file():
            assert "do-not-leak-me" not in f.read_text(encoding="utf-8", errors="replace")


def test_claimed_run_happens_after_the_lock_is_held(tmp_path):
    # A-1 regression: claimed must run inside the lock. If it ran first, a
    # second process mid-overlay could leave the working tree full of
    # baseline content while this run's claimed pytest pass reads it,
    # silently corrupting the verdict. Patch run_pytest and check the lock
    # file's actual on-disk state at the moment of its first call (claimed),
    # so this is a runtime assertion, not a source-text check.
    from unittest.mock import patch
    from greenproof import counterfactual as C

    repo = _project(tmp_path / "r")
    bl = tmp_path / "bl"
    take_snapshot(repo, bl)
    lock_path = _restore_dir(bl).parent / "LOCK"

    lock_existed_at_first_call = []
    real_run_pytest = C.run_pytest

    def spy(root):
        if not lock_existed_at_first_call:
            lock_existed_at_first_call.append(lock_path.exists())
        return real_run_pytest(root)

    with patch.object(C, "run_pytest", side_effect=spy):
        run_counterfactual(bl, repo)

    assert lock_existed_at_first_call == [True]
