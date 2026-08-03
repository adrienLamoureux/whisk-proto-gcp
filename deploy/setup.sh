#!/usr/bin/env bash
# One-time GCP project setup.
#
# Everything created here is pay-per-use with a $0 idle floor -- no VM, no load
# balancer, no SQL instance, no vector index endpoint. The one exception is
# Secret Manager, at ~$0.06/month per active secret version.
#
# Usage:  PROJECT_ID=my-project REPLICATE_API_TOKEN=r8_... bash deploy/setup.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-whisk-studio}"
BUCKET="${BUCKET:-${PROJECT_ID}-whisk-media}"
SA="${SERVICE}-run"
SA_EMAIL="${SA}@${PROJECT_ID}.iam.gserviceaccount.com"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> project=${PROJECT_ID} region=${REGION}"
gcloud config set project "${PROJECT_ID}"

echo "==> enabling APIs"
gcloud services enable \
  run.googleapis.com \
  aiplatform.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  identitytoolkit.googleapis.com \
  iamcredentials.googleapis.com \
  secretmanager.googleapis.com \
  firebasehosting.googleapis.com

echo "==> Firestore (Native mode, on-demand)"
gcloud firestore databases create --location="${REGION}" --type=firestore-native 2>/dev/null \
  || echo "    already exists"

echo "==> Cloud Storage bucket (uniform access, no public objects)"
gcloud storage buckets create "gs://${BUCKET}" \
  --location="${REGION}" --uniform-bucket-level-access 2>/dev/null \
  || echo "    already exists"

# Generated images are read through short-lived signed URLs from the browser, so
# the bucket needs CORS for the site origin -- without it the <img> loads but any
# fetch()/canvas read is blocked.
echo "==> bucket CORS"
cat > /tmp/whisk-cors.json <<EOF
[{"origin":["https://${PROJECT_ID}.web.app","https://${PROJECT_ID}.firebaseapp.com","http://localhost:5173"],
  "method":["GET","HEAD"],"responseHeader":["Content-Type"],"maxAgeSeconds":3600}]
EOF
gcloud storage buckets update "gs://${BUCKET}" --cors-file=/tmp/whisk-cors.json

echo "==> runtime service account"
gcloud iam service-accounts create "${SA}" --display-name="Whisk Cloud Run runtime" 2>/dev/null \
  || echo "    already exists"

for role in roles/datastore.user roles/storage.objectAdmin roles/aiplatform.user \
            roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" --role="${role}" --condition=None >/dev/null
done

# THE non-obvious grant. Cloud Run's runtime service account has no downloadable
# private key -- that is the point of it -- so `generate_signed_url` cannot sign
# locally. It must delegate to the IAM Credentials API (`signBlob`), which
# requires the SA to be able to impersonate *itself*. Without this, image reads
# fail at runtime with an opaque signing error and nothing else breaks first.
gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountTokenCreator" >/dev/null

echo "==> Replicate API token -> Secret Manager"
if [ -n "${REPLICATE_API_TOKEN:-}" ]; then
  gcloud secrets create replicate-api-token --replication-policy=automatic 2>/dev/null || true
  printf '%s' "${REPLICATE_API_TOKEN}" \
    | gcloud secrets versions add replicate-api-token --data-file=- >/dev/null
  echo "    stored"
else
  echo "    SKIPPED - set REPLICATE_API_TOKEN and re-run, or:"
  echo "      printf '%s' \"\$REPLICATE_API_TOKEN\" | gcloud secrets versions add replicate-api-token --data-file=-"
fi

echo "==> Firestore vector index for RAG"
# `find_nearest` requires a composite index with a vector config. Until this
# exists, KNN queries fail with FAILED_PRECONDITION (and the error text contains
# a link that creates it for you).
gcloud firestore indexes composite create \
  --collection-group=ragChunks \
  --query-scope=COLLECTION \
  --field-config=field-path=embedding,vector-config='{"dimension":768,"flat":{}}' \
  2>/dev/null || echo "    already exists (or still building)"

echo "==> Firebase project link"
printf '{\n  "projects": {\n    "default": "%s"\n  }\n}\n' "${PROJECT_ID}" > "${REPO_ROOT}/.firebaserc"
echo "    wrote .firebaserc"

API_KEY="$(gcloud services api-keys list --format='value(keyString)' --limit=1 2>/dev/null || true)"

cat <<EOF

Setup complete.

  PROJECT_ID=${PROJECT_ID}
  BUCKET=${BUCKET}
  SERVICE_ACCOUNT=${SA_EMAIL}

Two things still need the console (no gcloud equivalent):

  1. Identity Platform -> enable the Email/Password provider
     https://console.cloud.google.com/customer-identity/providers?project=${PROJECT_ID}

  2. Copy the Web API key (Identity Platform -> Application Setup Details)
     and export it before deploying:
       export IDENTITY_PLATFORM_API_KEY=AIza...
$( [ -n "${API_KEY}" ] && echo "     (an existing key in this project: ${API_KEY})" )

  3. Firebase Hosting must be initialised once:
       firebase projects:addfirebase ${PROJECT_ID}   # if not already a Firebase project
       firebase experiments:enable webframeworks 2>/dev/null || true

Then:
  PROJECT_ID=${PROJECT_ID} IDENTITY_PLATFORM_API_KEY=... bash deploy/deploy.sh
EOF
