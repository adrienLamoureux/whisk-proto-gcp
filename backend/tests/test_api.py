"""The HTTP surface, including the paths that must fail."""

from __future__ import annotations


def test_health_and_config(client):
    assert client.get("/health").json()["status"] == "ok"
    config = client.get("/config.json").json()
    assert config["mode"] == "local"
    assert config["limits"]["dailyImageCap"] == 50


def test_generate_then_list_then_delete(client):
    created = client.post(
        "/api/images/generate", json={"prompt": "a lighthouse", "style": "photoreal", "aspect": "16:9"}
    )
    assert created.status_code == 200
    media = created.json()["media"]
    assert media["style"] == "photoreal"

    assert client.get("/api/media").json()["count"] == 1
    assert client.delete(f"/api/media/{media['mediaId']}").status_code == 200
    assert client.get("/api/media").json()["count"] == 0


def test_signed_url_serves_and_rejects_tampering(client):
    media = client.post("/api/images/generate", json={"prompt": "a fox"}).json()["media"]
    url = media["url"]

    assert client.get(url).status_code == 200
    # Corrupting the signature must 403, not serve.
    assert client.get(url.replace("sig=", "sig=deadbeef")).status_code == 403


def test_expired_url_is_rejected(client):
    media = client.post("/api/images/generate", json={"prompt": "a fox"}).json()["media"]
    key = media["key"]
    assert client.get(f"/media/{key}?exp=1&sig=whatever").status_code == 403


def test_users_cannot_see_each_others_media(client):
    client.post("/api/images/generate", json={"prompt": "alice's boat"})

    other = client.get("/api/media", headers={"X-Whisk-User": "bob"})
    assert other.json()["count"] == 0


def test_share_publishes_to_the_gallery(client):
    media = client.post("/api/images/generate", json={"prompt": "a shared boat"}).json()["media"]

    assert client.get("/api/gallery").json()["count"] == 0
    assert client.post(f"/api/media/{media['mediaId']}/share").status_code == 200

    gallery = client.get("/api/gallery").json()
    assert gallery["count"] == 1
    assert gallery["items"][0]["prompt"] == "a shared boat"


def test_missing_media_is_404(client):
    assert client.delete("/api/media/nope").status_code == 404
    assert client.post("/api/media/nope/share").status_code == 404


def test_agent_turn_over_http(client):
    response = client.post(
        "/api/agent/turn", json={"message": "draw a fox, 1:1", "sessionId": "http"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["toolCalls"][0]["name"] == "generate_image"
    assert body["toolCalls"][0]["kind"] == "server_dispatch"
    assert body["text"]
    assert body["usage"]["totalTokens"] > 0


def test_agent_session_lifecycle(client):
    client.post("/api/agent/turn", json={"message": "hello", "sessionId": "alpha"})

    assert any(s["sessionId"] == "alpha" for s in client.get("/api/agent/sessions").json()["items"])
    assert client.patch("/api/agent/sessions/alpha", json={"name": "Alpha"}).status_code == 200
    assert client.patch("/api/agent/sessions/ghost", json={"name": "X"}).status_code == 404

    assert client.delete("/api/agent/sessions/alpha").status_code == 200
    assert client.get("/api/agent/sessions/alpha/history").json()["items"] == []


def test_admin_routes_require_admin(client):
    assert client.get("/api/admin/agent/cost").status_code == 403
    assert client.put("/api/admin/agent/model", json={"modelId": "x"}).status_code == 403

    admin = {"X-Whisk-Admin": "true"}
    assert client.get("/api/admin/agent/cost", headers=admin).status_code == 200
    assert client.put("/api/admin/agent/model", json={"modelId": ""}, headers=admin).json()["reset"]


def test_model_override_round_trips(client):
    admin = {"X-Whisk-Admin": "true"}
    client.put("/api/admin/agent/model", json={"modelId": "gemini-3.1-pro-preview"}, headers=admin)

    current = client.get("/api/admin/agent/model").json()
    assert current["modelId"] == "gemini-3.1-pro-preview"
    assert current["overridden"] is True

    client.put("/api/admin/agent/model", json={"modelId": ""}, headers=admin)
    assert client.get("/api/admin/agent/model").json()["overridden"] is False


def test_image_cap_returns_429_with_retry_after(client):
    # The cap is 50/day; exhaust it through the form path.
    for _ in range(50):
        client.post("/api/images/generate", json={"prompt": "x"})

    blocked = client.post("/api/images/generate", json={"prompt": "one too many"})
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"]
    assert blocked.json()["detail"]["error"] == "image_daily_cap_reached"


def test_usage_reports_both_counters(client):
    client.post("/api/images/generate", json={"prompt": "a fox"})
    usage = client.get("/api/agent/usage").json()
    assert usage["imagesToday"] == 1
    assert usage["imageCap"] == 50
    assert usage["tokenCap"] == 200_000


# --- pending generation (Replicate polling path) ---------------------------
class _PendingProvider:
    """Stands in for Replicate when a prediction misses the wait window."""

    def __init__(self) -> None:
        self.polls = 0

    async def generate(self, prompt, *, style="anime", aspect="3:4", seed=None):
        from app.providers.images import Generation

        return Generation(status="processing", prediction_id="pred-test")

    async def poll(self, prediction_id):
        from app.providers.images import GeneratedImage, Generation

        self.polls += 1
        if self.polls < 2:
            return Generation(status="processing", prediction_id=prediction_id)
        return Generation(
            status="succeeded",
            prediction_id=prediction_id,
            image=GeneratedImage(b"<svg/>", "image/svg+xml", "svg", 896, 1152),
        )


def test_pending_generation_then_poll_to_completion(client):
    client.app.state.deps.images = _PendingProvider()

    started = client.post("/api/images/generate", json={"prompt": "a slow fox"})
    assert started.status_code == 200
    pending = started.json()["pending"]
    assert pending["predictionId"] == "pred-test"
    assert client.get("/api/media").json()["count"] == 0

    still = client.get("/api/images/status", params={"predictionId": "pred-test"})
    assert still.json()["status"] == "processing"

    done = client.get("/api/images/status", params={"predictionId": "pred-test"})
    assert done.json()["status"] == "succeeded"
    assert done.json()["media"]["prompt"] == "a slow fox"
    assert client.get("/api/media").json()["count"] == 1


def test_polling_is_scoped_to_the_job_owner(client):
    """Prediction ids are not secret, so ownership -- not the id -- authorises."""
    client.app.state.deps.images = _PendingProvider()
    client.post("/api/images/generate", json={"prompt": "alice's fox"})

    stolen = client.get(
        "/api/images/status",
        params={"predictionId": "pred-test"},
        headers={"X-Whisk-User": "bob"},
    )
    assert stolen.status_code == 404


def test_status_of_unknown_prediction_is_404(client):
    assert client.get("/api/images/status", params={"predictionId": "nope"}).status_code == 404
