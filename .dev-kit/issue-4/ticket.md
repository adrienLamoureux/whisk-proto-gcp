# Issue #4: Human-readable retry hint for rate-limit responses

The guardrails return `retry_after_ms` when a user is rate limited, but nothing turns that
number into a sentence a user can read. Add a small, pure helper for it.

## Requirements

1. Create `backend/app/agent/retry_hint.py` exposing
   `format_retry_after(retry_after_ms: int) -> str`. Standard library only: no third-party
   imports, no imports from the rest of `app`.
2. Behaviour (round the total up to whole seconds first):
   - `retry_after_ms <= 0` returns `"Try again now."`
   - under 60 seconds: `"Try again in 1 second."`, `"Try again in 45 seconds."`
   - 60 seconds up to 3599 seconds: minutes, plus seconds only when non-zero, for example
     `"Try again in 2 minutes."`, `"Try again in 1 minute 5 seconds."`
   - 3600 seconds or more: hours, plus minutes only when non-zero, seconds dropped, for example
     `"Try again in 1 hour."`, `"Try again in 2 hours 30 minutes."`
   - singular for a count of 1, plural otherwise.
3. Create `backend/tests/test_retry_hint.py` covering every bullet above, including the
   rounding up (`1` ms gives 1 second, `1001` ms gives 2 seconds) and the zero / negative case.
   Write the tests with `unittest.TestCase`, because a fresh clone has no project virtualenv
   and the gate below must run with the system `python3`.

## Non-goals

- Do not wire the helper into routes, `guardrails.py` or the agent loop.
- Do not change any existing file, dependency or configuration.

## Gate

Run from the repository root:

    python3 -m unittest discover -s backend/tests -t backend -p test_retry_hint.py -v

