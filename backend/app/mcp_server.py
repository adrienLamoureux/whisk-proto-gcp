"""Expose studio capabilities over MCP.

## MCP vs A2A -- the distinction worth being precise about

They solve adjacent problems and get conflated constantly:

  * **MCP** connects an agent to **capabilities** -- tools, resources, prompts.
    The other end is a *server*, not a peer. It has no goals and no autonomy; it
    answers calls.
  * **A2A** connects an agent to **another agent** -- something with its own
    reasoning loop that you delegate a task to and that may come back with
    clarifying questions.

Rule of thumb: if the far side would need an LLM to do its job, that is A2A. If
it is a database, an API, or a filesystem, that is MCP.

They compose, and this repo shows both ends. The critic is an A2A *peer*
(`a2a_app.py`); the studio's read-only capabilities are an MCP *server* (here).
An external agent -- Claude Desktop, another ADK agent, anything speaking MCP --
can mount this and browse the gallery or query the studio docs.

## Why only read-only tools

Only `browse_gallery` and `search_studio_docs` are published. MCP carries no
end-user identity: an MCP client authenticates as *itself*, not as one of this
app's users. Anything user-scoped (`generate_image`, `recall_favorites`,
`share_image`) needs a `uid` to be safe and to attribute quota, and there is
nowhere trustworthy for that to come from over this transport. Publishing them
would mean either a shared identity spending everyone's budget, or a `uid`
parameter the caller supplies -- which is an IDOR with extra steps.

The two published tools touch only public data and cost no user quota.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .deps import Deps
from .rag.search import FirestoreVectorBackend, synthesise_answer
from .store import media as media_store

log = logging.getLogger("whisk.mcp")

SERVER_NAME = "whisk-studio"


def transport_security(settings: Settings):
    """Host/Origin allow-list for MCP's DNS-rebinding protection.

    The protection is on by default and `allowed_hosts` defaults to **empty**,
    which rejects everything that is not loopback with `421 Misdirected Request`.
    Loopback passes, so this is invisible in local development and only fails
    once the service is behind a real hostname -- exactly the kind of bug that
    ships. The Cloud Run domain therefore has to be allow-listed explicitly, and
    the deploy script sets `WHISK_PUBLIC_BASE_URL` for precisely this reason.

    Turning the protection off instead would be the wrong fix: it is what stops a
    malicious page from re-pointing a hostname at this origin and driving the MCP
    server from the victim's browser.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = {"localhost", "127.0.0.1", "localhost:8080", "127.0.0.1:8080", "testserver"}
    origins = set(settings.cors_origin_list)

    public = urlparse(settings.public_base_url)
    if public.netloc:
        hosts.add(public.netloc)
        origins.add(f"{public.scheme}://{public.netloc}")

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(origins),
    )


def build_mcp_server(deps: Deps):
    """Build the MCP server, or None if the `mcp` package is unavailable.

    `MCPServer` is the mcp>=2.0 entry point; it replaced the `FastMCP` class that
    lived at `mcp.server.fastmcp` in 1.x. The decorator-based tool API is the
    same, so only the import and the constructor moved.
    """
    try:
        from mcp.server import MCPServer
    except ImportError:  # pragma: no cover
        log.warning("mcp not installed; MCP surface disabled")
        return None

    mcp = MCPServer(SERVER_NAME, version="0.1.0")

    @mcp.tool()
    async def browse_gallery(limit: int = 6) -> dict[str, Any]:
        """Browse images the Whisk Studio community has shared publicly.

        Args:
            limit: How many to return, 1-12.
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

    @mcp.tool()
    async def search_studio_docs(question: str) -> dict[str, Any]:
        """Answer a question about how Whisk Studio is built, grounded in its docs.

        Covers architecture, GCP service choices, cost model, guardrails, the
        agent loop, and AWS-to-GCP equivalences.

        Args:
            question: The question to answer.
        """
        backend = FirestoreVectorBackend(deps.store, deps.embeddings)
        passages = await backend.retrieve(question, limit=4)
        return synthesise_answer(question, passages)

    return mcp


def surface_summary() -> dict:
    return {
        "name": SERVER_NAME,
        "mountPath": "/mcp",
        "protocol": "mcp",
        "transport": "streamable-http",
        "tools": ["browse_gallery", "search_studio_docs"],
        "note": "Read-only: MCP carries no end-user identity, so user-scoped tools stay off it.",
    }
