"""AnimagineXL 3.1 on Replicate -- the only image generator.

## Why Replicate on a GCP project

It looks like a contradiction until you price it. Vertex AI Imagen is the
"native" answer, and for photoreal work it is excellent, but this product is an
anime studio: AnimagineXL is a purpose-built SDXL fine-tune that produces better
results for the actual use case at roughly an order of magnitude lower cost per
image, and it understands Danbooru tag vocabulary that Imagen has no concept of.

It also costs nothing when idle -- Replicate bills per second of GPU time, so the
zero-idle rule holds. And it keeps this port honest about the AWS original, where
Replicate was already the dominant provider and the dominant cost.

The interview version: "picking the cloud's first-party model because you are on
that cloud is how you end up paying more for a worse fit. The constraint was
pay-per-use, not single-vendor."

## Pinned version, not a tag

`MODEL_VERSION` is a content hash, not `latest`. Replicate model owners can push
a new version with different inputs or different output quality at any time;
pinning means a redeploy changes behaviour, not someone else's push. Upgrading is
a deliberate one-line change.

## The `Prefer: wait` window

Replicate returns a prediction, not an image. `Prefer: wait=n` holds the
connection up to n seconds and returns a terminal state if the job lands.

`n` is small (5s) on purpose. **Cloud Run bills CPU for the whole request
duration**, so waiting out a 60s cold boot costs ~12x a 5s wait *and* occupies a
concurrency slot the entire time. Below the window the user gets a synchronous
image; above it they get a prediction id and the browser polls. Same tradeoff the
AWS original made, for the same reason.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .images import ASPECT_RATIOS, GeneratedImage, Generation, normalise_aspect, normalise_style

log = logging.getLogger("whisk.replicate")

API_ROOT = "https://api.replicate.com/v1"

# cjwbw/animagine-xl-3.1 -- pinned by content hash. See the module docstring.
MODEL_VERSION = "6afe2e6b27dad2d6f480b59195c221884b6acc589ff4d05ff0e5fc058690fbb9"
MODEL_SLUG = "cjwbw/animagine-xl-3.1"

# AnimagineXL's own style presets. Mapping the app's four styles onto them beats
# stuffing style words into the prompt: the presets are applied as trained
# prefix/suffix pairs the model was tuned against.
STYLE_SELECTOR = {
    "anime": "Anime",
    "manga": "Manga",
    "photoreal": "Photographic",
    "chibi": "Anime",  # no chibi preset; the tag below carries it
}

# Danbooru-vocabulary hints appended to the prompt. AnimagineXL is trained on tag
# text, so a natural-language sentence alone underperforms a sentence plus tags.
STYLE_TAGS = {
    "anime": "",
    "manga": "monochrome, greyscale, manga",
    "photoreal": "realistic, photorealistic",
    "chibi": "chibi, super deformed, mini character",
}

QUALITY_SELECTOR = "Standard v3.1"

# The model card's default negative prompt, which encodes the tags this
# fine-tune responds to. Deviating from it measurably degrades output, so it is
# reproduced rather than reinvented. `nsfw` leads it deliberately: this is a
# content-safety control, not a quality one, and it is not user-overridable.
NEGATIVE_PROMPT = (
    "nsfw, lowres, (bad), text, error, fewer, extra, missing, worst quality, "
    "jpeg artifacts, low quality, watermark, unfinished, displeasing, oldest, "
    "early, chromatic aberration, signature, extra digits, artistic error, "
    "username, scan, [abstract]"
)

NUM_INFERENCE_STEPS = 28
GUIDANCE_SCALE = 7.0

TERMINAL_FAILURE = ("failed", "canceled")


class ReplicateError(RuntimeError):
    pass


def build_prompt(prompt: str, style: str) -> str:
    """Compose the final prompt: user text, then style tags.

    Quality tags ("masterpiece, best quality") are NOT added here -- the
    `quality_selector` input applies them, and doing both double-weights them and
    washes out the subject.
    """
    tags = STYLE_TAGS.get(style, "")
    return f"{prompt.strip()}, {tags}" if tags else prompt.strip()


class ReplicateImageProvider:
    """AnimagineXL 3.1 via Replicate's HTTP API."""

    def __init__(
        self,
        api_token: str,
        *,
        wait_seconds: int = 5,
        request_timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_token:
            raise ValueError("replicate_token_missing")
        self._token = api_token
        self._wait_seconds = max(1, min(60, wait_seconds))
        # Injectable so tests can drive the whole thing through a MockTransport.
        self._client = client or httpx.AsyncClient(timeout=request_timeout)

    def _headers(self, *, wait: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        if wait:
            headers["Prefer"] = f"wait={self._wait_seconds}"
        return headers

    async def generate(
        self, prompt: str, *, style: str = "anime", aspect: str = "3:4", seed: int | None = None
    ) -> Generation:
        if not prompt or not prompt.strip():
            return Generation(status="failed", error="prompt_required")

        style = normalise_style(style)
        aspect = normalise_aspect(aspect)
        width, height = ASPECT_RATIOS[aspect]

        payload: dict[str, Any] = {
            "version": MODEL_VERSION,
            "input": {
                "prompt": build_prompt(prompt, style),
                "negative_prompt": NEGATIVE_PROMPT,
                "width": width,
                "height": height,
                "num_inference_steps": NUM_INFERENCE_STEPS,
                "guidance_scale": GUIDANCE_SCALE,
                "quality_selector": QUALITY_SELECTOR,
                "style_selector": STYLE_SELECTOR[style],
            },
        }
        if seed is not None:
            payload["input"]["seed"] = seed

        try:
            response = await self._client.post(
                f"{API_ROOT}/predictions", headers=self._headers(wait=True), json=payload
            )
        except httpx.HTTPError as exc:
            log.warning("replicate create failed: %s", exc)
            return Generation(status="failed", error="replicate_unreachable")

        if response.status_code >= 400:
            log.warning("replicate create %s: %s", response.status_code, response.text[:300])
            return Generation(
                status="failed",
                error="replicate_quota" if response.status_code == 402 else "replicate_create_failed",
            )

        return await self._from_prediction(response.json(), width, height)

    async def poll(self, prediction_id: str) -> Generation:
        if not prediction_id:
            return Generation(status="failed", error="unknown_prediction")
        try:
            response = await self._client.get(
                f"{API_ROOT}/predictions/{prediction_id}", headers=self._headers()
            )
        except httpx.HTTPError as exc:
            log.warning("replicate poll failed: %s", exc)
            # Transient: report still-processing so the client retries rather
            # than discarding a prediction that is probably still running.
            return Generation(status="processing", prediction_id=prediction_id)

        if response.status_code == 404:
            return Generation(status="failed", error="unknown_prediction")
        if response.status_code >= 400:
            return Generation(status="failed", error="replicate_poll_failed")

        return await self._from_prediction(response.json())

    async def _from_prediction(
        self, body: dict[str, Any], width: int = 0, height: int = 0
    ) -> Generation:
        status = str(body.get("status") or "")
        prediction_id = str(body.get("id") or "") or None

        if status in TERMINAL_FAILURE:
            detail = str(body.get("error") or "")[:200]
            log.warning("replicate prediction %s %s: %s", prediction_id, status, detail)
            return Generation(status="failed", prediction_id=prediction_id, error="generation_failed")

        if status != "succeeded":
            return Generation(status="processing", prediction_id=prediction_id)

        url = _first_output_url(body.get("output"))
        if not url:
            return Generation(status="failed", prediction_id=prediction_id, error="no_output")

        try:
            image = await self._download(url, width, height)
        except (httpx.HTTPError, ReplicateError) as exc:
            log.warning("replicate output download failed: %s", exc)
            return Generation(status="failed", prediction_id=prediction_id, error="download_failed")

        return Generation(status="succeeded", image=image, prediction_id=prediction_id)

    async def _download(self, url: str, width: int, height: int) -> GeneratedImage:
        """Fetch the rendered image so it lands in our own bucket.

        Replicate's output URLs expire (hours, not days). Persisting the bytes is
        what makes the library durable, and it keeps user media inside the
        `users/{uid}/` isolation model instead of on a third-party host.
        """
        response = await self._client.get(url, follow_redirects=True)
        if response.status_code >= 400:
            raise ReplicateError(f"output fetch returned {response.status_code}")

        content_type = response.headers.get("content-type", "image/png").split(";")[0].strip()
        extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(
            content_type, "png"
        )
        return GeneratedImage(
            data=response.content,
            content_type=content_type,
            extension=extension,
            width=width,
            height=height,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _first_output_url(output: Any) -> str | None:
    """AnimagineXL returns a list of URLs; other models return a bare string."""
    if isinstance(output, str):
        return output or None
    if isinstance(output, list):
        for item in output:
            if isinstance(item, str) and item:
                return item
    return None


async def wait_for(
    provider: ReplicateImageProvider,
    prediction_id: str,
    *,
    attempts: int = 30,
    interval: float = 2.0,
) -> Generation:
    """Poll to completion. For scripts and tests -- request handlers must not
    use this, for the Cloud Run billing reason in the module docstring."""
    generation = Generation(status="processing", prediction_id=prediction_id)
    for _ in range(attempts):
        generation = await provider.poll(prediction_id)
        if not generation.is_pending:
            return generation
        await asyncio.sleep(interval)
    return generation
