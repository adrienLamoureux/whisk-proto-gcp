# A2A and MCP

Two protocols that get conflated constantly. Both are mounted here, on purpose,
so the distinction is demonstrable rather than theoretical.

## The distinction

| | **MCP** | **A2A** |
|---|---|---|
| Connects an agent to | **capabilities** — tools, resources, prompts | **another agent** |
| The far side is | a server: no goals, no autonomy, answers calls | a peer: its own reasoning loop, may ask clarifying questions |
| Typical far side | a database, an API, a filesystem | another team's or company's agent |
| Discovery | tool list from the server | agent card at `/.well-known/agent-card.json` |

**Rule of thumb:** if the far side would need an LLM to do its job, that is A2A.
If it is a database, an API or a filesystem, that is MCP.

They compose. An A2A peer can itself consume MCP tools.

## When A2A is the wrong choice

This matters more than knowing the protocol exists:

| Situation | Right answer |
|---|---|
| Same process, same team | ADK `sub_agents` or `AgentTool` — A2A here is pure overhead: HTTP, serialisation, and a second failure mode buying nothing |
| Different process, org or framework | **A2A** — this is the case it exists for |
| The agent needs a *tool* | **MCP** |

A2A's value is interoperability across boundaries you do not control. Inside one
runtime, it is a distributed-systems tax you are paying voluntarily.

## What this repo exposes

### A2A: the critic agent — `/a2a/critic`

```
GET  /a2a/critic/.well-known/agent-card.json    discovery
POST /a2a/critic/                                invoke
```

A single-purpose agent that critiques text-to-image prompts. Any A2A client can
use it without knowing it is ADK, or Python.

The critic is deliberately built **both** ways: exposed over A2A because that
makes it reusable by anything, and invoked **in-process by default** because for
a co-located agent that is the honest engineering choice. Set
`WHISK_CRITIC_A2A_URL` and the same tool call goes over the wire instead — the
seam is real, not a diagram.

**The studio agent is deliberately not exposed over A2A.** Its tools write to a
specific user's library and consume that user's daily caps; identity arrives
through session state seeded from a verified token. Publishing it as a peer would
mean re-solving auth, quota attribution and tenancy at the protocol boundary.
A2A does support authenticated agent cards, so this is a scoping decision rather
than a protocol limitation — but shipping an unauthenticated agent that spends
someone else's budget would be a real vulnerability, not a demo.

#### Two mounting details that are easy to get wrong

1. **`to_a2a()` registers its routes inside its own Starlette lifespan**, and
   Starlette does *not* run the lifespan of a mounted sub-app. Mount it and do
   nothing else and every A2A path 404s — including the agent card. The parent
   lifespan has to enter the child's explicitly.

2. **The agent card advertises an absolute URL.** Left to its default, ADK builds
   it from `host`/`port` with no knowledge of the mount prefix, so the card sends
   clients to `/` instead of `/a2a/critic/`. A card pointing at the wrong endpoint
   is worse than no card, because discovery appears to succeed. The card is built
   explicitly from `WHISK_PUBLIC_BASE_URL`.

### MCP: read-only studio tools — `/mcp`

Streamable-HTTP transport, exposing `browse_gallery` and `search_studio_docs`.
An external agent — Claude Desktop, another ADK agent, anything speaking MCP —
can mount this and browse the gallery or query these docs.

**Why only read-only tools.** MCP carries no end-user identity: an MCP client
authenticates as *itself*, not as one of this application's users. Anything
user-scoped (`generate_image`, `recall_favorites`, `share_image`) needs a `uid`
to be safe and to attribute quota, and there is nowhere trustworthy for that to
come from over this transport. Publishing them would mean either a shared
identity spending everyone's budget, or a `uid` parameter the caller supplies —
which is an IDOR with extra steps.

The two published tools touch only public data and cost no user quota.

#### Two mounting details, again

1. **`streamable_http_app()` already serves at `/mcp` internally.** Mounting it
   at `/mcp` therefore resolves to `/mcp/mcp` and everything 404s. Pass
   `streamable_http_path="/"` so the mount prefix supplies the path.

2. **`stateless_http=True` is a correctness requirement, not an optimisation.**
   Cloud Run load-balances across instances with no session affinity, so a
   stateful MCP session held in one instance's memory breaks the moment the
   service scales past one instance. Stateless mode makes every request
   self-contained. This is the same class of constraint that ruled out
   `InMemorySessionService` for the agent.

Its session manager also has to be started from the parent lifespan, for the
same reason the A2A sub-app's lifespan does.

## Why all three surfaces share one Cloud Run service

REST, A2A and MCP are mounted on the same FastAPI app. Cloud Run bills per
request and scales to zero, so three services would triple the cold starts and
the deployment surface while saving nothing. They separate cleanly if any one of
them ever needs its own scaling profile.
