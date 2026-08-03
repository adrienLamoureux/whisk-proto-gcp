# ADR-001: Firestore native vectors over Vertex AI Vector Search

**Status:** accepted · **Date:** 2026-08-03

## Context

The AWS original uses OpenSearch / pgvector for retrieval. The direct GCP
equivalent is **Vertex AI Vector Search** (formerly Matching Engine), and it is
genuinely excellent: ScaNN under the hood, scales to billions of vectors, tuned
recall/latency tradeoffs.

The project constraint is that no component may bill when idle.

Vector Search serves queries from an **index endpoint with deployed nodes**.
Those nodes are VMs, billed hourly, whether or not a query arrives. The smallest
practical deployment runs into the hundreds of dollars per month at zero traffic.

The corpus here is the `docs/` directory — a few hundred chunks.

## Decision

Use **Firestore's native vector index** (`find_nearest`, COSINE) for retrieval.

Keep `RagBackend` as a protocol with a second implementation
(`VertexAiSearchBackend`) marking the swap point.

## Rationale

- Firestore is already in the stack for sessions, media and counters. This adds a
  query type, not a dependency.
- Pay-per-operation, $0 idle — satisfies the constraint.
- At a few hundred chunks the recall difference against ScaNN is not measurable.
- One component costing 100× everything else combined, to serve queries a
  brute-force index answers in milliseconds, is a bad trade regardless of how
  good that component is.

## Consequences

**Accepted cost.** Firestore's vector index is a brute-force scan behind a query
planner, so latency grows with collection size. This does not scale to a large
corpus.

**Trigger to revisit:** roughly O(100k) vectors, or sustained high QPS. At that
point the always-on nodes are earning their cost and `search.py` swaps backends
without touching a caller.

**Also considered: Vertex AI Search (Discovery Engine).** Pay-per-query, no
always-on node — it *does* fit the cost rule, and ADK ships `VertexAiSearchTool`
for it, which lets Gemini retrieve server-side with no application round-trip.
That is the better production shape. Rejected here only because it needs a
provisioned data store, which would stop this repo running end-to-end before a
reader creates GCP resources.
