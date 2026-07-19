"""Run pytest and read back the exit code plus, for display, failed nodeids."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# pytest exit codes
OK = 0
TESTS_FAILED = 1
INTERRUPTED = 2
INTERNAL_ERROR = 3
USAGE_ERROR = 4
NO_TESTS = 5

_LINE = re.compile(r"^(FAILED|ERROR)\s+(\S.*?)(?: - .*)?$")


@dataclass
class RunResult:
    exit_code: int
    failed: list = field(default_factory=list)
    errored: list = field(default_factory=list)
    collected_error: bool = False
    timed_out: bool = False
    no_pytest: bool = False
    raw_tail: str = ""

    @property
    def green(self) -> bool:
        return self.exit_code == OK

    @property
    def status(self) -> str:
        if self.timed_out:
            return "timed-out"
        if self.no_pytest:
            return "pytest-not-found"
        return {
            OK: "passed", TESTS_FAILED: "failed", INTERRUPTED: "interrupted",
            INTERNAL_ERROR: "internal-error", USAGE_ERROR: "usage-error", NO_TESTS: "no-tests",
        }.get(self.exit_code, f"exit-{self.exit_code}")


def run_pytest(root: Path, timeout: int = 900) -> RunResult:
    # Keep the project's addopts so its test discovery (testpaths, python_files)
    # still works; the verdict is driven by the exit code, not by the parsed
    # summary, so a project that hides the summary cannot change the result.
    cmd = [
        sys.executable, "-m", "pytest",
        "-p", "no:cacheprovider", "--color=no", "--tb=no", "-q", "-rfE",
    ]
    env = dict(os.environ, NO_COLOR="1", PY_COLORS="0")
    try:
        proc = subprocess.run(
            cmd, cwd=str(root), capture_output=True,
            encoding="utf-8", errors="replace", env=env, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return RunResult(exit_code=INTERRUPTED, timed_out=True)

    out = (proc.stdout or "") + (proc.stderr or "")
    if "No module named pytest" in out:
        return RunResult(exit_code=proc.returncode, no_pytest=True, raw_tail=out[-800:])

    failed, errored = [], []
    for line in out.splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        (errored if m.group(1) == "ERROR" else failed).append(m.group(2))
    collected_error = "errors during collection" in out or bool(errored)
    return RunResult(
        exit_code=proc.returncode,
        failed=failed,
        errored=errored,
        collected_error=collected_error,
        raw_tail="\n".join(out.splitlines()[-15:]),
    )
