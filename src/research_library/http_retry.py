"""Shared HTTP retry helpers: Retry-After parsing and exponential backoff + jitter."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

TRANSIENT_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})


def parse_retry_after(headers) -> Optional[float]:
    """Return wait seconds from a ``Retry-After`` header, or ``None`` if absent/invalid."""
    if headers is None:
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (dt - datetime.now(dt.tzinfo)).total_seconds())


def backoff_seconds(
    attempt: int,
    *,
    base: float,
    max_delay: float,
    err: Optional[BaseException] = None,
    jitter: bool = True,
) -> float:
    """Sleep length for ``attempt`` (0-based) after ``err``.

    Honors ``Retry-After`` on ``urllib.error.HTTPError`` when present (capped).
    Otherwise exponential backoff ``base * 2**attempt``, equal jitter, cap ``max_delay``.
    """
    headers = getattr(err, "headers", None) if err is not None else None
    retry_after = parse_retry_after(headers)
    if retry_after is not None:
        wait = min(float(max_delay), max(0.0, retry_after))
        if jitter:
            wait = min(float(max_delay), wait + random.uniform(0.0, min(1.0, max(0.0, base))))
        return wait
    cap = min(float(max_delay), float(base) * (2 ** max(0, attempt)))
    cap = max(0.0, cap)
    if not jitter or cap <= 0.0:
        return cap
    return cap * 0.5 + random.uniform(0.0, cap * 0.5)
