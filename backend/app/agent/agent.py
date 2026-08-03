"""The studio agent definition.

One `LlmAgent`, one tool fleet, two possible models. `WHISK_MODE` decides whether
`model=` is a real Gemini id or the offline stub -- nothing else about the agent
changes, which is the property that makes the local mode worth having.

## On model choice

`gemini-3.6-flash` is the default because agent turns are latency-sensitive and
overwhelmingly simple: pick a tool, fill three arguments, write one closing
sentence. Flash is the right tier for that, and the cost difference against Pro
compounds fast when every user message is a turn.

`gemini-3.1-pro-preview` (1M context) is wired as an admin-selectable override
for the cases where it earns its price -- long grounded reasoning over a large
retrieved context. The override lives in Firestore (`config/agent`) with a 60s
cache, so switching models is a database write, not a redeploy.
"""

from __future__ import annotations

import logging

from google.adk.agents import LlmAgent
from google.adk.models import LLMRegistry
from google.genai import types

from ..config import Settings
from ..deps import Deps
from .state import render_prefs
from .stub_llm import CRITIC_MODEL_NAME, STUB_MODEL_NAME, StubLlm
from .tools import build_tools

log = logging.getLogger("whisk.agent")

AGENT_NAME = "whisk_studio"
APP_NAME = "whisk-studio"

_registered = False


def register_stub_model() -> None:
    """Teach ADK's registry to resolve `whisk-stub-*` to the offline model."""
    global _registered
    if not _registered:
        LLMRegistry.register(StubLlm)
        _registered = True


BASE_INSTRUCTION = """You are Whisk, the resident agent of an AI creative studio.

Voice: warm, direct, a little wry. One or two sentences. Never bullet-point at
the user. Never narrate what you are about to do -- just do it and react to the
result.

Rules:
- When a request maps to a tool, call the tool. Do not describe the tool.
- Never ask for confirmation before generating an image; generating is cheap and
  reversible. Do ask before anything that publishes or deletes -- those tools
  return a proposal, and you should surface it in plain language.
- Never invent an image URL, a media id, or a fact about the studio. If you did
  not get it from a tool, you do not know it.
- For questions about how this studio is built, always call search_studio_docs
  rather than answering from memory. The answer must be grounded.
- After a tool runs, respond in one short sentence reacting to what actually came
  back, then stop.
"""

MODE_ADDENDA = {
    "studio": "",
    "companion": (
        "\nYou are in companion mode: the character surface, not the dashboard. "
        "Administrative and billing operations are out of reach here -- if asked, "
        "decline warmly and suggest the dashboard instead."
    ),
}


def build_instruction(*, mode: str = "studio", prefs: dict[str, str] | None = None) -> str:
    """Assemble the system prompt.

    Prefs are appended as a `<prefs>` block so the model biases tool defaults
    toward the user's prior choices. Every value in it has been enum-validated on
    read -- see `state.py` for why that is not paranoia.
    """
    parts = [BASE_INSTRUCTION, MODE_ADDENDA.get(mode, "")]
    rendered = render_prefs(prefs or {})
    if rendered:
        parts.append(
            "\nThe user's established preferences follow. Bias tool defaults toward "
            f"them unless the user says otherwise.\n{rendered}"
        )
    return "\n".join(p for p in parts if p)


def resolve_model(settings: Settings, override: str | None = None) -> str:
    """Pick the model id. Local mode always wins -- an override must never
    accidentally start billing a project that may not exist."""
    if settings.is_local:
        return STUB_MODEL_NAME
    return (override or settings.agent_model).strip() or settings.agent_model


def resolve_critic_model(settings: Settings) -> str:
    return CRITIC_MODEL_NAME if settings.is_local else settings.agent_model


def build_agent(
    deps: Deps, *, mode: str = "studio", prefs: dict[str, str] | None = None, model: str | None = None
) -> LlmAgent:
    register_stub_model()
    return LlmAgent(
        name=AGENT_NAME,
        model=resolve_model(deps.settings, model),
        description="Creative studio agent: generates images, browses libraries, explains the system.",
        instruction=build_instruction(mode=mode, prefs=prefs),
        tools=build_tools(deps),
        generate_content_config=types.GenerateContentConfig(
            temperature=0.7,
            # The closing turn is one sentence; an unbounded budget here is how
            # agents quietly become expensive.
            max_output_tokens=512,
        ),
    )
