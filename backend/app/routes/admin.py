"""Admin surface: model picker and cost dashboard.

Ports the AWS repo's "Sanctum" with one deliberate change. The original answers
the cost dashboard with a DynamoDB `Scan` filtered to `AGENT#COST`, capped at 10
internal pages so misuse cannot burn read capacity -- a scan is the only option
without a GSI.

Here the write path maintains a denormalised `costIndex` collection (see
`guardrails.record_tokens`), so the dashboard is a bounded, ordered collection
read. Firestore charges per document read, and a scan-shaped query over every
user is exactly the access pattern that turns a $0 database into a surprising
one. Paying one extra small write per turn to avoid it is the right trade.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..agent.config_store import get_model_override, set_model_override
from ..auth import User, require_admin
from ..deps import Deps
from ..store import keys
from .common import get_deps

router = APIRouter(prefix="/api/admin")

# Published Gemini rates, USD per 1M tokens. Client-side arithmetic on purpose:
# this is for spotting outliers, not for billing, and pinning exact prices in
# code guarantees they go stale.
PRICE_PER_MTOK = {
    "input": 0.30,
    "output": 2.50,
}


@router.get("/agent/model")
async def get_model(deps: Deps = Depends(get_deps)) -> dict:
    """Public read, mirroring the original -- the active model is not a secret
    and the frontend shows it in the footer."""
    override = await get_model_override(deps)
    return {
        "modelId": override or deps.settings.agent_model,
        "default": deps.settings.agent_model,
        "overridden": bool(override),
        "mode": deps.settings.mode,
    }


class ModelRequest(BaseModel):
    modelId: str = ""


@router.put("/agent/model")
async def put_model(
    body: ModelRequest,
    _: User = Depends(require_admin),
    deps: Deps = Depends(get_deps),
) -> dict:
    return await set_model_override(deps, body.modelId)


@router.get("/agent/cost")
async def cost(
    limit: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_admin),
    deps: Deps = Depends(get_deps),
) -> dict:
    rows = await deps.store.list(keys.COST_INDEX, order_by="totalTokens", desc=True, limit=limit)
    items = []
    for row in rows:
        input_tokens = int(row.get("inputTokens", 0))
        output_tokens = int(row.get("outputTokens", 0))
        items.append(
            {
                "userId": row.get("userId", row.get("id")),
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "totalTokens": input_tokens + output_tokens,
                "turnCount": int(row.get("turnCount", 0)),
                "estimatedUsd": round(
                    input_tokens / 1e6 * PRICE_PER_MTOK["input"]
                    + output_tokens / 1e6 * PRICE_PER_MTOK["output"],
                    4,
                ),
                "lastUpdatedAt": row.get("lastUpdatedAt"),
            }
        )
    return {
        "items": items,
        "count": len(items),
        "pricePerMTok": PRICE_PER_MTOK,
        "note": "Estimate for spotting outliers, not billing.",
    }
