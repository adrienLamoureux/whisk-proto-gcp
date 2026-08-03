"""Media records: Firestore metadata + GCS object, always paired.

Mirrors the AWS `IMG#` items. One rule runs through every function here: the
object is private and the URL is minted on read with a short TTL. Nothing
persists a URL, because a persisted URL is a permanent one.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from ..providers.images import GeneratedImage
from ..providers.storage import MediaStorage, assert_owned, media_key
from . import keys
from .docstore import DocStore

SIGNED_URL_TTL = 900  # 15 minutes
# A prediction that has not landed in this long is never going to. Replicate
# jobs run in seconds; the ceiling only exists so abandoned rows do not
# accumulate and so the UI can stop polling on something.
JOB_TTL_MS = 15 * 60 * 1000


def new_media_id() -> str:
    return uuid.uuid4().hex[:16]


# --- in-flight generation jobs --------------------------------------------
async def create_job(
    store: DocStore,
    *,
    uid: str,
    prediction_id: str,
    prompt: str,
    style: str,
    aspect: str,
    source: str = "studio",
) -> dict[str, Any]:
    """Record a pending prediction so the status route can finish it.

    Holds the generation parameters because the media record is written when the
    prediction *lands*, and by then the original request is long gone.
    """
    record = {
        "predictionId": prediction_id,
        "prompt": prompt,
        "style": style,
        "aspect": aspect,
        "source": source,
        "status": "processing",
        "createdAt": int(time.time() * 1000),
    }
    await store.set(keys.job_doc(uid, prediction_id), record)
    return record


async def get_job(store: DocStore, *, uid: str, prediction_id: str) -> dict[str, Any] | None:
    return await store.get(keys.job_doc(uid, prediction_id))


async def finish_job(
    store: DocStore, *, uid: str, prediction_id: str, status: str, media_id: str | None = None
) -> None:
    await store.set(
        keys.job_doc(uid, prediction_id),
        {"status": status, "mediaId": media_id, "finishedAt": int(time.time() * 1000)},
        merge=True,
    )


def job_expired(job: dict[str, Any], *, now_ms: int | None = None) -> bool:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    return now - int(job.get("createdAt", 0)) > JOB_TTL_MS


async def create_image_record(
    store: DocStore,
    storage: MediaStorage,
    *,
    uid: str,
    image: GeneratedImage,
    prompt: str,
    style: str,
    aspect: str,
    seed: int | None = None,
    source: str = "studio",
) -> dict[str, Any]:
    media_id = new_media_id()
    key = media_key(uid, media_id, image.extension)
    await storage.put(key, image.data, image.content_type)

    record = {
        "mediaId": media_id,
        "key": key,
        "kind": "image",
        "prompt": prompt,
        "style": style,
        "aspect": aspect,
        "seed": seed,
        "width": image.width,
        "height": image.height,
        "contentType": image.content_type,
        "source": source,
        "favorite": False,
        "shared": False,
        "createdAt": int(time.time() * 1000),
    }
    await store.set(keys.media_doc(uid, media_id), record)
    return record


async def with_url(storage: MediaStorage, record: dict[str, Any]) -> dict[str, Any]:
    return {**record, "url": await storage.signed_url(record["key"], ttl_seconds=SIGNED_URL_TTL)}


async def list_media(
    store: DocStore,
    storage: MediaStorage,
    *,
    uid: str,
    limit: int = 24,
    favorites_only: bool = False,
) -> list[dict[str, Any]]:
    rows = await store.list(
        keys.media_collection(uid),
        order_by="createdAt",
        desc=True,
        limit=limit if not favorites_only else None,
        where=("favorite", "==", True) if favorites_only else None,
    )
    return [await with_url(storage, r) for r in rows[:limit]]


async def get_media(store: DocStore, *, uid: str, media_id: str) -> dict[str, Any] | None:
    return await store.get(keys.media_doc(uid, media_id))


async def set_favorite(
    store: DocStore, *, uid: str, media_id: str, favorite: bool
) -> dict[str, Any] | None:
    record = await get_media(store, uid=uid, media_id=media_id)
    if record is None:
        return None
    await store.set(keys.media_doc(uid, media_id), {"favorite": favorite}, merge=True)
    return {**record, "favorite": favorite}


async def share_media(
    store: DocStore, storage: MediaStorage, *, uid: str, media_id: str
) -> dict[str, Any] | None:
    """Copy into the public `shared/` prefix and index it globally.

    A copy, not a move: sharing must not make the user's own object disappear
    from their library, and it must not give the public gallery a handle on an
    object inside `users/{uid}/` that a later delete would dangle.
    """
    record = await get_media(store, uid=uid, media_id=media_id)
    if record is None:
        return None

    source_key = assert_owned(record["key"], uid)
    extension = source_key.rsplit(".", 1)[-1]
    shared_key = f"shared/{media_id}.{extension}"
    await storage.copy(source_key, shared_key)

    shared_record = {
        "mediaId": media_id,
        "key": shared_key,
        "prompt": record.get("prompt", ""),
        "style": record.get("style", ""),
        "aspect": record.get("aspect", ""),
        "contentType": record.get("contentType"),
        "sharedBy": uid,
        "createdAt": int(time.time() * 1000),
    }
    await store.set(f"{keys.SHARED_MEDIA}/{media_id}", shared_record)
    await store.set(
        keys.media_doc(uid, media_id), {"shared": True, "sharedKey": shared_key}, merge=True
    )
    return shared_record


async def delete_media(
    store: DocStore, storage: MediaStorage, *, uid: str, media_id: str
) -> bool:
    record = await get_media(store, uid=uid, media_id=media_id)
    if record is None:
        return False

    await storage.delete(assert_owned(record["key"], uid))
    if record.get("shared") and record.get("sharedKey"):
        await storage.delete(record["sharedKey"])
        await store.delete(f"{keys.SHARED_MEDIA}/{media_id}")
    await store.delete(keys.media_doc(uid, media_id))
    return True


async def list_shared(
    store: DocStore, storage: MediaStorage, *, limit: int = 24
) -> list[dict[str, Any]]:
    rows = await store.list(keys.SHARED_MEDIA, order_by="createdAt", desc=True, limit=limit)
    return [await with_url(storage, r) for r in rows]
