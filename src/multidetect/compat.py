"""Standard-library compatibility helpers for the JetPack Python runtime."""

from __future__ import annotations

from datetime import timezone
from enum import Enum

try:  # Python 3.11+
    from datetime import UTC as UTC
except ImportError:  # pragma: no cover - exercised on JetPack's Python 3.10
    UTC = timezone.utc

try:  # Python 3.11+
    from enum import StrEnum as StrEnum
except ImportError:  # pragma: no cover - exercised on JetPack's Python 3.10

    class StrEnum(str, Enum):
        """Python 3.10 equivalent for enums with explicit string values."""

        def __str__(self) -> str:
            return str.__str__(self)
