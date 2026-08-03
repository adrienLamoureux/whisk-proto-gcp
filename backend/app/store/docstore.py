"""Document store abstraction: Firestore in `gcp` mode, in-memory in `local` mode.

Both implementations satisfy `DocStore`. Routes and agent tools never import
either one directly -- `app/deps.py` picks it once at startup.

## The one non-obvious operation: `increment_and_read`

The AWS original leans on DynamoDB's `UpdateExpression: ADD turnCount :n` with
`ReturnValues: UPDATED_NEW`. That single call is atomic *and* hands back the new
value, which is exactly what a "did this user blow the daily cap?" check needs.

Firestore has no equivalent single call:

  * `firestore.Increment(n)` is atomic but write-only -- it does not return the
    resulting value, so it cannot answer the cap question.
  * A transaction reads-then-writes atomically and *does* return the value, at
    the cost of optimistic-concurrency retries under contention.

So `increment_and_read` is a transaction, and `increment` (fire-and-forget) uses
the cheaper sentinel. Guardrails that gate a request use the former; telemetry
that merely accumulates uses the latter. This is a real API-shape difference
between the two databases, not an implementation detail.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Protocol

Doc = dict[str, Any]


class DocStore(Protocol):
    async def get(self, path: str) -> Doc | None: ...

    async def set(self, path: str, data: Doc, *, merge: bool = False) -> None: ...

    async def delete(self, path: str) -> None: ...

    async def list(
        self,
        collection: str,
        *,
        order_by: str | None = None,
        desc: bool = True,
        limit: int | None = None,
        where: tuple[str, str, Any] | None = None,
    ) -> list[Doc]: ...

    async def delete_collection(self, collection: str) -> int: ...

    async def increment(self, path: str, deltas: dict[str, float]) -> None: ...

    async def increment_and_read(
        self, path: str, deltas: dict[str, float], *, base: Doc | None = None
    ) -> Doc: ...

    async def vector_search(
        self, collection: str, field: str, query_vector: list[float], limit: int
    ) -> list[Doc]: ...

    async def close(self) -> None: ...


def _doc_id(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# local mode
# ---------------------------------------------------------------------------
class InMemoryDocStore:
    """Dict-backed store with the same semantics as the Firestore one.

    Paths are stored flat (`users/u1/media/m1`), and a "collection" read is a
    prefix match on the path with exactly one remaining segment -- which is how
    Firestore itself distinguishes a document from its subcollections.
    """

    def __init__(self) -> None:
        self._docs: dict[str, Doc] = {}
        self._lock = asyncio.Lock()

    async def get(self, path: str) -> Doc | None:
        async with self._lock:
            doc = self._docs.get(path)
            return dict(doc) if doc is not None else None

    async def set(self, path: str, data: Doc, *, merge: bool = False) -> None:
        async with self._lock:
            if merge and path in self._docs:
                self._docs[path] = {**self._docs[path], **data}
            else:
                self._docs[path] = dict(data)
            self._docs[path]["id"] = _doc_id(path)

    async def delete(self, path: str) -> None:
        async with self._lock:
            self._docs.pop(path, None)

    async def list(
        self,
        collection: str,
        *,
        order_by: str | None = None,
        desc: bool = True,
        limit: int | None = None,
        where: tuple[str, str, Any] | None = None,
    ) -> list[Doc]:
        prefix = collection.rstrip("/") + "/"
        async with self._lock:
            rows = [
                dict(doc)
                for path, doc in self._docs.items()
                if path.startswith(prefix) and "/" not in path[len(prefix) :]
            ]
        if where:
            field, op, value = where
            rows = [r for r in rows if _matches(r.get(field), op, value)]
        if order_by:
            rows.sort(key=lambda r: _sort_key(r.get(order_by)), reverse=desc)
        return rows[:limit] if limit else rows

    async def delete_collection(self, collection: str) -> int:
        prefix = collection.rstrip("/") + "/"
        async with self._lock:
            doomed = [p for p in self._docs if p.startswith(prefix)]
            for p in doomed:
                del self._docs[p]
            return len(doomed)

    async def increment(self, path: str, deltas: dict[str, float]) -> None:
        await self.increment_and_read(path, deltas)

    async def increment_and_read(
        self, path: str, deltas: dict[str, float], *, base: Doc | None = None
    ) -> Doc:
        async with self._lock:
            doc = dict(self._docs.get(path) or base or {})
            for field, delta in deltas.items():
                doc[field] = (doc.get(field) or 0) + delta
            doc["updatedAt"] = int(time.time() * 1000)
            doc["id"] = _doc_id(path)
            self._docs[path] = doc
            return dict(doc)

    async def vector_search(
        self, collection: str, field: str, query_vector: list[float], limit: int
    ) -> list[Doc]:
        rows = await self.list(collection)
        scored: list[tuple[float, Doc]] = []
        for row in rows:
            vec = row.get(field)
            if isinstance(vec, list):
                scored.append((cosine_similarity(query_vector, vec), row))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        out: list[Doc] = []
        for score, row in scored[:limit]:
            enriched = dict(row)
            # Firestore returns COSINE *distance* (1 - similarity); match that
            # so callers can't accidentally depend on local-mode semantics.
            enriched["vector_distance"] = 1.0 - score
            enriched.pop(field, None)
            out.append(enriched)
        return out

    async def close(self) -> None:
        return None


def _matches(actual: Any, op: str, expected: Any) -> bool:
    if op == "==":
        return actual == expected
    if op == "!=":
        return actual != expected
    if op == ">=":
        return actual is not None and actual >= expected
    if op == "<=":
        return actual is not None and actual <= expected
    if op == ">":
        return actual is not None and actual > expected
    if op == "<":
        return actual is not None and actual < expected
    raise ValueError(f"unsupported operator: {op}")


def _sort_key(value: Any) -> Any:
    if value is None:
        return (0, 0.0, "")
    if isinstance(value, (int, float)):
        return (1, float(value), "")
    return (2, 0.0, str(value))


# ---------------------------------------------------------------------------
# gcp mode
# ---------------------------------------------------------------------------
class FirestoreDocStore:
    """Real Firestore. Native mode, on-demand pricing, $0 when idle.

    Requires a composite vector index on the RAG collection before
    `vector_search` will work -- see deploy/setup.sh (`gcloud firestore indexes
    composite create --field-config=vector-config=...`).
    """

    def __init__(self, project_id: str, database: str = "(default)") -> None:
        from google.cloud import firestore  # imported lazily: local mode never needs it

        self._firestore = firestore
        self._client = firestore.AsyncClient(project=project_id, database=database)

    def _ref(self, path: str):
        return self._client.document(path)

    async def get(self, path: str) -> Doc | None:
        snap = await self._ref(path).get()
        if not snap.exists:
            return None
        return {**(snap.to_dict() or {}), "id": snap.id}

    async def set(self, path: str, data: Doc, *, merge: bool = False) -> None:
        await self._ref(path).set(data, merge=merge)

    async def delete(self, path: str) -> None:
        await self._ref(path).delete()

    async def list(
        self,
        collection: str,
        *,
        order_by: str | None = None,
        desc: bool = True,
        limit: int | None = None,
        where: tuple[str, str, Any] | None = None,
    ) -> list[Doc]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = self._client.collection(collection)
        if where:
            field, op, value = where
            query = query.where(filter=FieldFilter(field, op, value))
        if order_by:
            direction = self._firestore.Query.DESCENDING if desc else self._firestore.Query.ASCENDING
            query = query.order_by(order_by, direction=direction)
        if limit:
            query = query.limit(limit)
        return [{**(s.to_dict() or {}), "id": s.id} async for s in query.stream()]

    async def delete_collection(self, collection: str, batch_size: int = 200) -> int:
        """Firestore has no recursive delete in the client SDK.

        `gcloud firestore bulk-delete` exists server-side, but for the bounded
        collections here (a session's messages) a paged client loop is simpler
        and stays within the free-tier op budget.
        """
        deleted = 0
        while True:
            snaps = [s async for s in self._client.collection(collection).limit(batch_size).stream()]
            if not snaps:
                return deleted
            batch = self._client.batch()
            for snap in snaps:
                batch.delete(snap.reference)
            await batch.commit()
            deleted += len(snaps)
            if len(snaps) < batch_size:
                return deleted

    async def increment(self, path: str, deltas: dict[str, float]) -> None:
        payload: Doc = {f: self._firestore.Increment(d) for f, d in deltas.items()}
        payload["updatedAt"] = int(time.time() * 1000)
        await self._ref(path).set(payload, merge=True)

    async def increment_and_read(
        self, path: str, deltas: dict[str, float], *, base: Doc | None = None
    ) -> Doc:
        ref = self._ref(path)

        @self._firestore.async_transactional
        async def _txn(transaction) -> Doc:
            snap = await ref.get(transaction=transaction)
            current = (snap.to_dict() or {}) if snap.exists else dict(base or {})
            for field, delta in deltas.items():
                current[field] = (current.get(field) or 0) + delta
            current["updatedAt"] = int(time.time() * 1000)
            transaction.set(ref, current)
            return current

        result = await _txn(self._client.transaction())
        return {**result, "id": _doc_id(path)}

    async def vector_search(
        self, collection: str, field: str, query_vector: list[float], limit: int
    ) -> list[Doc]:
        from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
        from google.cloud.firestore_v1.vector import Vector

        query = self._client.collection(collection).find_nearest(
            vector_field=field,
            query_vector=Vector(query_vector),
            limit=limit,
            distance_measure=DistanceMeasure.COSINE,
            distance_result_field="vector_distance",
        )
        snaps = await query.get()
        out: list[Doc] = []
        for snap in snaps:
            row = {**(snap.to_dict() or {}), "id": snap.id}
            row.pop(field, None)
            out.append(row)
        return out

    async def close(self) -> None:
        self._client.close()
