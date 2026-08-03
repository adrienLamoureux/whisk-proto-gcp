"""FastAPI application factory.

Mirrors the AWS repo's `routes/index.js` route hub: one place that mounts
everything, so "what does this service expose?" has a single answer.

Three transports live side by side on one Cloud Run service:

    /api/*      REST, for the React frontend
    /a2a/critic A2A, for other agents
    /mcp        MCP, for agents that want this app's tools

Co-locating them is a cost decision as much as a convenience one. Cloud Run bills
per request and scales to zero, so three services would triple the cold starts
and the deployment surface while saving nothing. They separate cleanly if any one
of them ever needs its own scaling profile.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .a2a_app import MOUNT_PATH as A2A_MOUNT
from .a2a_app import agent_card_summary, build_critic_a2a_app
from .agent.whisk_runner import AgentService
from .config import Settings, get_settings
from .deps import build_deps
from .mcp_server import build_mcp_server, surface_summary, transport_security
from .rag.corpus import ingest_directory
from .routes import admin, agent, health, media, rag

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("whisk")


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.deps = build_deps(settings)
        app.state.agent_service = AgentService(app.state.deps)
        app.state.settings = settings

        # Ingest at startup so a cold container can answer grounded questions on
        # its first request. In local mode this is pure CPU; in GCP mode it is a
        # batch of embedding calls against a corpus baked into the image, so the
        # cost is bounded and paid once per revision, not per request.
        try:
            stats = await ingest_directory(
                app.state.deps.store, app.state.deps.embeddings, directory=settings.corpus_dir
            )
            log.info("corpus ready: %s", stats)
        except Exception:  # noqa: BLE001 - a docs failure must not block boot
            log.exception("corpus ingestion failed; RAG answers will be empty")

        async with AsyncExitStack() as stack:
            # Starlette does NOT run the lifespan of a mounted sub-app, and both
            # of these register their routes (A2A) or start their transport (MCP)
            # from inside one. Skip this and every /a2a and /mcp path 404s.
            a2a_app = getattr(app.state, "a2a_app", None)
            if a2a_app is not None:
                await stack.enter_async_context(a2a_app.router.lifespan_context(a2a_app))

            mcp = getattr(app.state, "mcp_server", None)
            if mcp is not None:
                await stack.enter_async_context(mcp.session_manager.run())
            yield

        await app.state.deps.close()

    app = FastAPI(
        title="Whisk Studio (GCP)",
        version="0.1.0",
        description="FastAPI + ADK agent on serverless GCP. Port of a Bedrock/Lambda original.",
        lifespan=lifespan,
    )

    # Assigned here as well as in the lifespan: `request_settings` reads it, and
    # a request arriving before startup completes must not silently fall back to
    # the process-wide env cache.
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(media.router)
    app.include_router(agent.router)
    app.include_router(rag.router)
    app.include_router(admin.router)

    surfaces: dict[str, object] = {"rest": {"mountPath": "/api", "protocol": "http"}}

    if settings.enable_a2a:
        a2a_app = build_critic_a2a_app(settings)
        if a2a_app is not None:
            app.mount(A2A_MOUNT, a2a_app)
            app.state.a2a_app = a2a_app  # lifespan is entered manually, see above
            surfaces["a2a"] = agent_card_summary(settings)
            log.info("A2A critic mounted at %s", A2A_MOUNT)

    if settings.enable_mcp:
        # Built before lifespan runs so the ASGI mount exists at import time;
        # its session manager is started in the lifespan above.
        mcp_server = build_mcp_server_deferred(app, settings)
        if mcp_server is not None:
            surfaces["mcp"] = surface_summary()
            log.info("MCP server mounted at /mcp")

    @app.get("/api/surfaces")
    async def list_surfaces() -> dict:
        """What this service speaks. Handy for a reader, and the frontend footer
        renders it to make the three protocols visible rather than theoretical."""
        return {"mode": settings.mode, "surfaces": surfaces}

    _mount_frontend(app, settings)
    return app


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    """Serve the built React bundle from this same service, if present.

    One Cloud Run service serves both the API and the SPA. The alternative --
    GCS + Cloud CDN behind a global external load balancer -- is the closer
    analogue of the original's CloudFront setup, but a forwarding rule bills
    roughly $18/month before a single request. That is the largest fixed cost
    available to accidentally incur here, and serving a ~170 KB bundle from
    Cloud Run costs nothing extra. See docs/cost-model.md.

    Firebase Hosting is the genuinely free CDN option and is the right answer if
    edge caching starts to matter; it is a deploy-target change, not a code one.
    """
    dist = Path(settings.frontend_dist)
    if not dist.is_dir():
        log.info("frontend bundle not found at %s; API-only mode", dist)
        return

    # `html=True` serves index.html for unknown paths, which is what a
    # client-routed SPA needs. Mounted last so it cannot shadow /api, /a2a, /mcp.
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
    log.info("serving frontend bundle from %s", dist)


def build_mcp_server_deferred(app: FastAPI, settings: Settings):
    """Mount MCP with a lazily-resolved dependency container.

    The MCP tools need `Deps`, which is built in the lifespan handler -- but the
    ASGI mount must exist before startup. A tiny proxy defers the lookup to call
    time, so the mount is static while the dependency stays request-scoped.
    """

    class _LazyDeps:
        def __getattr__(self, name: str):
            return getattr(app.state.deps, name)

    server = build_mcp_server(_LazyDeps())  # type: ignore[arg-type]
    if server is None:
        return None
    app.state.mcp_server = server
    app.mount(
        "/mcp",
        server.streamable_http_app(
            # The sub-app defaults to serving at "/mcp" internally, which behind a
            # "/mcp" mount would resolve to "/mcp/mcp".
            streamable_http_path="/",
            # Stateless is not an optimisation here, it is a correctness
            # requirement: Cloud Run load-balances across instances with no
            # session affinity, so a session pinned to one instance's memory
            # breaks as soon as the service scales past one.
            stateless_http=True,
            json_response=True,
            # Defaults to an empty host allow-list, which 421s anything that is
            # not loopback -- invisible locally, broken on Cloud Run.
            transport_security=transport_security(settings),
        ),
    )
    return server


app = build_app()
