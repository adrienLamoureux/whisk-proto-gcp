# Two-stage: build the React bundle, then serve it from the Python image.
# One container, one Cloud Run service, one deploy.

FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY backend/pyproject.toml ./backend/
RUN pip install --no-cache-dir ./backend

COPY backend/ ./backend/
COPY docs/ ./docs/
COPY --from=frontend /build/dist ./frontend/dist

# The corpus is the docs directory, baked into the image so a cold container can
# answer grounded questions on its first request.
ENV WHISK_MODE=gcp \
    WHISK_CORPUS_DIR=/srv/docs \
    WHISK_FRONTEND_DIST=/srv/frontend/dist

WORKDIR /srv/backend

# Cloud Run injects $PORT and expects the container to listen on it.
# One worker: the app is async and Cloud Run scales by adding instances, so
# in-process workers would just multiply memory for no concurrency gain.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
