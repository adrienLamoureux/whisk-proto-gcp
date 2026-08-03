# The agent loop

The architecturally interesting subsystem, and the one worth being able to draw
on a whiteboard.

## One turn, end to end

```
POST /api/agent/turn
  │
  ├─ 1. token bucket        users/{uid}/limits/rate      ← cheapest rejection first
  ├─ 2. daily token cap     users/{uid}/limits/cost
  ├─ 3. load preferences    users/{uid}/state/prefs      ← validated on read
  ├─ 4. build LlmAgent      instruction assembled per-turn
  │
  ├─ 5. Runner.run_async ──▶ Gemini  ──▶ function_call
  │                            │
  │                            ├─ tool executes (server-side)
  │                            │
  │                            └─▶ Gemini (closing turn) ──▶ text
  │
  └─ 6. record spend        users/{uid}/limits/cost + costIndex/{uid}
```

Guardrails run **before** the model, never after. The point of a cap is not to
spend the money; a check that runs post-response has already lost.

## Tool classes determine control flow

This is the design idea carried over from the original, and it is the thing to
lead with in an interview. Tools are not a flat list — they fall into classes,
and the class decides what happens:

| Class | Tools | What happens |
|---|---|---|
| **server-dispatch** | `generate_image`, `recall_favorites`, `browse_gallery`, `search_studio_docs` | Backend executes, then the model gets a short closing turn to react to the actual result |
| **client-action** | `set_theme` | Nothing happens server-side; the payload rides back and React applies it |
| **intent (confirm)** | `share_image`, `delete_image` | The tool returns a **proposal**, never a write. The user approves, and the browser calls the ordinary REST endpoint |
| **delegation** | `critique_prompt` | Handed to a second agent over A2A |
| **static** | `what_can_you_do` | Canned, no model call |

The intent class is the important one. Anything that publishes or destroys goes
through it, so the model can *propose* an irreversible action but can never
*perform* one. The confirm path reuses the same REST endpoint a human would hit,
so authorisation lives in exactly one place regardless of who initiated it.

> ADK also has a **native** confirmation protocol
> (`FunctionTool(require_confirmation=True)` + `ToolContext.request_confirmation`),
> which suspends the invocation and resumes it after approval. That is the more
> integrated mechanism. This port uses the return-a-proposal pattern instead
> because it matches the original's shape and keeps the HTTP surface a plain
> request/response — the tradeoff is written up in
> [`adr/004-intent-tools-over-native-confirmation.md`](./adr/004-intent-tools-over-native-confirmation.md).

## Tool descriptions are prompt text

The docstrings in `agent/tools.py` are not documentation for humans. They are the
tool descriptions the model sees, and they decide whether the right tool gets
picked. `recall_favorites` and `browse_gallery` both return a grid of images; the
only thing separating them is the sentence explaining that one is *the caller's
own work* and the other is *other people's*. Edit them like prompt text.

## Identity never comes from the model

`uid` is read from ADK session state, seeded by the runner from the verified
token:

```python
def _uid(tool_context: ToolContext) -> str:
    uid = (tool_context.state or {}).get("uid")
    if not uid:
        raise PermissionError("unauthorized")
    return str(uid)
```

If `uid` were a tool *parameter*, the model could be talked into passing someone
else's — prompt injection straight into an IDOR. This is the single most
important line in the tool layer.

## Preferences and self-injection

Cross-session preferences (`{lastStyle, lastAspect, theme}`) are injected into
the system prompt as `<prefs>…</prefs>` so tool defaults drift toward what the
user keeps choosing.

That injection is what makes the validation interesting: **these values flow back
into the prompt, so stored state is untrusted input to the model.** Validating
only on write leaves two holes — any other write path bypasses the check, and
records written before the validator existed are still in the database. So
`validate_pref` runs on read *and* write, and anything failing is dropped
silently rather than raised.

This is the self-injection loop most applications miss.

## Guardrails

Three limiters, deliberately separate:

| Limiter | Storage | Default | Why separate |
|---|---|---|---|
| Token bucket | `limits/rate` | 30 capacity, 1 token / 2s | Burst control |
| Daily token cap | `limits/cost` | 200k/day, UTC rollover | Bounds model spend |
| Daily image cap | `limits/images` | 50/day, UTC rollover | Bounds the *dominant* cost |

The image cap is not a nicety. Image generation is the largest cost vector, so a
token-only cap leaves it unbounded. Two vectors with different abuse profiles get
two counters with independent rollovers.

The token bucket refills **lazily** — elapsed time is computed at check time from
`lastRefillAt`, so there is no background job and nothing runs between requests.
That is what keeps it serverless.

**Every limiter fails open.** A Firestore hiccup must not become a site-wide
outage; "briefly un-rate-limited" beats "nobody can use the product", and the
daily caps still backstop real abuse.

The image cap uses `increment_and_read` (a transaction) rather than
check-then-consume, because split checks let two concurrent turns both observe
49/50 and both generate. The database decides the winner, not request ordering.

## Sessions on Firestore

ADK ships three session services and on a scale-to-zero deployment each has a
problem:

- `InMemorySessionService` — state lives in the process. Cloud Run recycles
  instances freely, so conversations vanish between turns.
- `DatabaseSessionService` — durable, but wants SQL. On GCP that is Cloud SQL,
  billing around the clock. A chat-history table would become the largest line on
  the bill, larger than model spend.
- `VertexAiSessionService` — managed and durable, but couples session storage to
  Agent Engine.

Firestore was already in the stack, is durable, bills per operation and costs
nothing idle — so the missing piece was an adapter, not another database.
`agent/session_service.py` implements `BaseSessionService` over it.

## The offline stub

`agent/stub_llm.py` is a `BaseLlm` subclass that keyword-routes to a tool and
fabricates a `FunctionCall` in the shape Gemini would emit.

**Real:** `Runner`, `LlmAgent`, session service, event streaming, tool dispatch,
argument marshalling, the `function_response` round-trip, state deltas, the
closing turn. All ADK's own code paths, unmodified.

**Faked:** only `generate_content_async`.

It also populates `usage_metadata`, because the cost telemetry and daily cap read
it — and a cap that only ever executes in production is a cap you cannot trust.

One subtlety worth knowing, because it is a bug that hides well: the stub must
inspect **only the final content block** for `function_response` parts. Scanning
the whole conversation makes every turn after the first look like a closing turn,
which silently disables tool calling from turn two onward.
