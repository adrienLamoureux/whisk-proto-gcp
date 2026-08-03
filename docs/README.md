# Documentation

This directory is also the **RAG corpus** — the agent answers questions about the
system by retrieving from these files, so what it tells you and what you read
cannot drift apart. Ask it in the app's *docs* tab, or:

```bash
curl 'http://localhost:8080/api/rag/search?q=why+not+vertex+ai+vector+search'
```

## Start here

| If you want… | Read |
|---|---|
| To rehearse explaining this out loud | [`interview-notes.md`](./interview-notes.md) |
| To understand how it fits together | [`architecture.md`](./architecture.md) |
| The AWS↔GCP equivalences | [`aws-gcp-mapping.md`](./aws-gcp-mapping.md) |
| To call the API | [`api.md`](./api.md) |

## By topic

| Doc | Covers |
|---|---|
| [`architecture.md`](./architecture.md) | System shape, layers, data model, security posture, testing, known gaps |
| [`agent-loop.md`](./agent-loop.md) | Tool classes, turn ordering, guardrails, memory, the offline stub |
| [`aws-gcp-mapping.md`](./aws-gcp-mapping.md) | The equivalence table — and the five rows where the naive mapping is wrong |
| [`cost-model.md`](./cost-model.md) | Idle cost by component, the standing-charge traps, unit economics, the cost of *waiting* |
| [`protocols.md`](./protocols.md) | A2A vs MCP, when each is the wrong choice, and the mounting details that bite |
| [`auth-and-hosting.md`](./auth-and-hosting.md) | Identity Platform, token lifecycle, Firebase Hosting rewrites |
| [`api.md`](./api.md) | All 23 endpoints, auth tiers, error shapes |
| [`interview-notes.md`](./interview-notes.md) | Talking points, expected follow-ups, weaknesses to name first |

## Decisions

Each ADR states the alternatives, the cost of the one chosen, and the **trigger
to revisit** it.

| ADR | Decision | Because |
|---|---|---|
| [001](./adr/001-firestore-vectors-over-vector-search.md) | Firestore vectors over Vertex AI Vector Search | An index endpoint means always-on nodes |
| [002](./adr/002-firestore-session-service.md) | A custom Firestore ADK session service | The three built-ins mean lost state, Cloud SQL, or Agent Engine |
| [003](./adr/003-cloud-run-over-agent-engine.md) | Cloud Run over Agent Engine | Scales to zero; the same ADK agent deploys to either |
| [004](./adr/004-intent-tools-over-native-confirmation.md) | Return-a-proposal over ADK's native confirmation | Keeps the HTTP surface request/response; one authorisation path |
| [005](./adr/005-firebase-hosting-over-load-balancer.md) | Firebase Hosting over Cloud CDN + ALB | A forwarding rule is ~$18/mo before any traffic |
| [006](./adr/006-replicate-animagine-over-imagen.md) | AnimagineXL on Replicate over Vertex AI Imagen | Better anime output at ~1/10 the cost per image |

## The through-line

One constraint drove most of this: **nothing may bill when idle.** Four of the
natural GCP answers carry a standing monthly charge, and each was replaced with a
pay-per-use path plus a written note on when to switch back.

| Tempting | Standing cost | Used instead |
|---|---|---|
| Vertex AI Vector Search | Always-on index nodes | Firestore `find_nearest` |
| Global external ALB + Cloud CDN | ~$18/mo forwarding rule | Firebase Hosting |
| Cloud SQL (ADK `DatabaseSessionService`) | ~$10–25/mo | Custom Firestore session service |
| Memorystore (shared config cache) | ~$35/mo | Per-instance 60s cache |

The point is not that cheap beat correct. It is that each substitution has a
stated limit and a threshold at which the expensive option starts earning its
keep.
