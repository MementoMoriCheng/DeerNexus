"""Shared pagination helpers for gateway routers."""

from __future__ import annotations

import base64
from datetime import datetime


def trim_run_message_page(rows: list[dict], *, limit: int, after_seq: int | None) -> tuple[list[dict], bool]:
    """Trim a ``limit + 1`` run-message page while preserving page boundaries."""
    has_more = len(rows) > limit
    if not has_more:
        return rows, False

    if after_seq is not None:
        return rows[:limit], True

    return rows[-limit:], True


def encode_cursor(created_at: datetime, run_id: str) -> str:
    """Encode a keyset cursor ``(created_at, run_id)`` as an opaque URL-safe token.

    The cursor is base64-encoded ``<iso>|<run_id>``. Consumers should treat it
    as opaque — the format is an implementation detail of keyset pagination
    on ``(created_at DESC, run_id DESC)``.
    """
    payload = f"{created_at.isoformat()}|{run_id}".encode()
    return base64.urlsafe_b64encode(payload).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode a cursor produced by :func:`encode_cursor`.

    ``rsplit("|", 1)`` is used so an ISO timestamp containing ``|`` (legal
    in the alternate ISO 8601 forms) does not split the run_id off early;
    the canonical ``datetime.isoformat()`` uses ``T`` / ``+`` / ``:`` so the
    separator is unambiguous in practice.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, run_id = raw.rsplit("|", 1)
        return datetime.fromisoformat(ts_str), run_id
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"Malformed cursor token: {cursor!r}") from exc
