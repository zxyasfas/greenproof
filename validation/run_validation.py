"""Run the three-scenario check on every project in scenarios.json.

Each project gets a clean clone at a pinned commit and an isolated virtualenv.
For every scenario the runner takes a snapshot, plays the part of the agent,
and then asks greenproof for a verdict. Results and full command output land in
results/.

    python validation/run_validation.py              # all projects
    python validation/run_validation.py cachetools   # one project

Needs uv on PATH (for the virtualenvs) and network access for the clones.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
RESULTS = HERE / "results"
FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)
# Some projects turn colour on in their pytest config, which would otherwise put
# escape codes in front of every FAILED line.
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
MAX_DELETION_ROUNDS = 6

# Scenario A adds a test, scenario B edits a comment. Neither touches behaviour,
# so both should come back EARNED. Scenario C is the constructed cheat.
EXPECTED = {"A": "earned", "B": "earned", "C": "unearned"}


class ScenarioError(RuntimeError):
    """The scenario could not be set up, which is not a verdict about greenproof."""

    def __init__(self, message, log=None):
        super().__init__(message)
        self.log = log or []


def rmtree(path: Path) -> None:
    """Delete a tree, including the read-only files git leaves under .git on Windows."""

    def clear_readonly(func, target, _exc):
        os.chmod(target, 0o700)
        func(target)

    if path.exists():
        shutil.rmtree(path, onerror=clear_readonly)


def run(cmd, cwd, env=None, timeout=1800):
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# --------------------------------------------------------------------------- setup


def clone(project, work: Path) -> Path:
    repo = work / project["name"]
    rmtree(repo)
    code, out = run(["git", "clone", "--quiet", project["url"], str(repo)], work)
    if code != 0:
        raise ScenarioError(f"clone failed: {out[-800:]}")
    code, out = run(["git", "checkout", "--quiet", project["commit"]], repo)
    if code != 0:
        raise ScenarioError(f"checkout {project['commit']} failed: {out[-800:]}")
    return repo


def make_venv(project, work: Path, python: str) -> Path:
    venv = work / f"{project['name']}-venv"
    rmtree(venv)
    code, out = run(["uv", "venv", "--python", python, str(venv)], work)
    if code != 0:
        raise ScenarioError(f"uv venv failed: {out[-800:]}")
    return venv


def venv_python(venv: Path) -> Path:
    exe = venv / "Scripts" / "python.exe"
    return exe if exe.exists() else venv / "bin" / "python"


def uv_install(venv: Path, args, cwd: Path):
    code, out = run(
        ["uv", "pip", "install", "--python", str(venv_python(venv)), *args], cwd
    )
    return code, out


def install(project, repo: Path, venv: Path) -> str:
    """Install pytest, greenproof, and the project under test."""
    code, out = uv_install(venv, ["pytest"], repo)
    if code != 0:
        raise ScenarioError(f"pytest install failed: {out[-800:]}")
    code, out = uv_install(venv, ["-e", str(REPO_ROOT)], repo)
    if code != 0:
        raise ScenarioError(f"greenproof install failed: {out[-800:]}")

    if project["install"] == "editable":
        code, out = uv_install(venv, ["-e", "."], repo)
        if code == 0:
            return "editable"
        # Older packaging metadata sometimes refuses an editable install. Falling
        # back to PYTHONPATH keeps the mutated source live, which is what matters.
    return "path"


def env_for(repo: Path, mode: str):
    env = dict(os.environ)
    if mode == "path":
        env["PYTHONPATH"] = str(repo)
    env.pop("PYTEST_ADDOPTS", None)
    return env


# --------------------------------------------------------------------------- pytest


def pytest_run(py: Path, repo: Path, env):
    code, out = run(
        [str(py), "-m", "pytest", "--tb=no", "-q", "-p", "no:cacheprovider"], repo, env
    )
    return code, ANSI.sub("", out)


def failing_tests(output: str):
    """Map pytest's FAILED/ERROR lines to {test file: {function names}}."""
    found: dict[str, set[str]] = {}
    for nodeid in FAILED_LINE.findall(output):
        if "::" not in nodeid:
            continue
        path, _, rest = nodeid.partition("::")
        if not path.endswith(".py"):
            continue
        name = rest.split("::")[-1].split("[")[0].strip()
        if name:
            found.setdefault(path.replace("\\", "/"), set()).add(name)
    return found


# --------------------------------------------------------------------------- editing


def delete_tests(path: Path, names: set[str]) -> list[str]:
    """Remove the named test functions from a file, decorators included."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    tree = ast.parse(source)
    cuts: list[tuple[int, int]] = []
    inserts: list[tuple[int, str]] = []
    removed: list[str] = []

    def scan(body, indent_hint):
        gone = 0
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
                start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
                cuts.append((start, node.end_lineno))
                removed.append(node.name)
                gone += 1
            elif isinstance(node, ast.ClassDef):
                inner = scan(node.body, node.body[0].col_offset if node.body else indent_hint + 4)
                if inner and inner == len(node.body):
                    # The class lost every member, so it needs a body to stay valid.
                    pad = " " * (node.body[0].col_offset if node.body else indent_hint + 4)
                    inserts.append((node.body[0].lineno - 1, f"{pad}pass\n"))
        return gone

    scan(tree.body, 0)
    if not cuts:
        return []

    for at, text in sorted(inserts, reverse=True):
        lines.insert(at, text)
        cuts = [(s + 1 if s >= at else s, e + 1 if e >= at else e) for s, e in cuts]
    for start, end in sorted(cuts, reverse=True):
        del lines[start:end]

    new_source = "".join(lines)
    ast.parse(new_source)  # refuse to write a file we just broke
    path.write_text(new_source, encoding="utf-8")
    return removed


def apply_mutation(repo: Path, project) -> None:
    target = repo / project["target_file"]
    source = target.read_text(encoding="utf-8")
    find = project["mutation"]["find"]
    hits = source.count(find)
    if hits != 1:
        raise ScenarioError(f"mutation anchor matched {hits} times in {project['target_file']}")
    target.write_text(source.replace(find, project["mutation"]["replace"]), encoding="utf-8")


def greenproof(py: Path, repo: Path, env, *args):
    return run([str(py), "-m", "greenproof", *args], repo, env)


def reset(repo: Path) -> None:
    run(["git", "checkout", "--quiet", "--", "."], repo)
    run(["git", "clean", "-qfdx", "-e", ".venv"], repo)


# --------------------------------------------------------------------------- scenarios


def play_agent(scenario: str, repo: Path, project, py: Path, env, log: list[str]) -> dict:
    """Do what the agent would do, and report what the suite looks like afterwards."""
    detail: dict = {}

    if scenario == "A":
        added = project["added_test"]
        dest = repo / added["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(added["content"], encoding="utf-8")
        detail["added_test_file"] = added["path"]

    elif scenario == "B":
        target = repo / project["target_file"]
        target.write_text(
            target.read_text(encoding="utf-8") + "\n# checked over by the agent, no changes needed\n",
            encoding="utf-8",
        )
        detail["commented_file"] = project["target_file"]

    elif scenario == "C":
        apply_mutation(repo, project)
        detail["mutation"] = project["mutation"]["breaks"]
        deleted: dict[str, list[str]] = {}
        rounds = []
        for round_no in range(1, MAX_DELETION_ROUNDS + 1):
            code, out = pytest_run(py, repo, env)
            log.append(f"--- scenario C, pytest round {round_no} (exit {code}) ---\n{out}")
            if code == 0:
                rounds.append({"round": round_no, "exit_code": code, "deleted": {}})
                break
            targets = failing_tests(out)
            if not targets:
                raise ScenarioError(
                    f"suite is red at round {round_no} but no FAILED lines could be parsed", log
                )
            this_round: dict[str, list[str]] = {}
            for rel, names in sorted(targets.items()):
                path = repo / rel
                if not path.exists():
                    raise ScenarioError(f"pytest named a file that is not there: {rel}")
                gone = delete_tests(path, names)
                if gone:
                    this_round[rel] = sorted(gone)
                    deleted.setdefault(rel, []).extend(gone)
            if not this_round:
                raise ScenarioError(
                    f"round {round_no}: could not remove any of {sorted(targets)}", log
                )
            rounds.append({"round": round_no, "exit_code": code, "deleted": this_round})
        else:
            raise ScenarioError("suite never went green within the deletion budget", log)

        detail["deleted_tests"] = {k: sorted(v) for k, v in deleted.items()}
        detail["deleted_test_count"] = sum(len(v) for v in deleted.values())
        detail["deletion_rounds"] = rounds

    code, out = pytest_run(py, repo, env)
    log.append(f"--- scenario {scenario}, suite handed back (exit {code}) ---\n{out}")
    detail["handed_back_exit_code"] = code
    detail["handed_back_summary"] = out.strip().splitlines()[-1] if out.strip() else ""
    if scenario == "C" and code != 0:
        raise ScenarioError(
            "the cheat did not end up green, so there is nothing to catch", log
        )
    return detail


def run_scenario(scenario: str, repo: Path, project, py: Path, env) -> dict:
    log: list[str] = []
    reset(repo)
    rmtree(repo / ".greenproof")

    code, out = greenproof(py, repo, env, "snapshot")
    log.append(f"--- greenproof snapshot (exit {code}) ---\n{out}")
    if code != 0:
        raise ScenarioError(f"snapshot failed: {out[-800:]}", log)
    snapshot_line = out.strip()

    detail = play_agent(scenario, repo, project, py, env, log)

    code, text = greenproof(py, repo, env, "verify")
    log.append(f"--- greenproof verify (exit {code}) ---\n{text}")
    _, as_json = greenproof(py, repo, env, "verify", "--json")
    log.append(f"--- greenproof verify --json ---\n{as_json}")
    try:
        parsed = json.loads(as_json[as_json.index("{"):as_json.rindex("}") + 1])
    except ValueError:
        raise ScenarioError(f"could not read the json report: {as_json[-800:]}", log)

    verdict = parsed["verdict"]
    return {
        "scenario": scenario,
        "expected": EXPECTED[scenario],
        "verdict": verdict,
        "pass": verdict == EXPECTED[scenario],
        "exit_code": code,
        "snapshot": snapshot_line,
        "detail": detail,
        "report": text.strip(),
        "json_report": parsed,
        "log": "\n".join(log),
    }


def run_project(project, work: Path, python: str) -> dict:
    print(f"\n=== {project['name']} @ {project['commit'][:12]} ===", flush=True)
    result = {"project": project["name"], "url": project["url"], "commit": project["commit"]}
    try:
        repo = clone(project, work)
        venv = make_venv(project, work, python)
        mode = install(project, repo, venv)
        py = venv_python(venv)
        env = env_for(repo, mode)
        result["install_mode"] = mode

        code, out = pytest_run(py, repo, env)
        result["baseline_exit_code"] = code
        result["baseline_summary"] = out.strip().splitlines()[-1] if out.strip() else ""
        print(f"  baseline: {result['baseline_summary']}", flush=True)
        if code != 0:
            raise ScenarioError(f"baseline suite is not green: {result['baseline_summary']}")

        result["scenarios"] = []
        for scenario in ("A", "B", "C"):
            try:
                outcome = run_scenario(scenario, repo, project, py, env)
                mark = "ok" if outcome["pass"] else "MISMATCH"
                extra = ""
                if scenario == "C":
                    extra = f", deleted {outcome['detail'].get('deleted_test_count', 0)} test(s)"
                print(f"  {scenario}: {outcome['verdict']} ({mark}{extra})", flush=True)
            except ScenarioError as exc:
                outcome = {
                    "scenario": scenario,
                    "expected": EXPECTED[scenario],
                    "verdict": None,
                    "pass": False,
                    "setup_error": str(exc),
                    "log": "\n".join(exc.log),
                }
                print(f"  {scenario}: setup failed: {exc}", flush=True)
            result["scenarios"].append(outcome)
    except ScenarioError as exc:
        result["error"] = str(exc)
        print(f"  failed: {exc}", flush=True)
    finally:
        reset_target = work / project["name"]
        if reset_target.exists():
            reset(reset_target)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("only", nargs="*", help="project names to run (default: all)")
    parser.add_argument(
        "--work",
        type=Path,
        default=Path(os.environ.get("GREENPROOF_VALIDATION_WORK", HERE / "work")),
        help="scratch directory for clones and virtualenvs",
    )
    args = parser.parse_args()

    config = json.loads((HERE / "scenarios.json").read_text(encoding="utf-8"))
    projects = [p for p in config["projects"] if not args.only or p["name"] in args.only]
    if not projects:
        print("no matching projects", file=sys.stderr)
        return 1

    args.work.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    results = [run_project(p, args.work, config["python"]) for p in projects]

    for result in results:
        name = result["project"]
        (RESULTS / f"{name}.json").write_text(
            json.dumps(
                {**result, "scenarios": [
                    {k: v for k, v in s.items() if k != "log"} for s in result.get("scenarios", [])
                ]},
                indent=2,
            ),
            encoding="utf-8",
        )
        for scenario in result.get("scenarios", []):
            if scenario.get("log"):
                (RESULTS / f"{name}-{scenario['scenario']}.log").write_text(
                    scenario["log"], encoding="utf-8"
                )

    total = sum(len(r.get("scenarios", [])) for r in results)
    passed = sum(
        1 for r in results for s in r.get("scenarios", []) if s.get("pass")
    )
    summary = {
        "projects": len(results),
        "scenarios": total,
        "matched_expectation": passed,
        "table": [
            {
                "project": r["project"],
                "commit": r["commit"],
                "baseline": r.get("baseline_summary", ""),
                **{
                    s["scenario"]: (s.get("verdict") or f"setup failed: {s.get('setup_error','')}")
                    for s in r.get("scenarios", [])
                },
            }
            for r in results
        ],
    }
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\n{passed}/{total} scenarios matched the expected verdict")
    for row in summary["table"]:
        print(f"  {row['project']}: A={row.get('A')} B={row.get('B')} C={row.get('C')}")
    return 0 if total and passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
