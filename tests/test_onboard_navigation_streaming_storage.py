"""Small constructed-file/fake torch and tiny real CPU tests; no Qwen/GPU."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import onboard_navigation_streaming_storage as storage


def create_file(tmp_path, header, data, *, raw_header=None, prefix=None):
    raw = raw_header if raw_header is not None else json.dumps(header).encode()
    path = tmp_path / "constructed.safetensors"
    path.write_bytes((prefix if prefix is not None else struct.pack("<Q", len(raw))) + raw + data)
    return path


@pytest.fixture
def sample(tmp_path):
    header = {
        "__metadata__": {"format": "pt", "purpose": "small test"},
        "a": {"dtype": "F32", "shape": [2], "data_offsets": [0, 8]},
        "b": {"dtype": "I16", "shape": [2, 2], "data_offsets": [8, 16]},
    }
    data = struct.pack("<2f4h", 1.25, -2.5, 1, -2, 3, 4)
    return create_file(tmp_path, header, data), header, data


@pytest.fixture
def fake_torch(monkeypatch):
    calls = []

    class Tensor:
        def __init__(self, owner, dtype):
            self.owner, self.dtype, self.shape, self.device = owner, dtype, None, "cpu"

        def reshape(self, shape):
            self.shape = tuple(shape)
            return self

        def to(self, *, device):
            calls.append(("to", device))
            self.device = device
            return self

    def frombuffer(buffer, *, dtype):
        assert type(buffer) is bytearray
        calls.append(("frombuffer", len(buffer), dtype, bytes(buffer)))
        return Tensor(buffer, dtype)

    module = SimpleNamespace(
        frombuffer=frombuffer, **{name: name for name, _size in storage.DTYPES.values()}
    )
    original_import = storage.importlib.import_module

    def importer(name, *args, **kwargs):
        if name == "torch":
            calls.append(("import_torch",))
            return module
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(storage.importlib, "import_module", importer)
    return calls, module


def test_header_metadata_only_no_tensor_or_torch(sample, fake_torch):
    path, header, _data = sample
    calls, _module = fake_torch
    with storage.safe_open(path, "pt", backend="pread") as reader:
        assert reader.keys() == ["a", "b"]
        assert reader.metadata() == header["__metadata__"]
        assert reader.get_slice("a").get_shape() == [2]
        assert reader.get_slice("a").get_dtype() == "F32"
        audit = reader.diagnostics()
        assert audit["tensor_payload_bytes_read"] == 0
        assert audit["total_actual_bytes_read"] == 8 + audit["header_declared_bytes"]
        assert audit["mmap_used"] is False and audit["rust_safe_open_used"] is False
        assert audit["caller_backend_request"] == "pread"
        assert calls == []
    assert reader.diagnostics()["closed"] is True


def test_each_tensor_reads_only_its_exact_payload(sample, fake_torch):
    path, _header, data = sample
    calls, _module = fake_torch
    with storage.safe_open(path, "pt") as reader:
        a = reader.get_tensor("a")
        assert bytes(a.owner) == data[:8] and a.dtype == "float32" and a.shape == (2,)
        b = reader.get_slice("b")[...]
        assert bytes(b.owner) == data[8:] and b.dtype == "int16" and b.shape == (2, 2)
        audit = reader.diagnostics()
        assert audit["tensor_payload_bytes_read"] == 16
        assert audit["tensor_calls_attempted"] == audit["tensor_calls_completed"] == 2
        assert audit["max_single_tensor_payload_bytes_read"] == 8
        assert [item["actual_bytes_read"] for item in audit["tensor_reads"]] == [8, 8]
        assert not any(call[0] == "to" for call in calls)
    assert bytes(a.owner) == data[:8]


def test_get_tensors_is_sequential_but_dictionary_retains_all(sample, fake_torch):
    path, _header, _data = sample
    with storage.safe_open(path, "pt") as reader:
        values = reader.get_tensors()
        assert list(values) == ["a", "b"]
        audit = reader.diagnostics()
        assert audit["tensor_calls_completed"] == 2
        assert audit["get_tensors_calls_completed"] == 1
        assert audit["get_tensors_returned_payload_bytes"] == 16
        assert audit["get_tensors_dictionary_may_retain_all_results"] is True
        assert audit["host_peak_memory_measured"] is False
        assert audit["single_tensor_bound_is_not_host_peak_memory_bound"] is True


@pytest.mark.parametrize(
    "device,expected",
    [
        (None, "cpu"),
        ("cpu", "cpu"),
        ("cpu:0", "cpu:0"),
        (0, "cuda:0"),
        (2, "cuda:2"),
        ("cuda", "cuda"),
        ("cuda:3", "cuda:3"),
    ],
)
def test_requested_device_is_preserved_without_fallback(sample, fake_torch, device, expected):
    path, _header, _data = sample
    calls, _module = fake_torch
    with storage.safe_open(path, "pt", device=device) as reader:
        result = reader.get_tensor("a")
        assert result.device == expected
        assert reader.diagnostics()["requested_device"] == device
        assert reader.diagnostics()["normalized_device"] == expected
        assert [call for call in calls if call[0] == "to"] == (
            [] if expected == "cpu" else [("to", expected)]
        )


@pytest.mark.parametrize("device", [-1, True, 1.5, "mps", "cuda:-1", "CPU", "cuda:foo", object()])
def test_unsupported_device_rejected_before_io(sample, device):
    path, _header, _data = sample
    with pytest.raises(storage.StreamingStorageError):
        storage.safe_open(path, "pt", device=device)


@pytest.mark.parametrize(
    "operation",
    [
        "keys",
        "metadata",
        "get_tensor",
        "get_slice",
        "get_tensors",
        "slice_shape",
        "slice_dtype",
        "slice_read",
        "enter",
    ],
)
def test_close_rejects_new_getters_but_keeps_audit(sample, fake_torch, operation):
    path, _header, _data = sample
    reader = storage.safe_open(path, "pt")
    sliced = reader.get_slice("a")
    reader.close()
    functions = {
        "keys": reader.keys,
        "metadata": reader.metadata,
        "get_tensor": lambda: reader.get_tensor("a"),
        "get_slice": lambda: reader.get_slice("a"),
        "get_tensors": reader.get_tensors,
        "slice_shape": sliced.get_shape,
        "slice_dtype": sliced.get_dtype,
        "slice_read": lambda: sliced[...],
        "enter": reader.__enter__,
    }
    with pytest.raises(storage.StreamingStorageError, match="closed"):
        functions[operation]()
    assert reader.diagnostics()["closed"] is True


@pytest.mark.parametrize("index", [0, slice(None), (slice(None),), None, (Ellipsis,)])
def test_unbound_partial_slice_fails_before_payload(sample, fake_torch, index):
    path, _header, _data = sample
    with storage.safe_open(path, "pt") as reader:
        with pytest.raises(storage.StreamingStorageError, match="full-tensor"):
            reader.get_slice("a")[index]
        assert reader.diagnostics()["tensor_payload_bytes_read"] == 0


@pytest.mark.parametrize(
    "defect",
    [
        "overlap",
        "gap",
        "extra_data",
        "truncated",
        "wrong_bytes",
        "dtype",
        "zero",
        "negative",
        "bool_dim",
        "float_dim",
        "bad_offsets",
        "bool_offset",
        "extra_field",
        "empty",
        "metadata",
        "top_list",
        "cap",
        "rank",
    ],
)
def test_header_fail_closed(tmp_path, defect, monkeypatch):
    header = {
        "a": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]},
        "b": {"dtype": "F32", "shape": [1], "data_offsets": [4, 8]},
    }
    data = bytes(8)
    if defect == "overlap":
        header["b"]["data_offsets"] = [0, 4]
    elif defect == "gap":
        header["b"]["data_offsets"] = [5, 9]
        data += bytes(1)
    elif defect == "extra_data":
        data += bytes(1)
    elif defect == "truncated":
        data = data[:-1]
    elif defect == "wrong_bytes":
        header["a"]["shape"] = [2]
    elif defect == "dtype":
        header["a"]["dtype"] = "F8_E4M3"
    elif defect in ("zero", "negative", "bool_dim", "float_dim"):
        header["a"]["shape"] = [
            {"zero": 0, "negative": -1, "bool_dim": True, "float_dim": 1.0}[defect]
        ]
    elif defect == "bad_offsets":
        header["a"]["data_offsets"] = [0, 4, 5]
    elif defect == "bool_offset":
        header["a"]["data_offsets"] = [False, 4]
    elif defect == "extra_field":
        header["a"]["guess"] = 1
    elif defect == "empty":
        header = {}
    elif defect == "metadata":
        header["__metadata__"] = {"not_string": 1}
    elif defect == "top_list":
        header = []
    elif defect == "cap":
        monkeypatch.setattr(storage, "MAX_TENSOR_BYTES", 3)
    else:
        header["a"]["shape"] = [1] * 33
    path = create_file(tmp_path, header, data)
    with pytest.raises(storage.StreamingStorageError):
        storage.safe_open(path, "pt")


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"v":NaN}',
        b'{"v":[1e309]}',
        b"\xff",
        b'{"a":{"dtype":"F32","dtype":"F16"}}',
    ],
)
def test_strict_header_rejects_duplicates_and_nonfinite(tmp_path, raw):
    path = create_file(tmp_path, {}, b"", raw_header=raw)
    with pytest.raises(storage.StreamingStorageError):
        storage.safe_open(path, "pt")


@pytest.mark.parametrize(
    "prefix",
    [
        b"",
        b"abc",
        struct.pack("<Q", 0),
        struct.pack("<Q", storage.MAX_HEADER_BYTES + 1),
        struct.pack("<Q", 2**64 - 1),
        struct.pack("<Q", 10),
    ],
)
def test_bounded_u64_header_prefix(tmp_path, prefix):
    path = tmp_path / "bad.safetensors"
    path.write_bytes(prefix)
    with pytest.raises(storage.StreamingStorageError):
        storage.safe_open(path, "pt")


@pytest.mark.parametrize("dtype", list(storage.DTYPES))
def test_exact_supported_dtype_mapping(tmp_path, fake_torch, dtype):
    name, size = storage.DTYPES[dtype]
    path = create_file(
        tmp_path, {"scalar": {"dtype": dtype, "shape": [], "data_offsets": [0, size]}}, bytes(size)
    )
    with storage.safe_open(path, "pytorch") as reader:
        tensor = reader.get_tensor("scalar")
        assert tensor.dtype == name and tensor.shape == ()


def test_big_endian_rejected(sample, monkeypatch):
    path, _header, _data = sample
    monkeypatch.setattr(storage.sys, "byteorder", "big")
    with pytest.raises(storage.StreamingStorageError, match="little-endian"):
        storage.safe_open(path, "pt")


def test_file_drift_after_header_is_rejected(sample, fake_torch):
    path, _header, _data = sample
    with storage.safe_open(path, "pt") as reader:
        with path.open("ab") as stream:
            stream.write(b"drift")
        with pytest.raises(storage.StreamingStorageError, match="changed"):
            reader.get_tensor("a")
        assert reader.diagnostics()["tensor_calls_attempted"] == 0


def test_failed_torch_conversion_never_returns_fake_tensor(sample, fake_torch, monkeypatch):
    path, _header, _data = sample
    _calls, module = fake_torch
    monkeypatch.delattr(module, "float32")
    with storage.safe_open(path, "pt") as reader:
        with pytest.raises(storage.StreamingStorageError, match="exact declared dtype"):
            reader.get_tensor("a")
        audit = reader.diagnostics()
        assert audit["tensor_calls_attempted"] == 1 and audit["tensor_calls_completed"] == 0
        assert audit["tensor_payload_bytes_read"] == 8
        assert audit["tensor_reads"][0]["completed"] is False


def test_metadata_shape_and_diagnostics_are_defensive(sample, fake_torch):
    path, _header, _data = sample
    with storage.safe_open(path, "pt") as reader:
        reader.metadata()["format"] = "changed"
        reader.get_slice("a").get_shape()[0] = 100
        reader.get_tensor("a")
        reader.diagnostics()["tensor_reads"][0]["shape"][0] = 99
        assert reader.metadata()["format"] == "pt"
        assert reader.get_slice("a").get_shape() == [2]
        assert reader.diagnostics()["tensor_reads"][0]["shape"] == [2]


def test_context_exception_closes_reader(sample):
    path, _header, _data = sample
    with pytest.raises(RuntimeError):
        with storage.safe_open(path, "pt") as reader:
            raise RuntimeError("caller failure")
    assert reader.diagnostics()["closed"] is True


def test_io_is_unbuffered_read_only_and_unknown_getters_fail(sample, fake_torch, monkeypatch):
    path, _header, _data = sample
    original = Path.open
    opens = []

    def observe(item, *args, **kwargs):
        opens.append((args, kwargs))
        return original(item, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observe)
    with storage.safe_open(path, "pt") as reader:
        with pytest.raises(storage.StreamingStorageError, match="Unknown"):
            reader.get_tensor("missing")
        with pytest.raises(storage.StreamingStorageError, match="Unknown"):
            reader.get_slice("missing")
        reader.get_tensor("a")
    assert opens == [(("rb",), {"buffering": 0})]


def test_truncated_during_tensor_read_is_counted_without_torch(sample, fake_torch):
    path, _header, _data = sample
    calls, _module = fake_torch
    with storage.safe_open(path, "pt") as reader:
        original = reader._stream

        class EarlyEOF:
            closed = False

            def fileno(self):
                return original.fileno()

            def seek(self, offset):
                return original.seek(offset)

            def readinto(self, target):
                if len(target) == 8:
                    return original.readinto(target[:4])
                return 0

            def close(self):
                original.close()
                self.closed = True

        reader._stream = EarlyEOF()
        with pytest.raises(storage.StreamingStorageError, match="Truncated"):
            reader.get_tensor("a")
        audit = reader.diagnostics()
        assert audit["tensor_payload_bytes_read"] == 4
        assert audit["max_single_tensor_payload_bytes_read"] == 4
        assert audit["tensor_calls_attempted"] == 1 and audit["tensor_calls_completed"] == 0
        assert calls == []


def test_get_tensors_failure_does_not_return_partial_fake_dict(sample, fake_torch, monkeypatch):
    path, _header, _data = sample
    _calls, module = fake_torch
    monkeypatch.delattr(module, "int16")
    with storage.safe_open(path, "pt") as reader:
        with pytest.raises(storage.StreamingStorageError):
            reader.get_tensors()
        audit = reader.diagnostics()
        assert audit["get_tensors_calls_attempted"] == 1
        assert audit["get_tensors_calls_completed"] == 0
        assert audit["get_tensors_returned_payload_bytes"] == 0
        assert audit["tensor_calls_completed"] == 1


def test_cuda_to_failure_is_not_cpu_fallback(sample, fake_torch, monkeypatch):
    path, _header, _data = sample
    calls, module = fake_torch
    original = module.frombuffer

    def fail_device(buffer, *, dtype):
        tensor = original(buffer, dtype=dtype)

        def fail(*, device):
            calls.append(("attempted_device", device))
            raise RuntimeError("synthetic CUDA failure, not a real GPU call")

        tensor.to = fail
        return tensor

    monkeypatch.setattr(module, "frombuffer", fail_device)
    with storage.safe_open(path, "pt", device=0) as reader:
        with pytest.raises(RuntimeError, match="synthetic CUDA"):
            reader.get_tensor("a")
        assert reader.diagnostics()["tensor_calls_completed"] == 0
    assert ("attempted_device", "cuda:0") in calls


def test_tiny_real_cpu_tensor_survives_close_and_keeps_bits(sample):
    torch = pytest.importorskip("torch")
    path, _header, data = sample
    with storage.safe_open(path, "pt", device="cpu") as reader:
        tensor = reader.get_tensor("a")
        assert tensor.device.type == "cpu" and tensor.dtype == torch.float32
        assert tensor.shape == (2,)
    assert tensor.tolist() == [1.25, -2.5]
    assert tensor.view(torch.uint8).numpy().tobytes() == data[:8]


def test_tiny_real_cpu_bf16_bits(tmp_path):
    torch = pytest.importorskip("torch")
    data = struct.pack("<2H", 0x3F80, 0xBF00)
    path = create_file(
        tmp_path, {"b": {"dtype": "BF16", "shape": [2], "data_offsets": [0, 4]}}, data
    )
    with storage.safe_open(path, "pt") as reader:
        tensor = reader.get_slice("b")[...]
    assert tensor.dtype == torch.bfloat16 and tensor.device.type == "cpu"
    assert tensor.float().tolist() == [1.0, -0.5]
    assert tensor.view(torch.uint8).numpy().tobytes() == data
