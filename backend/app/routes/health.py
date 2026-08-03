"""Health, runtime config, and the local media server.

`GET /config.json` is the direct analogue of the AWS repo's CloudFront-served
`config.json`: the frontend ships as a static bundle with no baked-in backend
URL and resolves its wiring at startup. Same bundle promotes across
environments; only this response differs.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..auth import request_settings
from ..config import Settings
from ..deps import Deps
from ..providers.storage import LocalMediaStorage
from .common import get_deps

router = APIRouter()

_STARTED_AT = time.time()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "uptimeSeconds": round(time.time() - _STARTED_AT, 1)}


@router.get("/config.json")
async def runtime_config(settings: Settings = Depends(request_settings)) -> dict:
    """Public runtime wiring. Deliberately contains no secrets -- everything here
    is safe in a browser, which is why the file can be served unauthenticated."""
    return {
        "mode": settings.mode,
        "apiBaseUrl": "",
        "agentModel": settings.agent_model if not settings.is_local else "whisk-stub-router",
        "imageModel": "cjwbw/animagine-xl-3.1" if not settings.is_local else "local-deterministic",
        "surfaces": {
            "a2a": settings.enable_a2a,
            "mcp": settings.enable_mcp,
        },
        "auth": {
            "required": settings.auth_required,
            "provider": "identity-platform" if not settings.is_local else "dev-header",
            "projectId": settings.identity_platform_project or settings.project_id,
            # The Identity Platform Web API key. Public by design: it identifies
            # the project to identitytoolkit.googleapis.com and authorises
            # nothing on its own -- every account operation still requires
            # credentials or a valid token. Shipping it to the browser is the
            # intended usage, exactly as the Firebase SDK config does.
            "apiKey": settings.identity_platform_api_key,
        },
        "limits": {
            "dailyTokenCap": settings.daily_token_cap,
            "dailyImageCap": settings.daily_image_cap,
        },
    }


@router.get("/media/{key:path}")
async def local_media(
    key: str,
    exp: int = Query(...),
    sig: str = Query(...),
    deps: Deps = Depends(get_deps),
) -> Response:
    """Serve locally-stored media behind a signature check.

    Only mounted in local mode -- in GCP the browser fetches the object straight
    from GCS with a V4 signed URL and this route never runs. It enforces the same
    expiry semantics so local development cannot depend on URLs that never die.
    """
    storage = deps.storage
    if not isinstance(storage, LocalMediaStorage):
        raise HTTPException(status_code=404, detail="not_found")

    if not storage.verify(key, exp, sig):
        raise HTTPException(status_code=403, detail="url_expired_or_invalid")

    try:
        data = await storage.read(key)
    except (FileNotFoundError, PermissionError) as exc:
        raise HTTPException(status_code=404, detail="not_found") from exc

    media_type = "image/svg+xml" if key.endswith(".svg") else "application/octet-stream"
    return Response(content=data, media_type=media_type, headers={"Cache-Control": "private, max-age=300"})
