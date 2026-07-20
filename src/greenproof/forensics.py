"""Diff the baseline test surface against the current one, for evidence.

This is shown as supporting detail. The verdict comes from the counterfactual
run, not from these heuristics, so a false positive here cannot flag a run.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from .discovery import is_config_file

SKIP_DECORATORS = {"skip", "skipif", "xfail"}


@dataclass
class TestInfo:
    asserts: int
    disabled: bool
    trivial: bool


@dataclass
class FileChanges:
    path: str
    deleted_file: bool = False
    config_changed: bool = False
    deleted_tests: list = field(default_factory=list)
    disabled_tests: list = field(default_factory=list)
    weakened_tests: list = field(default_factory=list)  # (name, before, after)
    gutted_tests: list = field(default_factory=list)

    def has_changes(self) -> bool:
        return bool(
            self.deleted_file or self.config_changed or self.deleted_tests
            or self.disabled_tests or self.weakened_tests or self.gutted_tests
        )


def _test_name(name: str) -> bool:
    return name.startswith("test_") or name.endswith("_test")


def _decorator_names(node) -> set:
    names = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        while isinstance(target, ast.Attribute):
            names.add(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            names.add(target.id)
    return names


def _count_asserts(node) -> int:
    n = 0
    for child in ast.walk(node):
        if isinstance(child, ast.Assert):
            n += 1
        elif isinstance(child, ast.Call):
            f = child.func
            attr = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if attr.startswith("assert") or attr in ("fail", "raises"):
                n += 1
    return n


def _is_trivial_body(node) -> bool:
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    if not body:
        return True
    return all(
        isinstance(s, ast.Pass)
        or (isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None), (ast.Constant, ast.Ellipsis)))
        for s in body
    )


def _collect(node, prefix, out):
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and _test_name(child.name):
            out[prefix + child.name] = TestInfo(
                asserts=_count_asserts(child),
                disabled=bool(_decorator_names(child) & SKIP_DECORATORS),
                trivial=_is_trivial_body(child),
            )
        elif isinstance(child, ast.ClassDef) and child.name.startswith("Test"):
            _collect(child, child.name + "::", out)


def _parse_tests(source: str) -> dict:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out: dict = {}
    _collect(tree, "", out)
    return out


def compare_file(rel_path: str, baseline_src: str, current_src) -> FileChanges:
    changes = FileChanges(path=rel_path)
    if is_config_file(rel_path):
        if current_src is None or current_src != baseline_src:
            changes.config_changed = True
        return changes

    if current_src is None:
        changes.deleted_file = True
        return changes

    before = _parse_tests(baseline_src)
    after = _parse_tests(current_src)
    for name, b in before.items():
        a = after.get(name)
        if a is None:
            changes.deleted_tests.append(name)
        elif a.disabled and not b.disabled:
            changes.disabled_tests.append(name)
        elif a.trivial and not b.trivial:
            changes.gutted_tests.append(name)
        elif a.asserts < b.asserts:
            changes.weakened_tests.append((name, b.asserts, a.asserts))
    return changes


def run_forensics(baseline_dir: Path, root: Path) -> list:
    from .discovery import stays_in_root
    from .snapshot import baseline_file, load_manifest

    manifest = load_manifest(baseline_dir)
    root = Path(root).resolve()
    results = []
    for rec in manifest["files"]:
        rel = rec["path"]
        baseline_src = baseline_file(baseline_dir, rel).read_text(encoding="utf-8", errors="replace")
        cur_path = root / rel
        # a file replaced by a symlink or a junctioned ancestor directory
        # shouldn't have its link target read into the report
        readable = cur_path.exists() and stays_in_root(cur_path, root)
        current_src = cur_path.read_text(encoding="utf-8", errors="replace") if readable else None
        fc = compare_file(rel, baseline_src, current_src)
        if fc.has_changes():
            results.append(fc)
    return results
