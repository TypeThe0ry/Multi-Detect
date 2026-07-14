from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TensorRtDependencyError(RuntimeError):
    """Raised when the Jetson TensorRT runtime dependencies are unavailable."""


class TensorRtEngineError(RuntimeError):
    """Raised when a serialized engine violates the static Nx6 session boundary."""


@dataclass(frozen=True, slots=True)
class TensorMeta:
    name: str
    shape: tuple[int, ...]


class TensorRtNx6Session:
    """Small TensorRT 8.6 session adapter matching the ONNX Runtime methods we use."""

    def __init__(self, engine_path: str | Path) -> None:
        try:
            import numpy as np
            import tensorrt as trt
            from cuda import cudart
        except ImportError as exc:  # pragma: no cover - Jetson-specific dependency path.
            raise TensorRtDependencyError(
                "TensorRT engines require JetPack TensorRT Python bindings and cuda-python==12.2.1"
            ) from exc

        self._np = np
        self._trt = trt
        self._cudart = cudart
        self._closed = False
        self._device_buffers: list[Any] = []
        artifact = Path(engine_path)
        if not artifact.is_file():
            raise TensorRtEngineError(f"TensorRT engine does not exist: {artifact}")

        logger = trt.Logger(trt.Logger.WARNING)
        runtime = trt.Runtime(logger)
        engine = runtime.deserialize_cuda_engine(artifact.read_bytes())
        if engine is None:
            raise TensorRtEngineError("TensorRT engine deserialization failed")
        if engine.num_bindings != 2:
            raise TensorRtEngineError("TensorRT Nx6 engine must expose one input and one output")

        input_indices = [
            index for index in range(engine.num_bindings) if engine.binding_is_input(index)
        ]
        output_indices = [
            index for index in range(engine.num_bindings) if not engine.binding_is_input(index)
        ]
        if len(input_indices) != 1 or len(output_indices) != 1:
            raise TensorRtEngineError("TensorRT Nx6 engine must expose one input and one output")
        self._input_index = input_indices[0]
        self._output_index = output_indices[0]
        self._input_name = engine.get_binding_name(self._input_index)
        self._output_name = engine.get_binding_name(self._output_index)
        self._input_shape = self._static_shape(engine, self._input_index, "input")
        self._output_shape = self._static_shape(engine, self._output_index, "output")
        if len(self._input_shape) != 4 or self._input_shape[0] != 1 or self._input_shape[1] != 3:
            raise TensorRtEngineError("TensorRT input must be static NCHW with shape 1x3xHxW")
        if len(self._output_shape) != 3 or self._output_shape[0] != 1 or self._output_shape[2] != 6:
            raise TensorRtEngineError("TensorRT output must have static shape 1xNx6")
        for index, label in (
            (self._input_index, "input"),
            (self._output_index, "output"),
        ):
            if trt.nptype(engine.get_binding_dtype(index)) != np.float32:
                raise TensorRtEngineError(f"TensorRT {label} must use float32 bindings")

        context = engine.create_execution_context()
        if context is None:
            raise TensorRtEngineError("TensorRT execution context creation failed")
        self._runtime = runtime
        self._engine = engine
        self._context = context
        self._input_bytes = int(np.prod(self._input_shape)) * np.dtype(np.float32).itemsize
        self._output_bytes = int(np.prod(self._output_shape)) * np.dtype(np.float32).itemsize
        try:
            self._input_device = self._cuda_value(
                cudart.cudaMalloc(self._input_bytes), "allocate TensorRT input"
            )
            self._device_buffers.append(self._input_device)
            self._output_device = self._cuda_value(
                cudart.cudaMalloc(self._output_bytes), "allocate TensorRT output"
            )
            self._device_buffers.append(self._output_device)
        except BaseException:
            self.close()
            raise

    def get_inputs(self) -> tuple[TensorMeta, ...]:
        return (TensorMeta(self._input_name, self._input_shape),)

    def get_providers(self) -> tuple[str, ...]:
        return ("TensorrtExecutionProvider",)

    def run(self, outputs: Any, feeds: dict[str, Any]) -> list[Any]:
        if self._closed:
            raise TensorRtEngineError("TensorRT session is closed")
        if outputs not in (None, []):
            raise TensorRtEngineError("TensorRT adapter only supports all-output inference")
        if set(feeds) != {self._input_name}:
            raise TensorRtEngineError("TensorRT input feed does not match the engine binding")
        tensor = self._np.asarray(feeds[self._input_name], dtype=self._np.float32)
        if tuple(tensor.shape) != self._input_shape:
            raise TensorRtEngineError(
                f"TensorRT input shape {tuple(tensor.shape)} does not match {self._input_shape}"
            )
        tensor = self._np.ascontiguousarray(tensor)
        result = self._np.empty(self._output_shape, dtype=self._np.float32)
        self._cuda_value(
            self._cudart.cudaMemcpy(
                self._input_device,
                tensor.ctypes.data,
                self._input_bytes,
                self._cudart.cudaMemcpyKind.cudaMemcpyHostToDevice,
            ),
            "copy TensorRT input",
        )
        bindings = [0] * self._engine.num_bindings
        bindings[self._input_index] = int(self._input_device)
        bindings[self._output_index] = int(self._output_device)
        if not self._context.execute_v2(bindings):
            raise TensorRtEngineError("TensorRT execution failed")
        self._cuda_value(
            self._cudart.cudaMemcpy(
                result.ctypes.data,
                self._output_device,
                self._output_bytes,
                self._cudart.cudaMemcpyKind.cudaMemcpyDeviceToHost,
            ),
            "copy TensorRT output",
        )
        return [result]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for pointer in reversed(self._device_buffers):
            try:
                self._cudart.cudaFree(pointer)
            except BaseException:
                pass
        self._device_buffers.clear()

    def __del__(self) -> None:  # pragma: no cover - interpreter-shutdown safety net.
        try:
            self.close()
        except BaseException:
            pass

    def _static_shape(self, engine: Any, index: int, label: str) -> tuple[int, ...]:
        shape = tuple(int(value) for value in engine.get_binding_shape(index))
        if not shape or any(value <= 0 for value in shape):
            raise TensorRtEngineError(f"TensorRT {label} binding must have a static shape")
        return shape

    def _cuda_value(self, result: tuple[Any, ...], operation: str) -> Any:
        if not result or int(result[0]) != 0:
            code = result[0] if result else "missing status"
            raise TensorRtEngineError(f"CUDA failed to {operation}: {code}")
        return result[1] if len(result) > 1 else None
