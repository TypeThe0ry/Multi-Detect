from __future__ import annotations

from pathlib import Path

import pytest

from multidetect.inference_provider import (
    InferenceProviderError,
    create_onnxruntime_session,
)


def _names(providers: list[object]) -> tuple[str, ...]:
    return tuple(item[0] if isinstance(item, tuple) else item for item in providers)


class _Session:
    def __init__(self, providers: tuple[str, ...]) -> None:
        self._providers = providers

    def get_providers(self) -> tuple[str, ...]:
        return self._providers


class _FakeOrt:
    def __init__(self, *, failures: set[tuple[str, ...]] | None = None) -> None:
        self.failures = failures or set()
        self.calls: list[tuple[str, ...]] = []
        self.descriptors: list[list[object]] = []

    def get_available_providers(self) -> list[str]:
        # Deliberately return an unstable order; the helper must impose the
        # platform-independent TensorRT -> CUDA -> CPU order.
        return ["CPUExecutionProvider", "TensorrtExecutionProvider", "CUDAExecutionProvider"]

    def InferenceSession(self, path: str, *, providers: list[object]) -> _Session:
        del path
        self.descriptors.append(providers)
        names = _names(providers)
        self.calls.append(names)
        if names in self.failures:
            raise RuntimeError(f"provider init failed: {names}")
        return _Session(names)


def test_default_provider_order_drops_failed_suffixes_and_records_attempts(tmp_path: Path) -> None:
    ort = _FakeOrt(
        failures={
            (
                "TensorrtExecutionProvider",
                "CUDAExecutionProvider",
                "CPUExecutionProvider",
            ),
            ("CUDAExecutionProvider", "CPUExecutionProvider"),
        }
    )

    session, selection = create_onnxruntime_session(ort, tmp_path / "model.onnx")

    assert session.get_providers() == ("CPUExecutionProvider",)
    assert ort.calls == [
        ("TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"),
        ("CUDAExecutionProvider", "CPUExecutionProvider"),
        ("CPUExecutionProvider",),
    ]
    assert selection.fallback_allowed is True
    assert selection.fallback_used is True
    assert selection.selected == ("CPUExecutionProvider",)
    assert selection.attempts[-1].error is None
    assert selection.attempts[0].error is not None


def test_explicit_provider_policy_does_not_silently_fallback(tmp_path: Path) -> None:
    ort = _FakeOrt(failures={("CUDAExecutionProvider", "CPUExecutionProvider")})

    with pytest.raises(InferenceProviderError, match="session construction failed"):
        create_onnxruntime_session(
            ort,
            tmp_path / "model.onnx",
            providers=("CUDAExecutionProvider", "CPUExecutionProvider"),
        )

    assert ort.calls == [("CUDAExecutionProvider", "CPUExecutionProvider")]


def test_tensor_rt_cache_options_are_scoped_to_tensor_rt_attempt(tmp_path: Path) -> None:
    ort = _FakeOrt()
    cache = tmp_path / "trt-cache"

    _, selection = create_onnxruntime_session(
        ort,
        tmp_path / "model.onnx",
        trt_engine_cache_path=cache,
    )

    assert selection.selected == (
        "TensorrtExecutionProvider",
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    )
    assert cache.is_dir()
    assert isinstance(ort.descriptors[0][0], tuple)
    assert ort.descriptors[0][0][0] == "TensorrtExecutionProvider"
    assert ort.descriptors[0][0][1]["trt_engine_cache_enable"] is True
    assert ort.descriptors[0][0][1]["trt_engine_cache_path"] == str(cache)


def test_no_available_requested_provider_is_fail_closed(tmp_path: Path) -> None:
    class _CpuOnly(_FakeOrt):
        def get_available_providers(self) -> list[str]:
            return ["CPUExecutionProvider"]

    with pytest.raises(InferenceProviderError, match="No requested"):
        create_onnxruntime_session(
            _CpuOnly(),
            tmp_path / "model.onnx",
            providers=("TensorrtExecutionProvider",),
        )
