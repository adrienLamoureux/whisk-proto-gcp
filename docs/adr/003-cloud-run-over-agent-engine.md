# ADR-003: Cloud Run over Agent Engine for agent hosting

**Status:** accepted · **Date:** 2026-08-03

## Context

The AWS original hosts its agent in Lambda behind API Gateway. The GCP mapping
offers two hosts:

- **Agent Engine** — a managed runtime purpose-built for agents, with session
  storage, tracing, evaluation and scaling handled for you. The direct analogue
  of "Lambda/Fargate for agent hosting" in the equivalence table.
- **Cloud Run** — general-purpose serverless containers, `min-instances=0`.

## Decision

Deploy to **Cloud Run**. Document Agent Engine as the production alternative and
keep `agent/agent.py` free of hosting-specific code.

## Rationale

- Cloud Run scales genuinely to zero. Idle cost is $0, which is the project's
  governing constraint.
- The application is not *only* an agent. It also serves a REST API, an A2A
  endpoint, an MCP server and a static bundle. Cloud Run hosts all of it in one
  service; Agent Engine would host the agent and leave the rest needing a second
  service anyway.
- **The same ADK agent deploys to either.** Host portability is a core ADK design
  goal, so this is reversible at low cost.

## Consequences

**Given up:** Agent Engine's managed session storage (hence
[ADR-002](./002-firestore-session-service.md)), built-in tracing, and its
evaluation tooling.

**Trigger to revisit:** wanting managed tracing/evaluation, or a multi-agent
deployment where per-agent scaling and versioning start to matter. At that point
the agent moves and the FastAPI service stays for REST, A2A and MCP.

**Cold starts** are accepted. They matter little for a creative tool, and the
same tradeoff was accepted in the AWS original.
