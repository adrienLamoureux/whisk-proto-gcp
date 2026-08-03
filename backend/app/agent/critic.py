"""The critic: a second agent, reachable over A2A.

## What A2A is actually for

A2A (Agent2Agent, now a Linux Foundation project) is an open protocol for agents
that do **not** share a runtime -- different teams, different frameworks,
different companies. An agent publishes a *card* at
`/.well-known/agent-card.json` describing its skills, and any A2A client can
discover and call it without knowing it is ADK, LangGraph, or bespoke.

The distinction that matters, and the one worth having straight in an interview:

  * **Same process, same team** -> ADK `sub_agents` or `AgentTool`. A2A here is
    pure overhead: HTTP, serialisation, and a second failure mode buying nothing.
  * **Different process/org/framework** -> A2A. This is the case it exists for.
  * **Agent needs a *tool*** (a database, an API, a filesystem) -> MCP, not A2A.
    A2A connects *peers*; MCP connects an agent to *capabilities*. They compose:
    this critic could itself consume MCP tools.

So the critic is deliberately built both ways. It is exposed over A2A because
that is what makes it reusable by anything (see `app/a2a_app.py`), and
`critique()` calls it **in-process by default** because for a co-located agent
that is the honest engineering choice. Point `WHISK_CRITIC_A2A_URL` at a remote
critic and the same tool call goes over the wire instead -- the seam is real, not
a diagram.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from google.adk.agents import LlmAgent

log = logging.getLogger("whisk.critic")

CRITIC_NAME = "prompt_critic"
CRITIC_DESCRIPTION = (
    "Critiques and rewrites text-to-image prompts. Given a prompt, returns "
    "concrete weaknesses and an improved version."
)

CRITIC_INSTRUCTION = """You are a terse, expert critic of text-to-image prompts.

Given a prompt, respond in exactly this shape and nothing else:

Verdict: <one sentence on what the prompt does and does not pin down>
Missing: <comma-separated list of the specific dimensions left unspecified>
Rewrite: <a single improved prompt, under 40 words>

Judge the prompt on: concrete subject, composition/framing, lighting, colour
palette, medium/rendering, and mood. Do not praise. Do not add preamble. If the
prompt is already strong, say so in the Verdict and still offer a sharper rewrite.
"""


def build_critic_agent(model: str) -> LlmAgent:
    """A single-purpose agent: no tools, no memory, one job."""
    return LlmAgent(
        name=CRITIC_NAME,
        model=model,
        description=CRITIC_DESCRIPTION,
        instruction=CRITIC_INSTRUCTION,
    )


# ---------------------------------------------------------------------------
# rule-based critique (drives the critic agent in local mode)
# ---------------------------------------------------------------------------
_DIMENSIONS: list[tuple[str, tuple[str, ...]]] = [
    (
        "lighting",
        ("light", "lit", "sunlight", "backlit", "golden hour", "shadow", "neon", "dusk", "dawn",
         "moonlit", "candle", "overcast"),
    ),
    (
        "composition",
        ("close-up", "wide shot", "portrait", "landscape", "overhead", "low angle", "framing",
         "centered", "profile", "silhouette", "foreground", "background"),
    ),
    (
        "colour",
        ("color", "colour", "palette", "monochrome", "muted", "vivid", "pastel", "saturated",
         "warm", "cool", "crimson", "teal", "amber", "indigo"),
    ),
    (
        "medium",
        ("oil", "watercolor", "watercolour", "ink", "digital", "pencil", "charcoal", "3d",
         "render", "photograph", "film", "cel"),
    ),
    ("mood", ("moody", "serene", "tense", "melancholy", "joyful", "eerie", "dreamlike", "stark")),
]

_VAGUE = frozenset({"nice", "cool", "good", "beautiful", "amazing", "pretty", "epic", "awesome"})

_SUGGESTIONS = {
    "lighting": "golden-hour backlight",
    "composition": "tight three-quarter framing",
    "colour": "a muted teal-and-amber palette",
    "medium": "painterly digital rendering",
    "mood": "a quiet, contemplative mood",
}


def rule_based_critique(prompt: str) -> dict[str, Any]:
    """Deterministic critique used when no live model is available.

    Real analysis, not a canned string: it checks which of five concrete
    dimensions the prompt leaves unspecified and builds the rewrite from the
    gaps. Weaker than Gemini, but it is genuinely responsive to the input, so the
    A2A round-trip demonstrates something real offline.
    """
    text = (prompt or "").strip()
    if not text:
        return {
            "critique": "Verdict: empty prompt.\nMissing: everything.\nRewrite: (nothing to work with)",
            "missing": [],
            "rewrite": "",
            "score": 0,
        }

    lowered = text.lower()
    missing = [
        name for name, markers in _DIMENSIONS if not any(m in lowered for m in markers)
    ]
    words = re.findall(r"[a-z']+", lowered)
    vague = sorted(_VAGUE.intersection(words))
    word_count = len(words)

    if word_count < 4:
        verdict = "Too sparse for the model to commit to anything - it will invent the rest."
    elif vague:
        verdict = (
            f"Leans on subjective filler ({', '.join(vague)}) that carries no visual information."
        )
    elif not missing:
        verdict = "Already specific across subject, light, framing, colour and medium."
    else:
        verdict = f"Subject is clear, but {len(missing)} visual dimension(s) are left to chance."

    additions = [_SUGGESTIONS[m] for m in missing[:3]]
    base = re.sub(r"\b(" + "|".join(_VAGUE) + r")\b", "", text, flags=re.IGNORECASE)
    base = re.sub(r"\s{2,}", " ", base).strip(" ,")
    rewrite = ", ".join([base, *additions]) if additions else base

    score = max(0, min(10, 10 - len(missing) * 2 - len(vague) - (2 if word_count < 4 else 0)))

    return {
        "critique": (
            f"Verdict: {verdict}\n"
            f"Missing: {', '.join(missing) if missing else 'nothing significant'}\n"
            f"Rewrite: {rewrite}"
        ),
        "missing": missing,
        "rewrite": rewrite,
        "score": score,
    }


# ---------------------------------------------------------------------------
# invocation
# ---------------------------------------------------------------------------
async def critique(prompt: str) -> dict[str, Any]:
    """Run the critic, remotely over A2A if configured, in-process otherwise."""
    remote_url = os.getenv("WHISK_CRITIC_A2A_URL", "").strip()
    if remote_url:
        try:
            return await _critique_remote(prompt, remote_url)
        except Exception as exc:  # noqa: BLE001
            # A remote peer is someone else's uptime. Degrade to local rather
            # than failing the user's turn.
            log.warning("remote critic at %s failed (%s); falling back in-process", remote_url, exc)

    result = rule_based_critique(prompt)
    return {**result, "via": "in-process"}


async def _critique_remote(prompt: str, base_url: str) -> dict[str, Any]:
    """Call a remote critic through the A2A protocol.

    Uses ADK's `RemoteA2aAgent`, which fetches the peer's agent card, negotiates
    the skill, and marshals the turn -- the same path any non-ADK A2A client
    would take.
    """
    from google.adk.agents.remote_a2a_agent import AGENT_CARD_WELL_KNOWN_PATH, RemoteA2aAgent
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    remote = RemoteA2aAgent(
        name="remote_prompt_critic",
        description=CRITIC_DESCRIPTION,
        agent_card=f"{base_url.rstrip('/')}{AGENT_CARD_WELL_KNOWN_PATH}",
    )
    runner = InMemoryRunner(agent=remote, app_name="whisk-critic-client")
    session = await runner.session_service.create_session(
        app_name="whisk-critic-client", user_id="whisk-agent"
    )

    text_parts: list[str] = []
    async for event in runner.run_async(
        user_id="whisk-agent",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        if event.content and event.content.parts:
            text_parts.extend(p.text for p in event.content.parts if getattr(p, "text", None))

    critique_text = "\n".join(t for t in text_parts if t).strip()
    if not critique_text:
        raise RuntimeError("remote critic returned no text")

    rewrite_match = re.search(r"Rewrite:\s*(.+)", critique_text, re.IGNORECASE | re.DOTALL)
    return {
        "critique": critique_text,
        "rewrite": rewrite_match.group(1).strip() if rewrite_match else "",
        "via": "a2a",
        "peer": base_url,
    }
