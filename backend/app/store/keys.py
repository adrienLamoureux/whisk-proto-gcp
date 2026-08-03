"""Document paths - the Firestore answer to the AWS repo's DynamoDB SK namespaces.

The AWS original is a single DynamoDB table keyed `pk = USER#{sub}` with a sort
key carrying a namespace prefix (`IMG#`, `AGENT#SESSION#{id}`, `AGENT#COST`, ...).
That prefix trick exists because DynamoDB gives you exactly one sort dimension,
so hierarchy has to be encoded *into the string*.

Firestore models hierarchy natively (collection -> document -> subcollection), so
the same access patterns become real paths. Two consequences worth knowing:

  * No `begins_with(sk, 'AGENT#SESSION#')` scans. A subcollection *is* the
    namespace, so listing sessions is a collection read, not a prefixed query.
  * `sanitiseSessionId` still matters. In DynamoDB an unescaped id could inject
    a `#MSG#` fragment into the sort key; in Firestore it could inject a `/` and
    escape into a sibling collection. Same class of bug, different separator --
    so the sanitiser is ported, not dropped. See `safe_id`.
"""

from __future__ import annotations

import re

_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")
DEFAULT_SESSION = "default"
MAX_ID_LEN = 80


def safe_id(raw: str | None, *, fallback: str = DEFAULT_SESSION) -> str:
    """Normalise a client-supplied id to `[a-z0-9_-]{1,80}`.

    Ported from the AWS repo's `sanitiseSessionId`. Anything that would let a
    caller traverse the path hierarchy (`/`, `.`, `..`) is stripped rather than
    escaped, and an empty result falls back instead of raising -- an unusable id
    should degrade to the shared default session, not 500 the turn.
    """
    if not raw:
        return fallback
    cleaned = _ID_RE.sub("", str(raw)).lower()[:MAX_ID_LEN]
    return cleaned or fallback


# --- user-scoped ----------------------------------------------------------
def user_doc(uid: str) -> str:
    return f"users/{safe_id(uid, fallback='anonymous')}"


def media_collection(uid: str) -> str:
    """IMG# / VID# -> users/{uid}/media"""
    return f"{user_doc(uid)}/media"


def media_doc(uid: str, media_id: str) -> str:
    return f"{media_collection(uid)}/{safe_id(media_id, fallback='unknown')}"


def jobs_collection(uid: str) -> str:
    """JOB# -> users/{uid}/jobs

    In-flight Replicate predictions. Stored under the *owner* so the status
    endpoint can authorise a poll: without this, a prediction id -- which is not
    secret and appears in logs -- would be enough to read someone else's result.
    """
    return f"{user_doc(uid)}/jobs"


def job_doc(uid: str, prediction_id: str) -> str:
    return f"{jobs_collection(uid)}/{safe_id(prediction_id, fallback='unknown')}"


def sessions_collection(uid: str) -> str:
    """AGENT#SESSION#{id} -> users/{uid}/agentSessions"""
    return f"{user_doc(uid)}/agentSessions"


def session_doc(uid: str, session_id: str) -> str:
    return f"{sessions_collection(uid)}/{safe_id(session_id)}"


def messages_collection(uid: str, session_id: str) -> str:
    """AGENT#{sid}#MSG#{ts} -> users/{uid}/agentSessions/{sid}/messages"""
    return f"{session_doc(uid, session_id)}/messages"


def prefs_doc(uid: str) -> str:
    """AGENT#STATE -> one doc per user, not per session."""
    return f"{user_doc(uid)}/state/prefs"


def limits_doc(uid: str, bucket: str) -> str:
    """AGENT#RATE / AGENT#COST / AGENT#IMG_COUNT -> users/{uid}/limits/{bucket}"""
    return f"{user_doc(uid)}/limits/{safe_id(bucket, fallback='rate')}"


RATE = "rate"
COST = "cost"
IMAGES = "images"

# --- global ----------------------------------------------------------------
SHARED_MEDIA = "sharedMedia"  # public gallery, mirrors S3 `shared/images/`
RAG_CHUNKS = "ragChunks"  # embedded doc chunks + vector index
AGENT_CONFIG = "config/agent"  # CONFIG#AGENT -- admin model override
COST_INDEX = "costIndex"  # per-user cost rollup for the admin dashboard
