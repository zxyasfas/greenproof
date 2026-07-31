# greenproof

Check that a coding agent's green test run was actually earned, not manufactured by editing the tests.

An agent says the suite passes. Sometimes that's true. Sometimes it deleted the test that was failing, loosened an assertion, or changed a config flag so pytest stops enforcing something. `git diff` shows you the edit, but not whether your code would still pass the tests you started with.

greenproof answers that directly: it snapshots your tests before the agent runs, then re-runs the agent's code against the original tests afterward. If the code fails the tests it started with, the green it handed you depended on the test changes, not on the code.

## install

Not on PyPI yet.

```bash
git clone https://github.com/zxyasfas/greenproof
pip install -e ./greenproof
```

Needs Python 3.9+ and pytest. No network calls, no LLM, nothing leaves your machine.

## use it

```bash
greenproof snapshot          # before you point an agent at your repo
# ... let the agent work ...
greenproof verify             # after it says the suite is green
```

`verify` exits 2 specifically for `UNEARNED`, a caught false green. `NOT GREEN` and `INCONCLUSIVE` both exit 0, same as `EARNED` (see verdicts below), so wire your gate on exit code 2, not on "nonzero."

## what a real run looks like

Take a small library, ask an agent to "make the tests pass," and let it break the code instead of fixing it, then delete the tests that would catch that:

```
$ greenproof verify
UNEARNED: the green depended on the agent changing the tests

Current code against the original tests:
  fails:  tests/test_discount.py::test_rounding
  fails:  tests/test_discount.py::test_rejects_bad_pct

Test files the agent changed:
  tests/test_discount.py: removed test_rejects_bad_pct
```

The suite the agent handed back was green, but the code wasn't correct. `verify` catches the gap by actually running the old tests against the new code, not by guessing from the diff.

If the agent's changes are legitimate, `verify` says so:

```
$ greenproof verify
EARNED: the current code still passes the original tests
```

## how it decides

Three things happen, and only one of them determines the verdict.

1. `snapshot` copies your test files, conftests, and pytest config to `.greenproof/baseline` before the agent touches anything.
2. `verify` runs the agent's code against its own current tests (what it's claiming is green), then overlays the original snapshot back over the working tree, keeps the agent's code, and runs pytest again. Your current files are journaled first, so an interrupted run recovers your work instead of losing it.
3. The verdict comes from that second run's exit code. If the code fails the original tests, the run is `UNEARNED`. Everything else is supporting evidence, shown in the report but not driving the result: which specific tests were deleted, disabled, or had assertions removed. Full detail in `--json`.

Deleting a test is not proof of cheating on its own, sometimes a test really was wrong, and adding tests or refactoring them is not something the tool should punish. What settles it is whether the current code survives the tests it was supposed to satisfy.

## verdicts

- `EARNED`: the current code still passes the original tests.
- `UNEARNED`: it doesn't. The green depended on the test changes. Exit code 2.
- `INCONCLUSIVE`: the original tests don't even run against the current code (a genuine rename can look like this; see limits).
- `NOT GREEN`: the suite the agent handed you wasn't green to begin with.

## limits

Only pytest, for now. Config tampering is caught for `pytest.ini`, `setup.cfg`, `pyproject.toml`, and `tox.ini`, since the whole file is restored in the counterfactual run along with the tests, not just the pytest-relevant keys. That also means an agent's unrelated edits to `pyproject.toml` (a dependency bump, project metadata) get reverted for that one run too; they don't affect the verdict, but don't be surprised if you see them in the diff.

A real API rename (the function moves, the tests are updated to match) can produce the same import failure as an agent hiding a break, and both show up as `INCONCLUSIVE`. The tool tells you the original tests don't run against the current code; it doesn't try to guess why.

The per-test evidence (deleted, disabled, weakened assertions) is a static diff, not a proof. It's there so you can see what changed at a glance. The verdict is what to act on; the per-test list is just context for why.

## tested against

Ran this on five real open source projects (39 to 407 tests each: slugify, termcolor, python-string-utils, cachetools, flashtext), three scenarios per project: adding a real test, a no-op comment edit, and a constructed cheat (break a function, then delete exactly the tests that would have caught it). None of the legitimate edits were flagged, and every constructed cheat was caught. That's 15 hand-built scenarios, not a statistical benchmark, so treat it as "the mechanism holds up on real code" rather than a detection-rate claim.

The scenarios are in [validation/](validation/), as a script you can run: it clones each project at a pinned commit into its own virtualenv, builds the cheat by breaking a function and deleting whatever pytest then reports as failing, and records the verdict. The results are checked in under `validation/results/`, including the raw pytest output for every run.

## development

```bash
pip install -e .
python -m pytest tests/
```

## license

MIT
