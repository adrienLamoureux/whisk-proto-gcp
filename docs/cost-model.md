# Cost model

The design constraint for this port was **no standing bill**: every component
must cost approximately nothing when nobody is using it. That single rule drove
more architectural decisions than anything else, and it is the most concrete
thing to talk about.

## Idle cost by component

| Component | Billing | Idle cost |
|---|---|---|
| Cloud Run (`min-instances=0`) | per request + CPU/memory while serving | **$0** |
| Firebase Hosting | free tier: 10 GB storage, 360 MB/day transfer | **$0** |
| Firestore (Native) | per read/write/delete + storage | **$0** |
| Cloud Storage | per GB + operations | **~$0** |
| Vertex AI (Gemini, embeddings) | per token | **$0** |
| Replicate (AnimagineXL) | per second of GPU time | **$0** |
| Identity Platform | free below 50k MAU | **$0** |
| Cloud Logging | free tier, then per GB | **~$0** |
| Secret Manager | per active version + access ops | **~$0.06/mo** |

Only one line is not literally zero, and it is six cents. Everything else bills
purely on use.

## The traps this design avoids

Four GCP services are the natural answer to a row in the AWS mapping table and
would each have introduced a standing charge:

| Tempting choice | Standing cost | What is used instead |
|---|---|---|
| **Vertex AI Vector Search** | Index endpoint = always-on nodes, hundreds of $/mo | Firestore native vector index (`find_nearest`) |
| **Global external ALB + Cloud CDN** | Forwarding rule ≈ **$18/mo** before any traffic | Firebase Hosting (free tier, no LB) |
| **Cloud SQL** (for ADK `DatabaseSessionService`) | Smallest instance ≈ **$10–25/mo** running always | Custom Firestore session service |
| **Memorystore/Redis** (for a shared config cache) | Smallest instance ≈ **$35/mo** | Per-instance 60s in-memory cache |
| **Self-hosted SDXL on GKE** | GPU node runs 24/7, hundreds of $/mo | Replicate, per-second GPU billing |

The Vector Search one is the most instructive, because it is the *correct* answer
to "what replaces pgvector on GCP" and still the wrong answer here. At a few
hundred document chunks it would have cost more than everything else combined by
two orders of magnitude, to serve queries a brute-force index answers in
milliseconds. The crossover is roughly O(100k) vectors or sustained high QPS.

The Memorystore one is the smallest but shows the same reasoning: the model
override needs a cache, and a *shared* cache means Redis. A per-instance cache
with a 60s TTL means each Cloud Run instance converges within one TTL — which a
60s staleness window already tolerates. Correct, not lazy.

## Per-action unit economics

Image generation dominates, exactly as in the AWS original.

| Action | Service | Approx. cost | Notes |
|---|---|---|---|
| Image generation | Replicate / AnimagineXL | ~$0.003–0.01 / image | The swing variable |
| Agent turn | Gemini 3.6 Flash | ~$0.001 / turn | Initial + closing turn |
| RAG query | embeddings + Firestore reads | fractions of a cent | One embed + a KNN read |
| Firestore / GCS / Cloud Run / Hosting | — | ~$0 | Rounding error per user |

Switching from Imagen to AnimagineXL cut the dominant cost line by roughly 4–10x
while improving output for the actual use case — see
[ADR-006](./adr/006-replicate-animagine-over-imagen.md).

### The cost of *waiting*

One non-obvious line: **Cloud Run bills CPU for the entire request duration**, so
a request that blocks on a third party costs real money while doing nothing.

Waiting out a 60s Replicate cold boot costs ~12x a 5s wait *and* holds a
concurrency slot the whole time, which at `--concurrency=40` means one slow
generation can crowd out others. Hence `Prefer: wait=5` plus client polling with
backoff, rather than a long synchronous call. The AWS original made the same call
for the same reason.

Figures are order-of-magnitude anchors for reasoning about ratios, not quotes;
published prices move and should be checked against the current pricing page.

## Why the caps are shaped the way they are

Two counters, independent rollovers:

- **200k tokens/day** bounds model spend at roughly $0.10–0.20/user/day.
- **50 images/day** bounds the dominant vector.

Without the image cap, a determined user could drive a multi-hundred-dollar
monthly bill on a single account while staying comfortably inside any token
limit — because they are different cost vectors with different abuse profiles.
Capping them together would force one number to serve two purposes and mis-serve
both.

The caps also make the worst case *computable*, which is the actual deliverable:

```
adversarial ceiling ≈ 50 images/day × 30 days × ~$0.008  ≈ $12
                    + 200k tokens/day × 30 days × Flash  ≈ $6
                    + infrastructure                      ≈ $1
                    ────────────────────────────────────────────
                                                          ≈ $19/user/month
```

Uncapped, the same user is unbounded. The number matters less than the fact that
one exists.

## Cost observability

`limits/cost` accumulates real token spend per user, including the closing turn
and any internal model calls — an internal call the user did not type is still
money spent, and excluding it creates a blind spot in the cap.

The admin dashboard reads a denormalised `costIndex` collection maintained on
write. The AWS original answers this with a DynamoDB `Scan` filtered to
`AGENT#COST`, which is the only option without a GSI. Firestore charges per
document read, so a scan-shaped query across every user is precisely the access
pattern that turns a $0 database into a surprising one. One extra small write per
turn buys a bounded, ordered read.
