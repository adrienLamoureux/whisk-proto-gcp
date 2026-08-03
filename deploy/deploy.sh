#!/usr/bin/env bash
# Build and deploy: Cloud Run for the API, Firebase Hosting for the CDN.
# Run deploy/setup.sh once first.
#
# Usage:  PROJECT_ID=my-project IDENTITY_PLATFORM_API_KEY=AIza... bash deploy/deploy.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-whisk-studio}"
BUCKET="${BUCKET:-${PROJECT_ID}-whisk-media}"
API_KEY="${IDENTITY_PLATFORM_API_KEY:?set IDENTITY_PLATFORM_API_KEY (Identity Platform -> Application Setup Details)}"
SA_EMAIL="${SERVICE}-run@${PROJECT_ID}.iam.gserviceaccount.com"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The public origin is Firebase Hosting, not the Cloud Run URL: Hosting proxies
# /api to Cloud Run, so users (and A2A peers) only ever see this host.
SITE_URL="${SITE_URL:-https://${PROJECT_ID}.web.app}"

echo "==> building image with Cloud Build"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/${SERVICE}"
gcloud builds submit "${REPO_ROOT}" \
  --config=/dev/stdin <<EOF
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-f", "deploy/Dockerfile", "-t", "${IMAGE}", "."]
images: ["${IMAGE}"]
EOF

echo "==> deploying to Cloud Run"
# --min-instances=0 is the setting that makes idle cost \$0. Setting it to 1 to
# avoid cold starts would put this design back on a standing bill.
#
# --set-secrets injects the Replicate token from Secret Manager as an env var, so
# the process reads os.environ and never links a secret client.
gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --min-instances=0 \
  --max-instances=10 \
  --memory=1Gi \
  --cpu=1 \
  --timeout=120 \
  --concurrency=40 \
  --allow-unauthenticated \
  --set-secrets="WHISK_REPLICATE_API_TOKEN=replicate-api-token:latest" \
  --set-env-vars="^@^WHISK_MODE=gcp@WHISK_PROJECT_ID=${PROJECT_ID}@WHISK_LOCATION=${REGION}@WHISK_GCS_BUCKET=${BUCKET}@WHISK_AUTH_REQUIRED=true@WHISK_IDENTITY_PLATFORM_PROJECT=${PROJECT_ID}@WHISK_IDENTITY_PLATFORM_API_KEY=${API_KEY}@WHISK_PUBLIC_BASE_URL=${SITE_URL}@WHISK_CORS_ORIGINS=${SITE_URL}"

RUN_URL="$(gcloud run services describe "${SERVICE}" --region="${REGION}" --format='value(status.url)')"

echo "==> building the frontend bundle"
(cd "${REPO_ROOT}/frontend" && npm ci --silent && npm run build)

echo "==> deploying to Firebase Hosting"
# Rewrites in firebase.json proxy /api, /a2a, /mcp, /media, /config.json and
# /health to Cloud Run, so the browser sees one origin and never a CORS preflight.
(cd "${REPO_ROOT}" && firebase deploy --only hosting --project "${PROJECT_ID}")

cat <<EOF

Deployed.

  App        ${SITE_URL}
  API        ${RUN_URL}   (proxied at ${SITE_URL}/api)
  Health     ${SITE_URL}/health
  Surfaces   ${SITE_URL}/api/surfaces
  A2A card   ${SITE_URL}/a2a/critic/.well-known/agent-card.json
  MCP        ${SITE_URL}/mcp

Idle cost: \$0 — Cloud Run min-instances=0, Firestore on-demand, Firebase
Hosting free tier, no load balancer. (Secret Manager: ~\$0.06/month.)

If sign-in fails with OPERATION_NOT_ALLOWED, enable the Email/Password provider:
  https://console.cloud.google.com/customer-identity/providers?project=${PROJECT_ID}

To grant yourself admin (custom claim, never client-assertable):
  node -e "const a=require('firebase-admin');a.initializeApp();a.auth().getUserByEmail(process.argv[1]).then(u=>a.auth().setCustomUserClaims(u.uid,{admin:true}))" you@example.com
EOF
