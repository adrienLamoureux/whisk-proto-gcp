# API reference

23 endpoints. Generated from the running app; `GET /docs` serves the live
OpenAPI UI.

## Auth

`Authorization: Bearer <Identity Platform ID token>` on every **User** route.

| Tier | Meaning |
|---|---|
| **Public** | No token |
| **User** | Valid ID token |
| **Admin** | Valid ID token with the `admin` custom claim |

In `local` mode with `WHISK_AUTH_REQUIRED=false`, identity comes from
`X-Whisk-User` / `X-Whisk-Admin` headers instead. That path is gated on the
*server's* mode, so sending those headers at a deployed instance does nothing.

Errors are `{"detail": <string | object>}`. Rate-limit and cap rejections return
**429** with a `Retry-After` header (seconds) and a body carrying the precise
millisecond figure:

```json
{"detail": {"error": "image_daily_cap_reached", "retryAfterMs": 41230000,
            "used": 50, "capacity": 50}}
```

---

## Health & config

#### `GET /health` — Public
`{"status": "ok", "uptimeSeconds": 12.4}`

#### `GET /config.json` — Public
Runtime wiring for the frontend. No secrets: the Identity Platform Web API key is
public by design.

```json
{
  "mode": "gcp",
  "agentModel": "gemini-3.6-flash",
  "imageModel": "cjwbw/animagine-xl-3.1",
  "surfaces": { "a2a": true, "mcp": true },
  "auth": { "required": true, "provider": "identity-platform",
            "projectId": "…", "apiKey": "AIza…" },
  "limits": { "dailyTokenCap": 200000, "dailyImageCap": 50 }
}
```

#### `GET /api/surfaces` — Public
Which protocols this service speaks, with mount paths and the A2A agent-card URL.

#### `GET /media/{key}?exp=&sig=` — Signed
Local-mode media only. Serves bytes behind an HMAC signature with the same expiry
semantics as a GCS signed URL. In `gcp` mode the browser fetches from GCS
directly and this route 404s.

---

## Images

#### `POST /api/images/generate` — User
```json
{ "prompt": "a red fox asleep in the snow", "style": "anime",
  "aspect": "16:9", "seed": 1234 }
```
`style` ∈ `anime | manga | photoreal | chibi` · `aspect` ∈ `1:1 | 3:4 | 16:9 | 9:16`
(anything else is coerced to the default). `seed` is optional.

Consumes one unit of the daily image cap **before** calling the provider.

Returns **either** a finished image:
```json
{ "media": { "mediaId": "…", "url": "https://…", "prompt": "…",
             "style": "anime", "aspect": "16:9", "width": 1344, "height": 768 },
  "imagesRemaining": 49 }
```
**or** a pending handle, when the prediction misses the `Prefer: wait=5` window:
```json
{ "pending": { "predictionId": "…", "prompt": "…", "style": "anime",
               "aspect": "16:9" },
  "imagesRemaining": 49 }
```
Errors: `429` (cap or rate limit), `502` (`replicate_quota`,
`replicate_unreachable`, `generation_failed`).

#### `GET /api/images/status?predictionId=` — User
Polls a pending generation and saves the image on the turn it lands.

`{"status": "processing"}` · `{"status": "succeeded", "media": {…}}` ·
`{"status": "failed", "error": "…"}`

`404` if the job is not the caller's — authorisation is by job ownership, since
prediction ids are not secret. `504` past the 15-minute job TTL.

---

## Media library

#### `GET /api/media?limit=24&favorites=false` — User
`{"items": [Media], "count": n}`, newest first. URLs are freshly-signed, 15-minute.

#### `GET /api/gallery?limit=24` — Public
The shared gallery — the one place media is world-readable.

#### `POST /api/media/{mediaId}/favorite` — User
`{"favorite": true}` → `{"media": Media}`

#### `POST /api/media/{mediaId}/share` — User
Copies the object into the public `shared/` prefix and indexes it. A copy, not a
move, so a later delete of the original cannot dangle a public URL.

Also the confirm endpoint for the agent's `share_image` intent.

#### `DELETE /api/media/{mediaId}` — User
Removes the object, its shared copy, and the metadata. Confirm endpoint for the
agent's `delete_image` intent.

---

## Agent

#### `POST /api/agent/turn` — User
```json
{ "message": "draw a red fox, 16:9", "sessionId": "default", "mode": "studio" }
```
```json
{
  "text": "Done — anime at 16:9. Want more contrast?",
  "sessionId": "default",
  "toolCalls": [{
    "name": "generate_image",
    "kind": "server_dispatch",
    "args": { "prompt": "a red fox", "style": "anime", "aspect": "16:9" },
    "result": { "status": "succeeded", "mediaId": "…", "imageUrl": "…" },
    "error": null
  }],
  "usage": { "inputTokens": 210, "outputTokens": 38, "totalTokens": 248 },
  "latencyMs": 1840,
  "model": "gemini-3.6-flash"
}
```

`kind` ∈ `server_dispatch | client_action | intent` — it tells the client what to
*do* with the result: render it, apply it, or ask the user first. An `intent`
result carries `requiresConfirm`, `summary` and `confirmEndpoint`.

`429` on rate limit (`rate_limited`) or daily token cap (`daily_cap_reached`).

> Not streaming. See [`architecture.md` §11](./architecture.md#11-known-gaps).

#### `GET /api/agent/sessions` — User
`{"items": [{sessionId, name, createdAt, lastUsedAt}]}`, most recent first.

#### `POST /api/agent/sessions` — User
`{"sessionId": "myproject", "name": "My Project"}`. Ids are sanitised to
`[a-z0-9_-]{1,80}`.

#### `PATCH /api/agent/sessions/{id}` — User
`{"name": "…"}` → `404 session_not_found` if absent.

#### `DELETE /api/agent/sessions/{id}` — User
Deletes the metadata **and** the conversation history beneath it.

#### `GET /api/agent/sessions/{id}/history` — User
`{"items": [{role, text, at}]}` — a flattened, renderable transcript.

#### `GET /api/agent/usage` — User
The caller's own quota, so the UI can show headroom before a 429 rather than
after.
```json
{ "tokensToday": 1209, "tokenCap": 200000,
  "imagesToday": 3, "imageCap": 50,
  "inputTokens": 986, "outputTokens": 223, "turnCount": 6,
  "resetsInMs": 41230000 }
```

---

## RAG

#### `GET /api/rag/search?q=&limit=4` — Public
Grounded answer over the `docs/` corpus, with the retrieved passages and their
cosine distances so the retrieval is inspectable.
```json
{ "question": "…", "answer": "…", "grounded": true,
  "citations": [{"doc": "cost-model", "heading": "…", "distance": 0.41}],
  "passages":  [{"doc": "…", "heading": "…", "text": "…", "distance": 0.41}] }
```

#### `POST /api/rag/reingest` — User
Rebuilds the corpus. Idempotent — chunk ids are content hashes, so only changed
chunks are rewritten.

---

## Admin

#### `GET /api/admin/agent/model` — Public
`{"modelId": "…", "default": "…", "overridden": false, "mode": "gcp"}`
Public because the active model is not a secret and the UI displays it.

#### `PUT /api/admin/agent/model` — Admin
`{"modelId": "gemini-3.1-pro-preview"}`. Empty string resets to the deploy
default. Persisted in Firestore with a 60s per-instance cache, so switching
models is a database write rather than a redeploy.

#### `GET /api/admin/agent/cost?limit=50` — Admin
Per-user token totals with a rough USD estimate, ordered by spend. Backed by the
denormalised `costIndex` collection — a bounded ordered read rather than a scan
over every user.

---

## Non-REST surfaces

| Path | Protocol |
|---|---|
| `GET /a2a/critic/.well-known/agent-card.json` | A2A discovery |
| `POST /a2a/critic/` | A2A invoke (JSON-RPC) |
| `POST /mcp/` | MCP, streamable-HTTP, stateless |

MCP publishes `browse_gallery` and `search_studio_docs` only — it carries no
end-user identity, so nothing user-scoped belongs on it. See
[`protocols.md`](./protocols.md).
