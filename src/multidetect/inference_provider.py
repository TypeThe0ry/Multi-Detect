"""Deterministic ONNX Runtime provider selection with bounded fallback.

The live perception graph uses the same provider order on every platform:
TensorRT, CUDA, then CPU.  ONNX Runtime normally performs operator-level
fallback when a provider is present, but session construction can still fail
when a provider library, tactic cache, or device is unavailable.  This module
adds a small construction-time fallback without hiding an explicitly supplied
provider list.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ORT_PROVIDER_ORDER: tuple[str, ...] = (
    "TensorrtExecutionProvider",
    "CUDAExecutionProvider",
    "CPUExecutionProvider",
)


class InferenceProviderError(RuntimeError):
    """Raised when no validated ONNX Runtime provider can create a session."""


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    """One ordered provider-list construction attempt."""

    providers: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class OrtSessionSelection:
    """Auditable result of provider discovery and session construction."""

    requested: tuple[str, ...]
    available: tuple[str, ...]
    selected: tuple[str, ...]
    attempts: tuple[ProviderAttempt, ...]
    fallback_allowed: bool

    @property
    def fallback_used(self) -> bool:
        """Whether construction needed to drop one or more providers."""

        return len(self.attempts) > 1


def _normalise_provider_names(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise InferenceProviderError("ONNX Runtime provider names must be non-empty strings")
        name = value.strip()
        if name not in seen:
            seen.add(name)
            result.append(name)
    return tuple(result)


def _available_provider_names(ort: Any) -> tuple[str, ...]:
    getter = getattr(ort, "get_available_providers", None)
    if not callable(getter):
        raise InferenceProviderError("ONNX Runtime does not expose get_available_providers()")
    raw = getter()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise InferenceProviderError("ONNX Runtime returned an invalid provider list")
    return _normalise_provider_names(tuple(str(value) for value in raw))


def _provider_descriptors(
    providers: Sequence[str],
    *,
    trt_engine_cache_path: Path | None,
) -> list[str | tuple[str, dict[str, str | bool]]]:
    descriptors: list[str | tuple[str, dict[str, str | bool]]] = []
    for provider in providers:
        if provider == "TensorrtExecutionProvider" and trt_engine_cache_path is not None:
            trt_engine_cache_path.mkdir(parents=True, exist_ok=True)
            descriptors.append(
                (
                    provider,
                    {
                        "trt_engine_cache_enable": True,
                        "trt_engine_cache_path": str(trt_engine_cache_path),
                    },
                )
            )
        else:
            descriptors.append(provider)
    return descriptors


def create_onnxruntime_session(
    ort: Any,
    model_path: str | Path,
    *,
    providers: Sequence[str] | None = None,
    trt_engine_cache_path: Path | None = None,
) -> tuple[Any, OrtSessionSelection]:
    """Create an ONNX Runtime session using a deterministic provider policy.

    An empty/omitted provider list means the validated default order and
    permits suffix fallback after session-construction failure.  A non-empty
    provider list is an explicit operator choice: unavailable names are
    filtered as before, but construction failure is surfaced without silently
    changing the requested execution policy.
    """

    explicit = bool(providers)
    requested = _normalise_provider_names(tuple(providers or DEFAULT_ORT_PROVIDER_ORDER))
    available = _available_provider_names(ort)
    candidates = tuple(provider for provider in requested if provider in available)
    if not candidates:
        raise InferenceProviderError(
            "No requested ONNX Runtime provider is available; "
            f"requested={list(requested)}, available={list(available)}"
        )

    attempt_lists = (
        (candidates,) if explicit else tuple(candidates[index:] for index in range(len(candidates)))
    )
    attempts: list[ProviderAttempt] = []
    failures: list[str] = []
    for candidate in attempt_lists:
        try:
            descriptors = _provider_descriptors(
                candidate,
                trt_engine_cache_path=trt_engine_cache_path,
            )
            session = ort.InferenceSession(str(model_path), providers=descriptors)
        except Exception as exc:  # provider libraries fail with several exception types
            detail = f"{type(exc).__name__}: {exc}"
            attempts.append(ProviderAttempt(candidate, detail))
            failures.append(f"{list(candidate)} -> {detail}")
            if explicit:
                break
            continue

        getter = getattr(session, "get_providers", None)
        selected = (
            _normalise_provider_names(tuple(str(value) for value in getter()))
            if callable(getter)
            else candidate
        )
        attempts.append(ProviderAttempt(candidate))
        return session, OrtSessionSelection(
            requested=requested,
            available=available,
            selected=selected or candidate,
            attempts=tuple(attempts),
            fallback_allowed=not explicit,
        )

    attempted = "; ".join(failures) or "no session construction attempt"
    raise InferenceProviderError(
        "ONNX Runtime session construction failed for all validated providers: " + attempted
    )


def provider_selection_document(selection: OrtSessionSelection | None) -> dict[str, Any] | None:
    """Serialize provider selection without exposing exception objects or paths."""

    if selection is None:
        return None
    return {
        "requested": list(selection.requested),
        "available": list(selection.available),
        "selected": list(selection.selected),
        "fallback_allowed": selection.fallback_allowed,
        "fallback_used": selection.fallback_used,
        "attempts": [
            {
                "providers": list(attempt.providers),
                "error": attempt.error,
            }
            for attempt in selection.attempts
        ],
    }


__all__ = [
    "DEFAULT_ORT_PROVIDER_ORDER",
    "InferenceProviderError",
    "OrtSessionSelection",
    "ProviderAttempt",
    "create_onnxruntime_session",
    "provider_selection_document",
]
