"""A2A and MCP mounting.

These exist because both surfaces failed silently the first time: Starlette does
not run a mounted sub-app's lifespan, and both register their routes (A2A) or
start their transport (MCP) from inside one. Everything else kept working, so
only a request to the mounted path revealed it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import build_app

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-06-18",
}


@pytest.fixture
def surfaced_settings(tmp_path) -> Settings:
    return Settings(
        mode="local",
        local_media_dir=str(tmp_path / "media"),
        corpus_dir=str(tmp_path / "corpus"),
        frontend_dist=str(tmp_path / "no-frontend"),
        public_base_url="https://whisk.example.com",
        enable_a2a=True,
        enable_mcp=True,
    )


@pytest.fixture
def surfaced_client(surfaced_settings):
    with TestClient(build_app(surfaced_settings)) as client:
        yield client


def test_surfaces_endpoint_lists_all_three(surfaced_client):
    surfaces = surfaced_client.get("/api/surfaces").json()["surfaces"]
    assert set(surfaces) == {"rest", "a2a", "mcp"}


def test_a2a_agent_card_is_served(surfaced_client):
    """Regression: the card 404s unless the sub-app's lifespan is entered."""
    response = surfaced_client.get("/a2a/critic/.well-known/agent-card.json")
    assert response.status_code == 200

    card = response.json()
    assert card["name"] == "prompt_critic"
    assert [s["id"] for s in card["skills"]] == ["critique_image_prompt"]


def test_agent_card_advertises_the_mounted_url(surfaced_client):
    """Regression: ADK builds the URL from host/port and knows nothing about the
    mount prefix, so the card would send clients to `/` -- discovery appears to
    succeed and invocation goes nowhere."""
    card = surfaced_client.get("/a2a/critic/.well-known/agent-card.json").json()
    url = card["supportedInterfaces"][0]["url"]

    assert url == "https://whisk.example.com/a2a/critic/"


def test_mcp_initialize_handshake(surfaced_client):
    """Regression: `streamable_http_app()` serves at `/mcp` internally, so
    mounting it at `/mcp` resolves to `/mcp/mcp`."""
    response = surfaced_client.post(
        "/mcp/",
        headers=MCP_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "whisk-studio"


def test_mcp_accepts_the_deployed_host(surfaced_settings):
    """Regression: MCP's DNS-rebinding protection defaults to an EMPTY host
    allow-list, which 421s everything that is not loopback.

    Loopback passes, so this is invisible locally and only breaks once the
    service sits behind a real hostname -- which is to say, in production.
    """
    app = build_app(surfaced_settings)
    with TestClient(app, base_url="https://whisk.example.com") as client:
        response = client.post(
            "/mcp/",
            headers=MCP_HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
        )
    assert response.status_code == 200, "deployed host must not be rejected as DNS rebinding"


def test_mcp_rejects_an_unknown_host(surfaced_settings):
    """The protection must still work -- allow-listing is not disabling."""
    app = build_app(surfaced_settings)
    with TestClient(app, base_url="https://attacker.example.net") as client:
        response = client.post(
            "/mcp/", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
    assert response.status_code == 421


def test_mcp_publishes_only_read_only_tools(surfaced_client):
    """MCP carries no end-user identity, so nothing user-scoped may be exposed."""
    response = surfaced_client.post(
        "/mcp/", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    names = {t["name"] for t in response.json()["result"]["tools"]}

    assert names == {"browse_gallery", "search_studio_docs"}
    assert not names & {"generate_image", "recall_favorites", "share_image", "delete_image"}
