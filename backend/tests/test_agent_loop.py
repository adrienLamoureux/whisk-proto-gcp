"""The ADK loop itself: routing, dispatch, the closing turn, and persistence.

These run the real `Runner` against the real tool fleet -- only the model is
stubbed -- so they exercise ADK's own dispatch and event streaming.
"""

from __future__ import annotations

import pytest

from app.agent.tools import CLIENT_ACTION_TOOLS, INTENT_TOOLS
from app.agent.whisk_runner import RateLimited
from app.store import keys

UID = "tester"


async def test_generate_image_dispatches_and_persists(agent_service, deps):
    result = await agent_service.run_turn(
        uid=UID, message="draw a red fox in the snow, 16:9", session_id="s1"
    )

    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.name == "generate_image"
    assert call.kind == "server_dispatch"
    assert call.error is None
    assert call.args["aspect"] == "16:9"
    assert call.result["imageUrl"]

    # A closing turn ran: the text reacts to the result rather than being empty.
    assert result.text
    assert result.output_tokens > 0

    media = await deps.store.list(keys.media_collection(UID))
    assert len(media) == 1
    assert media[0]["prompt"]


async def test_set_theme_is_a_client_action(agent_service):
    result = await agent_service.run_turn(
        uid=UID, message="switch to the glacier theme", session_id="s1"
    )
    call = result.tool_calls[0]
    assert call.name in CLIENT_ACTION_TOOLS
    assert call.kind == "client_action"
    assert call.result["theme"] == "glacier"


async def test_share_returns_a_proposal_and_writes_nothing(agent_service, deps):
    """Intent tools must not perform the action they describe."""
    await agent_service.run_turn(uid=UID, message="draw a boat", session_id="s1")
    media = await deps.store.list(keys.media_collection(UID))
    media_id = media[0]["mediaId"]

    result = await agent_service.run_turn(
        uid=UID, message=f"share image {media_id}", session_id="s1"
    )
    # The stub router has no share intent, so drive the tool directly -- the
    # contract under test is the tool's, not the router's.
    from app.agent.tools import build_tools

    tools = {t.__name__: t for t in build_tools(deps)}

    class Ctx:
        state = {"uid": UID}

    proposal = await tools["share_image"](media_id, tool_context=Ctx())

    assert proposal["requiresConfirm"] is True
    assert proposal["confirmEndpoint"].endswith(f"/{media_id}/share")
    assert "share_image" in INTENT_TOOLS
    assert await deps.store.list(keys.SHARED_MEDIA) == []  # nothing published
    assert result is not None


async def test_tools_reject_a_missing_identity(deps):
    """`uid` comes from session state, never a tool argument."""
    from app.agent.tools import build_tools

    tools = {t.__name__: t for t in build_tools(deps)}

    class Ctx:
        state: dict = {}

    with pytest.raises(PermissionError):
        await tools["generate_image"]("a fox", tool_context=Ctx())


async def test_image_cap_surfaces_as_a_tool_error(agent_service, deps):
    deps.guardrails._daily_image_cap = 1  # noqa: SLF001 - direct is clearer than a rebuild

    first = await agent_service.run_turn(uid=UID, message="draw a fox", session_id="s1")
    assert first.tool_calls[0].error is None

    second = await agent_service.run_turn(uid=UID, message="draw a wolf", session_id="s1")
    assert second.tool_calls[0].error == "image_daily_cap_reached"
    # A failed tool still gets a closing turn, so the user is never left silent.
    assert second.text


async def test_rate_limit_raises_before_the_model_runs(agent_service, deps):
    deps.guardrails._rate_capacity = 1  # noqa: SLF001
    deps.guardrails._rate_refill_seconds = 999.0  # noqa: SLF001

    await agent_service.run_turn(uid=UID, message="hello", session_id="s1")

    with pytest.raises(RateLimited) as excinfo:
        await agent_service.run_turn(uid=UID, message="hello again", session_id="s1")
    assert excinfo.value.decision.reason == "rate_limited"


async def test_conversation_survives_a_new_service_instance(agent_service, deps):
    """Sessions are durable, which is the whole point of the Firestore service.

    Cloud Run recycles instances between turns; an in-memory session service
    would lose the conversation here.
    """
    from app.agent.whisk_runner import AgentService

    await agent_service.run_turn(uid=UID, message="draw a fox", session_id="persist")

    fresh = AgentService(deps)  # simulates a cold container on the same data
    history = await fresh.history(UID, "persist")

    assert len(history) >= 2
    assert history[0]["role"] == "user"


async def test_turns_after_the_first_still_call_tools(agent_service):
    """Regression: scanning the whole conversation for `function_response` parts
    made every turn after the first look like a closing turn, silently disabling
    tool calling from turn two onward."""
    await agent_service.run_turn(uid=UID, message="what can you do?", session_id="s2")
    second = await agent_service.run_turn(uid=UID, message="draw a fox", session_id="s2")
    third = await agent_service.run_turn(uid=UID, message="show me my creations", session_id="s2")

    assert second.tool_calls[0].name == "generate_image"
    assert third.tool_calls[0].name == "recall_favorites"


async def test_unroutable_input_still_answers(agent_service):
    result = await agent_service.run_turn(uid=UID, message="asdfghjkl", session_id="s1")
    assert result.tool_calls == []
    assert result.text  # never an empty bubble
