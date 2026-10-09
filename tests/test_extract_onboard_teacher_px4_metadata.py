"""Bounded CPU/native-output fixtures; no Docker, flight or model execution."""

from __future__ import annotations

import copy
import hashlib
import json
import lzma
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import extract_onboard_teacher_px4_metadata as metadata

TOKEN = "b" * 32
CID = "c" * 64
ZERO_TIME = "0001-01-01T00:00:00Z"


def compressed(raw=b'{"version":1,"nested":{"value":3}}'):
    return lzma.compress(raw, filters=[{"id": lzma.FILTER_LZMA2, "dict_size": 65536}])


class DockerFixture:
    def __init__(self):
        self.calls = []
        self.created = False
        self.removed = False
        self.preexisting = False
        self.state = {"Running": False, "Status": "created", "StartedAt": ZERO_TIME}
        self.identity_changes = {}
        self.fail = None
        self.final_inspect_failure = None
        self.drift = None
        self.output = None

    def result(self, code=0, stdout=b"", stderr=b""):
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)

    def row(self):
        row = {
            "Id": CID,
            "Name": f"/multidetect-metadata-{TOKEN[:16]}",
            "Config": {"Image": metadata.PINNED_IMAGE, "Labels": {metadata.LABEL: TOKEN}},
            "State": copy.deepcopy(self.state),
        }
        for key, value in self.identity_changes.items():
            if key == "label":
                row["Config"]["Labels"][metadata.LABEL] = value
            elif key == "image":
                row["Config"]["Image"] = value
            else:
                row[key] = value
        return row

    def __call__(self, argv, *, timeout_s):
        assert argv[0] == "docker" and timeout_s == 15
        args = argv[1:]
        self.calls.append(args)
        stage = args[0]
        if self.fail == stage:
            return self.result(1, stderr=f"injected {stage} failure".encode())
        if stage == "container":
            if self.removed and self.final_inspect_failure is not None:
                return self.final_inspect_failure
            if self.preexisting or (self.created and not self.removed):
                return self.result(stdout=json.dumps([self.row()]).encode())
            return self.result(1, stderr=f"Error: No such container: {args[2]}\n".encode())
        if stage == "image":
            return self.result(stdout=b'[{"Id":"image-fixture"}]')
        if stage == "create":
            self.created = True
            return self.result(stdout=(CID + "\n").encode())
        if stage == "cp":
            assert args[1].startswith(CID + ":/opt/px4-gazebo/etc/extras/")
            Path(args[2]).write_bytes(compressed())
            self.output = Path(args[2]).parent
            return self.result()
        if stage == "rm":
            assert args[1] == CID
            if self.drift is not None:
                self.drift(self.output)
            self.removed = True
            self.preexisting = False
            return self.result()
        raise AssertionError(f"Unexpected Docker operation: {args}")


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, "ROOT", tmp_path)
    monkeypatch.setattr(metadata, "uuid4", lambda: SimpleNamespace(hex=TOKEN))
    directory = tmp_path / "artifacts/diagnostics/new-extraction"
    directory.parent.mkdir(parents=True)
    return directory, DockerFixture()


def execute(fixture, **kwargs):
    directory, docker = fixture
    report = metadata.extract_metadata(directory, execute=True, runner=docker, **kwargs)
    return directory, docker, report


def test_default_has_no_docker_or_outputs(fixture):
    directory, docker = fixture
    report = metadata.extract_metadata(directory, runner=docker)
    assert report["status"] == "planned" and report["execute_requested"] is False
    assert docker.calls == [] and not directory.exists()
    assert report["files"] == {} and not any(report["claims"].values())


@pytest.mark.parametrize("execute_value", [None, 0, 1, "yes"])
def test_execute_requires_exact_bool(fixture, execute_value):
    directory, docker = fixture
    with pytest.raises(TypeError):
        metadata.extract_metadata(directory, execute=execute_value, runner=docker)
    assert docker.calls == [] and not directory.exists()


@pytest.mark.parametrize("limit", [None, True, 64.0, 0, 127, 256])
def test_memory_limit_explicit_and_preoperation(fixture, limit):
    directory, docker = fixture
    with pytest.raises(ValueError):
        metadata.extract_metadata(directory, execute=True, runner=docker, memory_limit_mib=limit)
    assert docker.calls == [] and not directory.exists()


def test_existing_or_escape_output_refused_before_docker(fixture):
    directory, docker = fixture
    directory.mkdir()
    sentinel = directory / "sentinel"
    sentinel.write_bytes(b"preserve")
    with pytest.raises(ValueError):
        metadata.extract_metadata(directory, execute=True, runner=docker)
    with pytest.raises(ValueError):
        metadata.extract_metadata(directory.parents[2] / "outside", execute=True, runner=docker)
    assert sentinel.read_bytes() == b"preserve" and docker.calls == []


def test_success_pinned_never_started_only_and_exact_bytes(fixture):
    directory, docker, report = execute(fixture, memory_limit_mib=128)
    assert report["status"] == "pinned-image-metadata-extracted"
    assert report["cleanup_verified"] is True and report["container_started"] is False
    assert report["metadata_decoder_memory_limit_mib"] == 128
    assert not report["actual_live_firmware_verified"]
    assert not report["actual_parameter_values_observed"]
    assert not any(report["claims"].values())
    assert len(report["files"]) == 3 and report["container_id"] == CID
    assert report["final_metadata_sources_reverified"] is True
    assert not any(args[0] in {"run", "start", "exec", "pull", "stop"} for args in docker.calls)
    create = next(args for args in docker.calls if args[0] == "create")
    assert create[-1] == metadata.PINNED_IMAGE
    assert create[create.index("--network") + 1] == "none"
    assert create[create.index("--entrypoint") + 1] == "/bin/false"
    for record in report["files"].values():
        for kind in ("compressed", "expanded"):
            binding = record[kind]
            raw = Path(binding["path"]).read_bytes()
            assert len(raw) == binding["bytes"]
            assert hashlib.sha256(raw).hexdigest() == binding["sha256"]
    persisted = json.loads((directory / "metadata-extraction.json").read_bytes())
    assert persisted["files"] == report["files"] and persisted["error"] is None


@pytest.mark.parametrize(
    "state",
    [
        {"Running": False, "Status": "exited", "StartedAt": "2026-10-08T00:00:00Z"},
        {"Running": False, "Status": "created", "StartedAt": "2026-10-08T00:00:00Z"},
        {"Running": False, "Status": "paused", "StartedAt": ZERO_TIME},
        {"Running": False, "Status": "created"},
        {"Running": False, "StartedAt": ZERO_TIME},
        {"Running": True, "Status": "created", "StartedAt": ZERO_TIME},
    ],
)
def test_never_started_evidence_is_strict_and_no_unsafe_remove(fixture, state):
    fixture[1].state = state
    _, docker, report = execute(fixture)
    assert report["status"] == "metadata-extraction-failed"
    assert report["cleanup_verified"] is False
    assert not any(args[0] in {"cp", "rm"} for args in docker.calls)
    assert report["files"] == {}


def test_even_matching_preexisting_owner_is_never_removed(fixture):
    fixture[1].preexisting = True
    _, docker, report = execute(fixture)
    assert report["status"] == "metadata-extraction-failed"
    assert "Pre-existing" in report["error"]
    assert not any(args[0] in {"create", "cp", "rm"} for args in docker.calls)


@pytest.mark.parametrize(
    "field,value",
    [("label", "a" * 32), ("image", "other:latest"), ("Id", "e" * 64), ("Name", "/unowned")],
)
def test_mismatched_identity_never_copies_or_removes(fixture, field, value):
    fixture[1].identity_changes[field] = value
    _, docker, report = execute(fixture)
    assert report["status"] == "metadata-extraction-failed"
    assert not report["cleanup_verified"]
    assert not any(args[0] in {"cp", "rm"} for args in docker.calls)


@pytest.mark.parametrize("stage", ["image", "create", "cp"])
def test_native_failure_persists_primary_with_false_claims(fixture, stage):
    fixture[1].fail = stage
    directory, _, report = execute(fixture)
    assert report["status"] == "metadata-extraction-failed" and report["error"]
    assert not any(report["claims"].values())
    assert (directory / "metadata-extraction.json").is_file()


def test_cleanup_error_does_not_replace_primary(fixture):
    directory, docker = fixture
    original = docker.__call__

    def runner(argv, *, timeout_s):
        if argv[1] == "cp":
            return docker.result(1, stderr=b"primary-copy-failure")
        if argv[1] == "rm":
            raise subprocess.TimeoutExpired(argv, timeout_s)
        return original(argv, timeout_s=timeout_s)

    report = metadata.extract_metadata(directory, execute=True, runner=runner)
    assert "copy failed" in report["error"]
    assert "TimeoutExpired" in report["cleanup_error"]
    assert not report["cleanup_verified"] and report["status"] == "metadata-extraction-failed"


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(returncode=1, stdout=b"", stderr=b"daemon unavailable"),
        SimpleNamespace(returncode=1, stdout=b"", stderr=b"No such container: wrong-id"),
        SimpleNamespace(returncode=0, stdout=b"x" * (256 * 1024 + 1), stderr=b""),
    ],
)
def test_final_absence_requires_exact_evidence(fixture, result):
    fixture[1].final_inspect_failure = result
    _, _, report = execute(fixture)
    assert not report["cleanup_verified"] and report["status"] == "metadata-extraction-failed"
    assert report["cleanup_error"]


@pytest.mark.parametrize("invalid", ["", 0, False, []])
def test_falsey_native_nonbytes_is_not_repaired(fixture, invalid):
    directory, docker = fixture

    def runner(argv, *, timeout_s):
        if argv[1] == "image":
            return docker.result(stdout=invalid)
        return docker(argv, timeout_s=timeout_s)

    report = metadata.extract_metadata(directory, execute=True, runner=runner)
    assert report["status"] == "metadata-extraction-failed"
    assert "bytes" in report["error"]


@pytest.mark.parametrize("filename", ["all_events.json.xz", "parameters.json"])
def test_late_metadata_drift_is_not_successful(fixture, filename):
    fixture[1].drift = lambda output: (output / filename).write_bytes(b"changed")
    _, _, report = execute(fixture)
    assert report["status"] == "metadata-extraction-failed"
    assert report["final_metadata_sources_reverified"] is False
    assert report["source_verification_error"]
    assert report["cleanup_verified"] is True


def test_same_size_post_copy_drift_preserves_original_declared_hash(fixture):
    original = compressed()
    fixture[1].drift = lambda output: (output / "all_events.json.xz").write_bytes(
        b"x" * len(original)
    )
    _, _, report = execute(fixture)
    assert report["status"] == "metadata-extraction-failed"
    assert "hash" in report["source_verification_error"]
    assert report["files"]["all_events.json.xz"]["compressed"]["sha256"] == (
        hashlib.sha256(original).hexdigest()
    )


def test_native_timeout_does_not_prove_absence_or_create(fixture):
    directory, docker = fixture

    def runner(argv, *, timeout_s):
        raise subprocess.TimeoutExpired(argv, timeout_s)

    report = metadata.extract_metadata(directory, execute=True, runner=runner)
    assert report["status"] == "metadata-extraction-failed"
    assert "TimeoutExpired" in report["error"]
    assert not report["cleanup_verified"] and docker.calls == []


def test_uncertain_create_timeout_only_removes_exact_owned_created_id(fixture):
    directory, docker = fixture

    def runner(argv, *, timeout_s):
        if argv[1] == "create":
            docker(argv, timeout_s=timeout_s)
            raise subprocess.TimeoutExpired(argv, timeout_s)
        return docker(argv, timeout_s=timeout_s)

    report = metadata.extract_metadata(directory, execute=True, runner=runner)
    assert "TimeoutExpired" in report["error"]
    assert report["cleanup_verified"] is True
    assert report["status"] == "metadata-extraction-failed"
    assert [args for args in docker.calls if args[0] == "rm"] == [["rm", CID]]


def test_final_inspect_timeout_does_not_claim_absence(fixture):
    directory, docker = fixture

    def runner(argv, *, timeout_s):
        if argv[1] == "container" and docker.removed:
            raise subprocess.TimeoutExpired(argv, timeout_s)
        return docker(argv, timeout_s=timeout_s)

    report = metadata.extract_metadata(directory, execute=True, runner=runner)
    assert report["status"] == "metadata-extraction-failed"
    assert not report["cleanup_verified"] and "TimeoutExpired" in report["cleanup_error"]


@pytest.mark.parametrize("limit", [64, 128])
def test_expand_cpu_fixture_exact_original_bytes(limit):
    raw = b'{"unicode":"\xe8\x88\xaa\xe7\xba\xbf","integer":18446744073709551615}'
    expanded, value = metadata._expand(compressed(raw), memory_limit_mib=limit)
    assert expanded == raw and value["integer"] == 18446744073709551615


@pytest.mark.parametrize(
    "raw",
    [
        b'{"x":1,"x":2}',
        b'{"nested":{"x":1,"x":2}}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":1e999}',
        b'{"x":[-1e999]}',
        b"[]",
        b"null",
        b"1",
        b'{"x":"\xff"}',
    ],
)
def test_expand_strict_json_rejects_ambiguous_or_nonfinite(raw):
    with pytest.raises((ValueError, UnicodeError)):
        metadata._expand(compressed(raw))


@pytest.mark.parametrize("suffix", ["truncated", "concatenated", "garbage"])
def test_expand_requires_complete_single_xz_stream(suffix):
    raw = compressed()
    invalid = {"truncated": raw[:-4], "concatenated": raw + raw, "garbage": raw + b"x"}
    with pytest.raises(ValueError):
        metadata._expand(invalid[suffix])


def test_expand_caps_compressed_and_uncompressed_bytes(monkeypatch):
    with pytest.raises(ValueError):
        metadata._expand(b"")
    monkeypatch.setattr(metadata, "MAX_COMPRESSED", 10)
    with pytest.raises(ValueError):
        metadata._expand(b"x" * 11)
    monkeypatch.setattr(metadata, "MAX_COMPRESSED", 1024)
    monkeypatch.setattr(metadata, "MAX_DECOMPRESSED", 16)
    with pytest.raises(ValueError):
        metadata._expand(compressed(b'{"value":"a-long-string"}'))


@pytest.mark.parametrize("filename", metadata.FILES)
def test_retained_actual_r2_expand128_only_and_original_binding(filename):
    root = Path(__file__).resolve().parents[1]
    directory = root / "artifacts/diagnostics/onboard-teacher-px4-image-metadata-20261008-r2"
    receipt = directory / "metadata-extraction.json"
    if not receipt.is_file():
        pytest.skip("Retained local actual metadata artifacts absent in clean checkout")
    report = json.loads(receipt.read_bytes())
    record = report["files"][filename]
    raw = (directory / filename).read_bytes()
    assert len(raw) == record["compressed"]["bytes"]
    assert hashlib.sha256(raw).hexdigest() == record["compressed"]["sha256"]
    expanded, value = metadata._expand(raw, memory_limit_mib=128)
    assert len(expanded) == record["expanded"]["bytes"]
    assert hashlib.sha256(expanded).hexdigest() == record["expanded"]["sha256"]
    assert sorted(value) == record["top_level_keys"]


def test_cli_default_stdlib_only_no_docker_or_outputs(tmp_path):
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys;sys.path[:0]=[str(ROOT),str(ROOT/'src')];"
        "from scripts import extract_onboard_teacher_px4_metadata as m;"
        "from unittest.mock import patch;"
        "deny=lambda *a,**k:(_ for _ in ()).throw(AssertionError('Docker invoked'));"
        "m.ROOT=TEMP;"
        "patcher=patch.object(m.subprocess,'run',deny);patcher.start();"
        "assert m.main(['--output-directory',str(TEMP/'artifacts/diagnostics/no-output')])==0;"
        "assert not (TEMP/'artifacts/diagnostics/no-output').exists();"
        "assert not any(n in sys.modules for n in "
        "['torch','transformers','PIL','pymavlink','pyarrow','datasets'])"
    )
    bootstrap = f"from pathlib import Path;ROOT=Path({str(root)!r});TEMP=Path({str(tmp_path)!r});"
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", bootstrap + code],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "planned"
