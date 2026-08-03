"""Composition root.

Direct port of the AWS repo's `backend/lib/build-deps.js` and ADR-004 ("flat deps
shape"): one container assembled once at startup and threaded into every route
and every agent tool. No DI framework, no decorators, no import-time singletons.

The payoff is the same as the original's: a test constructs `Deps` with fakes and
calls the function under test directly. It is also the *only* place that branches
on `WHISK_MODE`, so "which implementation am I running?" has exactly one answer
to look up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .agent.guardrails import Guardrails
from .config import Settings, get_settings
from .providers.embeddings import EmbeddingProvider, LocalEmbeddingProvider, VertexEmbeddingProvider
from .providers.images import ImageProvider, LocalImageProvider
from .providers.replicate import ReplicateImageProvider
from .providers.storage import GcsMediaStorage, LocalMediaStorage, MediaStorage
from .store.docstore import DocStore, FirestoreDocStore, InMemoryDocStore

log = logging.getLogger("whisk.deps")


@dataclass(slots=True)
class Deps:
    settings: Settings
    store: DocStore
    storage: MediaStorage
    images: ImageProvider
    embeddings: EmbeddingProvider
    guardrails: Guardrails

    async def close(self) -> None:
        await self.store.close()


def build_deps(settings: Settings | None = None) -> Deps:
    settings = settings or get_settings()

    if settings.is_local:
        log.info("building deps in LOCAL mode (no GCP calls)")
        store: DocStore = InMemoryDocStore()
        storage: MediaStorage = LocalMediaStorage(settings.local_media_dir)
        images: ImageProvider = LocalImageProvider()
        embeddings: EmbeddingProvider = LocalEmbeddingProvider(settings.embedding_dim)
    else:
        log.info("building deps in GCP mode (project=%s)", settings.project_id)
        store = FirestoreDocStore(settings.project_id, settings.firestore_database)
        storage = GcsMediaStorage(settings.gcs_bucket, settings.project_id)
        # Replicate rather than Vertex AI Imagen -- see providers/replicate.py.
        images = ReplicateImageProvider(
            settings.replicate_api_token, wait_seconds=settings.replicate_wait_seconds
        )
        embeddings = VertexEmbeddingProvider(
            settings.project_id, settings.location, settings.embedding_model, settings.embedding_dim
        )

    return Deps(
        settings=settings,
        store=store,
        storage=storage,
        images=images,
        embeddings=embeddings,
        guardrails=Guardrails(
            store,
            rate_capacity=settings.rate_capacity,
            rate_refill_seconds=settings.rate_refill_seconds,
            daily_token_cap=settings.daily_token_cap,
            daily_image_cap=settings.daily_image_cap,
        ),
    )
