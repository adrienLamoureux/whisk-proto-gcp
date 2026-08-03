"""An offline `BaseLlm` so the *real* ADK agent loop runs with no Vertex AI call.

This is the single most useful thing in the repo for learning ADK, so it is worth
being precise about what is and is not faked.

**Real:** `Runner`, `LlmAgent`, the session service, event streaming, tool
dispatch, argument marshalling from the model's `FunctionCall`, the
`function_response` round-trip, state deltas, and the second "closing" turn after
a tool returns. Every one of those is ADK's own code path, unmodified.

**Faked:** only `generate_content_async` -- the network call to Gemini. In its
place, a keyword router picks a tool and fabricates a `FunctionCall` part with
the same shape Gemini would emit.

Swapping `WHISK_MODE=local` -> `gcp` replaces this class with `Gemini` and
changes nothing else. If a bug reproduces under the stub, it is a bug in the
agent wiring, not in the prompt.

The router is intentionally dumb (substring matching, first rule wins). It is a
test double, not a model -- making it clever would make failures ambiguous.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types

log = logging.getLogger("whisk.stub_llm")

STUB_MODEL_NAME = "whisk-stub-router"
CRITIC_MODEL_NAME = "whisk-stub-critic"

# Rough parity with Gemini's tokenizer for cost-telemetry testing: ~4 chars/token.
CHARS_PER_TOKEN = 4


class StubLlm(BaseLlm):
    """Keyword-routed stand-in for Gemini. Registered under `whisk-stub-*`.

    Behaviour is keyed off `self.model` so the studio agent and the critic agent
    share one class: `whisk-stub-router` drives the tool fleet,
    `whisk-stub-critic` produces a rule-based prompt critique.
    """

    model: str = STUB_MODEL_NAME

    @staticmethod
    def supported_models() -> list[str]:
        return [r"whisk-stub-.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if self.model == CRITIC_MODEL_NAME:
            from .critic import rule_based_critique

            critique = rule_based_critique(_last_user_text(llm_request))
            yield _text_response(critique["critique"], llm_request)
            return

        available = set(llm_request.tools_dict or {})
        last_user_text = _last_user_text(llm_request)
        tool_results = _tool_results(llm_request)

        if tool_results:
            # Second pass: a tool already ran, so this is the closing turn. The
            # real agent is instructed to keep it to one short sentence; the stub
            # honours the same contract so token accounting stays comparable.
            yield _text_response(_closing_line(tool_results), llm_request)
            return

        call = _route(last_user_text, available)
        if call is None:
            yield _text_response(_fallback_line(last_user_text), llm_request)
            return

        name, args = call
        log.debug("stub routed %r -> %s(%s)", last_user_text, name, args)
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))],
            ),
            usage_metadata=_usage(llm_request, extra_output=len(str(args))),
            turn_complete=True,
        )


# ---------------------------------------------------------------------------
# routing
#
# One rule per tool, first match wins. Compiled once at import: these run on
# every turn, and the patterns are long enough that recompiling is wasteful.
# ---------------------------------------------------------------------------
_THEMES = "sakura|moonrise|bamboo|ember|void|glacier|dusk|aurora|crimson|storm"

_HELP_RE = re.compile(r"\b(help|what can you|capabilities|who are you)\b")
_THEME_AFTER_RE = re.compile(rf"\b(?:theme|palette)\b.*?\b({_THEMES})\b")
_THEME_BEFORE_RE = re.compile(rf"\b({_THEMES})\b.*\b(?:theme|palette)\b")
_DOCS_RE = re.compile(
    r"\b(how does|how do|why |what is|explain|docs|documentation|architecture"
    r"|cost|guardrail|deploy)\b"
)
_GALLERY_RE = re.compile(r"\b(gallery|community|other people|public|shared|trending)\b")
_RECALL_RE = re.compile(
    r"\b(my (recent|latest|last|creations|images|gallery)|what have i|show me mine|recall)\b"
)
_CRITIQUE_RE = re.compile(r"\b(critique|improve|better prompt|feedback|review my prompt)\b")
_DRAW_RE = re.compile(
    r"\b(draw|paint|generate|create|make|render|image|picture|portrait|illustrat)\w*\b"
)


def _route(text: str, available: set[str]) -> tuple[str, dict[str, Any]] | None:
    lowered = text.lower().strip()
    if not lowered:
        return None

    def ok(name: str) -> bool:
        return name in available

    if ok("what_can_you_do") and _HELP_RE.search(lowered):
        return "what_can_you_do", {}

    if ok("set_theme"):
        # Either order: "the glacier theme" or "theme: glacier".
        match = _THEME_AFTER_RE.search(lowered) or _THEME_BEFORE_RE.search(lowered)
        if match:
            return "set_theme", {"theme": match.group(1)}

    if ok("search_studio_docs") and _DOCS_RE.search(lowered):
        return "search_studio_docs", {"question": text.strip()[:300]}

    if ok("browse_gallery") and _GALLERY_RE.search(lowered):
        return "browse_gallery", {"limit": 6}

    if ok("recall_favorites") and _RECALL_RE.search(lowered):
        return "recall_favorites", {"limit": _first_int(lowered, default=6)}

    if ok("critique_prompt") and _CRITIQUE_RE.search(lowered):
        return "critique_prompt", {"prompt": text.strip()[:300]}

    if ok("generate_image") and _DRAW_RE.search(lowered):
        return "generate_image", {
            "prompt": _strip_command(text),
            "style": _detect_style(lowered),
            "aspect": _detect_aspect(lowered),
        }

    return None


def _detect_style(lowered: str) -> str:
    for style in ("manga", "photoreal", "chibi", "anime"):
        if style in lowered:
            return style
    if "photo" in lowered or "realistic" in lowered:
        return "photoreal"
    return "anime"


def _detect_aspect(lowered: str) -> str:
    for token, aspect in (
        ("16:9", "16:9"),
        ("9:16", "9:16"),
        ("1:1", "1:1"),
        ("3:4", "3:4"),
        ("wide", "16:9"),
        ("landscape", "16:9"),
        ("square", "1:1"),
        ("portrait", "3:4"),
    ):
        if token in lowered:
            return aspect
    return "3:4"


_COMMAND_PREFIX = re.compile(
    r"^\s*(?:please\s+)?(?:can you\s+)?(?:draw|paint|generate|create|make|render)\s+"
    r"(?:me\s+)?(?:an?\s+|the\s+)?(?:image\s+of\s+|picture\s+of\s+)?",
    re.IGNORECASE,
)


def _strip_command(text: str) -> str:
    """"draw me a picture of a red fox" -> "a red fox"."""
    stripped = _COMMAND_PREFIX.sub("", text).strip()
    return (stripped or text.strip())[:300]


def _first_int(text: str, *, default: int) -> int:
    match = re.search(r"\b(\d{1,2})\b", text)
    if not match:
        return default
    return max(1, min(12, int(match.group(1))))


# ---------------------------------------------------------------------------
# request/response helpers
# ---------------------------------------------------------------------------
def _last_user_text(llm_request: LlmRequest) -> str:
    for content in reversed(llm_request.contents or []):
        if content.role != "user":
            continue
        text = " ".join(p.text for p in (content.parts or []) if getattr(p, "text", None))
        if text.strip():
            return text.strip()
    return ""


def _tool_results(llm_request: LlmRequest) -> list[dict[str, Any]]:
    """Collect `function_response` parts from the *current* turn only.

    Only the final content block counts. ADK appends the tool's response as the
    last entry before re-invoking the model, so anything earlier is history from
    a previous turn -- and scanning the whole conversation makes every turn after
    the first look like a closing turn, which silently disables tool calling.
    """
    contents = llm_request.contents or []
    if not contents:
        return []

    responses = [
        p.function_response
        for p in (contents[-1].parts or [])
        if getattr(p, "function_response", None)
    ]
    return [
        {
            "name": r.name,
            "response": r.response if isinstance(r.response, dict) else {},
        }
        for r in responses
    ]


def _closing_line(tool_results: list[dict[str, Any]]) -> str:
    first = tool_results[0]
    name = first["name"]
    payload = first.get("response") or {}

    if payload.get("error"):
        return f"That didn't go through — {payload['error'].replace('_', ' ')}. Want to try again?"

    if name == "generate_image":
        return (
            f"Done — {payload.get('style', 'anime')} at {payload.get('aspect', '3:4')}. "
            "Want me to push the contrast further?"
        )
    if name in ("recall_favorites", "browse_gallery"):
        count = len(payload.get("items", []))
        where = "your library" if name == "recall_favorites" else "the community gallery"
        return f"Pulled {count} from {where}. Anything here worth building on?"
    if name == "search_studio_docs":
        return payload.get("answer") or "I couldn't find that in the studio docs."
    if name == "set_theme":
        return f"Switched to {payload.get('theme', 'sakura')}. Better?"
    if name == "critique_prompt":
        return payload.get("critique") or "The critic had nothing to add."
    if name == "what_can_you_do":
        return "That's the tour — where do you want to start?"
    return "Done."


def _fallback_line(text: str) -> str:
    if not text:
        return "I'm listening — what should we make?"
    return (
        "I can generate images, pull up your library or the community gallery, switch themes, "
        "answer questions about how this studio is built, and critique a prompt. "
        "Try \"draw a red fox in the snow\"."
    )


def _text_response(text: str, llm_request: LlmRequest) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=text)]),
        usage_metadata=_usage(llm_request, extra_output=len(text)),
        turn_complete=True,
    )


def _usage(llm_request: LlmRequest, *, extra_output: int) -> types.GenerateContentResponseUsageMetadata:
    """Approximate token accounting.

    The cost telemetry and the daily cap both read `usage_metadata`, so the stub
    has to populate it or those code paths never execute offline -- and a cap
    that is only ever exercised in production is a cap you cannot trust.
    """
    prompt_chars = sum(
        len(p.text or "")
        for content in (llm_request.contents or [])
        for p in (content.parts or [])
        if getattr(p, "text", None)
    )
    prompt_tokens = max(1, prompt_chars // CHARS_PER_TOKEN)
    output_tokens = max(1, extra_output // CHARS_PER_TOKEN)
    return types.GenerateContentResponseUsageMetadata(
        prompt_token_count=prompt_tokens,
        candidates_token_count=output_tokens,
        total_token_count=prompt_tokens + output_tokens,
    )
