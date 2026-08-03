"""Expose the critic agent over A2A.

ADK's `to_a2a()` wraps an agent in a Starlette app implementing the A2A protocol:
an agent card at `/.well-known/agent-card.json` plus a JSON-RPC invoke endpoint.
Mounted under `/a2a/critic`, so:

    GET  /a2a/critic/.well-known/agent-card.json   -- discovery
    POST /a2a/critic/                              -- invoke

Any A2A client can then use the critic without knowing it is ADK, or Python. That
interoperability is the whole value proposition -- and the reason to expose the
*critic* rather than the studio agent: the critic is a pure function of its input,
with no user data, no quota to charge, and no identity to impersonate.

**The studio agent is deliberately not exposed over A2A.** Its tools write to a
specific user's library and consume that user's daily caps; identity arrives
through session state seeded from a verified token (`tools.py::_uid`). Publishing
it as a peer would mean re-solving auth, quota attribution and tenancy at the
protocol boundary. A2A does support authenticated agent cards, so this is a
scoping decision rather than a protocol limitation -- but shipping an
unauthenticated agent that spends someone else's budget would be a real
vulnerability, not a demo.

## Two mounting details that are easy to get wrong

1. **`to_a2a` registers its routes inside its own Starlette lifespan**, and
   Starlette does *not* run the lifespan of a mounted sub-app. Mount it and do
   nothing else and every A2A path 404s, including the agent card. The parent
   lifespan has to enter the child's explicitly -- see `main.py`.

2. **The agent card advertises an absolute URL.** Left to its default, ADK builds
   it from `host`/`port` with no knowledge of the mount prefix, so the card would
   send clients to `/` instead of `/a2a/critic/`. A card that points at the wrong
   endpoint is worse than no card, because discovery appears to succeed. The card
   is therefore built explicitly here from `WHISK_PUBLIC_BASE_URL`.
"""

from __future__ import annotations

import logging

from .agent.agent import register_stub_model, resolve_critic_model
from .agent.critic import CRITIC_DESCRIPTION, CRITIC_NAME, build_critic_agent
from .config import Settings

log = logging.getLogger("whisk.a2a")

MOUNT_PATH = "/a2a/critic"
AGENT_CARD_PATH = f"{MOUNT_PATH}/.well-known/agent-card.json"


def _build_agent_card(settings: Settings):
    """Construct the card explicitly so the advertised URL matches the mount."""
    from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

    base = settings.public_base_url.rstrip("/")
    return AgentCard(
        name=CRITIC_NAME,
        description=CRITIC_DESCRIPTION,
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(
                url=f"{base}{MOUNT_PATH}/",
                protocol_binding="JSONRPC",
                protocol_version="0.3.0",
            )
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="critique_image_prompt",
                name="Critique an image prompt",
                description=(
                    "Analyse a text-to-image prompt for missing visual dimensions "
                    "(lighting, composition, colour, medium, mood) and return a "
                    "verdict, the gaps, and an improved rewrite."
                ),
                tags=["prompt-engineering", "image-generation", "critique"],
                examples=[
                    "a cat on a roof",
                    "red fox asleep in the snow",
                ],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            )
        ],
    )


def build_critic_a2a_app(settings: Settings):
    """Build the Starlette A2A app for the critic, or None if unavailable.

    Returns None rather than raising: A2A is an optional surface, and a missing
    extra should degrade the deployment, not stop it from booting.
    """
    try:
        from google.adk.a2a.utils.agent_to_a2a import to_a2a
    except ImportError:  # pragma: no cover - depends on the [a2a] extra
        log.warning("google-adk[a2a] not installed; A2A surface disabled")
        return None

    register_stub_model()
    agent = build_critic_agent(resolve_critic_model(settings))

    try:
        return to_a2a(agent, agent_card=_build_agent_card(settings))
    except Exception:  # noqa: BLE001
        log.exception("failed to build the A2A app; surface disabled")
        return None


def agent_card_summary(settings: Settings) -> dict:
    """What the mounted card advertises -- surfaced on `/api/surfaces` so the
    frontend can show the A2A endpoint without fetching the card itself."""
    base = settings.public_base_url.rstrip("/")
    return {
        "name": CRITIC_NAME,
        "description": CRITIC_DESCRIPTION,
        "mountPath": MOUNT_PATH,
        "agentCardUrl": f"{base}{AGENT_CARD_PATH}",
        "protocol": "a2a",
        "skills": ["critique_image_prompt"],
    }
