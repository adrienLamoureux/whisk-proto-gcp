# Interview notes

Rehearsal material. The structure is: what to lead with, the questions this
design invites, and the things that are genuinely hard to answer.

---

## The 90-second version

> "I had a fully-serverless AI creative studio on AWS — Bedrock Converse with a
> tool-calling agent, Lambda, DynamoDB, S3, Cognito. I ported the interesting
> slice to GCP to learn the stack: FastAPI plus ADK, Gemini, Firestore, Cloud
> Storage, Cloud Run.
>
> The constraint I set was that nothing may bill when idle. That turned out to
> drive more architectural decisions than anything else, because three of the
> obvious GCP equivalents have a standing cost — Vector Search needs always-on
> index nodes, ADK's database session service implies Cloud SQL, and the
> CloudFront equivalent needs a load balancer with an $18/month forwarding rule.
> So the interesting work was finding the pay-per-use path through each of them
> and being able to say precisely when I'd switch back."

That paragraph invites exactly the follow-ups you want.

---

## Lead with this: the Vector Search decision

It is the strongest thing in the project because the *correct* textbook answer is
the wrong answer here, and you can say why with numbers.

**The setup.** Bedrock Knowledge Bases / pgvector maps to Vertex AI Vector
Search. It is a genuinely great product — ScaNN, billions of vectors.

**The problem.** Serving requires an index endpoint with deployed nodes. Nodes
are VMs, billed hourly, at zero traffic. Hundreds of dollars a month to serve a
few hundred document chunks.

**The answer.** Firestore's native vector index — `find_nearest` with COSINE.
Same database already holding sessions and media, pay-per-op, $0 idle.

**The part that makes it a good answer rather than a cheap one:**

> "It's a brute-force scan behind a query planner, so latency grows with
> collection size. The crossover where Vector Search earns its floor is around
> 100k vectors or sustained high QPS. Below that you're paying for idle nodes to
> serve queries a linear scan answers in milliseconds. I kept the retrieval
> behind a protocol with a second implementation so switching is a config
> change."

**Also mention Vertex AI Search (Discovery Engine)** — pay-per-query, no
always-on node, and ADK has a built-in tool for it that does retrieval
server-side. Knowing there are *three* options and why you picked the third is
what separates this from a lucky guess.

---

## Second: tool classes

The design idea worth drawing on a whiteboard.

> "Tools aren't a flat list. They fall into classes and the class determines
> control flow. Server-dispatch runs on the backend and gets a short closing turn
> so the model reacts to the real result. Client-action returns a payload the
> browser applies — theme switching never touches the server. Intent tools return
> a *proposal*, never a write: anything that publishes or deletes goes through
> there, the user confirms, and the browser then calls the same REST endpoint a
> human clicking the button would hit. So the model can propose an irreversible
> action but never perform one, and authorisation lives in exactly one place."

**Follow-up you should expect:** *"ADK has native tool confirmation — why not use
it?"*

Answer honestly: it does (`require_confirmation` + `ToolContext.request_confirmation`),
it is the more integrated mechanism, and it keeps the approval *inside* the turn
so the model can react to the outcome. It was not used because it implies a
suspended invocation needing durable resumption state on a service whose
instances are recycled between turns. The tradeoff is written up in ADR-004.
Knowing the mechanism and rejecting it deliberately is a much better answer than
not knowing.

---

## Third: the security detail most people miss

Two related points, both about treating your own stored state as untrusted.

**Identity never comes from the model.**

> "`uid` is read from ADK session state, seeded by the runner from the verified
> token. It's never a tool parameter — if it were, the model could be talked into
> passing someone else's, and that's prompt injection straight into an IDOR."

**Preferences are validated on read, not just write.**

> "Cross-session preferences get injected into the system prompt so tool defaults
> drift toward what the user keeps choosing. That means stored state is untrusted
> input *to the model*. Validating only on write leaves two holes: any other write
> path bypasses the check, and rows written before the validator existed are still
> in the database. So the enum check runs on read as well, and anything failing is
> dropped silently rather than raised."

That second one is a genuinely uncommon observation and lands well.

---

## Questions this design invites

**"Why Flash and not Pro?"**
Agent turns are latency-sensitive and structurally simple: pick a tool, fill
three arguments, write one sentence. Flash is the right tier and the cost
difference compounds when every user message is a turn. Pro (1M context) is wired
as an admin-selectable override for long grounded reasoning over large retrieved
context. The override lives in Firestore with a 60s cache, so switching models is
a database write, not a redeploy.

**"Why a per-instance cache instead of a shared one?"**
A shared cache means Memorystore, which is an always-on Redis instance for
something a 60s staleness window already solves. Cloud Run instances are
independent, so a change propagates everywhere within one TTL. Correct, not lazy.

**"How does Firestore compare to DynamoDB here?"**
Mostly smoother — hierarchy is native, so the original's `USER#{sub}` + `IMG#`
sort-key prefixes become real paths and there are no `begins_with` scans. One
sharp difference: DynamoDB's `ADD` with `ReturnValues: UPDATED_NEW` is atomic
*and* returns the new value, which is exactly what a cap check needs. Firestore's
`Increment` is atomic but write-only, so a cap check needs a transaction. Both
are exposed in the store layer; using the cheap one for a cap check lets two
concurrent turns both observe 49/50 and both generate.

**"Why is the image cap separate from the token cap?"**
Different cost vectors with different abuse profiles. Images dominate spend, so a
token-only cap leaves the expensive thing unbounded. Independent counters roll
over independently and tune independently. It also makes the adversarial ceiling
computable — around $50/user/month — and the point of a cap is that a number
exists.

**"You're on GCP — why is image generation on Replicate?"**
This is the question to *want*. The product is an anime studio; AnimagineXL is a
purpose-built SDXL fine-tune that beats Imagen for that use case at roughly an
order of magnitude lower cost per image, and it understands the Danbooru tag
vocabulary users actually type. Replicate bills per second of GPU time, so the
zero-idle rule still holds. The line to land: *"being on a cloud isn't an
argument for that cloud's model — the constraint was pay-per-use, not
single-vendor."* Then name the cost honestly: it's a hard dependency outside GCP,
in the critical path of the product's main action, and Imagen's built-in safety
filter had to be replaced with a negative prompt plus a noted gap.

**"Why does generation return a prediction id instead of an image?"**
Because Cloud Run bills CPU for the whole request duration. `Prefer: wait=5`
catches the warm case synchronously; anything slower comes back as an id the
browser polls with backoff. Blocking 60s on a cold GPU costs ~12x more *and*
holds one of 40 concurrency slots the entire time, so one slow generation crowds
out others. Same tradeoff the AWS original made.

**"Why hand-roll Identity Platform instead of using the Firebase SDK?"**
The SDK is ~200 KB gzipped for four HTTP calls and a refresh timer; the bundle is
57 KB without it. And say the other half: the SDK is worth taking the moment you
want federated sign-in, because Google/GitHub popup-redirect flows are genuinely
fiddly to reimplement. Email/password is the whole surface here.

**"Where do tokens live, and why?"**
`sessionStorage`, matching the AWS original. Scopes the session to the tab and
clears on close, limiting XSS blast radius on a shared machine. The cost is a new
tab means signing in again. Refresh runs on a 50-minute timer *and* on demand
inside `getToken()` — both, because a backgrounded tab has its timers throttled
and a timer alone would let the token die mid-session.

**"How is admin enforced?"**
Custom claim set out-of-band with the Admin SDK. The frontend decodes it
*without verifying* to decide whether to render the tab — safe, because the
signature is checked server-side on every request, so a tampered token buys a
useless button and a 403. Hiding the tab is a courtesy, not a control.

**"Isn't the API key in config.json a leak?"**
No — the Identity Platform Web API key identifies the project and authorises
nothing on its own. It is exactly what the Firebase SDK config object ships. The
*Replicate* token is a real secret: Secret Manager, injected by Cloud Run as an
env var, never sent to a browser.

**"What about A2A vs MCP?"**
MCP connects an agent to *capabilities* — the far side is a server with no goals.
A2A connects an agent to a *peer* with its own reasoning loop. Rule of thumb: if
the far side needs an LLM to do its job, that's A2A; if it's a database or an API,
that's MCP. And the honest addition: **for a co-located sub-agent, A2A is
overhead** — ADK's `AgentTool` or `sub_agents` is right, and A2A only earns its
cost across a boundary you don't control.

**"Why isn't the main agent exposed over A2A?"**
Its tools write to a specific user's library and spend that user's quota.
Publishing it means re-solving auth, quota attribution and tenancy at the protocol
boundary. The critic is exposed instead because it's a pure function of its input.
Same reason only read-only tools are on MCP: MCP carries no end-user identity, so
a user-scoped tool there would need a caller-supplied `uid`, which is an IDOR with
extra steps.

---

## Weaknesses — name them before you're asked

Volunteering these reads as engineering judgment. Hiding them reads as not having
noticed.

**No streaming.** `/api/agent/turn` blocks until the whole loop finishes, so a
multi-second turn shows nothing. ADK's `run_async` already yields events
incrementally, so the agent side is ready — what's missing is SSE on the route and
an `EventSource` client. Worth adding: on AWS this was *architectural* (Lambda
behind API Gateway can't stream; the fix means moving to a Function URL). On Cloud
Run it's just a change, because Cloud Run supports streaming responses. That
contrast is a good thing to have noticed.

**No memory compaction.** The original compacts above ~30 turns with a rolling
summary and bills the summariser's own tokens back. Here the working set is
bounded but not summarised. It belongs behind the session-service interface, not
bolted onto the route.

**Single image provider.** The original fans out across Replicate/Bedrock/
CivitAI/Gradio. That was cost arbitrage, not architecture, so it's out of scope —
but say so rather than implying the port is complete.

**Content safety is thinner than Imagen's.** Imagen ships Google's safety filter;
AnimagineXL doesn't. `nsfw` leads the negative prompt and isn't user-overridable,
but that's a prompt-level control, not moderation. A production deployment wants
Cloud Vision SafeSearch on the output. Name this as a known gap — it's the
obvious follow-up to "why not Imagen?" and having the answer ready is better than
being caught by it.

**No federated sign-in.** Email/password only. Google sign-in needs the Firebase
SDK's popup/redirect flow, which is the point at which the 200 KB dependency
earns its place.

**Partial coverage.** Tests cover guardrails, path safety, tools, the agent loop
and the REST surface. They don't cover the `gcp`-mode providers, which need real
credentials.

---

## The thing to be honest about

You have not run this against a real GCP project. Say so plainly if asked, and
frame what you *did* do:

> "I built it so the whole agent loop runs offline — the ADK Runner, tool
> dispatch, session persistence and the guardrails are all real, and only the
> model and the managed data plane are stubbed. So the wiring is exercised end to
> end. What I haven't validated is the GCP-mode provider implementations, and I
> know the two places I'd expect to hit friction: signing GCS URLs from Cloud Run
> needs `roles/iam.serviceAccountTokenCreator` on the runtime service account
> because there's no private key to sign with, and Firestore vector queries need a
> composite vector index created before `find_nearest` works."

Naming the two specific gotchas you'd hit is much stronger than claiming it works.

---

## Quick reference

| Component | Choice | One-line why |
|---|---|---|
| Agent framework | ADK `LlmAgent` | Owns the loop; host-portable |
| Model | Gemini 3.6 Flash | Right tier for tool turns; Pro as override |
| Image generation | **AnimagineXL 3.1 on Replicate** | Better anime output at ~1/10 the cost; still pay-per-use |
| Compute | Cloud Run, min-instances 0 | Scales to zero; serves REST/A2A/MCP on one service |
| CDN | **Firebase Hosting** | CloudFront feature set, $0; an ALB forwarding rule is ~$18/mo idle |
| Sessions | Custom Firestore service | The three built-ins imply lost state, Cloud SQL, or Agent Engine |
| Vectors | Firestore `find_nearest` | Vector Search needs always-on nodes |
| Database | Firestore Native | Hierarchy is native; pay-per-op |
| Media | Cloud Storage + V4 signed URLs | Never a public object |
| Auth | Identity Platform, REST not SDK | Cognito equivalent; 57 KB bundle instead of ~250 KB |
| Secrets | Secret Manager via `--set-secrets` | Replicate token never reaches the browser |
