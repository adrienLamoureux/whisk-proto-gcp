"""Text embeddings: Vertex AI `text-embedding-005` in `gcp` mode, hashed bag-of-words locally.

The local embedder is deliberately *not* random. It is a hashed bag-of-words
projection with L2 normalisation, so semantically overlapping strings genuinely
land near each other under cosine distance. That means the RAG retrieval path
(`rag/search.py`) returns sensible chunks offline and the whole grounding flow is
demonstrable without a Vertex AI call -- the ranking is weaker than a real dense
model, but it is not noise.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from typing import Protocol

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Dropped before hashing: with only a few hundred chunks, stopword collisions
# dominate the signal and every chunk starts looking equally relevant.
_STOPWORDS = frozenset(
    """a an and are as at be but by for from has have how in is it its of on or that the
    this to was were what when where which who why will with you your do does did can
    could should would i we they them their there here about into over under""".split()
)


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str], *, task: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]: ...

    @property
    def dimension(self) -> int: ...

    @property
    def max_distance(self) -> float:
        """Cosine distance beyond which a passage is noise, not a match.

        This belongs to the *provider*, not to the search code, because the
        distance distribution is a property of the embedding model. A hashed
        bag-of-words vector and a trained dense vector do not produce comparable
        numbers, so a single hardcoded cutoff necessarily mis-calibrates one of
        them.
        """
        ...


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


# ---------------------------------------------------------------------------
# local mode
# ---------------------------------------------------------------------------
class LocalEmbeddingProvider:
    def __init__(self, dimension: int = 768) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def max_distance(self) -> float:
        """Loose, because sparse vectors inflate distance for short queries.

        A three-word query has three non-zero components; its cosine against a
        200-word chunk is high even when the chunk is exactly right. Measured
        over this corpus, relevant queries land at 0.52-0.79 and irrelevant ones
        at 0.85-0.91, so the gate sits between them. A dense model separates far
        more sharply and wants a tighter value -- hence this being per-provider.
        """
        return 0.82

    async def embed(
        self, texts: list[str], *, task: str = "RETRIEVAL_DOCUMENT"
    ) -> list[list[float]]:
        await asyncio.sleep(0)
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self._dimension
        tokens = tokenize(text)
        if not tokens:
            return vector

        for token in tokens:
            digest = hashlib.blake2s(token.encode(), digest_size=8).digest()
            # Two buckets per token with opposing signs: the signed-hash trick
            # keeps unrelated collisions from accumulating in one direction.
            slot_a = int.from_bytes(digest[:4], "little") % self._dimension
            slot_b = int.from_bytes(digest[4:], "little") % self._dimension
            vector[slot_a] += 1.0
            vector[slot_b] -= 0.5

        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            return vector
        return [v / norm for v in vector]


# ---------------------------------------------------------------------------
# gcp mode
# ---------------------------------------------------------------------------
class VertexEmbeddingProvider:
    def __init__(self, project_id: str, location: str, model: str, dimension: int = 768) -> None:
        from google import genai

        self._client = genai.Client(vertexai=True, project=project_id, location=location)
        self._model = model
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def max_distance(self) -> float:
        """Tighter than the local twin: a trained dense model puts genuinely
        related text well below 0.5 and unrelated text near 1.0, so a strict gate
        costs little recall and keeps irrelevant context out of the answer."""
        return 0.65

    async def embed(
        self, texts: list[str], *, task: str = "RETRIEVAL_DOCUMENT"
    ) -> list[list[float]]:
        from google.genai import types

        if not texts:
            return []

        # Asymmetric embedding: documents and queries must be embedded with
        # different task types or retrieval quality drops noticeably.
        response = await asyncio.to_thread(
            self._client.models.embed_content,
            model=self._model,
            contents=texts,
            config=types.EmbedContentConfig(
                task_type=task, output_dimensionality=self._dimension
            ),
        )
        return [list(e.values) for e in response.embeddings]
