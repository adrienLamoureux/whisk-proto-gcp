"""Runtime model override, cached in-process.

Ports the AWS repo's `CONFIG#AGENT` record: an admin can repoint the agent at a
different model without a redeploy, and the read path is cached for 60s so the
hot turn path does not pay a database read per request.

The cache is per-instance, which is correct rather than sloppy: Cloud Run
instances are independent, so a change propagates within one TTL everywhere. A
shared cache would mean Memorystore, which is an always-on Redis instance and
therefore a standing bill for something a 60s staleness window already solves.
"""

from __future__ import annotations

import logging
import time

from ..deps import Deps
from ..store import keys

log = logging.getLogger("whisk.agent_config")

_cache: dict[str, tuple[float, str]] = {}


async def get_model_override(deps: Deps) -> str | None:
    """Return the admin-selected model id, or None to use the configured default."""
    ttl = deps.settings.agent_config_cache_seconds
    now = time.monotonic()

    cached = _cache.get("model")
    if cached and now - cached[0] < ttl:
        return cached[1] or None

    try:
        doc = await deps.store.get(keys.AGENT_CONFIG) or {}
    except Exception:  # noqa: BLE001 - config read must never fail a turn
        log.exception("agent config read failed; using default model")
        return None

    model = str(doc.get("modelId") or "").strip()
    _cache["model"] = (now, model)
    return model or None


async def set_model_override(deps: Deps, model_id: str) -> dict[str, object]:
    """Persist the override. Blank input resets to the deploy-time default."""
    cleaned = (model_id or "").strip()
    await deps.store.set(keys.AGENT_CONFIG, {"modelId": cleaned}, merge=True)
    _cache["model"] = (time.monotonic(), cleaned)
    return {
        "modelId": cleaned or deps.settings.agent_model,
        "reset": not cleaned,
        "default": deps.settings.agent_model,
    }


def invalidate_cache() -> None:
    _cache.clear()
