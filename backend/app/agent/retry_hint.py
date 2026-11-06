"""Human-readable retry hint for rate-limit responses."""
from __future__ import annotations


def _unit(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def format_retry_after(retry_after_ms: int) -> str:
    if retry_after_ms <= 0:
        return "Try again now."
    secs = -(-retry_after_ms // 1000)
    if secs < 60:
        return f"Try again in {_unit(secs, 'second')}."
    if secs < 3600:
        m, s = divmod(secs, 60)
        parts = [_unit(m, "minute")]
        if s:
            parts.append(_unit(s, "second"))
        return f"Try again in {' '.join(parts)}."
    h, rem = divmod(secs, 3600)
    m = rem // 60
    parts = [_unit(h, "hour")]
    if m:
        parts.append(_unit(m, "minute"))
    return f"Try again in {' '.join(parts)}."
