Commit read: c29118e

## Gates
- Ticket gate (repo root, system python3, no venv): `python3 -m unittest discover -s backend/tests -t backend -p test_retry_hint.py -v` (ticket.md:33)
- Repo lint: `cd backend && .venv/bin/ruff check app tests` (Makefile:35-36); ruff line-length 110 (backend/pyproject.toml, [tool.ruff])
- Repo tests: `make test` = pytest in backend venv + frontend build (Makefile). Not usable on fresh clone; new test must work under unittest.
- CI allows `python3 -m unittest:*` and `make lint/test` (.github/workflows/dev-kit.yml:176)
- No AGENTS.md / CLAUDE.md / CONTRIBUTING.md found.

## Relevant files
- CREATE backend/app/agent/retry_hint.py: `format_retry_after(retry_after_ms: int) -> str`, stdlib only, no `app` imports.
- CREATE backend/tests/test_retry_hint.py: `unittest.TestCase`.
- backend/app/agent/__init__.py exists and is empty (0 bytes); backend/tests/__init__.py exists. So `-t backend` makes the module `tests.test_retry_hint`. The test may import `app.agent.retry_hint` (top-level dir backend is on sys.path via -t). Importing `app.agent` runs only the empty `__init__`, and `app/__init__.py` should be checked for heavy imports (not read).
- Context only (do NOT edit): backend/app/agent/guardrails.py:38 `retry_after_ms: int = 0` in `LimitDecision`; :44-45 `retry_after_seconds` uses `max(1, round(ms/1000))` (rounds, not ceil; different from the ticket's round up).
- Producers of retryAfterMs (not to be wired): backend/app/agent/tools.py:93, backend/app/routes/media.py:56-58, backend/app/routes/common.py:37.

## Patterns to copy
- Module style: `from __future__ import annotations` and a docstring on line 1 (backend/tests/test_guardrails.py:1-3). Use the same in the new files.
- No existing unittest.TestCase tests; existing tests are pytest style (backend/tests/test_guardrails.py), and conftest.py is not loaded by unittest. Do not copy pytest fixtures or async tests.

## Shared shapes
- Input is the int `retry_after_ms` from `LimitDecision`. Output strings are fixed by ticket.md:12-18. No shared type, schema or config key is changed.
- Rounding: ceil to whole seconds, e.g. `-(-ms // 1000)` or `math.ceil(ms/1000)`. Prefer integer math: 1 -> 1s, 1001 -> 2s, 1000 -> 1s.
- Thresholds: <=0 "Try again now."; <60s seconds; 60..3599s minutes plus seconds if non-zero; >=3600s hours plus minutes if non-zero (seconds dropped). Singular only when count == 1. Each unit is separated by a space with no "and", ending with a period.

## Risks
- Python version: system python3 may differ from the project's >=3.11; keep syntax conservative.
- `unittest discover -p test_retry_hint.py` only collects TestCase classes; a pytest-style function would silently run 0 tests.
- Importing `app.agent.retry_hint` triggers `app/__init__.py` (not inspected); confirm it has no third-party imports, else the gate fails without a venv.
- Non-goal: no edits to existing files, deps or config. git status shows only untracked `.dev-kit/`.
- Hour-level edge: 3600s -> "Try again in 1 hour."; 3599s -> "Try again in 59 minutes 59 seconds."

## Unknowns
- Contents of backend/app/__init__.py (import side effects) are unchecked.
- The ticket doesn't say how huge or non-int input is handled; assume int only.
