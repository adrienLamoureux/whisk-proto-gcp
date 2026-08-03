"""One agent turn, end to end.

Order of operations, and why:

    1. rate limit        -- cheapest rejection first; costs one Firestore read
    2. daily token cap   -- still free to check, and gates the expensive part
    3. load prefs        -- validated on read (state.py)
    4. build agent       -- instruction assembled per-turn, so prefs and mode apply
    5. run the ADK loop  -- model -> tool -> model, streamed as events
    6. record spend      -- including the closing turn, after the fact

Guardrails run *before* the model, never after: the point of a cap is to not
spend the money, and a check that runs post-response has already lost.

## On memory compaction

The AWS original compacts conversations above ~30 turns with a rolling summary,
billing the summariser's own tokens back to the user. That is the right design
and it is deliberately *not* ported here -- ADK owns conversation history through
the session service, so compaction belongs behind that interface (a
`ContextCacheConfig` or a custom `include_contents` policy), not bolted onto the
route. What is implemented is the cheap half: a bounded working set
(`MAX_EVENTS`) so context cannot grow without limit. See docs/roadmap.md.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from google.adk.runners import Runner
from google.genai import types

from ..deps import Deps
from ..store import keys
from .agent import APP_NAME, build_agent
from .config_store import get_model_override
from .guardrails import LimitDecision
from .session_service import FirestoreSessionService
from .state import read_prefs
from .tools import CLIENT_ACTION_TOOLS, INTENT_TOOLS, STATE_UID

log = logging.getLogger("whisk.turn")


@dataclass(slots=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None

    @property
    def kind(self) -> str:
        if self.name in CLIENT_ACTION_TOOLS:
            return "client_action"
        if self.name in INTENT_TOOLS:
            return "intent"
        return "server_dispatch"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "args": self.args,
            "result": self.result,
            "error": self.error,
        }


@dataclass(slots=True)
class TurnResult:
    text: str
    session_id: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "sessionId": self.session_id,
            "toolCalls": [t.to_dict() for t in self.tool_calls],
            "usage": {
                "inputTokens": self.input_tokens,
                "outputTokens": self.output_tokens,
                "totalTokens": self.input_tokens + self.output_tokens,
            },
            "latencyMs": self.latency_ms,
            "model": self.model,
        }


class RateLimited(Exception):
    def __init__(self, decision: LimitDecision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


class AgentService:
    """Owns the session service and executes turns."""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps
        self.sessions = FirestoreSessionService(deps.store)

    async def ensure_session(self, uid: str, session_id: str | None) -> str:
        sid = keys.safe_id(session_id)
        existing = await self.sessions.get_session(
            app_name=APP_NAME, user_id=uid, session_id=sid
        )
        if existing is None:
            await self.sessions.create_session(
                app_name=APP_NAME, user_id=uid, session_id=sid, state={STATE_UID: uid}
            )
        return sid

    async def list_sessions(self, uid: str) -> list[dict[str, Any]]:
        rows = await self._deps.store.list(
            keys.sessions_collection(uid), order_by="lastUsedAt", desc=True
        )
        return [
            {
                "sessionId": r.get("sessionId", r.get("id")),
                "name": r.get("name") or r.get("sessionId", r.get("id")),
                "createdAt": r.get("createdAt"),
                "lastUsedAt": r.get("lastUsedAt"),
            }
            for r in rows
        ]

    async def rename_session(self, uid: str, session_id: str, name: str) -> bool:
        sid = keys.safe_id(session_id)
        if await self._deps.store.get(keys.session_doc(uid, sid)) is None:
            return False
        await self._deps.store.set(keys.session_doc(uid, sid), {"name": name[:80]}, merge=True)
        return True

    async def delete_session(self, uid: str, session_id: str) -> None:
        await self.sessions.delete_session(
            app_name=APP_NAME, user_id=uid, session_id=keys.safe_id(session_id)
        )

    async def history(self, uid: str, session_id: str) -> list[dict[str, Any]]:
        """Flatten stored events into a renderable transcript."""
        sid = keys.safe_id(session_id)
        rows = await self._deps.store.list(
            keys.messages_collection(uid, sid), order_by="seq", desc=False
        )
        transcript: list[dict[str, Any]] = []
        for row in rows:
            event = row.get("event") or {}
            content = event.get("content") or {}
            parts = content.get("parts") or []
            text = " ".join(p["text"] for p in parts if p.get("text")).strip()
            if not text:
                continue
            transcript.append(
                {
                    "role": "user" if content.get("role") == "user" else "assistant",
                    "text": text,
                    "at": row.get("seq"),
                }
            )
        return transcript

    async def run_turn(
        self, *, uid: str, message: str, session_id: str | None = None, mode: str = "studio"
    ) -> TurnResult:
        started = time.perf_counter()
        guardrails = self._deps.guardrails

        rate = await guardrails.check_rate(uid)
        if not rate.allowed:
            raise RateLimited(rate)

        daily = await guardrails.check_daily_tokens(uid)
        if not daily.allowed:
            raise RateLimited(daily)

        sid = await self.ensure_session(uid, session_id)
        prefs = await read_prefs(self._deps.store, uid)
        override = await get_model_override(self._deps)

        agent = build_agent(self._deps, mode=mode, prefs=prefs, model=override)
        runner = Runner(
            app_name=APP_NAME,
            agent=agent,
            session_service=self.sessions,
        )

        result = TurnResult(text="", session_id=sid, model=agent.model or "")
        pending: dict[str, ToolCall] = {}

        try:
            async for event in runner.run_async(
                user_id=uid,
                session_id=sid,
                new_message=types.Content(role="user", parts=[types.Part(text=message)]),
                # Re-seeded every turn: a session created before auth changed, or
                # restored from Firestore, must still carry a trustworthy uid.
                state_delta={STATE_UID: uid},
            ):
                _absorb(event, result, pending)
        finally:
            await runner.close()

        result.tool_calls = list(pending.values())
        result.latency_ms = int((time.perf_counter() - started) * 1000)

        if not result.text:
            result.text = _fallback_text(result.tool_calls)

        await guardrails.record_tokens(uid, result.input_tokens, result.output_tokens)

        log.info(
            "agent.turn uid=%s session=%s latencyMs=%d tools=%s inTok=%d outTok=%d model=%s",
            uid, sid, result.latency_ms,
            ",".join(t.name for t in result.tool_calls) or "-",
            result.input_tokens, result.output_tokens, result.model,
        )
        return result


def _absorb(event: Any, result: TurnResult, pending: dict[str, ToolCall]) -> None:
    """Fold one ADK event into the accumulating turn result.

    Calls and responses arrive as separate events, correlated by function-call
    id, so tool calls are staged in `pending` and completed when their response
    shows up. Partial (streaming) events are skipped -- only the final text of
    each turn is kept, or the closing sentence would arrive duplicated.
    """
    usage = getattr(event, "usage_metadata", None)
    if usage:
        result.input_tokens += usage.prompt_token_count or 0
        result.output_tokens += usage.candidates_token_count or 0

    content = getattr(event, "content", None)
    if not content or not content.parts:
        return

    for part in content.parts:
        call = getattr(part, "function_call", None)
        if call is not None:
            key = getattr(call, "id", None) or call.name
            pending[key] = ToolCall(name=call.name, args=dict(call.args or {}))
            continue

        response = getattr(part, "function_response", None)
        if response is not None:
            key = getattr(response, "id", None) or response.name
            entry = pending.get(key) or ToolCall(name=response.name, args={})
            payload = response.response if isinstance(response.response, dict) else {}
            # ADK wraps a bare return value under "result"; unwrap so callers see
            # the dict the tool actually returned.
            if set(payload) == {"result"} and isinstance(payload["result"], dict):
                payload = payload["result"]
            if payload.get("error"):
                entry.error = str(payload["error"])
            entry.result = payload
            pending[key] = entry
            continue

        text = getattr(part, "text", None)
        if text and not getattr(event, "partial", False):
            result.text = (result.text + " " + text).strip() if result.text else text.strip()


def _fallback_text(tool_calls: list[ToolCall]) -> str:
    """Never return an empty bubble.

    A tool can succeed while the closing turn yields nothing (safety filter,
    truncation, a model having an off day). Silence reads as a crash, so
    synthesise something honest from what actually ran.
    """
    if not tool_calls:
        return "I'm not sure how to help with that yet — try asking me to draw something."
    failed = [t for t in tool_calls if t.error]
    if failed:
        return f"That didn't work — {failed[0].error.replace('_', ' ')}."
    return "Done."
