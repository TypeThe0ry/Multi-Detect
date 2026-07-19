from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from multidetect.tensorrt_session import TensorRtEngineError, TensorRtSemanticSession


class _Context:
    def __init__(self, *, execute_ok: bool = True) -> None:
        self.execute_ok = execute_ok
        self.bindings = None

    def execute_v2(self, bindings) -> bool:
        self.bindings = tuple(bindings)
        return self.execute_ok


class _Engine:
    num_bindings = 2

    def __init__(
        self,
        *,
        input_shape=(1, 3, 4, 5),
        output_shape=(1, 4, 5, 1),
        input_dtype=np.float32,
        output_dtype=np.int32,
        execute_ok: bool = True,
    ) -> None:
        self.shapes = (input_shape, output_shape)
        self.dtypes = (input_dtype, output_dtype)
        self.context = _Context(execute_ok=execute_ok)

    def binding_is_input(self, index: int) -> bool:
        return index == 0

    def get_binding_name(self, index: int) -> str:
        return ("input", "output")[index]

    def get_binding_shape(self, index: int):
        return self.shapes[index]

    def get_binding_dtype(self, index: int):
        return self.dtypes[index]

    def create_execution_context(self):
        return self.context


class _CudaRuntime:
    cudaMemcpyKind = SimpleNamespace(
        cudaMemcpyHostToDevice=1,
        cudaMemcpyDeviceToHost=2,
    )

    def __init__(self) -> None:
        self.allocations: list[tuple[int, int]] = []
        self.freed: list[int] = []
        self.copies: list[tuple[int, int, int, int]] = []
        self.plugin_initializations: list[str] = []
        self._next_pointer = 1000

    def cudaMalloc(self, size: int):
        pointer = self._next_pointer
        self._next_pointer += 1000
        self.allocations.append((pointer, size))
        return 0, pointer

    def cudaMemcpy(self, destination: int, source: int, size: int, kind: int):
        self.copies.append((destination, source, size, kind))
        return (0,)

    def cudaFree(self, pointer: int):
        self.freed.append(pointer)
        return (0,)


def _install_runtime(monkeypatch, engine: _Engine):
    cuda = _CudaRuntime()

    class _Logger:
        WARNING = 1

        def __init__(self, _level) -> None:
            pass

    class _Runtime:
        def __init__(self, _logger) -> None:
            pass

        def deserialize_cuda_engine(self, _payload):
            return engine

    trt = SimpleNamespace(
        Logger=_Logger,
        Runtime=_Runtime,
        nptype=lambda value: value,
        init_libnvinfer_plugins=lambda _logger, namespace: (
            cuda.plugin_initializations.append(namespace) or True
        ),
    )
    monkeypatch.setitem(sys.modules, "tensorrt", trt)
    monkeypatch.setitem(sys.modules, "cuda", SimpleNamespace(cudart=cuda))
    return cuda


def test_semantic_tensorrt_session_runs_static_integer_mask_and_frees_buffers(
    tmp_path,
    monkeypatch,
) -> None:
    engine = _Engine()
    cuda = _install_runtime(monkeypatch, engine)
    artifact = tmp_path / "semantic.engine"
    artifact.write_bytes(b"serialized-engine")
    session = TensorRtSemanticSession(artifact, input_height=4, input_width=5)

    assert cuda.plugin_initializations == [""]
    assert session.get_inputs()[0].shape == (1, 3, 4, 5)
    assert session.get_outputs()[0].shape == (1, 4, 5, 1)
    assert session.get_providers() == ("TensorrtExecutionProvider",)
    output = session.run(
        ("output",),
        {"input": np.zeros((1, 3, 4, 5), dtype=np.float32)},
    )[0]

    assert output.shape == (1, 4, 5, 1)
    assert output.dtype == np.int32
    assert engine.context.bindings == (1000, 2000)
    assert [size for _pointer, size in cuda.allocations] == [240, 80]
    assert [copy[3] for copy in cuda.copies] == [1, 2]
    session.close()
    session.close()
    assert cuda.freed == [2000, 1000]


@pytest.mark.parametrize(
    ("engine", "message"),
    [
        (_Engine(input_shape=(1, 3, 4, 4)), "input shape"),
        (_Engine(output_shape=(1, 4, 5)), "output shape"),
        (_Engine(input_dtype=np.float16), "input must use float32"),
        (_Engine(output_dtype=np.float32), "output must use int32 or int64"),
    ],
)
def test_semantic_tensorrt_session_rejects_shape_or_dtype_before_allocating(
    tmp_path,
    monkeypatch,
    engine,
    message: str,
) -> None:
    cuda = _install_runtime(monkeypatch, engine)
    artifact = tmp_path / "semantic.engine"
    artifact.write_bytes(b"serialized-engine")

    with pytest.raises(TensorRtEngineError, match=message):
        TensorRtSemanticSession(artifact, input_height=4, input_width=5)

    assert cuda.allocations == []


def test_semantic_tensorrt_session_fails_closed_on_bad_request_or_execution(
    tmp_path,
    monkeypatch,
) -> None:
    engine = _Engine(execute_ok=False)
    _install_runtime(monkeypatch, engine)
    artifact = tmp_path / "semantic.engine"
    artifact.write_bytes(b"serialized-engine")
    session = TensorRtSemanticSession(artifact, input_height=4, input_width=5)

    with pytest.raises(TensorRtEngineError, match="output request"):
        session.run(("wrong",), {"input": np.zeros((1, 3, 4, 5), dtype=np.float32)})
    with pytest.raises(TensorRtEngineError, match="input shape"):
        session.run(("output",), {"input": np.zeros((1, 3, 4, 4), dtype=np.float32)})
    with pytest.raises(TensorRtEngineError, match="execution failed"):
        session.run(("output",), {"input": np.zeros((1, 3, 4, 5), dtype=np.float32)})
    session.close()
    with pytest.raises(TensorRtEngineError, match="closed"):
        session.run(("output",), {"input": np.zeros((1, 3, 4, 5), dtype=np.float32)})
