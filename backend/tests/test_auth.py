"""Auth gating.

The important assertion is the last one: the dev-header path must be unreachable
once the service is not running locally. It is the kind of convenience that
becomes an authentication bypass if its gate is ever loosened.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import _dev_user, current_user
from app.config import Settings
from app.main import build_app


@pytest.fixture
def guarded_settings(tmp_path) -> Settings:
    """A deployment-shaped config: auth required, no dev bypass."""
    return Settings(
        mode="local",
        auth_required=True,
        local_media_dir=str(tmp_path / "media"),
        corpus_dir=str(tmp_path / "corpus"),
        frontend_dist=str(tmp_path / "no-frontend"),
        enable_a2a=False,
        enable_mcp=False,
    )


def test_config_advertises_the_dev_provider_locally(client):
    auth = client.get("/config.json").json()["auth"]
    assert auth["provider"] == "dev-header"
    assert auth["required"] is False


def test_config_advertises_identity_platform_when_required(guarded_settings):
    guarded_settings.identity_platform_api_key = "AIzaTest"
    with TestClient(build_app(guarded_settings)) as client:
        auth = client.get("/config.json").json()["auth"]
    # The Web API key is public by design -- it identifies the project and
    # authorises nothing on its own.
    assert auth["apiKey"] == "AIzaTest"
    assert auth["required"] is True


def test_protected_routes_401_without_a_token(guarded_settings):
    with TestClient(build_app(guarded_settings)) as client:
        assert client.get("/api/media").status_code == 401
        assert client.post("/api/images/generate", json={"prompt": "x"}).status_code == 401
        assert client.post("/api/agent/turn", json={"message": "hi"}).status_code == 401


def test_dev_header_is_rejected_when_auth_is_required(guarded_settings):
    """The bypass is gated on the *server's* mode, not on what the client sends.

    Without this, anyone could impersonate any user on a deployed instance by
    setting a header.
    """
    with TestClient(build_app(guarded_settings)) as client:
        response = client.get(
            "/api/media", headers={"X-Whisk-User": "victim", "X-Whisk-Admin": "true"}
        )
    assert response.status_code == 401


def test_malformed_bearer_is_rejected(guarded_settings):
    with TestClient(build_app(guarded_settings)) as client:
        for header in ("", "Bearer", "Basic abc", "Bearer not-a-jwt"):
            assert client.get("/api/media", headers={"Authorization": header}).status_code in (
                401,
                403,
            )


def test_public_routes_stay_public(guarded_settings):
    with TestClient(build_app(guarded_settings)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/config.json").status_code == 200
        assert client.get("/api/gallery").status_code == 200


async def test_current_user_requires_a_token_when_auth_is_required(guarded_settings):
    with pytest.raises(HTTPException) as excinfo:
        await current_user(
            request=None,  # type: ignore[arg-type]
            authorization=None,
            x_whisk_user="someone",
            x_whisk_admin="true",
            settings=guarded_settings,
        )
    assert excinfo.value.status_code == 401


def test_dev_admin_flag_parsing():
    assert _dev_user("u", "true").is_admin
    assert _dev_user("u", "1").is_admin
    assert not _dev_user("u", "false").is_admin
    assert not _dev_user("u", None).is_admin
    assert _dev_user(None, None).uid == "dev-user"
