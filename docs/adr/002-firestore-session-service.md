# ADR-002: A custom Firestore ADK session service

**Status:** accepted · **Date:** 2026-08-03

## Context

ADK needs a `SessionService` to persist conversation state. It ships three, and
on a scale-to-zero Cloud Run deployment each has a disqualifying problem:

| Option | Problem |
|---|---|
| `InMemorySessionService` | State lives in the process. Cloud Run scales to zero and recycles instances freely, so a user's conversation vanishes between turns. |
| `DatabaseSessionService` | Durable, but wants a SQL database. On GCP that means Cloud SQL, billing for a running instance around the clock (~$10–25/mo minimum). |
| `VertexAiSessionService` | Managed and durable, but couples session storage to Agent Engine. |

Cloud SQL is the sharpest conflict: a chat-history table would have been the
single largest line on the bill — larger than the model spend it exists to
support.

## Decision

Implement `FirestoreSessionService(BaseSessionService)` over the Firestore
instance already in the stack.

Persistence shape:

```
users/{uid}/agentSessions/{sid}             metadata + session state
users/{uid}/agentSessions/{sid}/messages/   one document per ADK Event
```

## Rationale

- Firestore is already a dependency; this is an adapter, not a new database.
- Durable across cold starts, pay-per-operation, $0 idle.
- `BaseSessionService` is a small interface — four abstract methods plus
  `append_event`.

**One document per event, not an array.** Firestore documents cap at 1 MiB, and
appending to an array rewrites the entire document. Per-event documents make
`append_event` a single small write. This is the same reasoning behind the AWS
original's `SESSION#{id}#MSG#{ts}` sort keys.

## Consequences

- Reading a session is a collection read; the working set is bounded at
  `MAX_EVENTS = 60` to keep it predictable.
- A single unparseable event (schema drift across an ADK upgrade) is dropped with
  a warning rather than making the whole conversation unreadable.
- Rolling-summary compaction is **not** implemented. It belongs behind this
  interface, and the bounded working set is the cheap half of the same job.
- If the project later adopts Agent Engine, `VertexAiSessionService` becomes the
  natural choice and this class is deleted.
