# Architecture

The authoritative reference for how this system is put together. For *why* each
piece was chosen over the obvious alternative, see the [ADRs](./adr/); for the
AWS equivalences, [`aws-gcp-mapping.md`](./aws-gcp-mapping.md).

> Snapshot: 2026-08-03 · 36 backend modules (~5.0k LOC) · 15 frontend modules
> (~2.5k LOC) · 23 HTTP endpoints · 9 agent tools · 99 tests passing

---

## 1. What it is

An anime image studio where the same engine is reachable two ways: a
conventional form UI, and a conversational agent that calls real tools. A user
signs in, generates images, browses their library and the community gallery, asks
questions about how the system works, and can drive all of it by talking to it.

It is a port of the interesting slice of an AWS/Bedrock original
(`static-web-aws-ai`), rebuilt on Google's stack.

**The governing constraint is that no component may bill when idle.** That single
rule decided more of this design than any other consideration — four of the
"obvious" GCP services carry a standing monthly charge, and avoiding each one is
a documented decision rather than an accident.

---

## 2. System shape

```
                          Browser
                             │
                             ▼
              ┌──────────────────────────────┐
              │   Firebase Hosting (CDN)     │  static bundle from the edge
              │   free tier · no LB · $0     │  immutable assets, no-cache HTML
              └──────────────┬───────────────┘
                             │  rewrites — proxy, not redirect
                             │  so the browser sees ONE origin
                             ▼
   ┌─────────────────────────────────────────────────────┐
   │  Cloud Run  ·  min-instances=0  ·  $0 idle          │
   │                                                      │
   │  FastAPI                                             │
   │    /api          REST     → the React frontend       │
   │    /a2a/critic   A2A      → other agents             │
   │    /mcp          MCP      → agents wanting our tools │
   │                                                      │
   │  ADK LlmAgent — 9 tools in 5 classes                 │
   └───┬──────────┬───────────┬───────────┬───────────────┘
       │          │           │           │
  Vertex AI   Replicate   Firestore   Cloud Storage
  Gemini +   AnimagineXL   (Native)    private objects
  embeddings     3.1                   + V4 signed URLs
       │          │           │           │
       ▼          ▼           ▼           ▼
   per token  per GPU-s    per op      per GB
    $0 idle    $0 idle     $0 idle     ~$0 idle

  Identity Platform — JWT, free < 50k MAU
  Secret Manager    — Replicate token (~$0.06/mo, the only non-zero line)
```

No VM, no load balancer, no managed SQL instance, no Redis, no always-on vector
index endpoint. Full accounting in [`cost-model.md`](./cost-model.md).

---

## 3. Request flow

1. The browser loads the React bundle from **Firebase Hosting's CDN** and fetches
   `/config.json` for its runtime wiring. The bundle has no baked-in backend URL,
   so one artifact promotes across environments.
2. **Identity Platform** issues a JWT. It rides on every API call as a bearer
   token; Hosting rewrites proxy `/api` to Cloud Run, so it is same-origin and no
   request ever needs a CORS preflight.
3. FastAPI verifies the token (`app/auth.py`), resolves `Deps`, and dispatches.
4. **Agent turns** run the ADK loop — guardrails, then model, then tool, then a
   short closing turn. **Form requests** call the same domain functions directly.
5. **Image generation** calls Replicate. If the prediction lands inside the
   `Prefer: wait=5` window the response carries the image; otherwise it carries a
   prediction id and the browser polls `/api/images/status` with backoff.
6. Media bytes go to **Cloud Storage**, metadata to **Firestore**. Objects are
   private; every read is a freshly-minted 15-minute signed URL.

---

## 4. Layers

| Layer | Path | Notes |
|---|---|---|
| App factory / route hub | `backend/app/main.py` | Mounts REST + A2A + MCP; serves the SPA in dev |
| Composition root | `backend/app/deps.py` | One flat `Deps`; the only place that branches on mode |
| Config | `backend/app/config.py` | `WHISK_MODE` = `local` \| `gcp`; fails fast on a bad deploy |
| Auth | `backend/app/auth.py` | Identity Platform JWT; dev header only while local |
| Agent | `backend/app/agent/` | `LlmAgent`, tool fleet, guardrails, session service, offline stub |
| Providers | `backend/app/providers/` | Gemini · Replicate · embeddings · storage — each with a local twin |
| Data | `backend/app/store/` | Firestore or in-memory behind one protocol |
| RAG | `backend/app/rag/` | Chunking, ingestion, KNN retrieval |
| Routes | `backend/app/routes/` | health · media · agent · rag · admin |
| Hosting | `firebase.json` | CDN, rewrites to Cloud Run, cache + security headers |
| Frontend | `frontend/src/` | Vite + React + TS, hand-built `wsk-` CSS tokens |

### Composition root

Ported from the original's "flat deps shape" decision: one container assembled at
startup and threaded into every route and every agent tool. No DI framework, no
decorators, no import-time singletons.

The payoff is that a test constructs `Deps` with fakes and calls the function
under test directly — and that "which implementation am I running?" has exactly
one place to look.

---

## 5. The two-mode design

`WHISK_MODE` selects implementations once, in `deps.py`:

**`local`** — no cloud calls at all. Gemini, Replicate, embeddings, Firestore and
Cloud Storage are in-process fakes; sign-in is a username box.

**`gcp`** — real Vertex AI, Replicate, Firestore, Cloud Storage, Identity
Platform.

What is *not* faked in local mode is the part that matters: **the agent loop is
real.** ADK's `Runner`, real tool dispatch, real argument marshalling, real
session persistence, real event streaming, real guardrails. Only
`generate_content_async` is stubbed (`agent/stub_llm.py`), by a keyword router
that emits the same `FunctionCall` shape Gemini would.

The consequence worth stating: **if a bug reproduces in local mode, it is a bug
in the wiring, not the prompt.** That is why this exists instead of a mock
library.

Nothing else in the codebase branches on the mode.

---

## 6. The agent

Nine tools in five classes, and **the class decides the control flow** — the
design idea carried over from the original.

| Class | Tools | What happens |
|---|---|---|
| server-dispatch | `generate_image`, `recall_favorites`, `browse_gallery`, `search_studio_docs` | Backend executes, then the model gets a short closing turn on the real result |
| client-action | `set_theme` | Nothing server-side; the payload rides back and React applies it |
| intent (confirm) | `share_image`, `delete_image` | Returns a **proposal**, never a write. The user approves, then the browser calls the ordinary REST endpoint |
| delegation | `critique_prompt` | Handed to a second agent, reachable over A2A |
| static | `what_can_you_do` | Canned, no model call |

The intent class is the load-bearing one: anything that publishes or destroys
goes through it, so the model can *propose* an irreversible action but never
*perform* one — and because confirmation calls the same endpoint a human would
hit, authorisation lives in exactly one place.

Depth — turn ordering, guardrail numbers, the memory model, the offline stub —
in [`agent-loop.md`](./agent-loop.md).

### Retrieval

`search_studio_docs` runs KNN over the `docs/` corpus (166 chunks) in Firestore's
native vector index, then gates results twice:

- **Absolute**, per embedding provider. The cutoff lives on the provider because
  the distance distribution is a property of the *model* — a hashed
  bag-of-words vector and a trained dense vector do not produce comparable
  numbers, so one shared constant necessarily mis-calibrates one of them.
- **Relative**, a margin behind the best hit. Once a strong match exists,
  passages far behind it are dead weight: more tokens, and irrelevant context for
  the answer to reconcile.

The absolute gate alone had a failure mode worth knowing: a one- or two-word
query produces a sparse vector whose cosine against a long chunk is inflated even
when the chunk is exactly right, so a cutoff tuned on full-sentence queries
silently returned nothing for short ones.

The answer itself is **extractive** — corpus text plus citations, not a
paraphrase through the model. It cannot hallucinate and costs zero tokens; the
agent's closing turn is where voice gets added.

---

## 7. Data model

Firestore, hierarchical. The AWS original encodes hierarchy into a DynamoDB sort
key (`USER#{sub}` + `IMG#`, `AGENT#SESSION#{id}`); Firestore models it natively,
so those become real paths.

```
users/{uid}
  media/{mediaId}                     image metadata (bytes live in GCS)
  jobs/{predictionId}                 in-flight Replicate predictions
  agentSessions/{sid}                 session metadata + ADK state
  agentSessions/{sid}/messages/{seq}  one document per ADK Event
  state/prefs                         cross-session preferences
  limits/{rate|cost|images}           the three guardrail counters
sharedMedia/{mediaId}                 public gallery index
ragChunks/{chunkId}                   doc chunk + embedding vector
costIndex/{uid}                       denormalised admin rollup
config/agent                          admin model override
```

Three things about this are deliberate:

- **One document per event, not an array.** Firestore documents cap at 1 MiB and
  appending to an array rewrites the whole document, so `append_event` would get
  slower and eventually fail. Same reasoning as the original's `#MSG#{ts}` keys.
- **`costIndex` is denormalised on write.** The original answers the admin cost
  dashboard with a DynamoDB `Scan`. Firestore bills per document read, so a
  scan-shaped query across every user is the access pattern that turns a $0
  database into a surprising one. One extra small write per turn buys a bounded,
  ordered read.
- **The id sanitiser is ported, not dropped.** In DynamoDB an unescaped id could
  inject `#MSG#` into the sort key; in Firestore it could inject `/` and escape
  into a sibling collection. Same bug class, different separator —
  `store/keys.py::safe_id`.

### The atomic-counter difference

DynamoDB's `ADD` with `ReturnValues: UPDATED_NEW` is atomic **and** returns the
new value — exactly what a "has this user blown the cap?" check needs. Firestore
has no equivalent single call:

- `Increment(n)` is atomic but **write-only**; it cannot answer the question.
- A transaction reads-then-writes atomically and **does** return the value, at
  the cost of optimistic-concurrency retries.

So `store/docstore.py` exposes both. Guardrails that gate a request use
`increment_and_read`; telemetry that merely accumulates uses `increment`. Using
the cheap one for a cap check lets two concurrent turns both observe 49/50 and
both generate.

---

## 8. Security posture

- **Object isolation.** Every media operation is scoped to `users/{uid}/`,
  checked against the *normalised* path so `users/u1/../u2/x.png` is rejected.
  User content is never a public object; reads are 15-minute signed URLs.
- **Identity never comes from the model.** Agent tools read `uid` from session
  state seeded by the verified token, never from a tool argument. A `uid`
  parameter would be prompt injection straight into an IDOR.
- **Stored state is untrusted input to the model.** Cross-session preferences are
  injected into the system prompt, so they are enum-validated on **read as well
  as write**. Validating only on write leaves rows written by any other path —
  and rows written before the validator existed — free to be injected verbatim.
- **Admin is a custom claim**, set out-of-band with the Admin SDK. The frontend
  decodes it *unverified* only to decide whether to render a tab; the signature is
  checked server-side on every request, so a tampered token buys a useless button
  and a 403.
- **Prediction ids do not authorise.** `/api/images/status` resolves a poll by job
  *ownership*, because prediction ids are not secret — they appear in logs and in
  the client.
- **The dev-header bypass is gated on the server's mode**, not on what the client
  sends, so it is unreachable on a deployed instance.
- **Secrets are secrets; public keys are not.** The Replicate token lives in
  Secret Manager and reaches the process as an env var. The Identity Platform Web
  API key is served to the browser on purpose — it identifies the project and
  authorises nothing.
- **Three independent per-user abuse limits**, all fail-open.

---

## 9. Protocol surfaces

Three protocols, one Cloud Run service. Cloud Run bills per request and scales to
zero, so splitting them would triple the cold starts and the deployment surface
while saving nothing.

| Path | Protocol | Audience |
|---|---|---|
| `/api/**` | REST | the React frontend |
| `/a2a/critic` | A2A | other agents — agent card at `/.well-known/agent-card.json` |
| `/mcp` | MCP | agents that want *these tools* — read-only, stateless |

The distinction, since they get conflated: **MCP connects an agent to
capabilities** (the far side is a server with no goals); **A2A connects an agent
to a peer** (the far side has its own reasoning loop). If the far side would need
an LLM to do its job, that is A2A. Details, including why the *studio* agent is
deliberately not exposed over A2A and why only read-only tools are on MCP, in
[`protocols.md`](./protocols.md).

---

## 10. Testing

99 tests, all offline.

| File | Covers |
|---|---|
| `test_guardrails.py` | Token bucket, both daily caps, UTC rollover, concurrency, fail-open |
| `test_security.py` | Path traversal, tenancy, pref validation on read *and* write |
| `test_auth.py` | 401 gating, the dev-header bypass being unreachable when auth is required |
| `test_agent_loop.py` | The real ADK loop: dispatch, tool classes, session durability |
| `test_replicate.py` | The provider's fast path *and* polling path, via `httpx.MockTransport` |
| `test_api.py` | The REST surface, including the paths that must fail |
| `test_rag.py` | Chunking, idempotent ingestion, KNN ranking |
| `test_surfaces.py` | A2A card contents and URL, MCP handshake and host allow-list |

Two of those exist because the bug they cover shipped once: mounted sub-apps whose
lifespan never ran (A2A/MCP 404s), and MCP's DNS-rebinding allow-list rejecting
the production host while loopback passed.

---

## 11. Known gaps

Named rather than hidden.

- **No streaming.** `/api/agent/turn` blocks until the whole loop finishes. ADK's
  `run_async` already yields events incrementally, so the agent side is ready;
  what is missing is SSE on the route and an `EventSource` client. Worth noting
  the contrast: on AWS this was *architectural* (Lambda behind API Gateway cannot
  stream), whereas Cloud Run supports streaming responses — here it is a change,
  not a redesign.
- **No memory compaction.** The original compacts above ~30 turns with a rolling
  summary. Here the working set is bounded (`MAX_EVENTS = 60`) but not
  summarised; compaction belongs behind the session-service interface.
- **Content safety is thinner than Imagen's.** AnimagineXL has no built-in filter,
  so `nsfw` leads the negative prompt and is not user-overridable. That is a
  prompt-level control, not moderation — production wants Cloud Vision SafeSearch
  on the output.
- **Email/password only.** Federated sign-in needs the Firebase SDK's
  popup/redirect flow, which is the point at which the ~200 KB dependency earns
  its place.
- **Single image provider.** The original fans out across four; that was cost
  arbitrage rather than architecture, so it is out of scope.
- **The `gcp` path has never run against a real project.** Every provider is
  written and the offline half is exercised end to end, but real Vertex AI,
  Firestore, GCS, Identity Platform, Firebase Hosting and live Replicate calls
  are unverified. The two places friction is expected are named in
  [`interview-notes.md`](./interview-notes.md).
