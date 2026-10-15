"""Native TensorRT YOLOE proposer for Jetson Orin.

The module deliberately keeps TensorRT, CUDA and NumPy imports lazy.  This lets
the benchmark's documentation and CPU tests run on a development host that has
no Jetson runtime installed.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def unwrap_engine_bytes(path: str | Path) -> bytes:
    """Return raw engine bytes, accepting the optional Ultralytics metadata header."""
    data = Path(path).read_bytes()
    if len(data) >= 4:
        size = int.from_bytes(data[:4], "little")
        if 0 < size <= len(data) - 4:
            try:
                metadata = json.loads(data[4 : 4 + size].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                metadata = None
            if isinstance(metadata, dict):
                return data[4 + size :]
    return data


def letterbox_rgb(
    image: Any, size: int, *, pad_value: int = 114
) -> tuple[Any, float, tuple[float, float]]:
    """Resize a BGR/RGB HWC image to a square RGB CHW float32 tensor.

    The returned scale and (left, top) padding map model coordinates back to the
    source image.  Pillow is used when OpenCV is unavailable.
    """
    import numpy as np

    array = np.asarray(image)
    if array.ndim != 3 or array.shape[2] < 3:
        raise ValueError("image must be an HWC array with at least three channels")
    height, width = array.shape[:2]
    scale = min(size / width, size / height)
    new_w, new_h = max(1, round(width * scale)), max(1, round(height * scale))
    try:
        import cv2

        resized = cv2.resize(array[:, :, :3], (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    except ImportError:
        from PIL import Image

        resized = np.asarray(
            Image.fromarray(array[:, :, :3]).resize((new_w, new_h), Image.Resampling.BILINEAR)
        )
    canvas = np.full((size, size, 3), pad_value, dtype=np.uint8)
    left = (size - new_w) / 2
    top = (size - new_h) / 2
    left_i, top_i = round(left), round(top)
    canvas[top_i : top_i + new_h, left_i : left_i + new_w] = resized
    # Ultralytics receives BGR images and converts to RGB before inference.
    chw = canvas[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) / 255.0
    return chw[None, ...], scale, (float(left_i), float(top_i))


def decode_yoloe_output(
    output: Any,
    *,
    source_shape: tuple[int, int],
    scale: float,
    pad: tuple[float, float],
    conf_threshold: float = 0.05,
    iou_threshold: float = 0.7,
    max_det: int = 300,
) -> tuple[Any, Any, Any]:
    """Decode ``[1, 24, anchors]`` xywh + 20 class scores with class-agnostic NMS."""
    import numpy as np

    values = np.asarray(output)
    if values.ndim == 3:
        values = values[0]
    if values.ndim != 2 or values.shape[0] != 24:
        raise ValueError(f"expected output shape [1,24,N], got {tuple(np.asarray(output).shape)}")
    values = values.T.astype(np.float32, copy=False)
    boxes = values[:, :4]
    scores = values[:, 4:]
    cls = scores.argmax(axis=1)
    conf = scores[np.arange(scores.shape[0]), cls]
    keep = conf >= conf_threshold
    boxes, conf, cls = boxes[keep], conf[keep], cls[keep]
    if not len(boxes):
        return (
            np.empty((0, 4), dtype=np.float32),
            np.empty(0, dtype=np.float32),
            np.empty(0, dtype=np.float32),
        )
    xyxy = np.empty_like(boxes)
    xyxy[:, 0] = boxes[:, 0] - boxes[:, 2] / 2
    xyxy[:, 1] = boxes[:, 1] - boxes[:, 3] / 2
    xyxy[:, 2] = boxes[:, 0] + boxes[:, 2] / 2
    xyxy[:, 3] = boxes[:, 1] + boxes[:, 3] / 2
    order = np.argsort(-conf)
    selected: list[int] = []
    while len(order) and len(selected) < max_det:
        index = int(order[0])
        selected.append(index)
        if len(order) == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(xyxy[index, 0], xyxy[rest, 0])
        yy1 = np.maximum(xyxy[index, 1], xyxy[rest, 1])
        xx2 = np.minimum(xyxy[index, 2], xyxy[rest, 2])
        yy2 = np.minimum(xyxy[index, 3], xyxy[rest, 3])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        area_i = np.maximum(0, xyxy[index, 2] - xyxy[index, 0]) * np.maximum(
            0, xyxy[index, 3] - xyxy[index, 1]
        )
        area_r = np.maximum(0, xyxy[rest, 2] - xyxy[rest, 0]) * np.maximum(
            0, xyxy[rest, 3] - xyxy[rest, 1]
        )
        iou = inter / np.maximum(area_i + area_r - inter, 1e-9)
        order = rest[iou <= iou_threshold]
    selected_array = np.asarray(selected, dtype=np.int64)
    xyxy, conf, cls = xyxy[selected_array], conf[selected_array], cls[selected_array]
    left, top = pad
    xyxy[:, [0, 2]] = (xyxy[:, [0, 2]] - left) / scale
    xyxy[:, [1, 3]] = (xyxy[:, [1, 3]] - top) / scale
    height, width = source_shape
    xyxy[:, [0, 2]] = np.clip(xyxy[:, [0, 2]], 0, width)
    xyxy[:, [1, 3]] = np.clip(xyxy[:, [1, 3]], 0, height)
    return xyxy, conf, cls.astype(np.float32)


@dataclass
class _Buffers:
    input_ptr: int
    output_ptr: int
    input_nbytes: int
    output_nbytes: int


class TensorRTProposer:
    """Synchronous batch-one TensorRT runner using the Jetson libcudart API."""

    backend = "tensorrt_python_cudart"

    def __init__(self, engine: str | Path, *, device: int = 0) -> None:
        import numpy as np
        import tensorrt as trt

        self._np = np
        self._trt = trt
        self.device = int(device)
        self._runtime = trt.Runtime(trt.Logger(trt.Logger.WARNING))
        self._engine = self._runtime.deserialize_cuda_engine(unwrap_engine_bytes(engine))
        if self._engine is None:
            raise RuntimeError("TensorRT could not deserialize engine")
        self._context = self._engine.create_execution_context()
        self._input_index, self._output_index = self._find_io()
        self._tensor_api = hasattr(self._engine, "num_io_tensors")
        if self._tensor_api:
            self._input_name = self._engine.get_tensor_name(self._input_index)
            self._output_name = self._engine.get_tensor_name(self._output_index)
            shape = self._engine.get_tensor_shape(self._input_name)
        else:
            shape = self._engine.get_binding_shape(self._input_index)
        self.input_shape = tuple(int(x) for x in shape)
        if self._tensor_api:
            self._input_dtype = trt.nptype(self._engine.get_tensor_dtype(self._input_name))
            self._output_dtype = trt.nptype(self._engine.get_tensor_dtype(self._output_name))
        else:
            self._input_dtype = trt.nptype(self._engine.get_binding_dtype(self._input_index))
            self._output_dtype = trt.nptype(self._engine.get_binding_dtype(self._output_index))
        if len(self.input_shape) != 4 or any(x <= 0 for x in self.input_shape):
            raise ValueError(f"engine input must have static NCHW shape, got {self.input_shape}")
        self._cuda = _load_cudart()
        self._buffers: _Buffers | None = None

    def _find_io(self) -> tuple[int, int]:
        trt = self._trt
        if hasattr(self._engine, "num_io_tensors"):
            names = [self._engine.get_tensor_name(i) for i in range(self._engine.num_io_tensors)]
            inp = next(
                name
                for name in names
                if self._engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT
            )
            out = next(
                name
                for name in names
                if self._engine.get_tensor_mode(name) == trt.TensorIOMode.OUTPUT
            )
            return names.index(inp), names.index(out)
        inputs = [i for i in range(self._engine.num_bindings) if self._engine.binding_is_input(i)]
        outputs = [
            i for i in range(self._engine.num_bindings) if not self._engine.binding_is_input(i)
        ]
        return inputs[0], outputs[0]

    def _ensure_buffers(self, input_array: Any, output_shape: tuple[int, ...]) -> list[int]:
        n_in = int(input_array.nbytes)
        n_out = int(self._np.prod(output_shape) * self._output_dtype.itemsize)
        if (
            self._buffers is None
            or self._buffers.input_nbytes != n_in
            or self._buffers.output_nbytes != n_out
        ):
            if self._buffers:
                self._cuda.cudaFree(self._buffers.input_ptr)
                self._cuda.cudaFree(self._buffers.output_ptr)
            in_ptr = self._cuda.malloc(n_in)
            out_ptr = self._cuda.malloc(n_out)
            self._buffers = _Buffers(in_ptr, out_ptr, n_in, n_out)
        return [self._buffers.input_ptr, self._buffers.output_ptr]

    def predict(
        self, image: Any, *, imgsz: int | None = None, conf: float = 0.05, **_: Any
    ) -> list[Any]:
        import numpy as np

        size = int(imgsz or self.input_shape[-1])
        tensor, scale, pad = letterbox_rgb(image, size)
        tensor = np.ascontiguousarray(tensor, dtype=self._input_dtype)
        if self._tensor_api:
            shape = tuple(int(x) for x in self._engine.get_tensor_shape(self._output_name))
        else:
            shape = tuple(int(x) for x in self._engine.get_binding_shape(self._output_index))
        self._cuda.set_device(self.device)
        ptrs = self._ensure_buffers(tensor, shape)
        self._cuda.memcpy_htod(ptrs[0], tensor.ctypes.data, tensor.nbytes)
        ok = self._execute(ptrs)
        if not ok:
            raise RuntimeError("TensorRT execution failed")
        output = np.empty(shape, dtype=self._output_dtype)
        self._cuda.memcpy_dtoh(output.ctypes.data, ptrs[1], output.nbytes)
        self._cuda.synchronize()
        boxes, scores, classes = decode_yoloe_output(
            output, source_shape=image.shape[:2], scale=scale, pad=pad, conf_threshold=conf
        )
        result = SimpleNamespace(boxes=SimpleNamespace(xyxy=boxes, conf=scores, cls=classes))
        return [result]

    def _execute(self, ptrs: list[int]) -> bool:
        if self._tensor_api:
            self._context.set_tensor_address(self._input_name, ptrs[self._input_index])
            self._context.set_tensor_address(self._output_name, ptrs[self._output_index])
            return bool(self._context.execute_async_v3(0))
        bindings = [0] * (self._output_index + 1)
        bindings[self._input_index], bindings[self._output_index] = ptrs
        return bool(self._context.execute_v2(bindings))

    def close(self) -> None:
        if self._buffers:
            self._cuda.cudaFree(self._buffers.input_ptr)
            self._cuda.cudaFree(self._buffers.output_ptr)
            self._buffers = None


def _load_cudart() -> Any:
    library = ctypes.util.find_library("cudart") or "libcudart.so"
    raw = ctypes.CDLL(library)

    def check(code: int) -> None:
        if code:
            raise RuntimeError(f"CUDA runtime call failed with code {code}")

    raw.cudaSetDevice.argtypes = [ctypes.c_int]
    raw.cudaMalloc.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t]
    raw.cudaMemcpy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    raw.cudaFree.argtypes = [ctypes.c_void_p]
    raw.cudaDeviceSynchronize.argtypes = []
    cuda_memcpy_host_to_device, cuda_memcpy_device_to_host = 1, 2

    def malloc(size: int) -> int:
        ptr = ctypes.c_void_p()
        check(raw.cudaMalloc(ctypes.byref(ptr), size))
        return int(ptr.value)

    def memcpy_htod(dst: int, src: int, size: int) -> None:
        check(
            raw.cudaMemcpy(
                ctypes.c_void_p(dst), ctypes.c_void_p(src), size, cuda_memcpy_host_to_device
            )
        )

    def memcpy_dtoh(dst: int, src: int, size: int) -> None:
        check(
            raw.cudaMemcpy(
                ctypes.c_void_p(dst), ctypes.c_void_p(src), size, cuda_memcpy_device_to_host
            )
        )

    return SimpleNamespace(
        cudaSetDevice=lambda index: check(raw.cudaSetDevice(index)),
        malloc=malloc,
        cudaFree=lambda ptr: check(raw.cudaFree(ctypes.c_void_p(ptr))),
        memcpy_htod=memcpy_htod,
        memcpy_dtoh=memcpy_dtoh,
        synchronize=lambda: check(raw.cudaDeviceSynchronize()),
    )


__all__ = ["TensorRTProposer", "decode_yoloe_output", "letterbox_rgb", "unwrap_engine_bytes"]
