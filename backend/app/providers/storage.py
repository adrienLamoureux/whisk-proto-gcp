"""Media storage: GCS + V4 signed URLs in `gcp` mode, local disk in `local` mode.

Ports the AWS repo's S3 posture verbatim: user media is **never** a public
object. Every read goes through a short-lived signed URL, and every key is
scoped to `users/{uid}/` with cross-prefix access rejected.

## The Cloud Run signing gotcha

`generate_signed_url` normally needs a service-account *private key* to sign
with. Cloud Run's runtime service account has no downloadable key -- that is the
point of it -- so the naive call raises. The fix is to sign through the IAM
Credentials API (`signBlob`) using the runtime credentials, which needs:

    roles/iam.serviceAccountTokenCreator   on the service account, granted to itself

That is the single least-obvious IAM grant in the whole deployment, and omitting
it produces a runtime error that only shows up on the first image read. It is
handled in `_signing_kwargs` below and granted in deploy/setup.sh.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Protocol


class MediaStorage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def signed_url(self, key: str, *, ttl_seconds: int = 900) -> str: ...

    async def delete(self, key: str) -> None: ...

    async def copy(self, src_key: str, dest_key: str) -> None: ...


class OwnershipError(PermissionError):
    """Raised when a key escapes the caller's `users/{uid}/` prefix."""


def user_prefix(uid: str) -> str:
    return f"users/{uid}/"


def assert_owned(key: str, uid: str) -> str:
    """Reject any key outside the caller's own prefix.

    Checks the normalised path, so `users/u1/../u2/x.png` cannot slip through.
    Shared/public keys live under `shared/` and are handled by their own routes.
    """
    normalised = os.path.normpath(key).replace("\\", "/").lstrip("/")
    if not normalised.startswith(user_prefix(uid)):
        raise OwnershipError(f"key {key!r} is not owned by the caller")
    return normalised


def media_key(uid: str, media_id: str, ext: str = "svg") -> str:
    return f"{user_prefix(uid)}{media_id}.{ext}"


# ---------------------------------------------------------------------------
# local mode
# ---------------------------------------------------------------------------
class LocalMediaStorage:
    """Writes under `.localdata/media/` and serves via the `/media/{key}` route.

    The returned URL carries an `exp` + `sig` pair so the *expiry semantics* of
    signed URLs are exercised locally too -- a stub that returned a permanent
    path would hide every bug that only bites once URLs start expiring.
    """

    def __init__(self, root: str, secret: str = "local-dev-signing-key") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._secret = secret.encode()

    def _path(self, key: str) -> Path:
        target = (self.root / key).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            raise OwnershipError(f"key {key!r} escapes the media root")
        return target

    def sign(self, key: str, expires_at: int) -> str:
        payload = f"{key}:{expires_at}".encode()
        return hashlib.blake2s(payload, key=self._secret, digest_size=16).hexdigest()

    def verify(self, key: str, expires_at: int, signature: str) -> bool:
        if expires_at < int(time.time()):
            return False
        # constant-time compare: same discipline as the real thing
        return hmac.compare_digest(self.sign(key, expires_at), signature)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    async def signed_url(self, key: str, *, ttl_seconds: int = 900) -> str:
        expires_at = int(time.time()) + ttl_seconds
        return f"/media/{key}?exp={expires_at}&sig={self.sign(key, expires_at)}"

    async def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    async def copy(self, src_key: str, dest_key: str) -> None:
        dest = self._path(dest_key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self._path(src_key).read_bytes())


# ---------------------------------------------------------------------------
# gcp mode
# ---------------------------------------------------------------------------
class GcsMediaStorage:
    def __init__(self, bucket_name: str, project_id: str) -> None:
        from google.cloud import storage

        self._client = storage.Client(project=project_id)
        self._bucket = self._client.bucket(bucket_name)
        self._signing_email: str | None = None
        self._credentials = None

    def _signing_kwargs(self) -> dict:
        """Sign via IAM Credentials when no private key is available.

        On Cloud Run the ADC principal is the runtime service account, which has
        no key material. Passing `service_account_email` + `access_token` makes
        the client delegate signing to `iam.signBlob` instead.
        """
        import google.auth
        from google.auth.transport.requests import Request

        if self._credentials is None:
            self._credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )

        credentials = self._credentials
        if getattr(credentials, "private_key_id", None):
            return {}  # a real key is present (local dev with a key file)

        if not credentials.valid:
            credentials.refresh(Request())

        email = self._signing_email or getattr(credentials, "service_account_email", None)
        if not email:
            raise RuntimeError(
                "cannot sign GCS URLs: no private key and no service_account_email. "
                "Grant roles/iam.serviceAccountTokenCreator to the runtime SA on itself."
            )
        self._signing_email = email
        return {"service_account_email": email, "access_token": credentials.token}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self._bucket.blob(key).upload_from_string(data, content_type=content_type)

    async def signed_url(self, key: str, *, ttl_seconds: int = 900) -> str:
        return self._bucket.blob(key).generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=ttl_seconds),
            method="GET",
            **self._signing_kwargs(),
        )

    async def delete(self, key: str) -> None:
        blob = self._bucket.blob(key)
        if blob.exists():
            blob.delete()

    async def copy(self, src_key: str, dest_key: str) -> None:
        self._bucket.copy_blob(self._bucket.blob(src_key), self._bucket, dest_key)
