"""Bounded, read-only safetensors compatibility reader without mmap or Rust.

Header inspection uses only the standard library. Materialization lazily imports
torch and reads exactly one declared payload into an owned bytearray. This is a
storage adapter, not flight, transport, model policy or optimizer authority.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import stat
import struct
import sys
import threading
from pathlib import Path

MAX_HEADER_BYTES = 16 * 1024 * 1024
MAX_TENSOR_BYTES = 1024 * 1024 * 1024
DTYPES = {
    "BOOL": ("bool", 1),
    "I8": ("int8", 1),
    "U8": ("uint8", 1),
    "I16": ("int16", 2),
    "I32": ("int32", 4),
    "I64": ("int64", 8),
    "F16": ("float16", 2),
    "BF16": ("bfloat16", 2),
    "F32": ("float32", 4),
    "F64": ("float64", 8),
}


class StreamingStorageError(ValueError):
    """Malformed, unsupported, changed or unavailable read-only storage."""


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise StreamingStorageError("Duplicate JSON key")
            result[key] = value
        return result

    def reject(value):
        raise StreamingStorageError(f"Non-finite JSON constant: {value}")

    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=reject)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise StreamingStorageError("Invalid safetensors JSON header") from exc

    def finite(nested):
        if isinstance(nested, float) and not math.isfinite(nested):
            raise StreamingStorageError("Non-finite JSON exponent")
        if isinstance(nested, dict):
            for item in nested.values():
                finite(item)
        elif isinstance(nested, list):
            for item in nested:
                finite(item)

    try:
        finite(value)
    except RecursionError as exc:
        raise StreamingStorageError("Header nesting exceeds parser bounds") from exc
    return value


def normalize_device(device):
    if device is None:
        return "cpu"
    if type(device) is int and device >= 0:
        return f"cuda:{device}"
    if isinstance(device, str) and re.fullmatch(r"(?:cpu|cuda)(?::[0-9]+)?", device):
        return device
    raise StreamingStorageError("Only CPU/CUDA strings or non-negative CUDA indices are supported")


class StreamingSlice:
    def __init__(self, reader, name):
        self._reader, self._name = reader, name

    def get_shape(self):
        with self._reader._lock:
            self._reader._ensure_open()
            return list(self._reader._entries[self._name]["shape"])

    def get_dtype(self):
        with self._reader._lock:
            self._reader._ensure_open()
            return self._reader._entries[self._name]["dtype"]

    def __getitem__(self, index):
        # Current non-TP Transformers loader only uses param[...]. Do not guess
        # partial-reader semantics or silently materialize unsupported requests.
        if index is not Ellipsis:
            raise StreamingStorageError(
                "Streaming compatibility only admits full-tensor [...] reads"
            )
        return self._reader.get_tensor(self._name)


class StreamingReader:
    def __init__(self, filename, framework, device=None, *, backend=None):
        if sys.byteorder != "little":
            raise StreamingStorageError(
                "Only little-endian native torch tensor storage is admitted"
            )
        if framework not in ("pt", "pytorch"):
            raise StreamingStorageError("Only the admitted PyTorch framework is supported")
        if backend not in (None, "mmap", "pread"):
            raise StreamingStorageError("Unsupported caller backend request")
        self._lock = threading.RLock()
        self._stream = None
        self._closed = True
        self._device = normalize_device(device)
        self._stats = {
            "actual_backend": "stdlib_tensor_streaming_without_mmap_or_rust",
            "caller_backend_request": backend,
            "requested_device": device,
            "normalized_device": self._device,
            "header_bytes_read": 0,
            "tensor_payload_bytes_read": 0,
            "total_actual_bytes_read": 0,
            "tensor_calls_attempted": 0,
            "tensor_calls_completed": 0,
            "max_single_tensor_payload_bytes_read": 0,
            "get_tensors_calls_attempted": 0,
            "get_tensors_calls_completed": 0,
            "get_tensors_returned_payload_bytes": 0,
            "get_tensors_dictionary_may_retain_all_results": False,
            "host_peak_memory_measured": False,
            "single_tensor_bound_is_not_host_peak_memory_bound": True,
            "mmap_used": False,
            "rust_safe_open_used": False,
            "whole_file_buffer_created": False,
            "optimizer_authority": False,
            "flight_authority": False,
            "transport_authority": False,
            "tensor_reads": [],
        }
        try:
            path = Path(filename).resolve(strict=True)
            self._stats["filename"] = str(path)
            # No BufferedReader read-ahead into adjacent tensor payloads.
            self._stream = path.open("rb", buffering=0)
            info = os.fstat(self._stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise StreamingStorageError("Storage must be a regular file")
            self._identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            self._file_size = info.st_size
            prefix = self._read_header(8)
            header_size = struct.unpack("<Q", prefix)[0]
            if not 1 <= header_size <= MAX_HEADER_BYTES or 8 + header_size > self._file_size:
                raise StreamingStorageError("Header size is invalid or exceeds the 16 MiB bound")
            raw = self._read_header(header_size)
            self._data_start = 8 + header_size
            self._entries, self._metadata = self._validate_header(strict_json(raw))
            self._stats.update(
                {
                    "file_bytes": self._file_size,
                    "header_declared_bytes": header_size,
                    "header_sha256": hashlib.sha256(raw).hexdigest(),
                    "declared_payload_bytes": self._file_size - self._data_start,
                    "declared_tensor_count": len(self._entries),
                }
            )
            self._closed = False
            self._ensure_open()
        except BaseException:
            self.close()
            raise

    def _read_header(self, size):
        raw = self._stream.read(size)
        self._stats["header_bytes_read"] += len(raw)
        self._stats["total_actual_bytes_read"] += len(raw)
        if len(raw) != size:
            raise StreamingStorageError("Truncated safetensors header")
        return raw

    def _validate_header(self, header):
        if not isinstance(header, dict):
            raise StreamingStorageError("Header must be an object")
        metadata = header.get("__metadata__")
        if "__metadata__" in header and (
            not isinstance(metadata, dict)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in metadata.items()
            )
        ):
            raise StreamingStorageError("Metadata must contain string keys and values")
        entries = {}
        ranges = []
        data_size = self._file_size - self._data_start
        for name, entry in header.items():
            if name == "__metadata__":
                continue
            if (
                not name
                or not isinstance(entry, dict)
                or set(entry) != {"dtype", "shape", "data_offsets"}
            ):
                raise StreamingStorageError("Invalid tensor declaration")
            dtype, shape, offsets = entry["dtype"], entry["shape"], entry["data_offsets"]
            if not isinstance(dtype, str) or dtype not in DTYPES:
                raise StreamingStorageError("Unsupported tensor dtype")
            if not isinstance(shape, list) or any(
                type(size) is not int or size <= 0 for size in shape
            ):
                raise StreamingStorageError("Invalid or zero-element tensor shape")
            if len(shape) > 32:
                raise StreamingStorageError("Tensor rank exceeds supported bounds")
            size = math.prod(shape) * DTYPES[dtype][1]
            if not 1 <= size <= MAX_TENSOR_BYTES:
                raise StreamingStorageError("Tensor payload exceeds the one GiB bound")
            if (
                not isinstance(offsets, list)
                or len(offsets) != 2
                or any(type(offset) is not int for offset in offsets)
            ):
                raise StreamingStorageError("Offsets must be exactly two integers")
            start, end = offsets
            if not 0 <= start < end <= data_size or end - start != size:
                raise StreamingStorageError("Tensor shape/dtype/offset byte span mismatch")
            entries[name] = {
                "dtype": dtype,
                "shape": tuple(shape),
                "start": start,
                "end": end,
                "bytes": size,
            }
            ranges.append((start, end))
        if not entries:
            raise StreamingStorageError("Empty storage is not admitted")
        expected = 0
        for start, end in sorted(ranges):
            if start != expected:
                raise StreamingStorageError("Tensor ranges overlap or contain gaps")
            expected = end
        if expected != data_size:
            raise StreamingStorageError("Tensor ranges do not exactly cover the complete data span")
        return entries, dict(metadata) if metadata is not None else None

    def _ensure_open(self):
        if self._closed or self._stream is None or self._stream.closed:
            raise StreamingStorageError("Reader is closed")
        info = os.fstat(self._stream.fileno())
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != self._identity:
            raise StreamingStorageError("Storage changed after header admission")

    def keys(self):
        with self._lock:
            self._ensure_open()
            return sorted(self._entries)

    def metadata(self):
        with self._lock:
            self._ensure_open()
            return dict(self._metadata) if self._metadata is not None else None

    def get_slice(self, name):
        with self._lock:
            self._ensure_open()
            if name not in self._entries:
                raise StreamingStorageError("Unknown tensor name")
            return StreamingSlice(self, name)

    def get_tensor(self, name):
        with self._lock:
            self._ensure_open()
            if name not in self._entries:
                raise StreamingStorageError("Unknown tensor name")
            entry = self._entries[name]
            self._stats["tensor_calls_attempted"] += 1
            record = {
                "name": name,
                "dtype": entry["dtype"],
                "shape": list(entry["shape"]),
                "declared_bytes": entry["bytes"],
                "actual_bytes_read": 0,
                "device": self._device,
                "completed": False,
            }
            self._stats["tensor_reads"].append(record)
            payload = bytearray(entry["bytes"])
            self._stream.seek(self._data_start + entry["start"])
            view = memoryview(payload)
            try:
                while record["actual_bytes_read"] < entry["bytes"]:
                    count = self._stream.readinto(view[record["actual_bytes_read"] :])
                    if not count:
                        raise StreamingStorageError("Truncated tensor payload")
                    record["actual_bytes_read"] += count
                    self._stats["tensor_payload_bytes_read"] += count
                    self._stats["total_actual_bytes_read"] += count
                    self._stats["max_single_tensor_payload_bytes_read"] = max(
                        self._stats["max_single_tensor_payload_bytes_read"],
                        record["actual_bytes_read"],
                    )
            finally:
                view.release()
            self._ensure_open()
            torch = importlib.import_module("torch")
            dtype_name = DTYPES[entry["dtype"]][0]
            dtype = getattr(torch, dtype_name, None)
            if dtype is None:
                raise StreamingStorageError("Installed torch lacks the exact declared dtype")
            tensor = torch.frombuffer(payload, dtype=dtype).reshape(entry["shape"])
            if self._device != "cpu":
                tensor = tensor.to(device=self._device)
            self._ensure_open()
            record["completed"] = True
            self._stats["tensor_calls_completed"] += 1
            return tensor

    def get_tensors(self):
        with self._lock:
            self._ensure_open()
            self._stats["get_tensors_calls_attempted"] += 1
            result = {name: self.get_tensor(name) for name in self.keys()}
            self._stats["get_tensors_calls_completed"] += 1
            self._stats["get_tensors_returned_payload_bytes"] += sum(
                entry["bytes"] for entry in self._entries.values()
            )
            self._stats["get_tensors_dictionary_may_retain_all_results"] = True
            return result

    def diagnostics(self):
        """Audit remains readable after close; not a tensor/header getter."""
        with self._lock:
            return {
                **self._stats,
                "closed": self._closed,
                "tensor_reads": [
                    {**item, "shape": list(item["shape"])} for item in self._stats["tensor_reads"]
                ],
            }

    def close(self):
        with self._lock:
            if self._stream is not None:
                self._stream.close()
            self._closed = True

    def __enter__(self):
        with self._lock:
            self._ensure_open()
            return self

    def __exit__(self, *_args):
        self.close()
        return False

    def __del__(self):
        stream = getattr(self, "_stream", None)
        if stream is not None:
            stream.close()


def safe_open(filename, framework, device=None, *, backend=None):
    """Compatibility factory; requested backend is logged, not used for mmap."""
    return StreamingReader(filename, framework, device, backend=backend)
