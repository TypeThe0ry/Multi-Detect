from __future__ import annotations

from uuid import UUID


def normalize_evidence_session_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("evidence session ID must be a non-empty UUID")
    try:
        parsed = UUID(value.strip())
    except ValueError as exc:
        raise ValueError("evidence session ID must be a valid UUID") from exc
    return str(parsed)


__all__ = ["normalize_evidence_session_id"]
