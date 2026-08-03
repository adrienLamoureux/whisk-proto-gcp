"""Authentication: Identity Platform (Firebase Auth) in `gcp` mode, dev header locally.

Identity Platform is the Cognito equivalent -- same job, same JWT-on-every-request
shape, and it is free below 50k MAU with no idle cost, so it fits the constraint.

The AWS original has API Gateway verify the JWT *before* Lambda ever runs. Cloud
Run has no equivalent free-standing authorizer in front of it (API Gateway on GCP
exists but adds a hop and a bill), so verification moves into the app. The
practical consequence is that this code must be correct about the things a
managed authorizer would have handled: signature, issuer, audience, expiry.

`_verify_token` checks all four against Google's public certs. It does *not*
hand-roll JWT parsing -- `google.oauth2.id_token` handles the crypto, with a
cached cert fetch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request

from .config import Settings, get_settings

log = logging.getLogger("whisk.auth")

DEV_USER = "dev-user"
ADMIN_CLAIM = "admin"


def request_settings(request: Request) -> Settings:
    """Resolve settings from the app, not the process-wide cache.

    `get_settings()` is `lru_cache`d off the environment, so depending on it
    directly makes `build_app(settings)` non-authoritative for auth: an app
    constructed with `auth_required=True` would still consult whatever the
    ambient env said. That is a config-divergence bug with an authentication
    blast radius, and it is exactly the kind of thing that only shows up in a
    test that builds an app with non-default settings.

    `app.state.settings` is assigned in the lifespan handler; the fallback keeps
    this usable in the narrow window before startup completes.
    """
    return getattr(request.app.state, "settings", None) or get_settings()


@dataclass(slots=True)
class User:
    uid: str
    email: str = ""
    is_admin: bool = False


def _dev_user(uid: str | None, admin_header: str | None) -> User:
    """Local mode identity.

    A header, not a token. This exists so the app runs without an Identity
    Platform project -- and it is gated on `settings.is_local`, so it cannot be
    reached in a deployed configuration even if the header is sent.
    """
    return User(
        uid=uid or DEV_USER,
        email=f"{uid or DEV_USER}@local",
        is_admin=(admin_header or "").lower() in ("1", "true", "yes"),
    )


def _verify_token(token: str, settings: Settings) -> User:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    audience = settings.identity_platform_project or settings.project_id
    try:
        claims = google_id_token.verify_firebase_token(
            token, google_requests.Request(), audience=audience
        )
    except Exception as exc:  # noqa: BLE001
        log.info("token verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="invalid_token") from exc

    if not claims:
        raise HTTPException(status_code=401, detail="invalid_token")

    uid = claims.get("user_id") or claims.get("sub") or claims.get("uid")
    if not uid:
        raise HTTPException(status_code=401, detail="invalid_token")

    # Admin is a custom claim set out-of-band (Admin SDK / a provisioning
    # script), never something a client can assert. Same posture as reading
    # `cognito:groups` rather than trusting a request field.
    return User(
        uid=str(uid),
        email=str(claims.get("email", "")),
        is_admin=bool(claims.get(ADMIN_CLAIM, False)),
    )


async def current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    x_whisk_user: str | None = Header(default=None),
    x_whisk_admin: str | None = Header(default=None),
    settings: Settings = Depends(request_settings),
) -> User:
    """Resolve the caller. Raises 401 when auth is required and absent."""
    if settings.is_local and not settings.auth_required:
        return _dev_user(x_whisk_user, x_whisk_admin)

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")

    return _verify_token(authorization.split(" ", 1)[1].strip(), settings)


async def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin_required")
    return user


async def optional_user(
    request: Request,
    authorization: str | None = Header(default=None),
    x_whisk_user: str | None = Header(default=None),
    settings: Settings = Depends(request_settings),
) -> User | None:
    """For public routes that personalise when a caller happens to be signed in."""
    if settings.is_local and not settings.auth_required:
        return _dev_user(x_whisk_user, None)
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        return _verify_token(authorization.split(" ", 1)[1].strip(), settings)
    except HTTPException:
        return None
