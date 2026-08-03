"""Runtime configuration.

Mirrors the AWS repo's `frontend/public/config.json` + env-var contract, but the
important axis here is `WHISK_MODE`:

    local  -- no GCP calls at all. Gemini, Imagen, embeddings, Firestore and GCS
              are all served by in-process fakes. The *agent loop itself is real*
              (ADK Runner, real tool dispatch, real session state) -- only the
              model and the managed data plane are faked. This is what lets the
              whole stack run with zero cloud spend and no `gcloud` install.

    gcp    -- real Vertex AI (Gemini + Imagen + embeddings), real Firestore,
              real GCS. Every one of those is pay-per-use with a $0 idle floor,
              which is the whole point of the service selection (see
              docs/cost-model.md).

Nothing else in the codebase branches on the mode: `app/deps.py` picks the
implementations once and every route/tool takes them by injection.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["local", "gcp"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WHISK_",
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- runtime mode -------------------------------------------------------
    mode: Mode = "local"

    # --- GCP wiring (only read when mode == "gcp") --------------------------
    project_id: str = ""
    location: str = "us-central1"
    gcs_bucket: str = ""
    firestore_database: str = "(default)"

    # --- models -------------------------------------------------------------
    # Gemini 3.6 Flash is the stable workhorse; 3.1 Pro (1M ctx) is the preview
    # escalation path. Admin can override the agent model at runtime -- the
    # override lives in Firestore at config/agent (see store/keys.py).
    agent_model: str = "gemini-3.6-flash"
    pro_model: str = "gemini-3.1-pro-preview"
    embedding_model: str = "text-embedding-005"
    embedding_dim: int = 768

    # --- image generation (Replicate / AnimagineXL) -------------------------
    # The only generator. Injected by Cloud Run from Secret Manager
    # (`--set-secrets`), so the process just reads an env var and no secret
    # client library is needed. See providers/replicate.py for the model pin.
    replicate_api_token: str = ""
    # Synchronous window before falling back to client polling. Small on purpose:
    # Cloud Run bills CPU for the whole request duration.
    replicate_wait_seconds: int = 5

    # --- auth ---------------------------------------------------------------
    # Identity Platform / Firebase Auth is the Cognito equivalent. In local mode
    # we accept a dev header instead of verifying a JWT.
    auth_required: bool = False
    identity_platform_project: str = ""
    # Public Web API key for the Identity Platform REST API. Safe in a browser --
    # it identifies the project, it does not authorise anything. Served to the
    # frontend via /config.json.
    identity_platform_api_key: str = ""

    # --- guardrails (ported 1:1 from the AWS repo's numbers) ----------------
    rate_capacity: int = 30
    rate_refill_seconds: float = 2.0
    daily_token_cap: int = 200_000
    daily_image_cap: int = 50
    agent_config_cache_seconds: int = 60

    # --- local-mode storage --------------------------------------------------
    local_media_dir: str = ".localdata/media"
    # Built React bundle. Served by this same service when present; see main.py.
    frontend_dist: str = "../frontend/dist"
    # The RAG corpus *is* the docs directory. The agent grounds its answers about
    # this system in the same markdown a human reader would open, so the docs
    # cannot drift from what the agent claims.
    corpus_dir: str = "../docs"

    # --- surfaces -------------------------------------------------------------
    enable_a2a: bool = True
    enable_mcp: bool = True
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # Absolute base URL this service is reachable at. The A2A agent card
    # advertises absolute URLs, so a wrong value here sends peers to the wrong
    # endpoint while discovery still appears to succeed. Set it to the Cloud Run
    # service URL on deploy.
    public_base_url: str = "http://localhost:8080"

    @property
    def is_local(self) -> bool:
        return self.mode == "local"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def require_gcp(self) -> None:
        """Fail fast on a misconfigured deployment.

        These are checked at startup rather than on first use so a bad revision
        fails its Cloud Run health check and never takes traffic, instead of
        serving until someone tries to generate an image.
        """
        if self.mode != "gcp":
            return
        missing = [
            name
            for name, value in (
                ("WHISK_PROJECT_ID", self.project_id),
                ("WHISK_GCS_BUCKET", self.gcs_bucket),
                ("WHISK_REPLICATE_API_TOKEN", self.replicate_api_token),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"WHISK_MODE=gcp requires: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.require_gcp()
    return settings
