# Issue #4: Human-readable retry hint for rate-limit responses

> slug: issue-4 · size: S · planned at c29118e on 2026-11-06

## Problem
The guardrails return `retry_after_ms` (backend/app/agent/guardrails.py:38) when a user is rate
limited, but nothing turns that number into a sentence a user can read. Add a pure, stdlib-only
helper `format_retry_after` in a new module, plus a `unittest.TestCase` test file. Done means the
ticket gate passes with the system `python3` (no venv).

## Non-goals
- Do not wire the helper into routes, `guardrails.py`, `tools.py` or the agent loop.
- Do not change any existing file, dependency or configuration (including `pyproject.toml`,
  `conftest.py`, `__init__.py` files).
- Do not "fix" `LimitDecision.retry_after_seconds` (guardrails.py:44-45), which rounds rather than ceils.
- No pytest-style tests, fixtures or async tests.

## Frozen contracts
None — slices share no interface (single slice). The public function the ticket fixes is:

### format_retry_after (produced by retry-hint; no consumers in this ticket)
```python
def format_retry_after(retry_after_ms: int) -> str: ...
```
Exact output strings (all end with a period, units separated by a single space, no "and"):
```text
ms <= 0                      -> "Try again now."
secs = ceil(ms / 1000)  (integer math: -(-ms // 1000))
secs < 60                    -> "Try again in {s} second(s)."
60 <= secs < 3600            -> "Try again in {m} minute(s)."  or  "Try again in {m} minute(s) {s} second(s)."  (seconds only if non-zero)
secs >= 3600                 -> "Try again in {h} hour(s)."    or  "Try again in {h} hour(s) {m} minute(s)."    (minutes only if non-zero; seconds dropped)
singular unit word when count == 1, plural ("seconds"/"minutes"/"hours") otherwise
```

## Slices

### retry-hint — Retry hint helper and unittest tests
- **Owns:** `backend/app/agent/retry_hint.py`, `backend/tests/test_retry_hint.py`
- **Depends on:** none
- **Does:**
  - Create `backend/app/agent/retry_hint.py`: module docstring on line 1, then
    `from __future__ import annotations` (style of backend/tests/test_guardrails.py:1-3). No imports
    besides the standard library; no imports from `app`. Integer math only, no `math` needed.
  - Implement `format_retry_after(retry_after_ms: int) -> str` exactly per the contract; a small
    private helper `_unit(count: int, word: str) -> str` returning e.g. `"1 second"` / `"2 seconds"` is fine.
    Hours branch: `h, rem = divmod(secs, 3600)`, `m = rem // 60`. Minutes branch: `m, s = divmod(secs, 60)`.
  - Keep lines <= 110 chars (ruff, backend/pyproject.toml) and syntax compatible with Python 3.11+.
  - Create `backend/tests/test_retry_hint.py`: docstring, `from __future__ import annotations`,
    `import unittest`, `from app.agent.retry_hint import format_retry_after` (all `__init__.py`
    files under backend/app and backend/tests are empty, so the import has no side effects).
  - One `class FormatRetryAfterTest(unittest.TestCase)` with test methods covering at least:
    `0 -> "Try again now."`, `-5 -> "Try again now."`, `1 -> "Try again in 1 second."`,
    `1000 -> "Try again in 1 second."`, `1001 -> "Try again in 2 seconds."`,
    `45000 -> "Try again in 45 seconds."`, `59000 -> "Try again in 59 seconds."`,
    `59001 -> "Try again in 1 minute."`, `60000 -> "Try again in 1 minute."`,
    `120000 -> "Try again in 2 minutes."`, `65000 -> "Try again in 1 minute 5 seconds."`,
    `61000 -> "Try again in 1 minute 1 second."`, `3599000 -> "Try again in 59 minutes 59 seconds."`,
    `3600000 -> "Try again in 1 hour."`, `3659000 -> "Try again in 1 hour."` (seconds dropped),
    `3660000 -> "Try again in 1 hour 1 minute."`, `9000000 -> "Try again in 2 hours 30 minutes."`,
    `7200000 -> "Try again in 2 hours."`.
  - Add `if __name__ == "__main__": unittest.main()` at the end.
- **Acceptance:** the ticket gate prints `OK` with >= 6 tests run and 0 failures; `git status`
  shows only the two new files (plus `.dev-kit/`).
- **Gates:** `python3 -m unittest discover -s backend/tests -t backend -p test_retry_hint.py -v`
- **Effort:** medium

## Gates
- Ticket gate (ticket.md:33), run from repo root with system python3:
  `python3 -m unittest discover -s backend/tests -t backend -p test_retry_hint.py -v` — run by retry-hint and as integration gate.
- Repo lint `cd backend && .venv/bin/ruff check app tests` (Makefile:35-36) needs a venv that a fresh
  clone lacks; run it only if `backend/.venv` exists. Not an integration gate.
- `make test` (pytest + frontend build) is not usable without a venv; not used.

## Merge order
1. retry-hint — new files only; no integration risk with existing code.

## Risks
- A pytest-style function instead of a `TestCase` method would make discover run 0 tests and still
  print OK; contained by acceptance requiring >= 6 tests run.
- Rounding differs from `LimitDecision.retry_after_seconds` (round vs ceil). Default: follow the
  ticket (ceil); leave guardrails.py untouched.
- Open question: handling of non-int / float input. Default: assume int; no type validation.
- Open question: "59 minutes 59.x seconds" rounding up across a unit boundary (e.g. 3599001 ms -> 3600 s).
  Default: ceil first, then format, so it yields "Try again in 1 hour."

## Manifest
```json plan-manifest
{
  "slug": "issue-4",
  "size": "S",
  "approved": false,
  "slices": [
    {
      "id": "retry-hint",
      "title": "Retry hint helper and unittest tests",
      "owns": ["backend/app/agent/retry_hint.py", "backend/tests/test_retry_hint.py"],
      "depends_on": [],
      "effort": "medium",
      "gates": ["python3 -m unittest discover -s backend/tests -t backend -p test_retry_hint.py -v"],
      "acceptance": [
        "ticket gate prints OK with >= 6 tests run and 0 failures",
        "git status shows only the two new files besides .dev-kit/"
      ]
    }
  ],
  "merge_order": ["retry-hint"],
  "integration_gates": ["python3 -m unittest discover -s backend/tests -t backend -p test_retry_hint.py -v"]
}
```
