"""Small standard-library/fake-reader checks: no real Qwen, GPU or safetensors."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import probe_onboard_navigation_checkpoint_storage as probe


@pytest.fixture
def small_checkpoint(tmp_path, monkeypatch):
    values = struct.pack("<8H", *range(1, 9))
    info = {"dtype": "BF16", "shape": [1, 8], "data_offsets": [0, 16]}
    raw_header = json.dumps({"__metadata__": {"format": "pt"}, probe.TENSOR_NAME: info}).encode()
    contents = struct.pack("<Q", len(raw_header)) + raw_header + values
    snapshot = tmp_path / "sealed-snapshot"
    snapshot.mkdir()
    path = snapshot / "small-original.safetensors"
    path.write_bytes(contents)
    monkeypatch.setattr(probe, "CHECKPOINT_BYTES", len(contents))
    monkeypatch.setattr(probe, "CHECKPOINT_SHA256", hashlib.sha256(contents).hexdigest())
    monkeypatch.setattr(probe, "HEADER_BYTES", len(raw_header))
    monkeypatch.setattr(probe, "HEADER_SHA256", hashlib.sha256(raw_header).hexdigest())
    monkeypatch.setattr(probe, "TENSOR_INFO", info)
    monkeypatch.setattr(probe, "SLICE_SHA256", hashlib.sha256(values).hexdigest())
    return path, values


@pytest.fixture
def invocation(small_checkpoint, tmp_path):
    path, _values = small_checkpoint
    return SimpleNamespace(
        python=Path(sys.executable),
        checkpoint=path,
        source_sha256=probe.file_record(probe.__file__)["sha256"],
        output_dir=tmp_path / "new-result",
        backend="default",
        expected_safetensors_version="0.4.5",
        timeout_seconds=10,
        worker_phase="slice",
    )


def stub_command(monkeypatch, *, status=0, mutate=None, omit_completion=False):
    def command(args, phase):
        code = (
            "import json,os;print(json.dumps({'event':'worker_started','pid':os.getpid(),"
            f"'parent_pid':os.getppid(),'phase':{phase!r}}}),flush=True);"
            "print('raw diagnostic stdout',flush=True);"
            "import sys;print('raw diagnostic stderr',file=sys.stderr,flush=True);"
        )
        metadata = {
            "event": "reader_metadata_verified",
            "phase": phase,
            "tensor_name": probe.TENSOR_NAME,
            "shape": probe.TENSOR_INFO["shape"],
            "dtype": "BF16",
        }
        code += f"print({json.dumps(metadata)!r},flush=True);"
        if phase == "slice":
            verified = {
                "event": "first_eight_cpu_slice_verified",
                "phase": phase,
                "slice_sha256": probe.SLICE_SHA256,
                "elements": 8,
                "bytes": 16,
            }
            code += f"print({json.dumps(verified)!r},flush=True);"
        if mutate is not None:
            code += "from pathlib import Path;Path(sys.argv[1]).write_bytes(b'drift');"
        if not omit_completion:
            completed = {
                "event": "worker_completed",
                "phase": phase,
                "source_and_header_verified": True,
                **probe.CLAIMS,
            }
            code += f"print({json.dumps(completed)!r},flush=True);"
        code += f"sys.exit({status})"
        return [sys.executable, "-u", "-B", "-c", code, str(args.checkpoint)]

    monkeypatch.setattr(probe, "child_command", command)


def report(args):
    return json.loads((args.output_dir / "report.json").read_bytes())


def test_original_header_and_exact_gold(small_checkpoint):
    path, _values = small_checkpoint
    record, header = probe.verify_checkpoint(path)
    assert record["sha256"] == probe.CHECKPOINT_SHA256
    assert header["output_elements"] == 8
    assert header["output_bytes"] == 16
    assert header["cpu_pread_may_read_whole_tensor"] is True
    assert header["initial_header_mmap_not_claimed_avoided"] is True


@pytest.mark.parametrize("part", ["prefix", "header", "gold", "size"])
def test_pinned_checkpoint_rejects_drift(small_checkpoint, part):
    path, _values = small_checkpoint
    raw = bytearray(path.read_bytes())
    index = {"prefix": 0, "header": 10, "gold": len(raw) - 1}.get(part)
    if part == "size":
        raw += b"extra"
    else:
        raw[index] ^= 1
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        probe.verify_checkpoint(path)


def test_header_cannot_change_first_tensor(small_checkpoint, monkeypatch):
    path, _values = small_checkpoint
    monkeypatch.setattr(probe, "TENSOR_NAME", "unbound_tensor")
    with pytest.raises(ValueError, match="identity"):
        probe.inspect_header(path)


@pytest.mark.parametrize(
    "raw", [b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":[1e309]}']
)
def test_strict_json_rejects_bad_values(raw):
    with pytest.raises(ValueError):
        probe.strict_json(raw)


def test_stdlib_supervisor_two_phases_capture_native_output(invocation, monkeypatch):
    stub_command(monkeypatch)
    assert probe.supervise(invocation) == 0
    result = report(invocation)
    assert [item["phase"] for item in result["phases"]] == ["metadata", "slice"]
    assert result["source_and_checkpoint_unchanged"] is True
    assert result["completed"] is True
    for phase in result["phases"]:
        assert phase["returncode"] == 0
        assert phase["worker_pid"] > 0
        assert phase["worker_parent_pid"] in (os.getpid(), phase["launcher_pid"])
        assert phase["worker_pid_basis"].endswith("not independently verified by OS")
        assert phase["stdout"]["sha256"] == probe.file_record(phase["stdout"]["path"])["sha256"]
        assert b"raw diagnostic stderr" in Path(phase["stderr"]["path"]).read_bytes()
        launch = json.loads((invocation.output_dir / phase["phase"] / "launch.json").read_bytes())
        assert launch["cuda_visible_devices"] == ""
    assert all(value is False for key, value in result.items() if key in probe.CLAIMS)


def test_create_only_does_not_overwrite(invocation, monkeypatch):
    stub_command(monkeypatch)
    assert probe.supervise(invocation) == 0
    before = (invocation.output_dir / "report.json").read_bytes()
    with pytest.raises(FileExistsError):
        probe.supervise(invocation)
    assert (invocation.output_dir / "report.json").read_bytes() == before


@pytest.mark.parametrize("target", ["snapshot", "nested", "ancestor"])
def test_output_cannot_pollute_sealed_snapshot(invocation, target):
    snapshot = invocation.checkpoint.parent
    before = sorted(item.name for item in snapshot.iterdir())
    invocation.output_dir = {
        "snapshot": snapshot,
        "nested": snapshot / "new" / "probe",
        "ancestor": snapshot.parent,
    }[target]
    with pytest.raises(ValueError, match="sealed checkpoint"):
        probe.supervise(invocation)
    assert sorted(item.name for item in snapshot.iterdir()) == before
    assert not (snapshot / "new").exists()


def test_launch_record_failure_still_reaps_owned_process(invocation, monkeypatch):
    stub_command(monkeypatch)
    original_popen, original_create = probe.subprocess.Popen, probe.create_json
    owned = []

    def capture(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        owned.append(process)
        return process

    def fail_launch(path, value):
        if Path(path).name == "launch.json":
            raise OSError("injected launch-record failure")
        return original_create(path, value)

    monkeypatch.setattr(probe.subprocess, "Popen", capture)
    monkeypatch.setattr(probe, "create_json", fail_launch)
    assert probe.supervise(invocation) == 1
    assert len(owned) == 1 and owned[0].poll() == 0
    result = report(invocation)
    phase = result["phases"][0]
    assert phase["returncode"] == 0
    assert phase["launch_record_error"]["type"] == "OSError"
    assert phase["phase_events_verified"] is True
    assert phase["completed"] is False
    assert Path(phase["stdout"]["path"]).is_file()
    assert Path(phase["stderr"]["path"]).is_file()


def valid_events(phase):
    events = [
        {"event": "worker_started", "phase": phase, "pid": 100, "parent_pid": 90},
        {
            "event": "reader_metadata_verified",
            "phase": phase,
            "tensor_name": probe.TENSOR_NAME,
            "shape": probe.TENSOR_INFO["shape"],
            "dtype": "BF16",
        },
    ]
    if phase == "slice":
        events.append(
            {
                "event": "first_eight_cpu_slice_verified",
                "phase": phase,
                "slice_sha256": probe.SLICE_SHA256,
                "elements": 8,
                "bytes": 16,
            }
        )
    events.append(
        {
            "event": "worker_completed",
            "phase": phase,
            "source_and_header_verified": True,
            **probe.CLAIMS,
        }
    )
    return events


@pytest.mark.parametrize("phase", ["metadata", "slice"])
def test_exact_phase_event_protocol(phase):
    assert probe.phase_events_verified(valid_events(phase), phase) is True


@pytest.mark.parametrize(
    "defect",
    [
        "missing_start",
        "duplicate_start",
        "missing_metadata",
        "wrong_phase",
        "missing_gold",
        "wrong_gold",
        "wrong_bytes",
        "no_source_receipt",
        "claim",
        "order",
        "pid",
    ],
)
def test_phase_protocol_rejects_incomplete_or_wrong_receipts(defect):
    events = valid_events("slice")
    if defect == "missing_start":
        events.pop(0)
    elif defect == "duplicate_start":
        events.insert(1, events[0].copy())
    elif defect == "missing_metadata":
        events.pop(1)
    elif defect == "wrong_phase":
        events[-1]["phase"] = "metadata"
    elif defect == "missing_gold":
        events.pop(2)
    elif defect == "wrong_gold":
        events[2]["slice_sha256"] = "0" * 64
    elif defect == "wrong_bytes":
        events[2]["bytes"] = 32
    elif defect == "no_source_receipt":
        events[-1]["source_and_header_verified"] = False
    elif defect == "claim":
        events[-1]["gpu_used"] = True
    elif defect == "order":
        events[1], events[2] = events[2], events[1]
    else:
        events[0]["pid"] = True
    assert probe.phase_events_verified(events, "slice") is False


def test_metadata_phase_cannot_claim_slice_receipt():
    assert probe.phase_events_verified(valid_events("slice"), "metadata") is False


def test_failure_retains_raw_and_skips_repeated_slice(invocation, monkeypatch):
    stub_command(monkeypatch, status=17, omit_completion=True)
    assert probe.supervise(invocation) == 1
    result = report(invocation)
    assert len(result["phases"]) == 1
    assert result["phases"][0]["returncode"] == 17
    assert result["remaining_phases_skipped_after_failure"] is True
    assert result["checkpoint_after"] == result["checkpoint_before"]
    assert result["completed"] is False


def test_exit_zero_without_completion_is_not_success(invocation, monkeypatch):
    stub_command(monkeypatch, omit_completion=True)
    assert probe.supervise(invocation) == 1
    assert report(invocation)["phases"][0]["returncode"] == 0
    assert report(invocation)["completed"] is False


def test_checkpoint_drift_during_worker_is_caught(invocation, monkeypatch):
    stub_command(monkeypatch, mutate=True)
    assert probe.supervise(invocation) == 1
    result = report(invocation)
    assert result["checkpoint_after"] != result["checkpoint_before"]
    assert result["source_and_checkpoint_unchanged"] is False
    assert result["completed"] is False


@pytest.mark.parametrize("change", ["sha", "pread_version", "timeout"])
def test_prelaunch_wrong_source_or_policy_rejected(invocation, change):
    if change == "sha":
        invocation.source_sha256 = "0" * 64
    elif change == "pread_version":
        invocation.backend = "pread"
    else:
        invocation.timeout_seconds = 301
    with pytest.raises(ValueError):
        probe.supervise(invocation)
    assert not invocation.output_dir.exists()


@pytest.fixture
def fake_reader(invocation, small_checkpoint, monkeypatch):
    _path, raw = small_checkpoint
    calls = []
    reader_shape = list(probe.TENSOR_INFO["shape"])

    class Tensor:
        shape = [1, 8]
        dtype = "BF16-torch"
        device = SimpleNamespace(type="cpu")

        def numel(self):
            return 8

        def detach(self):
            return self

        def contiguous(self):
            return self

        def view(self, dtype):
            assert dtype == "uint8-torch"
            return self

        def numpy(self):
            return self

        def tobytes(self):
            return raw

        def float(self):
            return self

        def tolist(self):
            return [[0.0] * 8]

    class Slice:
        def get_shape(self):
            return reader_shape

        def get_dtype(self):
            return "BF16"

        def __getitem__(self, key):
            calls.append(("slice", key))
            assert key == (slice(0, 1), slice(0, 8))
            return Tensor()

    class Reader:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_slice(self, name):
            assert name == probe.TENSOR_NAME
            return Slice()

    def safe_open(path, **kwargs):
        calls.append(("open", kwargs))
        assert path == str(invocation.checkpoint)
        assert kwargs["device"] == "cpu"
        return Reader()

    monkeypatch.setitem(
        sys.modules,
        "safetensors",
        SimpleNamespace(
            safe_open=safe_open,
            __file__="fake-safetensors",
            __version__=invocation.expected_safetensors_version,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            bfloat16="BF16-torch",
            uint8="uint8-torch",
            __file__="fake-torch",
            __version__="fake-torch",
        ),
    )
    monkeypatch.setattr(
        probe.importlib.metadata,
        "version",
        lambda name: (
            invocation.expected_safetensors_version if name == "safetensors" else "fake-torch"
        ),
    )
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    return calls, Tensor, reader_shape


@pytest.mark.parametrize("phase", ["metadata", "slice"])
@pytest.mark.parametrize("backend", ["default", "mmap", "pread"])
def test_worker_exact_cpu_read_and_explicit_backend(
    invocation, fake_reader, phase, backend, capsys
):
    calls, _tensor, _shape = fake_reader
    invocation.worker_phase = phase
    invocation.backend = backend
    assert probe.worker(invocation) == 0
    kwargs = calls[0][1]
    assert kwargs["framework"] == "pt"
    assert ("backend" not in kwargs) if backend == "default" else kwargs["backend"] == backend
    assert len([call for call in calls if call[0] == "slice"]) == (1 if phase == "slice" else 0)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["event"] == "worker_completed"
    if phase == "slice":
        assert events[-2]["slice_sha256"] == probe.SLICE_SHA256


@pytest.mark.parametrize(
    "failure", ["cuda_visible", "version", "shape", "dtype", "device", "numel", "gold"]
)
def test_worker_fail_closed_limits(invocation, fake_reader, failure, monkeypatch):
    _calls, tensor, shape = fake_reader
    if failure == "cuda_visible":
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    elif failure == "version":
        monkeypatch.setattr(probe.importlib.metadata, "version", lambda name: "wrong")
    elif failure == "shape":
        shape[1] = 9
    elif failure == "dtype":
        tensor.dtype = "float32"
    elif failure == "device":
        tensor.device = SimpleNamespace(type="cuda")
    elif failure == "numel":
        monkeypatch.setattr(tensor, "numel", lambda self: 9)
    else:
        monkeypatch.setattr(tensor, "tobytes", lambda self: b"wrong")
    with pytest.raises(ValueError):
        probe.worker(invocation)


def test_child_command_is_explicit_cpu_worker(invocation):
    command = probe.child_command(invocation, "metadata")
    assert command[0] == str(invocation.python)
    assert "faulthandler" in command
    assert command[command.index("--worker-phase") + 1] == "metadata"
    assert "--checkpoint" in command and "--source-sha256" in command
