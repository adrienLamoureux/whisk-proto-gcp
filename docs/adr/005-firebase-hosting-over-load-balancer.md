# ADR-005: Firebase Hosting over Cloud CDN + load balancer

**Status:** accepted · **Date:** 2026-08-03

## Context

The AWS original serves its React bundle from **S3 behind CloudFront**: static
origin, global CDN, free TLS, custom domain. The port needs the same thing.

There are three ways to do it on GCP:

| Option | Shape | Idle cost |
|---|---|---|
| **Cloud Storage + Cloud CDN + global external ALB** | The literal CloudFront analogue | **~$18/mo** for the forwarding rule alone, before any traffic |
| **Serve the bundle from Cloud Run** | One service does everything | $0, but no CDN — every asset is a container invocation |
| **Firebase Hosting** | Managed global CDN, static origin, rewrites to Cloud Run | **$0** (free tier: 10 GB storage, 360 MB/day transfer) |

The load-balancer option is the trap. A global external Application Load Balancer
bills per forwarding rule per hour regardless of traffic, which makes it the
single largest fixed cost available to incur in this design — more than the model
spend it would sit in front of.

Serving from Cloud Run (what this repo did before) works and costs nothing idle,
but every CSS and JS request becomes a container invocation with cold-start
latency, and there is no edge caching anywhere.

## Decision

**Firebase Hosting** for static assets, with rewrites proxying `/api`, `/a2a`,
`/mcp`, `/media`, `/config.json` and `/health` to the Cloud Run service.

Cloud Run keeps its ability to serve the bundle, used by `make dev` so local
development stays a single process.

## Rationale

- Genuine global CDN with edge caching, free TLS and custom domains — the
  CloudFront feature set — with no load balancer and no forwarding rule.
- **One origin.** Because the rewrites proxy rather than redirect, the browser
  only ever talks to `https://<site>.web.app`. No CORS preflight on any API call,
  no third-party cookie considerations, and the A2A agent card advertises the
  same host users visit.
- Atomic deploys with instant rollback, versioned by release.
- Firebase Hosting is Identity Platform's own neighbourhood, so auth and hosting
  live in one project.

## Consequences

**Extra hop.** Requests to `/api` traverse Hosting before reaching Cloud Run,
adding a few milliseconds. Irrelevant next to a Replicate GPU render.

**Free-tier ceiling.** 360 MB/day of transfer is generous for a bundle of ~180 KB
but is a real limit; the Blaze plan bills per GB past it. Generated images do not
count against it — they are served by GCS signed URLs directly from the bucket,
not through Hosting.

**Rewrites are first-match-wins.** The SPA catch-all (`**` → `/index.html`) must
stay last in the array. Putting it first silently swallows the entire API, and
the failure looks like "the API returns HTML" rather than a routing error.

**Cache headers are load-bearing.** Vite emits content-hashed asset filenames, so
`/assets/**` is `immutable` for a year. `index.html` must be `no-cache`: caching
it leaves the CDN serving an old document that references deleted asset hashes —
the classic "deployed successfully, users still see the old app, now with broken
assets" failure.

**Trigger to revisit:** needing Cloud Armor (WAF, DDoS rules), a private origin
in a VPC, or path-based routing across several backends. Those are load-balancer
features, and at that point the forwarding rule is buying something real.
