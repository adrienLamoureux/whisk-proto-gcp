"""Image generation and the media library.

The form-driven half of the product: the same operations the agent reaches
through tools, exposed as plain REST. One engine, two surfaces -- which is the
whole point of the AWS original's dashboard/companion split.

The confirm-intent tools depend on this: when the agent proposes
`share_image`, the browser calls `POST /api/media/{id}/share` here after the user
approves. The write path is identical whether a human or an agent initiated it,
so authorisation lives in one place.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import User, current_user
from ..deps import Deps
from ..providers.images import normalise_aspect, normalise_style
from ..store import media as media_store
from .common import get_deps

router = APIRouter(prefix="/api")


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000)
    style: str = "anime"
    aspect: str = "3:4"
    seed: int | None = None


@router.post("/images/generate")
async def generate_image(
    body: GenerateRequest,
    user: User = Depends(current_user),
    deps: Deps = Depends(get_deps),
) -> dict:
    """Start a generation.

    Returns either a finished `media` (the prediction landed inside the
    `Prefer: wait` window) or a `pending` handle for the client to poll. See
    providers/replicate.py for why the window is short.
    """
    # Same check-and-consume the agent tool uses. Both entry points must pay the
    # quota or the cap is trivially bypassed by using the form instead.
    decision = await deps.guardrails.check_and_consume_image(user.uid)
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": decision.reason,
                "imagesToday": decision.used,
                "capacity": decision.capacity,
                "retryAfterMs": decision.retry_after_ms,
            },
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    style = normalise_style(body.style)
    aspect = normalise_aspect(body.aspect)
    prompt = body.prompt.strip()

    try:
        generation = await deps.images.generate(prompt, style=style, aspect=aspect, seed=body.seed)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="image_generation_failed") from exc

    if generation.status == "failed":
        raise HTTPException(status_code=502, detail=generation.error or "image_generation_failed")

    if generation.is_pending and generation.prediction_id:
        await media_store.create_job(
            deps.store,
            uid=user.uid,
            prediction_id=generation.prediction_id,
            prompt=prompt,
            style=style,
            aspect=aspect,
        )
        return {
            "pending": {
                "predictionId": generation.prediction_id,
                "prompt": prompt,
                "style": style,
                "aspect": aspect,
            },
            "imagesRemaining": decision.remaining,
        }

    record = await media_store.create_image_record(
        deps.store,
        deps.storage,
        uid=user.uid,
        image=generation.image,
        prompt=prompt,
        style=style,
        aspect=aspect,
        seed=body.seed,
        source="studio",
    )
    enriched = await media_store.with_url(deps.storage, record)
    return {"media": enriched, "imagesRemaining": decision.remaining}


@router.get("/images/status")
async def generation_status(
    predictionId: str,  # noqa: N803 - matches the JSON field the client sends
    user: User = Depends(current_user),
    deps: Deps = Depends(get_deps),
) -> dict:
    """Poll a pending generation, saving the image on the turn it lands.

    Authorisation is by job ownership, not by prediction id. Prediction ids are
    not secret -- they appear in logs and in the client -- so resolving one
    without checking who owns it would let anyone read anyone's output.
    """
    job = await media_store.get_job(deps.store, uid=user.uid, prediction_id=predictionId)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown_prediction")

    if job.get("status") == "succeeded" and job.get("mediaId"):
        record = await media_store.get_media(deps.store, uid=user.uid, media_id=job["mediaId"])
        if record:
            return {"status": "succeeded", "media": await media_store.with_url(deps.storage, record)}

    if media_store.job_expired(job):
        await media_store.finish_job(
            deps.store, uid=user.uid, prediction_id=predictionId, status="failed"
        )
        raise HTTPException(status_code=504, detail="generation_timed_out")

    generation = await deps.images.poll(predictionId)

    if generation.is_pending:
        return {"status": "processing"}

    if not generation.succeeded:
        await media_store.finish_job(
            deps.store, uid=user.uid, prediction_id=predictionId, status="failed"
        )
        return {"status": "failed", "error": generation.error or "generation_failed"}

    record = await media_store.create_image_record(
        deps.store,
        deps.storage,
        uid=user.uid,
        image=generation.image,
        prompt=job.get("prompt", ""),
        style=job.get("style", "anime"),
        aspect=job.get("aspect", "3:4"),
        source=job.get("source", "studio"),
    )
    await media_store.finish_job(
        deps.store,
        uid=user.uid,
        prediction_id=predictionId,
        status="succeeded",
        media_id=record["mediaId"],
    )
    return {"status": "succeeded", "media": await media_store.with_url(deps.storage, record)}


@router.get("/media")
async def list_media(
    limit: int = 24,
    favorites: bool = False,
    user: User = Depends(current_user),
    deps: Deps = Depends(get_deps),
) -> dict:
    items = await media_store.list_media(
        deps.store,
        deps.storage,
        uid=user.uid,
        limit=max(1, min(60, limit)),
        favorites_only=favorites,
    )
    return {"items": items, "count": len(items)}


@router.get("/gallery")
async def public_gallery(limit: int = 24, deps: Deps = Depends(get_deps)) -> dict:
    """Public: the shared gallery is the one place media is world-readable."""
    items = await media_store.list_shared(deps.store, deps.storage, limit=max(1, min(60, limit)))
    return {"items": items, "count": len(items)}


class FavoriteRequest(BaseModel):
    favorite: bool = True


@router.post("/media/{media_id}/favorite")
async def favorite(
    media_id: str,
    body: FavoriteRequest,
    user: User = Depends(current_user),
    deps: Deps = Depends(get_deps),
) -> dict:
    record = await media_store.set_favorite(
        deps.store, uid=user.uid, media_id=media_id, favorite=body.favorite
    )
    if record is None:
        raise HTTPException(status_code=404, detail="media_not_found")
    return {"media": record}


@router.post("/media/{media_id}/share")
async def share(
    media_id: str,
    user: User = Depends(current_user),
    deps: Deps = Depends(get_deps),
) -> dict:
    """Confirm endpoint for the agent's `share_image` intent."""
    record = await media_store.share_media(
        deps.store, deps.storage, uid=user.uid, media_id=media_id
    )
    if record is None:
        raise HTTPException(status_code=404, detail="media_not_found")
    return {"shared": record}


@router.delete("/media/{media_id}")
async def delete(
    media_id: str,
    user: User = Depends(current_user),
    deps: Deps = Depends(get_deps),
) -> dict:
    """Confirm endpoint for the agent's `delete_image` intent."""
    if not await media_store.delete_media(
        deps.store, deps.storage, uid=user.uid, media_id=media_id
    ):
        raise HTTPException(status_code=404, detail="media_not_found")
    return {"deleted": media_id}
