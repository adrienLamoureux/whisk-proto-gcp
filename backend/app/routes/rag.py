"""Grounded search over the studio documentation.

The same retrieval the `search_studio_docs` tool uses, exposed directly so the
frontend can show *what was retrieved* alongside the answer. Being able to see
the passages and their distances is the difference between trusting a RAG system
and hoping.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..auth import User, current_user
from ..deps import Deps
from ..rag.corpus import ingest_directory
from ..rag.search import FirestoreVectorBackend, synthesise_answer
from .common import get_deps

router = APIRouter(prefix="/api/rag")


@router.get("/search")
async def search(
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=4, ge=1, le=10),
    deps: Deps = Depends(get_deps),
) -> dict:
    backend = FirestoreVectorBackend(deps.store, deps.embeddings)
    passages = await backend.retrieve(q, limit=limit)
    answer = synthesise_answer(q, passages)
    return {
        **answer,
        "passages": [
            {
                "doc": p.doc,
                "heading": p.heading,
                "text": p.text,
                "distance": round(p.distance, 4),
            }
            for p in passages
        ],
    }


class ReingestResponse(BaseModel):
    documents: int
    chunks: int


@router.post("/reingest", response_model=ReingestResponse)
async def reingest(
    user: User = Depends(current_user),
    deps: Deps = Depends(get_deps),
) -> dict:
    """Rebuild the corpus from `docs/`.

    Any signed-in user may trigger it: ingestion is idempotent (chunk ids are
    content hashes) and reads from a directory baked into the container image,
    so the worst case is wasted embedding calls, not corruption.
    """
    return await ingest_directory(
        deps.store, deps.embeddings, directory=deps.settings.corpus_dir
    )
