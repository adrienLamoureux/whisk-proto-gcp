"""A Firestore-backed ADK `SessionService`.

## Why this file exists

ADK ships three session services, and on a scale-to-zero deployment each has a
problem:

  * `InMemorySessionService` -- state lives in the process. Cloud Run scales to
    zero and recycles instances freely, so a user's conversation vanishes between
    turns. Fine for tests, wrong for production.
  * `DatabaseSessionService` -- durable, but it wants a SQL database. On GCP that
    means Cloud SQL, which bills for a running instance around the clock. That is
    a hard violation of the zero-idle constraint: a chat-history table would
    become the single largest line on the bill, larger than the model spend.
  * `VertexAiSessionService` -- managed and durable, but couples session storage
    to Agent Engine. Worth adopting *with* Agent Engine; premature without it.

Firestore is already in the stack for media, prefs and counters. It is durable,
pay-per-operation, and costs nothing idle -- so the missing piece was an adapter,
not another database. That is what this is.

## Persistence shape

    users/{uid}/agentSessions/{sid}            -- metadata + session state
    users/{uid}/agentSessions/{sid}/messages/  -- one doc per ADK Event

Events are stored individually rather than as one growing array, because
Firestore documents cap at 1 MiB and a long conversation would eventually hit it.
It also makes `append_event` a single small write instead of a rewrite of the
entire history -- the same reason the AWS original used `SESSION#{id}#MSG#{ts}`
sort keys rather than a list attribute.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from google.adk.events import Event
from google.adk.sessions import BaseSessionService, Session
from google.adk.sessions.base_session_service import GetSessionConfig, ListSessionsResponse

from ..store import keys
from ..store.docstore import DocStore

log = logging.getLogger("whisk.sessions")

# Turns kept in the working set. Beyond this the conversation is truncated at
# read time -- the cheap form of the AWS original's rolling-summary compaction.
# See the note in `whisk_runner.py` on why summarisation was not ported.
MAX_EVENTS = 60


class FirestoreSessionService(BaseSessionService):
    """Durable ADK sessions on Firestore. Zero idle cost, survives cold starts."""

    def __init__(self, store: DocStore) -> None:
        self._store = store

    # --- lifecycle ---------------------------------------------------------
    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Session:
        sid = keys.safe_id(session_id)
        now = time.time()
        await self._store.set(
            keys.session_doc(user_id, sid),
            {
                "sessionId": sid,
                "appName": app_name,
                "userId": user_id,
                "state": state or {},
                "createdAt": int(now * 1000),
                "lastUsedAt": int(now * 1000),
            },
            merge=True,
        )
        return Session(
            id=sid, app_name=app_name, user_id=user_id, state=state or {}, events=[],
            last_update_time=now,
        )

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: GetSessionConfig | None = None,
    ) -> Session | None:
        sid = keys.safe_id(session_id)
        doc = await self._store.get(keys.session_doc(user_id, sid))
        if doc is None:
            return None

        rows = await self._store.list(
            keys.messages_collection(user_id, sid), order_by="seq", desc=False
        )
        if config and config.num_recent_events:
            rows = rows[-config.num_recent_events :]
        else:
            rows = rows[-MAX_EVENTS:]

        events: list[Event] = []
        for row in rows:
            payload = row.get("event")
            if not payload:
                continue
            try:
                events.append(Event.model_validate(payload))
            except Exception:  # noqa: BLE001
                # A single unparseable event (schema drift across an ADK upgrade)
                # must not make the whole conversation unreadable.
                log.warning("dropping unparseable event %s", row.get("id"))

        return Session(
            id=sid,
            app_name=app_name,
            user_id=user_id,
            state=doc.get("state") or {},
            events=events,
            last_update_time=doc.get("lastUsedAt", 0) / 1000,
        )

    async def list_sessions(
        self, *, app_name: str, user_id: str | None = None
    ) -> ListSessionsResponse:
        if not user_id:
            return ListSessionsResponse(sessions=[])
        rows = await self._store.list(
            keys.sessions_collection(user_id), order_by="lastUsedAt", desc=True
        )
        return ListSessionsResponse(
            sessions=[
                # Listing is metadata only: hydrating every conversation's events
                # to render a sidebar would be a read amplification bug.
                Session(
                    id=row.get("sessionId", row.get("id", "")),
                    app_name=app_name,
                    user_id=user_id,
                    state=row.get("state") or {},
                    events=[],
                    last_update_time=row.get("lastUsedAt", 0) / 1000,
                )
                for row in rows
            ]
        )

    async def delete_session(self, *, app_name: str, user_id: str, session_id: str) -> None:
        sid = keys.safe_id(session_id)
        await self._store.delete_collection(keys.messages_collection(user_id, sid))
        await self._store.delete(keys.session_doc(user_id, sid))

    # --- writes ------------------------------------------------------------
    async def append_event(self, session: Session, event: Event) -> Event:
        # Keep the in-memory Session consistent and let the base class apply the
        # event's state_delta, then persist both halves.
        await super().append_event(session=session, event=event)

        uid = session.user_id
        sid = keys.safe_id(session.id)
        seq = int(time.time() * 1000)

        await self._store.set(
            f"{keys.messages_collection(uid, sid)}/{seq}-{event.id}",
            {
                "seq": seq,
                "author": event.author,
                "event": event.model_dump(mode="json", exclude_none=True),
            },
        )
        await self._store.set(
            keys.session_doc(uid, sid),
            {"lastUsedAt": seq, "state": session.state},
            merge=True,
        )
        return event
