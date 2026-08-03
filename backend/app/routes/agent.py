"""Agent turn + session management.

`POST /api/agent/turn` is the counterpart of the AWS repo's Bedrock Converse
endpoint. It is intentionally **not** streaming, and that is the single largest
known gap -- see the note at the bottom of this file.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..agent.whisk_runner import AgentService, RateLimited
from ..auth import User, current_user
from ..deps import Deps
from .common import get_agent_service, get_deps, rate_limit_response

router = APIRouter(prefix="/api/agent")


class TurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    sessionId: str = "default"
    mode: str = "studio"


@router.post("/turn")
async def turn(
    body: TurnRequest,
    user: User = Depends(current_user),
    service: AgentService = Depends(get_agent_service),
) -> dict:
    try:
        result = await service.run_turn(
            uid=user.uid,
            message=body.message,
            session_id=body.sessionId,
            mode=body.mode,
        )
    except RateLimited as exc:
        raise rate_limit_response(exc) from exc
    return result.to_dict()


@router.get("/sessions")
async def list_sessions(
    user: User = Depends(current_user),
    service: AgentService = Depends(get_agent_service),
) -> dict:
    return {"items": await service.list_sessions(user.uid)}


class CreateSessionRequest(BaseModel):
    sessionId: str = Field(min_length=1, max_length=80)
    name: str = Field(default="", max_length=80)


@router.post("/sessions")
async def create_session(
    body: CreateSessionRequest,
    user: User = Depends(current_user),
    service: AgentService = Depends(get_agent_service),
) -> dict:
    sid = await service.ensure_session(user.uid, body.sessionId)
    if body.name:
        await service.rename_session(user.uid, sid, body.name)
    return {"session": {"sessionId": sid, "name": body.name or sid}}


class RenameSessionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.patch("/sessions/{session_id}")
async def rename_session(
    session_id: str,
    body: RenameSessionRequest,
    user: User = Depends(current_user),
    service: AgentService = Depends(get_agent_service),
) -> dict:
    if not await service.rename_session(user.uid, session_id, body.name):
        raise HTTPException(status_code=404, detail="session_not_found")
    return {"session": {"sessionId": session_id, "name": body.name}}


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    user: User = Depends(current_user),
    service: AgentService = Depends(get_agent_service),
) -> dict:
    """Deletes metadata *and* the conversation history under it.

    Leaving orphaned messages behind would be both a privacy problem and a slow
    Firestore leak, since nothing else ever collects them.
    """
    await service.delete_session(user.uid, session_id)
    return {"deleted": session_id}


@router.get("/sessions/{session_id}/history")
async def history(
    session_id: str,
    user: User = Depends(current_user),
    service: AgentService = Depends(get_agent_service),
) -> dict:
    return {"items": await service.history(user.uid, session_id)}


@router.get("/usage")
async def usage(
    user: User = Depends(current_user),
    deps: Deps = Depends(get_deps),
) -> dict:
    """The caller's own quota state -- lets the UI show remaining budget before
    the user hits a 429 rather than after."""
    return await deps.guardrails.usage_snapshot(user.uid)


# --- Known gap: streaming --------------------------------------------------
# `/turn` blocks until the full loop (model -> tool -> closing turn) completes,
# so a multi-second turn shows the user nothing. ADK's `Runner.run_async` already
# yields events incrementally, so the agent side is ready; what is missing is an
# SSE transport on this route and an EventSource client.
#
# It is not done here because it interacts with Cloud Run request buffering and
# would need its own testing pass. Cloud Run *does* support streaming responses,
# so unlike the AWS original (where Lambda behind API Gateway cannot stream and
# the fix requires moving to a Function URL) this is a straightforward change
# rather than an architectural one. See docs/roadmap.md.
