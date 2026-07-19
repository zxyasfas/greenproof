"""End-to-end: snapshot a tiny project, apply a change, check the verdict.

Each case builds a real project in a tmp dir and runs pytest in a subprocess
through the counterfactual, so these exercise the real mechanism.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from greenproof.counterfactual import run_counterfactual
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
