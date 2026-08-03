"""Chunking, ingestion and KNN retrieval."""

from __future__ import annotations

from app.rag.corpus import chunk_markdown, ingest_directory
from app.rag.search import FirestoreVectorBackend, synthesise_answer
from app.store import keys


def test_chunking_is_heading_aware():
    chunks = chunk_markdown(
        "# Title\n\nIntro text.\n\n## Section A\n\nBody A.\n\n## Section B\n\nBody B.\n",
        doc_name="doc",
    )
    headings = {c.heading for c in chunks}
    assert "Section A" in headings
    assert "Section B" in headings
    # The heading is prepended to the body so retrieval sees the context.
    assert any(c.text.startswith("Section A") for c in chunks)


def test_chunk_ids_are_content_hashes():
    """Ingestion is idempotent: re-running rewrites only what changed."""
    first = chunk_markdown("# A\n\nStable body.\n", doc_name="doc")
    again = chunk_markdown("# A\n\nStable body.\n", doc_name="doc")
    changed = chunk_markdown("# A\n\nDifferent body.\n", doc_name="doc")

    assert [c.chunk_id for c in first] == [c.chunk_id for c in again]
    assert [c.chunk_id for c in first] != [c.chunk_id for c in changed]


async def test_ingest_then_retrieve_ranks_the_right_passage(deps, corpus):
    stats = await ingest_directory(deps.store, deps.embeddings, directory=corpus)
    assert stats["documents"] == 2
    assert stats["chunks"] > 0

    backend = FirestoreVectorBackend(deps.store, deps.embeddings)

    passages = await backend.retrieve("why not vertex ai vector search index nodes?", limit=3)
    assert passages
    assert "Vector Search" in passages[0].text or "vector" in passages[0].text.lower()

    passages = await backend.retrieve("does the token bucket fail open?", limit=3)
    assert passages
    assert "guardrails" in passages[0].doc


async def test_reingestion_is_idempotent(deps, corpus):
    await ingest_directory(deps.store, deps.embeddings, directory=corpus)
    first = len(await deps.store.list(keys.RAG_CHUNKS))

    await ingest_directory(deps.store, deps.embeddings, directory=corpus)
    assert len(await deps.store.list(keys.RAG_CHUNKS)) == first


async def test_missing_corpus_directory_is_not_fatal(deps, tmp_path):
    stats = await ingest_directory(
        deps.store, deps.embeddings, directory=tmp_path / "does-not-exist"
    )
    assert stats == {"documents": 0, "chunks": 0}


async def test_irrelevant_query_returns_ungrounded(deps, corpus):
    await ingest_directory(deps.store, deps.embeddings, directory=corpus)
    backend = FirestoreVectorBackend(deps.store, deps.embeddings)

    passages = await backend.retrieve("zebra marmalade trombone dentistry", limit=3)
    answer = synthesise_answer("zebra marmalade trombone dentistry", passages)
    assert answer["grounded"] is False
    assert answer["citations"] == []


async def test_search_endpoint_exposes_passages(client, deps, corpus):
    await ingest_directory(
        client.app.state.deps.store, client.app.state.deps.embeddings, directory=corpus
    )
    body = client.get("/api/rag/search", params={"q": "daily image cap"}).json()

    assert body["grounded"] is True
    assert body["passages"]
    assert 0.0 <= body["passages"][0]["distance"] <= 2.0
    assert deps is not None


# --- relevance gating ------------------------------------------------------
def test_gate_drops_noise_and_tail():
    """Two gates: absolute (is anything relevant?) and relative (given the best
    hit, is this one still worth its tokens?)."""
    from app.rag.search import Passage, gate

    passages = [
        Passage("a", "h", "t", 0.30),  # best
        Passage("b", "h", "t", 0.40),  # within margin
        Passage("c", "h", "t", 0.60),  # tail: far behind the best
        Passage("d", "h", "t", 0.95),  # noise: past the absolute cutoff
    ]
    kept = gate(passages, max_distance=0.82)
    assert [p.doc for p in kept] == ["a", "b"]


def test_gate_returns_nothing_when_all_are_noise():
    from app.rag.search import Passage, gate

    assert gate([Passage("a", "h", "t", 0.9)], max_distance=0.82) == []


def test_threshold_is_a_property_of_the_embedder(deps):
    """A hashed bag-of-words vector and a dense vector do not produce comparable
    distances, so one hardcoded cutoff would mis-calibrate one of them."""
    from app.providers.embeddings import LocalEmbeddingProvider

    assert deps.embeddings.max_distance == LocalEmbeddingProvider().max_distance
    assert 0.5 < deps.embeddings.max_distance < 1.0


async def test_short_query_still_retrieves(deps, corpus):
    """Regression: a 1-3 token query has a sparse vector, so its cosine against a
    long chunk is inflated even when the chunk is exactly right. A cutoff tuned
    on long queries silently returned nothing for these."""
    await ingest_directory(deps.store, deps.embeddings, directory=corpus)
    backend = FirestoreVectorBackend(deps.store, deps.embeddings)

    assert await backend.retrieve("guardrails", limit=3)
    assert await backend.retrieve("vector search", limit=3)
