"""Shared route wiring.

`Deps` and `AgentService` are built once in the lifespan handler and hung on
`app.state`. Routes reach them through these dependencies rather than importing a
module-level singleton, which keeps `TestClient(build_app(fake_settings))` a
one-liner.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from ..agent.whisk_runner import AgentService, RateLimited
from ..deps import Deps


def get_deps(request: Request) -> Deps:
    return request.app.state.deps


def get_agent_service(request: Request) -> AgentService:
    return request.app.state.agent_service


def rate_limit_response(exc: RateLimited) -> HTTPException:
    """Translate a limiter rejection into 429 + Retry-After.

    `Retry-After` is in seconds per RFC 9110, while the decision carries
    milliseconds -- the body keeps the precise value so clients can show an
    accurate countdown.
    """
    decision = exc.decision
    return HTTPException(
        status_code=429,
        detail={
            "error": decision.reason,
            "retryAfterMs": decision.retry_after_ms,
            "used": decision.used,
            "capacity": decision.capacity,
        },
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )


__all__ = ["Depends", "get_deps", "get_agent_service", "rate_limit_response"]
