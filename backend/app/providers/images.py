"""Image generation: shared types, and the offline provider.

The real generator is Replicate/AnimagineXL (`providers/replicate.py`). This
module holds what both implementations agree on.

## Why generation is asynchronous

Replicate returns a *prediction*, not an image. The provider can wait for it
(`Prefer: wait=n`) or return an id for the client to poll.

Waiting is tempting and wrong past a few seconds, because **Cloud Run bills CPU
for the entire request duration**. Blocking 60s on a cold Replicate container
costs roughly 12x what a 5s wait costs, for one image, and it burns a request
slot against the concurrency limit the whole time. So the shape here is the AWS
original's: try a short synchronous window, and if the prediction has not landed,
hand back an id and let the browser poll.

That is why `generate()` returns a `Generation` rather than a `GeneratedImage`.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Protocol

# SDXL-native resolution buckets. AnimagineXL is an SDXL fine-tune, and SDXL was
# trained on a fixed set of aspect buckets around ~1M pixels -- feeding it an
# off-bucket size (say 1408x768) degrades composition and limb coherence. 896x1152
# is the model card's own default.
ASPECT_RATIOS: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "3:4": (896, 1152),
    "16:9": (1344, 768),
    "9:16": (768, 1344),
}

STYLES = ("anime", "manga", "photoreal", "chibi")


@dataclass(slots=True)
class GeneratedImage:
    data: bytes
    content_type: str
    extension: str
    width: int
    height: int


@dataclass(slots=True)
class Generation:
    """Either a finished image, or a handle to poll."""

    status: str  # "succeeded" | "processing" | "failed"
    image: GeneratedImage | None = None
    prediction_id: str | None = None
    error: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.status == "processing"

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded" and self.image is not None


class ImageProvider(Protocol):
    async def generate(
        self, prompt: str, *, style: str = "anime", aspect: str = "3:4", seed: int | None = None
    ) -> Generation: ...

    async def poll(self, prediction_id: str) -> Generation: ...


def normalise_style(style: str | None) -> str:
    value = (style or "").strip().lower()
    return value if value in STYLES else "anime"


def normalise_aspect(aspect: str | None) -> str:
    value = (aspect or "").strip()
    return value if value in ASPECT_RATIOS else "3:4"


def _digest(prompt: str, style: str, aspect: str, seed: int | None) -> bytes:
    payload = f"{prompt}|{style}|{aspect}|{seed if seed is not None else ''}".encode()
    return hashlib.sha256(payload).digest()


# ---------------------------------------------------------------------------
# local mode
# ---------------------------------------------------------------------------
class LocalImageProvider:
    """Deterministic SVG art derived from the prompt hash.

    Not a grey placeholder box: the same prompt always yields the same image and
    different prompts look visibly different, so the gallery, favourites and the
    agent's `recall_favorites` tool are all demonstrable without spending a cent.

    Always resolves synchronously -- it genuinely is instant. The pending/polling
    path is covered by `tests/test_replicate.py` against a mock transport.
    """

    async def generate(
        self, prompt: str, *, style: str = "anime", aspect: str = "3:4", seed: int | None = None
    ) -> Generation:
        style = normalise_style(style)
        aspect = normalise_aspect(aspect)
        width, height = ASPECT_RATIOS[aspect]
        digest = _digest(prompt, style, aspect, seed)

        # Two hues 40-140 degrees apart keep every pairing legible.
        hue_a = digest[0] / 255 * 360
        hue_b = (hue_a + 40 + (digest[1] / 255) * 100) % 360
        sat = 55 + (digest[2] / 255) * 30
        light_a = 32 + (digest[3] / 255) * 18
        light_b = 58 + (digest[4] / 255) * 20
        angle = int(digest[5] / 255 * 360)

        blobs = []
        for i in range(5):
            off = 6 + i * 4
            cx = int(digest[off] / 255 * width)
            cy = int(digest[off + 1] / 255 * height)
            r = int((0.08 + digest[off + 2] / 255 * 0.22) * min(width, height))
            opacity = round(0.10 + digest[off + 3] / 255 * 0.28, 3)
            blobs.append(
                f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="hsl({(hue_b + i * 24) % 360:.0f} '
                f'{sat:.0f}% {light_b:.0f}%)" opacity="{opacity}"/>'
            )

        # Simulated await: the real provider is a network round-trip, and code
        # that only ever sees an instant provider tends to hide ordering bugs.
        await asyncio.sleep(0)

        sans = "ui-sans-serif,system-ui,sans-serif"
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg"
     width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <defs>
    <linearGradient id="bg" gradientTransform="rotate({angle})">
      <stop offset="0%" stop-color="hsl({hue_a:.0f} {sat:.0f}% {light_a:.0f}%)"/>
      <stop offset="100%" stop-color="hsl({hue_b:.0f} {sat:.0f}% {light_b:.0f}%)"/>
    </linearGradient>
    <filter id="soft"><feGaussianBlur stdDeviation="{min(width, height) // 18}"/></filter>
  </defs>
  <rect width="{width}" height="{height}" fill="url(#bg)"/>
  <g filter="url(#soft)">{"".join(blobs)}</g>
  <text x="{width // 2}" y="{height - 54}" text-anchor="middle"
        font-family="{sans}" font-size="{max(15, width // 34)}"
        fill="rgba(255,255,255,0.82)">{_escape(prompt)[:58]}</text>
  <text x="{width // 2}" y="{height - 24}" text-anchor="middle"
        font-family="ui-monospace,monospace" font-size="{max(11, width // 52)}"
        fill="rgba(255,255,255,0.5)">local &#183; {style} &#183; {aspect}</text>
</svg>"""
        return Generation(
            status="succeeded",
            image=GeneratedImage(
                data=svg.encode("utf-8"),
                content_type="image/svg+xml",
                extension="svg",
                width=width,
                height=height,
            ),
        )

    async def poll(self, prediction_id: str) -> Generation:
        # Nothing is ever pending locally, so a poll can only be for an id that
        # does not exist.
        return Generation(status="failed", error="unknown_prediction")


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
