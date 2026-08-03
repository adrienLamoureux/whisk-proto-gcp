"""Corpus ingestion: markdown -> chunks -> embeddings -> Firestore vectors.

This is the "Bedrock Knowledge Bases" slot in the mapping table, and it is the
one place where the obvious GCP answer is the wrong one for this project.

## Why not Vertex AI Vector Search

Vertex AI Vector Search (formerly Matching Engine) is genuinely excellent -- ScaNN
under the hood, scales to billions of vectors. But serving requires an **index
endpoint with deployed nodes**, and deployed nodes are VMs that bill by the hour
whether or not anyone queries them. The smallest practical deployment runs into
the hundreds of dollars per month *at zero traffic*. For a portfolio app with a
few hundred chunks and spiky traffic, that single component would cost more than
everything else combined by two orders of magnitude.

## Why not Vertex AI Search / RAG Engine

Vertex AI Search (Discovery Engine) is the managed, pay-per-query option and has
no always-on node -- it is a legitimate fit, and ADK ships `VertexAiSearchTool`
for exactly this. It is wired but unused here for one reason: it needs a
provisioned data store, which means this repo could not run end-to-end before the
reader creates GCP resources. Enabling it is a config change, not a rewrite --
see `rag/search.py::VertexAiSearchBackend`.

## What this uses instead

**Firestore's native vector index.** Firestore gained KNN (`find_nearest`, COSINE
/ EUCLIDEAN / DOT_PRODUCT) as a first-class query. It is the same database
already holding sessions, media and counters, it bills per operation, and it
costs nothing when idle. For a corpus this size the recall difference against
ScaNN is not measurable.

**The honest limit:** Firestore's vector index is a brute-force scan behind a
query planner, so latency grows with collection size. The crossover where
Vector Search wins is roughly O(100k) vectors or sustained high QPS. Below that,
paying for idle nodes buys nothing. Above it, `search.py` swaps backends without
touching a caller.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..providers.embeddings import EmbeddingProvider
from ..store import keys
from ..store.docstore import DocStore

log = logging.getLogger("whisk.rag")

VECTOR_FIELD = "embedding"
TARGET_CHUNK_CHARS = 900
CHUNK_OVERLAP_CHARS = 150


@dataclass(slots=True)
class Chunk:
    chunk_id: str
    doc: str
    heading: str
    text: str


def chunk_markdown(text: str, *, doc_name: str) -> list[Chunk]:
    """Split on headings first, then pack to a target size with overlap.

    Heading-aware because a chunk that straddles two sections retrieves badly for
    both. Overlap because a fact that lands exactly on a chunk boundary is
    otherwise unretrievable -- the cheapest fix for the most annoying RAG failure.
    """
    sections = _split_by_heading(text)
    chunks: list[Chunk] = []

    for heading, body in sections:
        body = body.strip()
        if not body:
            continue
        for piece in _pack(body):
            payload = f"{heading}\n\n{piece}" if heading else piece
            chunk_id = hashlib.blake2s(
                f"{doc_name}:{payload}".encode(), digest_size=10
            ).hexdigest()
            chunks.append(
                Chunk(chunk_id=chunk_id, doc=doc_name, heading=heading or doc_name, text=payload)
            )
    return chunks


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)


def _split_by_heading(text: str) -> list[tuple[str, str]]:
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [("", text)]

    sections: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        sections.append(("", text[: matches[0].start()]))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((match.group(2).strip(), text[match.end() : end]))
    return sections


def _pack(body: str) -> list[str]:
    if len(body) <= TARGET_CHUNK_CHARS:
        return [body]

    pieces: list[str] = []
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > TARGET_CHUNK_CHARS:
            pieces.append(current)
            current = current[-CHUNK_OVERLAP_CHARS:] + "\n\n" + paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current.strip():
        pieces.append(current)
    return pieces


async def ingest_directory(
    store: DocStore,
    embeddings: EmbeddingProvider,
    *,
    directory: str | Path,
    batch_size: int = 32,
) -> dict[str, int]:
    """(Re)build the corpus. Idempotent: chunk ids are content hashes.

    Re-running after an edit rewrites changed chunks and leaves the rest alone,
    so ingestion is cheap enough to run on every deploy.
    """
    root = Path(directory)
    if not root.exists():
        log.warning("corpus directory %s does not exist; nothing ingested", root)
        return {"documents": 0, "chunks": 0}

    all_chunks: list[Chunk] = []
    files = sorted(root.rglob("*.md"))
    for path in files:
        all_chunks.extend(
            chunk_markdown(path.read_text(encoding="utf-8"), doc_name=path.stem)
        )

    for start in range(0, len(all_chunks), batch_size):
        batch = all_chunks[start : start + batch_size]
        vectors = await embeddings.embed(
            [c.text for c in batch], task="RETRIEVAL_DOCUMENT"
        )
        for chunk, vector in zip(batch, vectors, strict=True):
            await store.set(
                f"{keys.RAG_CHUNKS}/{chunk.chunk_id}",
                {
                    "doc": chunk.doc,
                    "heading": chunk.heading,
                    "text": chunk.text,
                    VECTOR_FIELD: _as_vector(store, vector),
                },
            )

    log.info("ingested %d chunks from %d documents", len(all_chunks), len(files))
    return {"documents": len(files), "chunks": len(all_chunks)}


def _as_vector(store: DocStore, values: list[float]):
    """Firestore needs a `Vector` wrapper to build a vector index; the in-memory
    store just needs the list. Keeping the conversion here means `corpus.py` is
    the only file that knows the difference."""
    if type(store).__name__ == "FirestoreDocStore":
        from google.cloud.firestore_v1.vector import Vector

        return Vector(values)
    return values
