"""The Replicate/AnimagineXL provider, driven through an httpx MockTransport.

The local provider always resolves synchronously, so without these the pending/
polling path -- the half that only runs in production -- would never execute.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.providers.images import ASPECT_RATIOS
from app.providers.replicate import (
    MODEL_VERSION,
    NEGATIVE_PROMPT,
    ReplicateImageProvider,
    build_prompt,
    wait_for,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def make_provider(handler, **kwargs) -> ReplicateImageProvider:
    transport = httpx.MockTransport(handler)
    return ReplicateImageProvider(
        "test-token", client=httpx.AsyncClient(transport=transport), **kwargs
    )


def image_response() -> httpx.Response:
    return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})


# --- prompt composition ----------------------------------------------------
def test_style_tags_are_appended():
    assert build_prompt("a fox", "manga").endswith("monochrome, greyscale, manga")
    assert build_prompt("a fox", "chibi").endswith("chibi, super deformed, mini character")


def test_anime_adds_no_redundant_tags():
    """Quality tags come from `quality_selector`; adding them here too
    double-weights them and washes out the subject."""
    assert build_prompt("a fox", "anime") == "a fox"


# --- the synchronous fast path ---------------------------------------------
async def test_fast_path_returns_an_image():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/predictions":
            seen["headers"] = dict(request.headers)
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"id": "pred1", "status": "succeeded", "output": ["https://cdn/out.png"]},
            )
        return image_response()

    provider = make_provider(handler)
    result = await provider.generate("a red fox", style="anime", aspect="16:9", seed=7)

    assert result.succeeded
    assert result.image.data == PNG
    assert result.image.extension == "png"
    assert (result.image.width, result.image.height) == ASPECT_RATIOS["16:9"]

    # The version is pinned by hash, never a floating tag.
    assert seen["body"]["version"] == MODEL_VERSION
    assert seen["headers"]["prefer"] == "wait=5"
    assert seen["headers"]["authorization"] == "Bearer test-token"

    payload = seen["body"]["input"]
    assert payload["width"], payload["height"] == ASPECT_RATIOS["16:9"]
    assert payload["negative_prompt"] == NEGATIVE_PROMPT
    assert payload["style_selector"] == "Anime"
    assert payload["seed"] == 7


async def test_seed_is_omitted_when_unset():
    """Sending seed=None pins every generation to the model's null-seed
    behaviour instead of letting it randomise."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/predictions":
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "p", "status": "succeeded", "output": ["https://cdn/o.png"]})
        return image_response()

    await make_provider(handler).generate("a fox")
    assert "seed" not in seen["body"]["input"]


async def test_aspect_maps_to_sdxl_buckets():
    """Off-bucket sizes degrade SDXL composition, so the mapping is fixed."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/predictions":
            seen["input"] = json.loads(request.content)["input"]
            return httpx.Response(200, json={"id": "p", "status": "succeeded", "output": ["https://cdn/o.png"]})
        return image_response()

    provider = make_provider(handler)
    for aspect, (width, height) in ASPECT_RATIOS.items():
        await provider.generate("a fox", aspect=aspect)
        assert (seen["input"]["width"], seen["input"]["height"]) == (width, height)


# --- the polling path ------------------------------------------------------
async def test_pending_then_succeeded():
    """The prediction misses the wait window, then lands on a later poll."""
    calls = {"poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/predictions":
            return httpx.Response(200, json={"id": "pred9", "status": "processing"})
        if request.url.path == "/v1/predictions/pred9":
            calls["poll"] += 1
            if calls["poll"] < 2:
                return httpx.Response(200, json={"id": "pred9", "status": "processing"})
            return httpx.Response(
                200, json={"id": "pred9", "status": "succeeded", "output": ["https://cdn/o.png"]}
            )
        return image_response()

    provider = make_provider(handler)

    started = await provider.generate("a fox")
    assert started.is_pending
    assert started.prediction_id == "pred9"
    assert started.image is None

    finished = await wait_for(provider, "pred9", attempts=5, interval=0)
    assert finished.succeeded
    assert finished.image.data == PNG


async def test_failed_prediction_surfaces_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "p", "status": "failed", "error": "NSFW detected"})

    result = await make_provider(handler).generate("a fox")
    assert result.status == "failed"
    assert result.error == "generation_failed"


async def test_succeeded_without_output_is_a_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "p", "status": "succeeded", "output": []})

    result = await make_provider(handler).generate("a fox")
    assert result.status == "failed"
    assert result.error == "no_output"


async def test_payment_required_is_reported_distinctly():
    """A billing failure needs different copy from a transient one -- retrying
    an exhausted account forever is the wrong behaviour."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": "Insufficient credit"})

    result = await make_provider(handler).generate("a fox")
    assert result.error == "replicate_quota"


async def test_network_error_on_create_is_not_fatal():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    result = await make_provider(handler).generate("a fox")
    assert result.status == "failed"
    assert result.error == "replicate_unreachable"


async def test_network_error_while_polling_keeps_the_prediction():
    """Reporting failure here would discard a job that is probably still running."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    result = await make_provider(handler).poll("pred1")
    assert result.is_pending
    assert result.prediction_id == "pred1"


async def test_unknown_prediction_is_terminal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    result = await make_provider(handler).poll("nope")
    assert result.status == "failed"
    assert result.error == "unknown_prediction"


async def test_output_download_failure_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/predictions":
            return httpx.Response(
                200, json={"id": "p", "status": "succeeded", "output": ["https://cdn/gone.png"]}
            )
        return httpx.Response(410, text="expired")

    result = await make_provider(handler).generate("a fox")
    assert result.status == "failed"
    assert result.error == "download_failed"


async def test_empty_prompt_short_circuits():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not call Replicate for an empty prompt")

    result = await make_provider(handler).generate("   ")
    assert result.error == "prompt_required"


def test_missing_token_fails_construction():
    """Fail at startup, not on the first user's generation."""
    with pytest.raises(ValueError):
        ReplicateImageProvider("")
