# ADR-004: Return-a-proposal intent tools over ADK's native confirmation

**Status:** accepted · **Date:** 2026-08-03

## Context

Some tools do things that cannot be undone: `share_image` publishes to a public
gallery, `delete_image` destroys an object. The model must be able to *propose*
these and never *perform* them unilaterally.

Two mechanisms exist:

1. **ADK native confirmation** — `FunctionTool(func, require_confirmation=True)`
   plus `ToolContext.request_confirmation`. The framework suspends the invocation,
   surfaces a confirmation request, and resumes it after approval.
2. **Return-a-proposal** (what the AWS original does) — the tool returns a
   payload describing what *would* happen, including the endpoint that performs
   it. The UI renders a confirm bar; on approval the browser calls that ordinary
   REST endpoint.

## Decision

Use **return-a-proposal**.

## Rationale

- **The HTTP surface stays request/response.** Native confirmation implies a
  suspended invocation that must be resumed, which means either a long-lived
  connection or server-side resumable state keyed to a pending approval. On a
  scale-to-zero service where instances are recycled between turns, that
  resumption state needs somewhere durable to live — more moving parts than the
  problem warrants here.
- **Authorisation lives in one place.** The confirm path calls the *same* REST
  endpoint a human clicking "share" in the Studio tab hits. There is no second
  write path to keep consistent, and no way for the agent path to drift from the
  form path.
- **It matches the original**, which keeps the port comparable.

## Consequences

**Given up:** ADK's native confirmation is the more integrated mechanism, and it
keeps the whole interaction inside the agent's turn — the model sees the approval
outcome as a tool result and can react to it. Here the confirmation happens
*outside* the turn, so the model never learns whether the user approved. The UI
acknowledges the action instead.

**Trigger to revisit:** wanting the agent to reason about the approval outcome
("you cancelled the delete — want me to unshare it instead?"), or adding enough
confirm-requiring tools that the parallel UI handling becomes repetitive.

**Note for the interview:** knowing the native mechanism exists and choosing the
other one deliberately is the point. "I didn't know about it" and "I evaluated it
and here's the tradeoff" are different answers.
