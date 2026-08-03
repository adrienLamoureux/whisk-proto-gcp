"""Cross-session user preferences -- and why they are validated on *read*.

The AWS original stores `{lastStyle, lastAspect, lastLora, theme}` per user and
injects them into the system prompt as `<prefs>...</prefs>` so tool defaults drift
toward what the user keeps choosing.

That injection is what makes the validation interesting. These values flow back
into the prompt on every turn, so stored state is **untrusted input to the model**.
Validating only on write leaves two holes:

  1. any other write path (a migration, a fixture, a future admin tool) bypasses
     the check and poisons the prompt;
  2. records written before the validator existed are still in the database.

So `validate_pref` runs on read *and* write, and anything failing is dropped
silently rather than raised -- a bad stored pref should degrade to the default,
not break the user's session. This is the self-injection loop most apps miss.
"""

from __future__ import annotations

import logging
import re
import time

from ..store import keys
from ..store.docstore import DocStore

log = logging.getLogger("whisk.state")

STYLES = frozenset({"anime", "manga", "photoreal", "chibi"})
ASPECTS = frozenset({"1:1", "3:4", "16:9", "9:16"})
THEMES = frozenset(
    {"sakura", "moonrise", "bamboo", "ember", "void", "glacier", "dusk", "aurora", "crimson", "storm"}
)
_LORA_RE = re.compile(r"^[a-zA-Z0-9_\-:./]{1,120}$")

_ENUMS: dict[str, frozenset[str]] = {
    "lastStyle": STYLES,
    "lastAspect": ASPECTS,
    "theme": THEMES,
}


def validate_pref(key: str, value: object) -> str | None:
    """Return the value if it is a legal pref, else None. Never raises."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if key in _ENUMS:
        return candidate if candidate in _ENUMS[key] else None
    if key == "lastLora":
        return candidate if _LORA_RE.match(candidate) else None
    return None  # unknown keys are dropped, not stored


async def patch_prefs(store: DocStore, uid: str, updates: dict[str, object]) -> dict[str, str]:
    """Best-effort pref write. Failures are logged, never propagated.

    Called after a successful tool dispatch; a pref-write error must not turn a
    generated image into a failed turn.
    """
    clean = {k: v for k, v in ((k, validate_pref(k, v)) for k, v in updates.items()) if v}
    if not clean:
        return {}
    try:
        await store.set(
            keys.prefs_doc(uid), {**clean, "updatedAt": int(time.time() * 1000)}, merge=True
        )
    except Exception:  # noqa: BLE001
        log.exception("pref write failed for uid=%s", uid)
        return {}
    return clean


async def read_prefs(store: DocStore, uid: str) -> dict[str, str]:
    """Read prefs, re-validating every field. See the module docstring."""
    try:
        doc = await store.get(keys.prefs_doc(uid)) or {}
    except Exception:  # noqa: BLE001
        log.exception("pref read failed for uid=%s", uid)
        return {}
    return {k: v for k, v in ((k, validate_pref(k, raw)) for k, raw in doc.items()) if v}


def render_prefs(prefs: dict[str, str]) -> str:
    """Render prefs for the system prompt. Empty string when there is nothing to say."""
    if not prefs:
        return ""
    body = ", ".join(f"{k}={v}" for k, v in sorted(prefs.items()))
    return f"<prefs>{body}</prefs>"
