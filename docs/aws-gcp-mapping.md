# AWS to GCP: the mapping, and where it breaks down

The original Whisk Studio is a fully-serverless AI creative studio on AWS. This
repo ports a slice of it to GCP. The table below is the starting point; the
sections after it are the parts where a one-to-one swap is wrong, which is where
the actual engineering is.

## The table

| AWS (original) | GCP (this port) | Status here |
|---|---|---|
| Bedrock (Converse) | Vertex AI / Gemini | Used — `gemini-3.6-flash` default |
| Bedrock Agents | **ADK** (Agent Development Kit) | Used — `LlmAgent` + 9-tool fleet |
| Lambda + API Gateway | **Cloud Run** | Used — one service, min-instances 0 |
| Lambda/Fargate for agent hosting | **Agent Engine** | Documented, not used — see below |
| Bedrock Knowledge Bases | Vertex AI Search / RAG Engine | Substituted — see below |
| OpenSearch / pgvector | Vertex AI Vector Search | **Substituted — Firestore vectors** |
| DynamoDB | **Firestore** (Native) | Used |
| S3 | **Cloud Storage** | Used |
| Cognito | **Identity Platform** | Used — email/password over the REST API |
| S3 + CloudFront (static site) | **Firebase Hosting** | Used — rewrites proxy to Cloud Run |
| Secrets Manager | **Secret Manager** | Used — Replicate token via `--set-secrets` |
| CloudWatch | Cloud Logging / Monitoring | Structured logs |
| Claude via Bedrock | Gemini 3.1 Pro / 3.6 Flash | Used |
| Replicate (image gen) | **Replicate** — unchanged | Used — AnimagineXL 3.1, the only generator |
| — | **A2A protocol** | Used — critic agent at `/a2a/critic` |
| MCP | **MCP** | Used — server at `/mcp` |

Note the last-but-one row. The image generator did **not** move to a GCP service,
and that is a deliberate decision rather than an unfinished port —
[ADR-006](./adr/006-replicate-animagine-over-imagen.md).

## Where the naive mapping is wrong

Three rows in that table would, if followed literally, produce a system that
costs hundreds of dollars a month at zero traffic. The constraint driving this
port was **no standing bill** — every component must cost approximately nothing
when nobody is using it.

### 1. Vector Search → Firestore's native vector index

**The trap.** Vertex AI Vector Search is the obvious answer for "OpenSearch /
pgvector on GCP", and it is genuinely excellent — ScaNN under the hood, scales to
billions of vectors. But serving requires an **index endpoint with deployed
nodes**, and those nodes are VMs billed by the hour whether or not a single query
arrives. The smallest practical deployment runs into the hundreds of dollars per
month at idle. For a corpus of a few hundred document chunks, that one component
would cost more than the entire rest of the system by two orders of magnitude.

**What this uses instead.** Firestore gained KNN vector queries as a first-class
feature: `find_nearest` with COSINE, EUCLIDEAN or DOT_PRODUCT. It is the same
database already storing sessions, media and counters, it bills per operation,
and it costs nothing idle.

**The honest limit.** Firestore's vector index is a brute-force scan behind a
query planner, so latency grows with collection size. The crossover where Vector
Search earns its floor is roughly O(100k) vectors or sustained high QPS. Below
that, paying for idle nodes buys nothing. Above it, `rag/search.py` swaps
backends behind the `RagBackend` protocol without touching a caller.

### 2. Knowledge Bases → Vertex AI Search, considered

Vertex AI Search (Discovery Engine) is the managed, **pay-per-query** option with
no always-on node, so unlike Vector Search it does fit the cost rule. ADK even
ships `VertexAiSearchTool`, which lets Gemini do retrieval server-side with no
application round-trip — the better production shape.

It is wired but inactive here for one reason: it requires a provisioned data
store, which would mean this repo could not run end-to-end before a reader
creates GCP resources. `VertexAiSearchBackend` in `rag/search.py` marks the seam.

### 3. Agent Engine → Cloud Run

Agent Engine is the direct analogue of "Lambda/Fargate for agent hosting": a
managed runtime purpose-built for agents, with session storage, tracing and
scaling handled. It is the right answer for a production agent platform.

Cloud Run is used here because it scales genuinely to zero and because the same
ADK agent deploys to either — that portability is a core ADK design goal.
`agent/agent.py` has no hosting-specific code, so moving to Agent Engine is a
deployment change.

### 4. CloudFront → Firebase Hosting, not a load balancer

The literal GCP equivalent of "CloudFront in front of an S3 bucket" is Cloud CDN
behind a **global external Application Load Balancer**. A forwarding rule bills
roughly **$18/month before the first request** — the single largest fixed cost
available to accidentally incur in this design.

**Firebase Hosting** delivers the CloudFront feature set — global CDN, free TLS,
custom domains, atomic deploys — on a free tier with no load balancer. Its
rewrites proxy `/api`, `/a2a`, `/mcp` and `/media` to Cloud Run, so the browser
sees a single origin and never issues a CORS preflight. See
[ADR-005](./adr/005-firebase-hosting-over-load-balancer.md) and
[`auth-and-hosting.md`](./auth-and-hosting.md).

### 5. Imagen → the row that stayed on Replicate

The one row where the "port it to GCP" instinct is wrong. Vertex AI Imagen is the
native answer, but the product is an *anime* studio: **AnimagineXL 3.1** is a
purpose-built SDXL fine-tune that produces better output for the actual use case
at roughly an order of magnitude lower cost per image, and it understands the
Danbooru tag vocabulary users type.

Replicate bills per second of GPU time, so the zero-idle rule still holds. Being
on a cloud is not an argument for that cloud's model —
[ADR-006](./adr/006-replicate-animagine-over-imagen.md).

## Where the mapping is clean

**DynamoDB → Firestore** is the smoothest row, with one instructive difference.

The AWS original uses a single table keyed `pk = USER#{sub}` with sort-key
namespace prefixes (`IMG#`, `AGENT#SESSION#{id}`, `AGENT#COST`). That prefix
trick exists because DynamoDB gives you one sort dimension, so hierarchy has to
be encoded into the string. Firestore models hierarchy natively, so those become
real paths: `users/{uid}/media/{id}`, `users/{uid}/agentSessions/{sid}/messages/`.

Two consequences:

- **No prefix scans.** A subcollection *is* the namespace, so listing a user's
  sessions is a collection read rather than a `begins_with` query.
- **The id sanitiser still matters.** In DynamoDB an unescaped session id could
  inject a `#MSG#` fragment into the sort key. In Firestore it could inject a `/`
  and escape into a sibling collection. Same bug class, different separator — so
  `store/keys.py::safe_id` is ported, not dropped.

**The atomic-counter difference is real.** DynamoDB's `ADD` with
`ReturnValues: UPDATED_NEW` is atomic *and* returns the new value in one call —
exactly what a "did this user blow the cap?" check needs. Firestore has no
equivalent:

- `firestore.Increment(n)` is atomic but **write-only**; it cannot answer the cap
  question.
- A transaction reads-then-writes atomically and *does* return the value, at the
  cost of optimistic-concurrency retries.

So `store/docstore.py` exposes both: `increment` (cheap sentinel) for telemetry
that only accumulates, and `increment_and_read` (transaction) for guardrails that
gate a request. Using the wrong one lets two concurrent turns both observe 49/50
images and both generate.

## Bedrock Converse → ADK, in one paragraph

Bedrock Converse is an *API*: you send messages plus tool specs and handle the
tool-use loop yourself. ADK is a *framework*: `LlmAgent` owns the loop, tool
dispatch, session state and event streaming, and the model is a swappable
component underneath. The practical consequence for this port is that the
hand-rolled turn loop disappeared, and what replaced it is a tool fleet plus a
`Runner` — see [`agent-loop.md`](./agent-loop.md).
