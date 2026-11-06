## Verdict
approve. `format_retry_after` matches the frozen contract exactly, the ticket gate passes with system python3 (6 tests, OK), and the change is limited to the two owned new files.

## Must fix
None.

## Should fix
- `backend/tests/test_retry_hint.py:35-45`: `test_seconds`, `test_minutes` and `test_hours` repeat what `test_all_cases` (line 47) already checks, and they look values up through `dict(self.CASES)` with no `subTest`, so a failure there does not say which input failed. This is optional. Keep them, since they help the ">= 6 tests" acceptance count, or wrap each loop body in `self.subTest(ms=ms)`.

## Acceptance
- retry-hint: **met**.
  - Ticket gate: `python3 -m unittest discover -s backend/tests -t backend -p test_retry_hint.py -v`. I re-ran it: Ran 6 tests, OK, 0 failures.
  - Ownership: `plan_tool.py ownership` returns ok, with nothing unowned. `git diff <base>` only touches `.dev-kit/issue-4/plan.md` (the manifest's `approved` flag and reformatting). No existing source, config or `__init__.py` files changed.
  - Contract: I checked each branch at `backend/app/agent/retry_hint.py:9-26`. ms <= 0 returns "Try again now.". Rounding uses ceil (`-(-ms // 1000)`). The under-60 s, under-1 h and 1 h-or-more branches use the right divmod. The zero part is left out. Seconds are dropped in the hours branch. Singular and plural forms are correct. The 3599001 ms boundary case gives "1 hour".
  - All 18 required cases are in `CASES` and asserted with `subTest`.
  - Module style: docstring and `from __future__ import annotations`, stdlib only, no `app` imports.
  - Lint: `backend/.venv` exists, so I ran ruff on both files: All checks passed.

## Unverified
- Python 3.11 compatibility was not run on a 3.11 interpreter. The syntax used (f-strings, divmod, `list` literals) is 3.11-safe.

## Residual risks
- Float or non-int input is not validated, as the plan says. For example, `1.5` gives `-(-1.5 // 1000) == 1.0`, so the output is "Try again in 1.0 second.". Callers must pass an int.
- The helper is not wired into anything yet. Its rounding (ceil) differs from `LimitDecision.retry_after_seconds` (round), as the plan accepts.
