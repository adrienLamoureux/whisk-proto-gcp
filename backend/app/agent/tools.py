"""The tool fleet.

Ported from the AWS repo's 10-tool Bedrock Converse fleet, preserving the part
that actually matters: **tools fall into classes, and the class determines the
control flow.**

  * **server-dispatch** -- `generate_image`, `recall_favorites`, `browse_gallery`,
    `search_studio_docs`. The backend executes, then the model gets a short
    closing turn to react to the result.
  * **client-action** -- `set_theme`. Nothing happens server-side; the payload
    rides back to the browser and React applies it.
  * **intent (requires confirm)** -- `share_image`, `delete_image`. The tool
    returns a *proposal*, never a write. The user approves in the UI, which then
    calls the ordinary REST endpoint. Anything that publishes or destroys goes
    through here.
  * **delegation** -- `critique_prompt`. Hands off to a second agent over A2A.
  * **static** -- `what_can_you_do`. Canned, no model call.

Tools are closures over `Deps` rather than module-level functions reaching for
globals: ADK builds the function-calling schema from the signature, and a closure
keeps both the schema and the dependency graph explicit. `tool_context.state`
carries the caller's identity, seeded by the runner on every turn.

Docstrings here are not documentation for humans. **They are the tool
descriptions the model sees**, and they decide whether the right tool gets picked.
Edit them like prompt text.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.tools import ToolContext

from ..deps import Deps
from ..providers.images import normalise_aspect, normalise_style
from ..rag.search import FirestoreVectorBackend, synthesise_answer
from ..store import media as media_store
from .state import THEMES, patch_prefs

log = logging.getLogger("whisk.tools")

STATE_UID = "uid"


def _uid(tool_context: ToolContext) -> str:
    """Identity comes from session state, never from a model-supplied argument.

    If `uid` were a tool parameter the model could be talked into passing someone
    else's -- prompt injection straight into an IDOR. The runner seeds it from
    the verified token and the model never sees it.
    """
    uid = (tool_context.state or {}).get(STATE_UID)
    if not uid:
        raise PermissionError("unauthorized")
    return str(uid)


def build_tools(deps: Deps) -> list:
    """Bind the fleet to a dependency container."""

    rag = FirestoreVectorBackend(deps.store, deps.embeddings)

    # --- server-dispatch --------------------------------------------------
    async def generate_image(
        prompt: str,
        style: str = "anime",
        aspect: str = "3:4",
        tool_context: ToolContext = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Generate an image from a text prompt and save it to the user's library.

        Use this whenever the user asks to draw, paint, render, create or imagine
        something visual.

        Args:
            prompt: What to depict. Be concrete and visual; do not include style words.
            style: One of "anime", "manga", "photoreal", "chibi".
            aspect: One of "1:1", "3:4", "16:9", "9:16".
        """
        uid = _uid(tool_context)
        if not prompt or not prompt.strip():
            return {"error": "prompt_required"}

        # Consume the image quota *before* spending money with the provider.
        decision = await deps.guardrails.check_and_consume_image(uid)
        if not decision.allowed:
            return {
                "error": decision.reason,
                "imagesToday": decision.used,
                "capacity": decision.capacity,
                "retryAfterMs": decision.retry_after_ms,
            }

        style = normalise_style(style)
        aspect = normalise_aspect(aspect)
        cleaned = prompt.strip()

        try:
            generation = await deps.images.generate(cleaned, style=style, aspect=aspect)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool error
            log.exception("image generation failed")
            return {"error": "image_generation_failed", "detail": str(exc)[:200]}

        if generation.status == "failed":
            return {"error": generation.error or "image_generation_failed"}

        # Best-effort: a failed pref write must not fail the generation.
        await patch_prefs(deps.store, uid, {"lastStyle": style, "lastAspect": aspect})

        if generation.is_pending and generation.prediction_id:
            # The prediction did not land inside the wait window. Hand the id
            # back so the browser polls; the model's closing turn describes what
            # was started rather than pretending an image exists.
            await media_store.create_job(
                deps.store,
                uid=uid,
                prediction_id=generation.prediction_id,
                prompt=cleaned,
                style=style,
                aspect=aspect,
                source="agent",
            )
            return {
                "status": "processing",
                "predictionId": generation.prediction_id,
                "prompt": cleaned,
                "style": style,
                "aspect": aspect,
                "imagesRemaining": decision.remaining,
            }

        record = await media_store.create_image_record(
            deps.store,
            deps.storage,
            uid=uid,
            image=generation.image,
            prompt=cleaned,
            style=style,
            aspect=aspect,
            source="agent",
        )
        enriched = await media_store.with_url(deps.storage, record)
        return {
            "status": "succeeded",
            "mediaId": enriched["mediaId"],
            "imageUrl": enriched["url"],
            "prompt": enriched["prompt"],
            "style": style,
            "aspect": aspect,
            "imagesRemaining": decision.remaining,
        }

    async def recall_favorites(
        limit: int = 6, tool_context: ToolContext = None  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Look at the user's own recent creations, newest first.

        Use this when the user refers to their past work -- "what have I made",
        "my recent images", "show me mine".

        Args:
            limit: How many to pull back, 1-12.
        """
        uid = _uid(tool_context)
        items = await media_store.list_media(
            deps.store, deps.storage, uid=uid, limit=max(1, min(12, limit))
        )
        return {
            "items": [
                {
                    "mediaId": i["mediaId"],
                    "url": i["url"],
                    "prompt": i.get("prompt", ""),
                    "style": i.get("style", ""),
                }
                for i in items
            ],
            "count": len(items),
        }

    async def browse_gallery(
        limit: int = 6, tool_context: ToolContext = None  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Look at what the wider community has shared publicly.

        Use this for "what are other people making", "show me the gallery",
        "what's trending". This is other users' work, not the caller's.

        Args:
            limit: How many to pull back, 1-12.
        """
        items = await media_store.list_shared(
            deps.store, deps.storage, limit=max(1, min(12, limit))
        )
        return {
            "items": [
                {"mediaId": i["mediaId"], "url": i["url"], "prompt": i.get("prompt", "")}
                for i in items
            ],
            "count": len(items),
        }

    async def search_studio_docs(
        question: str, tool_context: ToolContext = None  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Answer a question about how this studio is built, using its documentation.

        Use this for questions about architecture, cost, GCP services, the agent
        loop, guardrails, deployment, or AWS-to-GCP equivalences. The answer is
        grounded in retrieved documentation with citations.

        Args:
            question: The user's question, verbatim.
        """
        passages = await rag.retrieve(question, limit=4)
        return synthesise_answer(question, passages)

    # --- client-action -----------------------------------------------------
    async def set_theme(
        theme: str, tool_context: ToolContext = None  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Change the app's colour theme.

        Args:
            theme: One of sakura, moonrise, bamboo, ember, void, glacier, dusk,
                aurora, crimson, storm.
        """
        normalised = (theme or "").strip().lower()
        if normalised not in THEMES:
            return {"error": "unknown_theme", "allowed": sorted(THEMES)}

        uid = _uid(tool_context)
        await patch_prefs(deps.store, uid, {"theme": normalised})
        # `clientAction` tells the frontend this result is an instruction to
        # apply, not data to render.
        return {"theme": normalised, "clientAction": "set_theme"}

    # --- intent (requires confirmation) ------------------------------------
    async def share_image(
        media_id: str, tool_context: ToolContext = None  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Propose publishing one of the user's images to the public gallery.

        This does NOT publish. It returns a proposal the user must confirm,
        because publishing is visible to everyone and cannot be un-seen.

        Args:
            media_id: The id of the image to share.
        """
        uid = _uid(tool_context)
        record = await media_store.get_media(deps.store, uid=uid, media_id=media_id)
        if record is None:
            return {"error": "media_not_found"}
        return {
            "requiresConfirm": True,
            "intent": "share_image",
            "mediaId": media_id,
            "prompt": record.get("prompt", ""),
            "confirmEndpoint": f"/api/media/{media_id}/share",
            "summary": "Publish this image to the public gallery?",
        }

    async def delete_image(
        media_id: str, tool_context: ToolContext = None  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Propose permanently deleting one of the user's images.

        This does NOT delete. It returns a proposal the user must confirm,
        because deletion is irreversible.

        Args:
            media_id: The id of the image to delete.
        """
        uid = _uid(tool_context)
        record = await media_store.get_media(deps.store, uid=uid, media_id=media_id)
        if record is None:
            return {"error": "media_not_found"}
        return {
            "requiresConfirm": True,
            "intent": "delete_image",
            "mediaId": media_id,
            "prompt": record.get("prompt", ""),
            "confirmEndpoint": f"/api/media/{media_id}",
            "confirmMethod": "DELETE",
            "summary": "Permanently delete this image? This cannot be undone.",
        }

    # --- delegation (A2A) ---------------------------------------------------
    async def critique_prompt(
        prompt: str, tool_context: ToolContext = None  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Ask the specialist critic agent to improve an image prompt.

        Use this when the user wants feedback on a prompt, or asks how to get a
        better result. The critic is a separate agent reached over A2A.

        Args:
            prompt: The prompt to critique.
        """
        from .critic import critique  # local import: keeps A2A optional

        return await critique(prompt)

    # --- static -------------------------------------------------------------
    async def what_can_you_do(
        tool_context: ToolContext = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """List what this agent can do. Use for "help", "what can you do", or a lost user."""
        return {
            "title": "Here's what I can do",
            "items": [
                {"label": "Generate images", "example": "draw a red fox in the snow, 16:9"},
                {"label": "Show your library", "example": "what have I made recently?"},
                {"label": "Browse the community gallery", "example": "what are others making?"},
                {"label": "Explain how this studio works", "example": "how does the cost model work?"},
                {"label": "Critique a prompt", "example": "critique: a cat on a roof"},
                {"label": "Change the theme", "example": "switch to the glacier theme"},
            ],
        }

    return [
        generate_image,
        recall_favorites,
        browse_gallery,
        search_studio_docs,
        set_theme,
        share_image,
        delete_image,
        critique_prompt,
        what_can_you_do,
    ]


# Tools whose result is an instruction for the browser rather than data.
CLIENT_ACTION_TOOLS = frozenset({"set_theme"})
# Tools that return a proposal the user must approve before anything is written.
INTENT_TOOLS = frozenset({"share_image", "delete_image"})
