# Whisk Studio — GCP

A serverless anime image studio: **FastAPI + an ADK agent + Gemini + AnimagineXL
on Cloud Run**, behind **Firebase Hosting**, with a React frontend and Identity
Platform sign-in.

It is a port of the interesting slice of an AWS/Bedrock creative studio
([`static-web-aws-ai`](../static-web-aws-ai)), rebuilt on Google's stack to learn
it properly. The design constraint was **nothing may bill when idle** — which
turned out to drive most of the decisions worth talking about.

Three protocols on one service: **REST** for the frontend, **A2A** for other
agents, **MCP** for agents that want these tools.

```
Firebase Hosting (CDN, $0) ──rewrites──▶ Cloud Run (min-instances=0, $0 idle)
                                            ├── /api        REST
                                            ├── /a2a/critic A2A
                                            └── /mcp        MCP
                                         Gemini · AnimagineXL · Firestore · GCS
```

---

## Run it — no GCP account needed

```bash
make install && make dev
```

Opens on `http://localhost:8080`. Sign in with any username.

In `local` mode there are **no cloud calls at all**: Gemini, Replicate,
embeddings, Firestore and Cloud Storage are in-process fakes.

But **the agent loop is real** — ADK's `Runner`, real tool dispatch, real session
persistence, real guardrails, real event streaming. Only the model call itself is
stubbed. So if something breaks locally, it is broken in the wiring, not the
prompt. That is the whole reason it exists instead of a mock library.

Try, in the **companion** tab:

| Say this | Exercises |
|---|---|
| `draw a red fox asleep in the snow, 16:9` | server-dispatch tool → real image, quota consumed |
| `switch to the glacier theme` | client-action tool → applied by the browser |
| `how does the cost model work?` | RAG grounded in `docs/`, with citations |
| `critique: a cat on a roof` | delegation → the critic agent |
| `what can you do?` | static tool, no model call |

---

## What's in it

| | |
|---|---|
| **Agent** | ADK `LlmAgent`, 9 tools in 5 classes, Firestore-backed durable sessions |
| **Images** | AnimagineXL 3.1 on Replicate, pinned by version hash, `Prefer: wait` + client polling |
| **Guardrails** | Token bucket + daily token cap + independent daily image cap, all fail-open |
| **RAG** | Markdown → chunks → embeddings → Firestore native vector index (`find_nearest`) |
| **Auth** | Identity Platform over its REST API — no Firebase SDK, 57 KB bundle |
| **Hosting** | Firebase Hosting CDN, rewrites proxying `/api` `/a2a` `/mcp` `/media` to Cloud Run |
| **A2A** | Critic agent with an agent card at `/a2a/critic/.well-known/agent-card.json` |
| **MCP** | Read-only studio tools at `/mcp`, stateless |
| **Frontend** | Vite + React + TS, hand-built CSS tokens, 10 themes × dark/light |
| **Tests** | **99 passing** — guardrails, path safety, auth gating, the agent loop, the Replicate provider, the REST surface |

~5.0k LOC backend · ~2.5k LOC frontend · 23 HTTP endpoints.

---

## The two decisions worth reading

**Vertex AI Vector Search is the correct answer, and the wrong one here.** It
needs an *index endpoint with always-on nodes* — hundreds of dollars a month at
zero traffic, to serve a few hundred document chunks. This uses **Firestore's
native vector index** instead: same database, pay-per-op, $0 idle. The honest
limit is that it is a brute-force scan, so the crossover where Vector Search
earns its floor is around 100k vectors or sustained high QPS — at which point
`rag/search.py` swaps backends behind a protocol.
([ADR-001](docs/adr/001-firestore-vectors-over-vector-search.md))

**Image generation stayed on Replicate rather than moving to Imagen.** The
product is an anime studio; AnimagineXL 3.1 is a purpose-built SDXL fine-tune
that produces better output for that at roughly a tenth the cost per image, and
it understands the Danbooru tag vocabulary users actually type. Replicate bills
per second of GPU, so zero-idle still holds. *Being on a cloud is not an argument
for that cloud's model.* The cost: a hard dependency outside GCP in the critical
path, and Imagen's built-in safety filter replaced by a negative prompt plus a
noted gap. ([ADR-006](docs/adr/006-replicate-animagine-over-imagen.md))

Three more standing-cost traps get the same treatment — ADK's
`DatabaseSessionService` → Cloud SQL (~$25/mo), CloudFront → a global external
ALB whose forwarding rule alone is ~$18/mo, a shared config cache → Memorystore
(~$35/mo). See [`docs/cost-model.md`](docs/cost-model.md).

---

## Documentation

**[`docs/`](docs/README.md) is the index.** It is also the RAG corpus, so the
agent's answers cannot drift from what a human reader would find.

Start with [`docs/interview-notes.md`](docs/interview-notes.md) — written as
rehearsal material: what to lead with, the follow-ups this design invites, and
the weaknesses to name before you are asked. Then
[`docs/architecture.md`](docs/architecture.md) for how it all fits together, and
[`docs/adr/`](docs/adr/) for six decisions with their tradeoffs and revisit
triggers.

---

## Commands

```bash
make install    # venv + npm install for both halves
make dev        # build frontend + serve everything on :8080
make api        # backend only, with reload
make web        # frontend dev server on :5173 (proxies to :8080)
make test       # 99 backend tests + frontend typecheck/build
make smoke      # drive six agent turns through the real ADK loop
make lint       # ruff + tsc
```

---

## Deploying

Needs `gcloud`, `firebase-tools`, a GCP project with billing, and a Replicate API
token.

```bash
PROJECT_ID=your-project REPLICATE_API_TOKEN=r8_... bash deploy/setup.sh
```

```bash
PROJECT_ID=your-project IDENTITY_PLATFORM_API_KEY=AIza... bash deploy/deploy.sh
```

`setup.sh` provisions Firestore, the media bucket, Secret Manager, IAM and the
vector index. `deploy.sh` builds the container, deploys Cloud Run, then ships the
bundle to Firebase Hosting. Two steps still need the console: enabling the
Email/Password provider and copying the Web API key.

Two things `setup.sh` handles that are easy to miss, and that fail confusingly:

- **Signing GCS URLs from Cloud Run.** The runtime service account has no private
  key, so signing must delegate to the IAM Credentials API — which requires the
  account to impersonate *itself* (`roles/iam.serviceAccountTokenCreator`). Miss
  it and image reads fail at runtime while everything else works.
- **The Firestore composite vector index.** Without it, `find_nearest` fails with
  `FAILED_PRECONDITION`.

---

## Configuration

All settings are `WHISK_`-prefixed (see `backend/app/config.py`);
[`.env.example`](.env.example) has the full list.

| Variable | Default | Notes |
|---|---|---|
| `WHISK_MODE` | `local` | `local` or `gcp` — the only branch in the codebase |
| `WHISK_PROJECT_ID` | — | Required when `mode=gcp` |
| `WHISK_GCS_BUCKET` | — | Media bucket |
| `WHISK_REPLICATE_API_TOKEN` | — | Injected from Secret Manager; required when `mode=gcp` |
| `WHISK_IDENTITY_PLATFORM_API_KEY` | — | Public Web API key, served to the browser |
| `WHISK_AUTH_REQUIRED` | `false` | `true` in GCP mode — disables the dev-header path |
| `WHISK_PUBLIC_BASE_URL` | `http://localhost:8080` | The Hosting origin; the A2A card advertises absolute URLs and MCP allow-lists this host |
| `WHISK_AGENT_MODEL` | `gemini-3.6-flash` | Runtime-overridable from the Sanctum tab |
| `WHISK_DAILY_TOKEN_CAP` | `200000` | Per user, UTC rollover |
| `WHISK_DAILY_IMAGE_CAP` | `50` | Per user, independent rollover |

---

## Status

Runs end to end in `local` mode with 99 passing tests, including the Replicate
provider's fast path and polling path against a mock transport.

The `gcp` path — real Vertex AI, Firestore, GCS, Identity Platform, Firebase
Hosting and live Replicate calls — is **written but never run against a real
project**. [`docs/architecture.md` §11](docs/architecture.md#11-known-gaps) lists
that and the other known gaps.
