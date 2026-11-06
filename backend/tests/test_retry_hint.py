"""Tests for the retry hint formatter."""
from __future__ import annotations

import unittest

from app.agent.retry_hint import format_retry_after


class FormatRetryAfterTest(unittest.TestCase):
    CASES = [
        (0, "Try again now."),
        (-5, "Try again now."),
        (1, "Try again in 1 second."),
        (1000, "Try again in 1 second."),
        (1001, "Try again in 2 seconds."),
        (45000, "Try again in 45 seconds."),
        (59000, "Try again in 59 seconds."),
        (59001, "Try again in 1 minute."),
        (60000, "Try again in 1 minute."),
        (120000, "Try again in 2 minutes."),
        (65000, "Try again in 1 minute 5 seconds."),
        (61000, "Try again in 1 minute 1 second."),
        (3599000, "Try again in 59 minutes 59 seconds."),
        (3600000, "Try again in 1 hour."),
        (3659000, "Try again in 1 hour."),
        (3660000, "Try again in 1 hour 1 minute."),
        (9000000, "Try again in 2 hours 30 minutes."),
        (7200000, "Try again in 2 hours."),
    ]

    def test_now(self) -> None:
        self.assertEqual(format_retry_after(0), "Try again now.")
        self.assertEqual(format_retry_after(-5), "Try again now.")

    def test_seconds(self) -> None:
        for ms in (1, 1000, 1001, 45000, 59000):
            self.assertEqual(format_retry_after(ms), dict(self.CASES)[ms])

    def test_minutes(self) -> None:
        for ms in (59001, 60000, 120000, 65000, 61000, 3599000):
            self.assertEqual(format_retry_after(ms), dict(self.CASES)[ms])

    def test_hours(self) -> None:
        for ms in (3600000, 3659000, 3660000, 9000000, 7200000):
            self.assertEqual(format_retry_after(ms), dict(self.CASES)[ms])

    def test_all_cases(self) -> None:
        for ms, expected in self.CASES:
            with self.subTest(ms=ms):
                self.assertEqual(format_retry_after(ms), expected)

    def test_rounds_up_across_boundary(self) -> None:
        self.assertEqual(format_retry_after(3599001), "Try again in 1 hour.")


if __name__ == "__main__":
    unittest.main()
