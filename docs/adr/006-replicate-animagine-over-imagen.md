# ADR-006: AnimagineXL on Replicate as the only image generator

**Status:** accepted · **Date:** 2026-08-03 · **Supersedes** the Vertex AI Imagen
choice in the initial port

## Context

The product is an **anime** creative studio. The AWS original fanned out across
Replicate, Bedrock, CivitAI and Gradio, with Replicate carrying ~90% of both the
traffic and the cost.

The first cut of this port collapsed that to **Vertex AI Imagen**, on the
reasoning that a GCP port should use GCP's first-party model. That is the wrong
reasoning, and worth naming as such: *being on a cloud is not an argument for
that cloud's model.*

Options considered:

| Option | Fit for anime | Cost/image | Idle cost |
|---|---|---|---|
| Vertex AI Imagen 4 | General-purpose; no Danbooru tag vocabulary | ~$0.04 | $0 |
| **AnimagineXL 3.1 on Replicate** | Purpose-built SDXL anime fine-tune | **~$0.003–0.01** | **$0** |
| Self-hosted SDXL on GKE + GPU | Best unit cost at scale | — | **Hundreds of $/mo** (idle GPU) |

## Decision

**`cjwbw/animagine-xl-3.1` on Replicate, pinned by version hash, as the only
generator.** Vertex AI Imagen is removed rather than kept as a fallback.

## Rationale

- **Better output for the actual use case.** AnimagineXL is trained on
  Danbooru-tagged anime imagery. It understands the tag vocabulary users of this
  product actually type, and ships `quality_selector` / `style_selector` presets
  that were tuned with the model. Imagen has no equivalent concept.
- **Roughly an order of magnitude cheaper per image**, which matters because
  image generation is the dominant cost vector (the reason it has its own daily
  cap — see `docs/agent-loop.md`).
- **Still zero idle.** Replicate bills per second of GPU time. Self-hosting SDXL
  would mean a GPU node running around the clock, which is the same trap as
  Vector Search's index endpoint.
- **One provider, not four.** The original's fan-out was cost arbitrage across
  providers, not architecture. Collapsing it removes four failure modes.

## Consequences

**A hard dependency outside GCP.** The deployment is no longer single-vendor, and
Replicate's availability is now in the critical path for the product's main
action. Accepted knowingly: the alternative buys vendor tidiness with worse
output at higher cost.

**Generation is asynchronous.** Replicate returns a *prediction*, not an image.
The provider uses `Prefer: wait=5` and falls back to client polling — see
`providers/replicate.py`. The 5s window is not arbitrary: **Cloud Run bills CPU
for the whole request duration**, so blocking out a 60s cold boot costs ~12x a 5s
wait and holds a concurrency slot the entire time.

**The version is pinned to a content hash, not `latest`.** Replicate model owners
can push a new version with different inputs or different output quality
whenever they like. Pinning means behaviour changes on our redeploy, not on
someone else's push.

**Output URLs expire**, so the bytes are downloaded and written to our own GCS
bucket. That is also what keeps user media inside the `users/{uid}/` isolation
model instead of on a third-party host.

**Content safety moves to us.** Imagen has Google's safety filter built in.
AnimagineXL does not, so `nsfw` leads the negative prompt and is not
user-overridable. This is weaker than a real moderation layer, and a production
deployment would want the Cloud Vision SafeSearch API on the output — noted as a
gap rather than claimed as solved.

**Trigger to revisit:** needing photoreal output as a first-class mode (Imagen or
a second Replicate model earns its place), or sustained volume high enough that a
dedicated GPU beats per-second billing.
