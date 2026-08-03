"""Retrieval + grounded answer synthesis.

`RagBackend` is a two-implementation seam. `FirestoreVectorBackend` is what runs
(see `corpus.py` for why); `VertexAiSearchBackend` documents the swap for when
the corpus outgrows a brute-force index. Callers -- the `search_studio_docs`
agent tool and `GET /api/rag/search` -- see the same shape either way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from ..providers.embeddings import EmbeddingProvider
from ..store import keys
from ..store.docstore import DocStore
from .corpus import VECTOR_FIELD

log = logging.getLogger("whisk.rag")

# How much worse than the best hit a passage may be and still be kept.
#
# The absolute cutoff lives on the embedding provider (`max_distance`), because
# the distance distribution is a property of the model. This is the second,
# *relative* gate: once a strong match exists, passages far behind it are
# dead weight -- they add tokens and give the answer irrelevant context to
# reconcile without adding information.
RELATIVE_MARGIN = 0.18


@dataclass(slots=True)
class Passage:
    doc: str
    heading: str
    text: str
    distance: float

    def cite(self) -> str:
        return f"{self.doc} > {self.heading}"


class RagBackend(Protocol):
    async def retrieve(self, question: str, *, limit: int = 4) -> list[Passage]: ...


class FirestoreVectorBackend:
    """KNN over Firestore's native vector index. Pay-per-op, $0 idle."""

    def __init__(self, store: DocStore, embeddings: EmbeddingProvider) -> None:
        self._store = store
        self._embeddings = embeddings

    async def retrieve(self, question: str, *, limit: int = 4) -> list[Passage]:
        if not question.strip():
            return []

        # RETRIEVAL_QUERY, not RETRIEVAL_DOCUMENT: the query and the corpus are
        # embedded asymmetrically, and mismatching them quietly costs recall.
        vectors = await self._embeddings.embed([question], task="RETRIEVAL_QUERY")
        if not vectors:
            return []

        rows = await self._store.vector_search(
            keys.RAG_CHUNKS, VECTOR_FIELD, vectors[0], limit=limit
        )
        passages = [
            Passage(
                doc=row.get("doc", "unknown"),
                heading=row.get("heading", ""),
                text=row.get("text", ""),
                distance=float(row.get("vector_distance", 1.0)),
            )
            for row in rows
        ]
        return gate(passages, max_distance=self._embeddings.max_distance)


def gate(passages: list[Passage], *, max_distance: float) -> list[Passage]:
    """Drop noise, then drop the tail.

    Two gates rather than one because they answer different questions:
    *is anything here relevant at all?* (absolute, model-specific) and *is this
    one worth keeping given how good the best one is?* (relative).
    """
    passages = sorted(passages, key=lambda p: p.distance)
    kept = [p for p in passages if p.distance <= max_distance]
    if not kept:
        return []
    ceiling = kept[0].distance + RELATIVE_MARGIN
    return [p for p in kept if p.distance <= ceiling]


class VertexAiSearchBackend:
    """Managed Vertex AI Search (Discovery Engine).

    Not active by default -- it needs a provisioned data store, which would stop
    this repo from running end-to-end out of the box. Pricing is per-query with
    no always-on node, so it stays inside the zero-idle rule; the reason to
    switch is corpus size, not cost.

    ADK also exposes this as a *built-in* tool (`VertexAiSearchTool`), which lets
    Gemini do retrieval server-side with no application round-trip. That is the
    better production shape; this class exists so the retrieved passages stay
    inspectable and the answer synthesis stays comparable across backends.
    """

    def __init__(self, project_id: str, location: str, data_store_id: str) -> None:
        self._project_id = project_id
        self._location = location
        self._data_store_id = data_store_id

    async def retrieve(self, question: str, *, limit: int = 4) -> list[Passage]:
        raise NotImplementedError(
            "VertexAiSearchBackend needs a provisioned Discovery Engine data store. "
            "See docs/adr/003-firestore-vectors-over-vector-search.md"
        )


def synthesise_answer(question: str, passages: list[Passage]) -> dict[str, Any]:
    """Assemble a grounded answer from retrieved passages.

    Extractive, not generative: it returns the corpus text plus citations rather
    than paraphrasing through the model. Two reasons -- it cannot hallucinate,
    and it costs zero tokens. The agent's closing turn is where the model adds
    voice, and that turn already sees this output.
    """
    if not passages:
        return {
            "question": question,
            "answer": (
                "I couldn't find that in the studio docs. Try asking about the architecture, "
                "the cost model, guardrails, or how the agent loop works."
            ),
            "citations": [],
            "grounded": False,
        }

    lead = passages[0]
    body = lead.text.strip()
    if len(body) > 700:
        body = body[:700].rsplit(" ", 1)[0] + "..."

    return {
        "question": question,
        "answer": body,
        "citations": [
            {"doc": p.doc, "heading": p.heading, "distance": round(p.distance, 4)}
            for p in passages
        ],
        "grounded": True,
    }
