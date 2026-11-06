## Changed files
- backend/app/agent/retry_hint.py: new `format_retry_after` helper (stdlib only, integer ceil).
- backend/tests/test_retry_hint.py: unittest TestCase covering all plan cases.

## Gates
- `python3 -m unittest discover -s backend/tests -t backend -p test_retry_hint.py -v`: pass. Ran 6 tests, OK.

## Acceptance
- Gate prints OK with >= 6 tests, 0 failures: met.
- Only two new files besides .dev-kit/: met (no other files edited).

## Blockers
None.

## Notes for the reviewer
Ruff was not run (no venv checked). The 18 plan cases are table-driven across 6 test methods.
