"""Per-user abuse limits. Ported 1:1 from the AWS repo's numbers and semantics.

Three independent limiters, deliberately kept separate:

  * **token bucket** (`limits/rate`)   - burst control, capacity 30, refill 1/2s
  * **daily token cap** (`limits/cost`) - 200k tokens/day, UTC-midnight rollover
  * **daily image cap** (`limits/images`) - 50 images/day, independent rollover

The image cap is not a nicety. In the AWS original, image generation is ~90% of
spend, so a token-only cap leaves the dominant cost vector unbounded. Two vectors
with different abuse profiles get two counters that roll over on their own
schedules and can be tuned independently.

**Every limiter fails open.** A Firestore hiccup must not turn into a site-wide
outage -- the failure mode of "briefly un-rate-limited" is strictly better than
"nobody can use the product", and the daily caps still backstop real abuse.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from ..store import keys
from ..store.docstore import DocStore

log = logging.getLogger("whisk.guardrails")

MS_PER_DAY = 86_400_000


@dataclass(slots=True)
class LimitDecision:
    allowed: bool
    reason: str = ""
    retry_after_ms: int = 0
    remaining: int = 0
    used: int = 0
    capacity: int = 0

    @property
    def retry_after_seconds(self) -> int:
        return max(1, round(self.retry_after_ms / 1000))


def _utc_day_start(now_ms: int) -> int:
    dt = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def _ms_until_utc_midnight(now_ms: int) -> int:
    return max(1, _utc_day_start(now_ms) + MS_PER_DAY - now_ms)


class Guardrails:
    def __init__(
        self,
        store: DocStore,
        *,
        rate_capacity: int = 30,
        rate_refill_seconds: float = 2.0,
        daily_token_cap: int = 200_000,
        daily_image_cap: int = 50,
    ) -> None:
        self._store = store
        self._rate_capacity = rate_capacity
        self._rate_refill_seconds = rate_refill_seconds
        self._daily_token_cap = daily_token_cap
        self._daily_image_cap = daily_image_cap

    # --- burst control ----------------------------------------------------
    async def check_rate(self, uid: str, *, cost: int = 1) -> LimitDecision:
        """Lazy token bucket: refill is computed from elapsed time on read.

        No background job and no scheduled refill -- the bucket is a single
        document holding `tokens` + `lastRefillAt`, and the arithmetic happens
        at check time. That is what keeps this serverless: there is nothing
        running between requests.
        """
        now_ms = int(time.time() * 1000)
        path = keys.limits_doc(uid, keys.RATE)
        try:
            doc = await self._store.get(path) or {}
            tokens = float(doc.get("tokens", self._rate_capacity))
            last = int(doc.get("lastRefillAt", now_ms))

            elapsed_s = max(0.0, (now_ms - last) / 1000)
            refilled = min(
                float(self._rate_capacity), tokens + elapsed_s / self._rate_refill_seconds
            )

            if refilled < cost:
                deficit = cost - refilled
                return LimitDecision(
                    allowed=False,
                    reason="rate_limited",
                    retry_after_ms=int(deficit * self._rate_refill_seconds * 1000),
                    remaining=int(refilled),
                    capacity=self._rate_capacity,
                )

            await self._store.set(
                path,
                {"tokens": refilled - cost, "lastRefillAt": now_ms},
                merge=True,
            )
            return LimitDecision(
                allowed=True, remaining=int(refilled - cost), capacity=self._rate_capacity
            )
        except Exception:  # noqa: BLE001 - fail open, see module docstring
            log.exception("rate limiter failed open for uid=%s", uid)
            return LimitDecision(allowed=True, remaining=self._rate_capacity)

    # --- daily token cap ---------------------------------------------------
    async def check_daily_tokens(self, uid: str) -> LimitDecision:
        now_ms = int(time.time() * 1000)
        try:
            doc = await self._store.get(keys.limits_doc(uid, keys.COST)) or {}
            tokens_today = int(doc.get("tokensToday", 0))
            day_started = int(doc.get("dayStartedAt", 0))

            if day_started < _utc_day_start(now_ms):
                tokens_today = 0  # rolled over; the write happens on record()

            if tokens_today >= self._daily_token_cap:
                return LimitDecision(
                    allowed=False,
                    reason="daily_cap_reached",
                    retry_after_ms=_ms_until_utc_midnight(now_ms),
                    used=tokens_today,
                    capacity=self._daily_token_cap,
                )
            return LimitDecision(
                allowed=True,
                used=tokens_today,
                capacity=self._daily_token_cap,
                remaining=self._daily_token_cap - tokens_today,
            )
        except Exception:  # noqa: BLE001
            log.exception("daily token cap failed open for uid=%s", uid)
            return LimitDecision(allowed=True, capacity=self._daily_token_cap)

    async def record_tokens(self, uid: str, input_tokens: int, output_tokens: int) -> dict:
        """Accumulate spend. Includes compaction/summariser tokens by design --
        an internal model call the user did not type is still money spent, and
        excluding it creates a blind spot in the cap.
        """
        now_ms = int(time.time() * 1000)
        path = keys.limits_doc(uid, keys.COST)
        total = max(0, input_tokens) + max(0, output_tokens)
        try:
            doc = await self._store.get(path) or {}
            if int(doc.get("dayStartedAt", 0)) < _utc_day_start(now_ms):
                # New UTC day: reset the daily counter but keep lifetime totals.
                await self._store.set(
                    path,
                    {"tokensToday": 0, "dayStartedAt": _utc_day_start(now_ms)},
                    merge=True,
                )
            updated = await self._store.increment_and_read(
                path,
                {
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "tokensToday": total,
                    "turnCount": 1,
                },
                base={"dayStartedAt": _utc_day_start(now_ms)},
            )
            # Denormalised rollup so the admin dashboard is a bounded collection
            # read instead of a full-table scan (the AWS version scans).
            await self._store.set(
                f"{keys.COST_INDEX}/{keys.safe_id(uid, fallback='anonymous')}",
                {
                    "userId": uid,
                    "inputTokens": updated.get("inputTokens", 0),
                    "outputTokens": updated.get("outputTokens", 0),
                    "totalTokens": updated.get("inputTokens", 0) + updated.get("outputTokens", 0),
                    "turnCount": updated.get("turnCount", 0),
                    "lastUpdatedAt": now_ms,
                },
                merge=True,
            )
            return updated
        except Exception:  # noqa: BLE001
            log.exception("token telemetry failed for uid=%s", uid)
            return {}

    # --- daily image cap ---------------------------------------------------
    async def check_and_consume_image(self, uid: str) -> LimitDecision:
        """Check-and-consume in one atomic step.

        Split check/consume would let concurrent turns both observe 49/50 and
        both generate. `increment_and_read` is a transaction precisely so the
        winner is decided by the database, not by request ordering.
        """
        now_ms = int(time.time() * 1000)
        path = keys.limits_doc(uid, keys.IMAGES)
        try:
            doc = await self._store.get(path) or {}
            if int(doc.get("dayStartedAt", 0)) < _utc_day_start(now_ms):
                await self._store.set(
                    path, {"count": 0, "dayStartedAt": _utc_day_start(now_ms)}, merge=True
                )

            updated = await self._store.increment_and_read(
                path, {"count": 1}, base={"dayStartedAt": _utc_day_start(now_ms)}
            )
            count = int(updated.get("count", 0))
            if count > self._daily_image_cap:
                return LimitDecision(
                    allowed=False,
                    reason="image_daily_cap_reached",
                    retry_after_ms=_ms_until_utc_midnight(now_ms),
                    used=count - 1,
                    capacity=self._daily_image_cap,
                )
            return LimitDecision(
                allowed=True,
                used=count,
                capacity=self._daily_image_cap,
                remaining=self._daily_image_cap - count,
            )
        except Exception:  # noqa: BLE001
            log.exception("image cap failed open for uid=%s", uid)
            return LimitDecision(allowed=True, capacity=self._daily_image_cap)

    async def usage_snapshot(self, uid: str) -> dict:
        cost = await self._store.get(keys.limits_doc(uid, keys.COST)) or {}
        images = await self._store.get(keys.limits_doc(uid, keys.IMAGES)) or {}
        now_ms = int(time.time() * 1000)
        day_start = _utc_day_start(now_ms)
        images_today = int(images.get("count", 0)) if int(images.get("dayStartedAt", 0)) >= day_start else 0
        tokens_today = int(cost.get("tokensToday", 0)) if int(cost.get("dayStartedAt", 0)) >= day_start else 0
        return {
            "tokensToday": tokens_today,
            "tokenCap": self._daily_token_cap,
            "imagesToday": images_today,
            "imageCap": self._daily_image_cap,
            "inputTokens": int(cost.get("inputTokens", 0)),
            "outputTokens": int(cost.get("outputTokens", 0)),
            "turnCount": int(cost.get("turnCount", 0)),
            "resetsInMs": _ms_until_utc_midnight(now_ms),
        }
