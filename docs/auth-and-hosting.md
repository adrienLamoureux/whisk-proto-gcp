# Auth and hosting

The two pieces that turn a local prototype into something with real users in
front of it. Both are direct ports of an AWS pattern, and both have one detail
that is easy to get wrong.

## Hosting: the S3 + CloudFront equivalent

The AWS original serves its bundle from **S3 behind CloudFront**. Three GCP
options, and the obvious one is a trap:

| Option | Idle cost | Verdict |
|---|---|---|
| Cloud Storage + Cloud CDN + **global external ALB** | **~$18/mo** (forwarding rule, before any traffic) | The literal analogue, and the most expensive thing in the design |
| Bundle served from Cloud Run | $0, but no CDN — every asset is a container invocation | Fine for dev, wasteful in production |
| **Firebase Hosting** | **$0** (free tier) | Global CDN, free TLS, custom domains, no load balancer |

**Firebase Hosting** it is. Full reasoning in
[ADR-005](./adr/005-firebase-hosting-over-load-balancer.md).

### How the pieces fit

```
        browser
           │
           ▼
   Firebase Hosting  ── static assets from the edge (immutable, 1yr)
           │
           │  rewrites (proxy, not redirect)
           ├── /api/**        ─┐
           ├── /a2a/**         │
           ├── /mcp/**         ├──▶  Cloud Run (min-instances=0)
           ├── /media/**       │
           ├── /config.json    │
           └── /health        ─┘
           │
           └── ** ──▶ /index.html   (SPA fallback, MUST be last)
```

Because rewrites **proxy** rather than redirect, the browser only ever sees one
origin. That is worth more than it sounds: no CORS preflight on any API call, no
third-party cookie considerations, and the A2A agent card advertises the same
host users actually visit.

### Three things that bite

1. **Rewrites are first-match-wins.** The `**` → `/index.html` catch-all must
   stay last. Put it first and it silently swallows the entire API — and the
   symptom is "the API returns HTML", not a routing error.
2. **`index.html` must be `no-cache`.** Vite emits content-hashed asset names, so
   `/assets/**` is safely `immutable` for a year. But a cached `index.html` keeps
   pointing at deleted asset hashes after a deploy: users see the old app, then a
   blank page. This is the classic CDN deployment failure.
3. **Generated images do not go through Hosting.** They are served by GCS signed
   URLs straight from the bucket, so they never count against the Hosting free
   tier's daily transfer — but the bucket needs CORS for the site origin, or
   `fetch()`/canvas reads fail while `<img>` still works.

## Auth: the Cognito equivalent

**Identity Platform** (Firebase Auth's enterprise-branded twin) is the Cognito
analogue: same job, same JWT-on-every-request shape, free below 50k MAU, no idle
cost.

### Where verification happens

The AWS original has **API Gateway verify the JWT before Lambda runs**. Cloud Run
has no free-standing authorizer in front of it — GCP's API Gateway exists but
adds a hop and a bill — so verification moves **into the application**
(`app/auth.py`).

The consequence is that this code has to be right about everything a managed
authorizer would have handled: signature, issuer, audience, expiry. It does not
hand-roll JWT parsing; `google.oauth2.id_token.verify_firebase_token` does the
crypto against Google's public certs, with a cached fetch.

### The login page, and why there is no Firebase SDK

Sign-in talks to the Identity Platform **REST API** directly
(`accounts:signInWithPassword`, `accounts:signUp`, and
`securetoken.googleapis.com` for refresh).

The `firebase` npm package is ~200 KB gzipped for what is four HTTP calls and a
refresh timer. Hand-rolling keeps the bundle at ~57 KB and makes the token
lifecycle legible rather than hidden — which is the point of a project built to
be explained.

The tradeoff is real: the SDK gives you federated sign-in (Google, GitHub) via
popup/redirect flows that are genuinely fiddly to reimplement, plus MFA and
account linking. **Adding Google sign-in is the reason to take the dependency.**
Email/password is the whole surface here.

### Token lifecycle

ID tokens last one hour. A timer refreshes at 50 minutes, *and* `getToken()`
refreshes on demand when the token is within 2 minutes of expiry. Both, not
either: a backgrounded tab has its timers throttled, so a timer alone lets a
token die mid-session.

Tokens live in **`sessionStorage`**, matching the original's `whisk_auth_tokens`.
It scopes the session to the tab and clears on close, limiting the blast radius
of an XSS on a shared machine. The cost is that a new tab means signing in again;
`localStorage` would be the call if "stay signed in" mattered more.

### Admin is a custom claim

`admin: true` is set out-of-band with the Admin SDK, never asserted by a client:

```bash
node -e "const a=require('firebase-admin');a.initializeApp();a.auth().getUserByEmail(process.argv[1]).then(u=>a.auth().setCustomUserClaims(u.uid,{admin:true}))" you@example.com
```

The frontend decodes the claim **without verifying it** to decide whether to
render the Sanctum tab. That is safe because it is display-only: the signature is
checked server-side on every request, so a tampered token buys a useless button
and a 403. Hiding the tab is a courtesy, not a control.

### The API key in `/config.json` is public on purpose

The Identity Platform Web API key ships to the browser. It identifies the project
to `identitytoolkit.googleapis.com` and authorises nothing on its own — every
account operation still needs credentials or a valid token. This is exactly what
the Firebase SDK config object does. It is not a secret, and treating it as one
leads people to invent pointless proxy endpoints.

The **Replicate** token is a real secret, and lives in Secret Manager, injected
by Cloud Run as an env var via `--set-secrets`. It never reaches the browser.

## Local mode

With `WHISK_MODE=local` the login page offers a username box instead of
email/password, and the backend accepts an `X-Whisk-User` header.

That path is gated on the *server* running locally (`settings.is_local`), so
sending the header at a deployed instance does nothing. The dev login is visible
rather than hidden behind a flag, because the entire point of local mode is that
someone can clone this and run it without a GCP project.
