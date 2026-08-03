from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent.whisk_runner import AgentService
from app.config import Settings
from app.deps import build_deps
from app.main import build_app


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        mode="local",
        local_media_dir=str(tmp_path / "media"),
        corpus_dir=str(tmp_path / "corpus"),
        frontend_dist=str(tmp_path / "no-frontend"),
        enable_a2a=False,  # skips a slow agent-card build in unit tests
        enable_mcp=False,
    )


@pytest.fixture
def deps(settings):
    return build_deps(settings)


@pytest.fixture
def agent_service(deps) -> AgentService:
    return AgentService(deps)


@pytest.fixture
def client(settings):
    with TestClient(build_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def corpus(tmp_path):
    """A tiny corpus with lexically distinct sections, so retrieval assertions
    test ranking rather than luck."""
    directory = tmp_path / "corpus"
    directory.mkdir(exist_ok=True)
    (directory / "cost.md").write_text(
        "# Cost model\n\n"
        "Every component bills per use with a zero idle floor. The daily image cap "
        "bounds the dominant spend vector at fifty generations.\n\n"
        "## Vector search\n\n"
        "Vertex AI Vector Search requires deployed index endpoint nodes billed hourly, "
        "so Firestore native vector queries were used instead.\n",
        encoding="utf-8",
    )
    (directory / "guardrails.md").write_text(
        "# Guardrails\n\n"
        "A token bucket refills lazily from elapsed time. Every limiter fails open "
        "when the database errors.\n",
        encoding="utf-8",
    )
    return directory
