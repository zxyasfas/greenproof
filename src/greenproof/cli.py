from __future__ import annotations

import argparse
from pathlib import Path

from . import report
from .counterfactual import recover_if_interrupted, run_counterfactual
from .forensics import run_forensics
from .snapshot import take_snapshot

DEFAULT_BASELINE = ".greenproof/baseline"


def _snapshot(args) -> int:
    manifest = take_snapshot(args.repo, args.baseline)
    print(f"snapshot: captured {len(manifest['files'])} test/config files to {args.baseline}")
    return 0


def _verify(args) -> int:
    recovered = recover_if_interrupted(args.baseline)
    if recovered:
        print(f"recovered {len(recovered)} files from an interrupted earlier run")
    forensics = run_forensics(args.baseline, args.repo)
    cf = run_counterfactual(args.baseline, args.repo)
    if args.json:
        print(report.to_json(forensics, cf))
    else:
        print(report.to_text(forensics, cf), end="")
    return 2 if report.build_verdict(cf) == "unearned" else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="greenproof",
        description="Check that an agent earned its green test run instead of changing the tests.",
    )
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--baseline", type=Path, default=Path(DEFAULT_BASELINE))
    sub = parser.add_subparsers(dest="cmd")

    p_snap = sub.add_parser("snapshot", help="record the tests before the agent runs")
    p_snap.set_defaults(func=_snapshot)

    p_ver = sub.add_parser("verify", help="check the agent's run against the snapshot")
    p_ver.add_argument("--json", action="store_true")
    p_ver.set_defaults(func=_verify)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
