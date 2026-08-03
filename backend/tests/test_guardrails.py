"""Guardrail behaviour, including the failure modes that only matter under load."""

from __future__ import annotations

import asyncio

import pytest

from app.agent.guardrails import Guardrails, _utc_day_start
from app.store import keys
from app.store.docstore import InMemoryDocStore

UID = "u1"


async def test_token_bucket_exhausts_then_refills():
    store = InMemoryDocStore()
    guard = Guardrails(store, rate_capacity=3, rate_refill_seconds=2.0)

    for _ in range(3):
        assert (await guard.check_rate(UID)).allowed

    denied = await guard.check_rate(UID)
    assert not denied.allowed
    assert denied.reason == "rate_limited"
    assert denied.retry_after_ms > 0

    # Refill is computed from elapsed time at check time, not by a background
    # job -- so rewinding lastRefillAt is equivalent to waiting.
    doc = await store.get(keys.limits_doc(UID, keys.RATE))
    await store.set(
        keys.limits_doc(UID, keys.RATE),
        {"lastRefillAt": doc["lastRefillAt"] - 10_000},
        merge=True,
    )
    assert (await guard.check_rate(UID)).allowed


async def test_image_cap_blocks_beyond_capacity():
    guard = Guardrails(InMemoryDocStore(), daily_image_cap=2)

    assert (await guard.check_and_consume_image(UID)).allowed
    assert (await guard.check_and_consume_image(UID)).allowed

    denied = await guard.check_and_consume_image(UID)
    assert not denied.allowed
    assert denied.reason == "image_daily_cap_reached"
    assert denied.retry_after_ms > 0


async def test_image_cap_is_atomic_under_concurrency():
    """The reason check-and-consume is one transaction rather than two calls.

    Split check/consume lets concurrent turns both observe the last slot and both
    proceed. Exactly `cap` grants must survive regardless of interleaving.
    """
    guard = Guardrails(InMemoryDocStore(), daily_image_cap=5)

    results = await asyncio.gather(*(guard.check_and_consume_image(UID) for _ in range(20)))

    assert sum(1 for r in results if r.allowed) == 5


async def test_caps_roll_over_at_utc_midnight():
    store = InMemoryDocStore()
    guard = Guardrails(store, daily_image_cap=1)

    assert (await guard.check_and_consume_image(UID)).allowed
    assert not (await guard.check_and_consume_image(UID)).allowed

    # Backdate the day marker: the counter should reset on the next check.
    await store.set(
        keys.limits_doc(UID, keys.IMAGES),
        {"dayStartedAt": _utc_day_start(0) - 1},
        merge=True,
    )
    assert (await guard.check_and_consume_image(UID)).allowed


async def test_token_cap_blocks_when_spent():
    store = InMemoryDocStore()
    guard = Guardrails(store, daily_token_cap=100)

    assert (await guard.check_daily_tokens(UID)).allowed
    await guard.record_tokens(UID, 80, 40)  # 120 > 100

    denied = await guard.check_daily_tokens(UID)
    assert not denied.allowed
    assert denied.reason == "daily_cap_reached"


async def test_record_tokens_maintains_admin_rollup():
    """The dashboard reads a bounded rollup instead of scanning every user."""
    store = InMemoryDocStore()
    guard = Guardrails(store)

    await guard.record_tokens(UID, 100, 50)
    await guard.record_tokens(UID, 10, 5)

    rows = await store.list(keys.COST_INDEX)
    assert len(rows) == 1
    assert rows[0]["totalTokens"] == 165
    assert rows[0]["turnCount"] == 2


class ExplodingStore(InMemoryDocStore):
    async def get(self, path):
        raise RuntimeError("firestore is down")


@pytest.mark.parametrize(
    "check",
    ["check_rate", "check_daily_tokens", "check_and_consume_image"],
)
async def test_limiters_fail_open(check):
    """A database hiccup must not become a site-wide outage.

    "Briefly un-rate-limited" is strictly better than "nobody can use the
    product", and the daily caps still backstop sustained abuse.
    """
    guard = Guardrails(ExplodingStore())
    assert (await getattr(guard, check)(UID)).allowed
