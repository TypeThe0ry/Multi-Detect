from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TensorRtDependencyError(RuntimeError):
    """Raised when the Jetson TensorRT runtime dependencies are unavailable."""


class TensorRtEngineError(RuntimeError):
    """Raised when a serialized engine violates its declared tensor boundary."""


def _initialize_tensorrt_plugins(trt: Any, logger: Any) -> None:
    """Register standard TensorRT plugins before deserializing an engine.

    ``trtexec`` initializes the plugin registry automatically, while a direct
    Python ``Runtime`` does not.  Engines containing layers such as
    ``InstanceNormalization_TRT`` therefore build successfully but fail to
    deserialize unless the registry is initialized in-process first.
    """

    initializer = getattr(trt, "init_libnvinfer_plugins", None)
    if callable(initializer) and initializer(logger, "") is False:
        raise TensorRtDependencyError("TensorRT standard plugin initialization failed")


@dataclass(frozen=True, slots=True)
class TensorMeta:
    name: str
    shape: tuple[int, ...]


class TensorRtNx6Session:
    """Small TensorRT 8.6 session adapter matching the ONNX Runtime methods we use."""

    def __init__(
        self,
        engine_path: str | Path,
        *,
        raw_yolo_class_count: int | None = None,
    ) -> None:
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
        _initialize_tensorrt_plugins(trt, logger)
        runtime = trt.Runtime(logger)
        engine = runtime.deserialize_cuda_engine(artifact.read_bytes())
        if engine is None:
            raise TensorRtEngineError("TensorRT engine deserialization failed")
        contract_name = "raw YOLO" if raw_yolo_class_count is not None else "Nx6"
        if engine.num_bindings != 2:
            raise TensorRtEngineError(
                f"TensorRT {contract_name} engine must expose one input and one output"
            )

        input_indices = [
            index for index in range(engine.num_bindings) if engine.binding_is_input(index)
        ]
        output_indices = [
            index for index in range(engine.num_bindings) if not engine.binding_is_input(index)
        ]
        if len(input_indices) != 1 or len(output_indices) != 1:
            raise TensorRtEngineError(
                f"TensorRT {contract_name} engine must expose one input and one output"
            )
        self._input_index = input_indices[0]
        self._output_index = output_indices[0]
        self._input_name = engine.get_binding_name(self._input_index)
        self._output_name = engine.get_binding_name(self._output_index)
        self._input_shape = self._static_shape(engine, self._input_index, "input")
        self._output_shape = self._static_shape(engine, self._output_index, "output")
        if len(self._input_shape) != 4 or self._input_shape[0] != 1 or self._input_shape[1] != 3:
            raise TensorRtEngineError("TensorRT input must be static NCHW with shape 1x3xHxW")
        if raw_yolo_class_count is None:
            valid_output = (
                len(self._output_shape) == 3
                and self._output_shape[0] == 1
                and self._output_shape[2] == 6
            )
            expected_output = "1xNx6"
        else:
            feature_count = 4 + raw_yolo_class_count
            valid_output = (
                len(self._output_shape) == 3
                and self._output_shape[0] == 1
                and feature_count in self._output_shape[1:]
            )
            expected_output = f"1x{feature_count}xN or 1xNx{feature_count}"
        if not valid_output:
            raise TensorRtEngineError(f"TensorRT output must have static shape {expected_output}")
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


class TensorRtRawYoloSession(TensorRtNx6Session):
    """Direct TensorRT session for a traditional Ultralytics raw detect head."""

    def __init__(self, engine_path: str | Path, *, class_count: int) -> None:
        if isinstance(class_count, bool) or not isinstance(class_count, int) or class_count <= 0:
            raise ValueError("raw YOLO class_count must be a positive integer")
        super().__init__(engine_path, raw_yolo_class_count=class_count)


class TensorRtDepthSession:
    """TensorRT 8.6 adapter for a batch-1 NCHW monocular depth network."""

    def __init__(
        self,
        engine_path: str | Path,
        *,
        input_height: int = 518,
        input_width: int = 518,
    ) -> None:
        if input_height <= 0 or input_width <= 0:
            raise ValueError("depth input dimensions must be positive")
        try:
            import numpy as np
            import tensorrt as trt
            from cuda import cudart
        except ImportError as exc:  # pragma: no cover - Jetson-specific dependency path.
            raise TensorRtDependencyError(
                "TensorRT engines require JetPack TensorRT Python bindings and cuda-python==12.2.1"
            ) from exc

        artifact = Path(engine_path)
        if not artifact.is_file():
            raise TensorRtEngineError(f"TensorRT depth engine does not exist: {artifact}")
        self._np = np
        self._cudart = cudart
        self._closed = False
        self._device_buffers: list[Any] = []
        logger = trt.Logger(trt.Logger.WARNING)
        _initialize_tensorrt_plugins(trt, logger)
        runtime = trt.Runtime(logger)
        engine = runtime.deserialize_cuda_engine(artifact.read_bytes())
        if engine is None:
            raise TensorRtEngineError("TensorRT depth engine deserialization failed")
        if engine.num_bindings != 2:
            raise TensorRtEngineError("TensorRT depth engine must expose one input and one output")
        inputs = [index for index in range(engine.num_bindings) if engine.binding_is_input(index)]
        outputs = [
            index
            for index in range(engine.num_bindings)
            if not engine.binding_is_input(index)
        ]
        if len(inputs) != 1 or len(outputs) != 1:
            raise TensorRtEngineError("TensorRT depth engine must expose one input and one output")
        self._input_index, self._output_index = inputs[0], outputs[0]
        self._input_name = engine.get_binding_name(self._input_index)
        self._output_name = engine.get_binding_name(self._output_index)
        declared_input = tuple(int(value) for value in engine.get_binding_shape(self._input_index))
        if len(declared_input) != 4 or declared_input[1] not in {-1, 3}:
            raise TensorRtEngineError("TensorRT depth input must be NCHW RGB")
        expected_input = (1, 3, input_height, input_width)
        context = engine.create_execution_context()
        if context is None:
            raise TensorRtEngineError("TensorRT depth execution context creation failed")
        if any(value <= 0 for value in declared_input) and not context.set_binding_shape(
            self._input_index, expected_input
        ):
            raise TensorRtEngineError("TensorRT depth input shape was rejected")
        runtime_input = tuple(int(value) for value in context.get_binding_shape(self._input_index))
        output_shape = tuple(int(value) for value in context.get_binding_shape(self._output_index))
        if runtime_input != expected_input:
            raise TensorRtEngineError(
                f"TensorRT depth input shape {runtime_input} does not match {expected_input}"
            )
        if (
            len(output_shape) not in {3, 4}
            or output_shape[0] != 1
            or any(value <= 0 for value in output_shape)
            or math.prod(output_shape) != input_height * input_width
        ):
            raise TensorRtEngineError("TensorRT depth output must contain one dense depth map")
        self._input_dtype = np.dtype(trt.nptype(engine.get_binding_dtype(self._input_index)))
        self._output_dtype = np.dtype(trt.nptype(engine.get_binding_dtype(self._output_index)))
        if self._input_dtype not in {np.dtype(np.float16), np.dtype(np.float32)}:
            raise TensorRtEngineError("TensorRT depth input must use float16 or float32")
        if self._output_dtype not in {np.dtype(np.float16), np.dtype(np.float32)}:
            raise TensorRtEngineError("TensorRT depth output must use float16 or float32")
        self._runtime, self._engine, self._context = runtime, engine, context
        self._input_shape, self._output_shape = expected_input, output_shape
        self._input_bytes = math.prod(expected_input) * self._input_dtype.itemsize
        self._output_bytes = math.prod(output_shape) * self._output_dtype.itemsize
        try:
            self._input_device = self._cuda_value(
                cudart.cudaMalloc(self._input_bytes), "allocate TensorRT depth input"
            )
            self._device_buffers.append(self._input_device)
            self._output_device = self._cuda_value(
                cudart.cudaMalloc(self._output_bytes), "allocate TensorRT depth output"
            )
            self._device_buffers.append(self._output_device)
        except BaseException:
            self.close()
            raise

    def get_inputs(self) -> tuple[TensorMeta, ...]:
        return (TensorMeta(self._input_name, self._input_shape),)

    def get_outputs(self) -> tuple[TensorMeta, ...]:
        return (TensorMeta(self._output_name, self._output_shape),)

    def get_providers(self) -> tuple[str, ...]:
        return ("TensorrtExecutionProvider",)

    def run(self, outputs: Any, feeds: dict[str, Any]) -> list[Any]:
        if self._closed:
            raise TensorRtEngineError("TensorRT depth session is closed")
        if outputs not in (None, [], [self._output_name]):
            raise TensorRtEngineError("TensorRT depth output request is unsupported")
        if set(feeds) != {self._input_name}:
            raise TensorRtEngineError("TensorRT depth input feed does not match the engine")
        tensor = self._np.asarray(feeds[self._input_name], dtype=self._input_dtype)
        if tuple(tensor.shape) != self._input_shape:
            raise TensorRtEngineError("TensorRT depth input tensor has an invalid shape")
        tensor = self._np.ascontiguousarray(tensor)
        result = self._np.empty(self._output_shape, dtype=self._output_dtype)
        self._cuda_value(
            self._cudart.cudaMemcpy(
                self._input_device,
                tensor.ctypes.data,
                self._input_bytes,
                self._cudart.cudaMemcpyKind.cudaMemcpyHostToDevice,
            ),
            "copy TensorRT depth input",
        )
        bindings = [0] * self._engine.num_bindings
        bindings[self._input_index] = int(self._input_device)
        bindings[self._output_index] = int(self._output_device)
        if not self._context.execute_v2(bindings):
            raise TensorRtEngineError("TensorRT depth execution failed")
        self._cuda_value(
            self._cudart.cudaMemcpy(
                result.ctypes.data,
                self._output_device,
                self._output_bytes,
                self._cudart.cudaMemcpyKind.cudaMemcpyDeviceToHost,
            ),
            "copy TensorRT depth output",
        )
        return [result.astype(self._np.float32, copy=False)]

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

    def _cuda_value(self, result: tuple[Any, ...], operation: str) -> Any:
        if not result or int(result[0]) != 0:
            code = result[0] if result else "missing status"
            raise TensorRtEngineError(f"CUDA failed to {operation}: {code}")
        return result[1] if len(result) > 1 else None


class TensorRtSemanticSession:
    """Static batch-1 TensorRT adapter for categorical NHWC segmentation masks."""

    def __init__(
        self,
        engine_path: str | Path,
        *,
        input_height: int = 1024,
        input_width: int = 1820,
    ) -> None:
        if input_height <= 0 or input_width <= 0:
            raise ValueError("semantic TensorRT dimensions must be positive")
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
            raise TensorRtEngineError(f"TensorRT semantic engine does not exist: {artifact}")

        logger = trt.Logger(trt.Logger.WARNING)
        _initialize_tensorrt_plugins(trt, logger)
        runtime = trt.Runtime(logger)
        engine = runtime.deserialize_cuda_engine(artifact.read_bytes())
        if engine is None:
            raise TensorRtEngineError("TensorRT semantic engine deserialization failed")
        if engine.num_bindings != 2:
            raise TensorRtEngineError(
                "TensorRT semantic engine must expose one input and one output"
            )
        input_indices = [
            index for index in range(engine.num_bindings) if engine.binding_is_input(index)
        ]
        output_indices = [
            index for index in range(engine.num_bindings) if not engine.binding_is_input(index)
        ]
        if len(input_indices) != 1 or len(output_indices) != 1:
            raise TensorRtEngineError(
                "TensorRT semantic engine must expose one input and one output"
            )
        self._input_index = input_indices[0]
        self._output_index = output_indices[0]
        self._input_name = engine.get_binding_name(self._input_index)
        self._output_name = engine.get_binding_name(self._output_index)
        self._input_shape = self._static_shape(engine, self._input_index, "input")
        self._output_shape = self._static_shape(engine, self._output_index, "output")
        expected_input = (1, 3, input_height, input_width)
        expected_output = (1, input_height, input_width, 1)
        if self._input_shape != expected_input:
            raise TensorRtEngineError(
                f"TensorRT semantic input shape {self._input_shape} does not match {expected_input}"
            )
        if self._output_shape != expected_output:
            raise TensorRtEngineError(
                f"TensorRT semantic output shape {self._output_shape} does not match "
                f"{expected_output}"
            )
        input_dtype = trt.nptype(engine.get_binding_dtype(self._input_index))
        output_dtype = trt.nptype(engine.get_binding_dtype(self._output_index))
        if input_dtype != np.float32:
            raise TensorRtEngineError("TensorRT semantic input must use float32")
        if output_dtype not in {np.dtype(np.int32).type, np.dtype(np.int64).type}:
            raise TensorRtEngineError("TensorRT semantic output must use int32 or int64 class IDs")
        self._output_dtype = output_dtype

        context = engine.create_execution_context()
        if context is None:
            raise TensorRtEngineError("TensorRT semantic execution context creation failed")
        self._runtime = runtime
        self._engine = engine
        self._context = context
        self._input_bytes = int(np.prod(self._input_shape)) * np.dtype(np.float32).itemsize
        self._output_bytes = int(np.prod(self._output_shape)) * np.dtype(output_dtype).itemsize
        try:
            self._input_device = self._cuda_value(
                cudart.cudaMalloc(self._input_bytes),
                "allocate TensorRT semantic input",
            )
            self._device_buffers.append(self._input_device)
            self._output_device = self._cuda_value(
                cudart.cudaMalloc(self._output_bytes),
                "allocate TensorRT semantic output",
            )
            self._device_buffers.append(self._output_device)
        except BaseException:
            self.close()
            raise

    def get_inputs(self) -> tuple[TensorMeta, ...]:
        return (TensorMeta(self._input_name, self._input_shape),)

    def get_outputs(self) -> tuple[TensorMeta, ...]:
        return (TensorMeta(self._output_name, self._output_shape),)

    def get_providers(self) -> tuple[str, ...]:
        return ("TensorrtExecutionProvider",)

    def run(self, outputs: Any, feeds: dict[str, Any]) -> list[Any]:
        if self._closed:
            raise TensorRtEngineError("TensorRT semantic session is closed")
        if outputs is not None and tuple(outputs) != (self._output_name,):
            raise TensorRtEngineError("TensorRT semantic output request is unsupported")
        if set(feeds) != {self._input_name}:
            raise TensorRtEngineError("TensorRT semantic input feed does not match the engine")
        tensor = self._np.asarray(feeds[self._input_name], dtype=self._np.float32)
        if tuple(tensor.shape) != self._input_shape:
            raise TensorRtEngineError(
                f"TensorRT semantic input shape {tuple(tensor.shape)} does not match "
                f"{self._input_shape}"
            )
        tensor = self._np.ascontiguousarray(tensor)
        result = self._np.empty(self._output_shape, dtype=self._output_dtype)
        self._cuda_value(
            self._cudart.cudaMemcpy(
                self._input_device,
                tensor.ctypes.data,
                self._input_bytes,
                self._cudart.cudaMemcpyKind.cudaMemcpyHostToDevice,
            ),
            "copy TensorRT semantic input",
        )
        bindings = [0] * self._engine.num_bindings
        bindings[self._input_index] = int(self._input_device)
        bindings[self._output_index] = int(self._output_device)
        if not self._context.execute_v2(bindings):
            raise TensorRtEngineError("TensorRT semantic execution failed")
        self._cuda_value(
            self._cudart.cudaMemcpy(
                result.ctypes.data,
                self._output_device,
                self._output_bytes,
                self._cudart.cudaMemcpyKind.cudaMemcpyDeviceToHost,
            ),
            "copy TensorRT semantic output",
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
            raise TensorRtEngineError(f"TensorRT semantic {label} binding must have a static shape")
        return shape

    def _cuda_value(self, result: tuple[Any, ...], operation: str) -> Any:
        if not result or int(result[0]) != 0:
            code = result[0] if result else "missing status"
            raise TensorRtEngineError(f"CUDA failed to {operation}: {code}")
        return result[1] if len(result) > 1 else None


class TensorRtEmbeddingSession:
    """TensorRT 8.6 adapter for dynamic-batch NCHW appearance embeddings."""

    def __init__(
        self,
        engine_path: str | Path,
        *,
        maximum_batch_size: int,
        input_height: int = 256,
        input_width: int = 128,
        feature_size: int = 256,
    ) -> None:
        if maximum_batch_size <= 0:
            raise ValueError("maximum_batch_size must be positive")
        if input_height <= 0 or input_width <= 0 or feature_size <= 1:
            raise ValueError("embedding dimensions must be positive")
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
        self._maximum_batch_size = maximum_batch_size
        self._input_height = input_height
        self._input_width = input_width
        self._feature_size = feature_size
        self._closed = False
        self._device_buffers: list[Any] = []
        artifact = Path(engine_path)
        if not artifact.is_file():
            raise TensorRtEngineError(f"TensorRT embedding engine does not exist: {artifact}")

        logger = trt.Logger(trt.Logger.WARNING)
        _initialize_tensorrt_plugins(trt, logger)
        runtime = trt.Runtime(logger)
        engine = runtime.deserialize_cuda_engine(artifact.read_bytes())
        if engine is None:
            raise TensorRtEngineError("TensorRT embedding engine deserialization failed")
        if engine.num_bindings != 2:
            raise TensorRtEngineError(
                "TensorRT embedding engine must expose one input and one output"
            )
        input_indices = [
            index for index in range(engine.num_bindings) if engine.binding_is_input(index)
        ]
        output_indices = [
            index for index in range(engine.num_bindings) if not engine.binding_is_input(index)
        ]
        if len(input_indices) != 1 or len(output_indices) != 1:
            raise TensorRtEngineError(
                "TensorRT embedding engine must expose one input and one output"
            )
        self._input_index = input_indices[0]
        self._output_index = output_indices[0]
        self._input_name = engine.get_binding_name(self._input_index)
        self._output_name = engine.get_binding_name(self._output_index)
        input_shape = tuple(int(value) for value in engine.get_binding_shape(self._input_index))
        output_shape = tuple(int(value) for value in engine.get_binding_shape(self._output_index))
        if len(input_shape) != 4 or input_shape[1:] != (3, input_height, input_width):
            raise TensorRtEngineError(
                "TensorRT embedding input must be NCHW with the configured spatial dimensions"
            )
        if input_shape[0] not in {-1, maximum_batch_size} and not (
            1 <= input_shape[0] <= maximum_batch_size
        ):
            raise TensorRtEngineError("TensorRT embedding input batch dimension is unsupported")
        if len(output_shape) < 2 or output_shape[0] not in {-1, input_shape[0]}:
            raise TensorRtEngineError("TensorRT embedding output batch dimension is unsupported")
        static_output_features = _static_embedding_feature_count(output_shape)
        if static_output_features is not None and static_output_features != feature_size:
            raise TensorRtEngineError(
                "TensorRT embedding output feature size does not match the configured contract"
            )
        for index, label in (
            (self._input_index, "input"),
            (self._output_index, "output"),
        ):
            if trt.nptype(engine.get_binding_dtype(index)) != np.float32:
                raise TensorRtEngineError(f"TensorRT embedding {label} must use float32 bindings")

        context = engine.create_execution_context()
        if context is None:
            raise TensorRtEngineError("TensorRT embedding execution context creation failed")
        maximum_input_shape = (maximum_batch_size, 3, input_height, input_width)
        if input_shape[0] == -1 and not context.set_binding_shape(
            self._input_index, maximum_input_shape
        ):
            raise TensorRtEngineError("TensorRT embedding maximum batch shape was rejected")
        maximum_output_shape = tuple(
            int(value) for value in context.get_binding_shape(self._output_index)
        )
        if any(value <= 0 for value in maximum_output_shape):
            raise TensorRtEngineError("TensorRT embedding output shape remains unresolved")
        if maximum_output_shape[0] != maximum_batch_size:
            raise TensorRtEngineError("TensorRT embedding output batch does not match the input")
        if math.prod(maximum_output_shape[1:]) != feature_size:
            raise TensorRtEngineError("TensorRT embedding runtime feature size is invalid")

        self._runtime = runtime
        self._engine = engine
        self._context = context
        self._dynamic_batch = input_shape[0] == -1
        self._static_batch_size = None if self._dynamic_batch else input_shape[0]
        self._input_meta_shape = (-1, 3, input_height, input_width)
        self._output_meta_shape = (-1, feature_size)
        float_size = np.dtype(np.float32).itemsize
        self._maximum_input_bytes = int(np.prod(maximum_input_shape)) * float_size
        self._maximum_output_bytes = int(np.prod(maximum_output_shape)) * float_size
        try:
            self._input_device = self._cuda_value(
                cudart.cudaMalloc(self._maximum_input_bytes),
                "allocate TensorRT embedding input",
            )
            self._device_buffers.append(self._input_device)
            self._output_device = self._cuda_value(
                cudart.cudaMalloc(self._maximum_output_bytes),
                "allocate TensorRT embedding output",
            )
            self._device_buffers.append(self._output_device)
        except BaseException:
            self.close()
            raise

    def get_inputs(self) -> tuple[TensorMeta, ...]:
        return (TensorMeta(self._input_name, self._input_meta_shape),)

    def get_outputs(self) -> tuple[TensorMeta, ...]:
        return (TensorMeta(self._output_name, self._output_meta_shape),)

    def get_providers(self) -> tuple[str, ...]:
        return ("TensorrtExecutionProvider",)

    def run(self, outputs: Any, feeds: dict[str, Any]) -> list[Any]:
        if self._closed:
            raise TensorRtEngineError("TensorRT embedding session is closed")
        if outputs not in (None, [], [self._output_name]):
            raise TensorRtEngineError("TensorRT embedding output request is unsupported")
        if set(feeds) != {self._input_name}:
            raise TensorRtEngineError("TensorRT embedding input feed does not match the engine")
        tensor = self._np.asarray(feeds[self._input_name], dtype=self._np.float32)
        if tensor.ndim != 4 or tuple(tensor.shape[1:]) != (
            3,
            self._input_height,
            self._input_width,
        ):
            raise TensorRtEngineError("TensorRT embedding input tensor has an invalid shape")
        batch_size = int(tensor.shape[0])
        if not 1 <= batch_size <= self._maximum_batch_size:
            raise TensorRtEngineError("TensorRT embedding batch is outside the engine profile")
        if self._static_batch_size is not None and batch_size != self._static_batch_size:
            raise TensorRtEngineError("TensorRT embedding batch does not match the static engine")
        if self._dynamic_batch and not self._context.set_binding_shape(
            self._input_index,
            tuple(int(value) for value in tensor.shape),
        ):
            raise TensorRtEngineError("TensorRT embedding input shape was rejected")
        output_shape = tuple(
            int(value) for value in self._context.get_binding_shape(self._output_index)
        )
        if (
            not output_shape
            or output_shape[0] != batch_size
            or any(value <= 0 for value in output_shape)
            or math.prod(output_shape[1:]) != self._feature_size
        ):
            raise TensorRtEngineError("TensorRT embedding runtime output shape is invalid")

        tensor = self._np.ascontiguousarray(tensor)
        result = self._np.empty(output_shape, dtype=self._np.float32)
        input_bytes = tensor.nbytes
        output_bytes = result.nbytes
        if input_bytes > self._maximum_input_bytes or output_bytes > self._maximum_output_bytes:
            raise TensorRtEngineError("TensorRT embedding buffers exceed their bounded allocation")
        self._cuda_value(
            self._cudart.cudaMemcpy(
                self._input_device,
                tensor.ctypes.data,
                input_bytes,
                self._cudart.cudaMemcpyKind.cudaMemcpyHostToDevice,
            ),
            "copy TensorRT embedding input",
        )
        bindings = [0] * self._engine.num_bindings
        bindings[self._input_index] = int(self._input_device)
        bindings[self._output_index] = int(self._output_device)
        if not self._context.execute_v2(bindings):
            raise TensorRtEngineError("TensorRT embedding execution failed")
        self._cuda_value(
            self._cudart.cudaMemcpy(
                result.ctypes.data,
                self._output_device,
                output_bytes,
                self._cudart.cudaMemcpyKind.cudaMemcpyDeviceToHost,
            ),
            "copy TensorRT embedding output",
        )
        return [result.reshape(batch_size, self._feature_size)]

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

    def _cuda_value(self, result: tuple[Any, ...], operation: str) -> Any:
        if not result or int(result[0]) != 0:
            code = result[0] if result else "missing status"
            raise TensorRtEngineError(f"CUDA failed to {operation}: {code}")
        return result[1] if len(result) > 1 else None


def _static_embedding_feature_count(shape: tuple[int, ...]) -> int | None:
    if any(value <= 0 for value in shape[1:]):
        return None
    return math.prod(shape[1:])
