"""Injected CPU diagnostics only; never start Docker, PX4, sockets or a model."""

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import probe_onboard_teacher_gazebo_navref as probe
from scripts.onboard_teacher_sim_operator import SourceBinding


def native_result(raw=b"0\n", **updates):
    result = {
        "native_exit_code": 0,
        "owned_process_id": 1234,
        "owned_process_exited": True,
        "reader_threads_exited": True,
        "timeout": False,
        "owned_process_killed": False,
        "complete_output_observed": True,
        "output_limit_bytes": 256 * 1024,
        "prefix_limit_bytes_per_stream": 128 * 1024,
        "streams": {
            label: {
                "prefix": value,
                "observed_bytes": len(value),
                "EOF": True,
                "error": None,
                "all_bytes_retained": True,
                "full_output_sha256": hashlib.sha256(value).hexdigest(),
            }
            for label, value in (("stdout", raw), ("stderr", b""))
        },
    }
    result.update(updates)
    return result


@pytest.fixture
def h(tmp_path, monkeypatch):
    events, instances = [], []
    config = SimpleNamespace(
        cleanup_error=None, owner_changed=False, receiver_alive=False, queued=0, camera_alive=False
    )

    class Clock:
        session_id = "cpu-same-clock"
        now = 50.0

        def monotonic(self):
            self.now += 0.01
            return self.now

    plan = SimpleNamespace(
        docker_plan=SimpleNamespace(docker_executable="CPU-docker-not-called"),
        to_mapping=lambda: {"CPU_fixture": True, "flight_authority": False},
    )

    def initialize(session, selected, *, execute):
        assert selected is plan
        session.plan, session.execute = selected, execute
        session._entered, session._closed = True, False
        session.container_id = "d" * 64
        session.clock = Clock()
        session.receiver = SimpleNamespace(
            to_mapping=lambda: {
                "thread_alive": config.receiver_alive,
                "queued_packets": config.queued,
            }
        )
        session._inspect = lambda identity: {"Id": identity}

        def owned(row):
            events.append("inspect-owned")
            return "e" * 64 if config.owner_changed else row["Id"]

        session._owned, session._remaining = owned, lambda: 50
        instances.append(session)

    def base_exit(session, kind, body, traceback):
        events.append("real-base-cleanup")
        session._closed = True
        if config.cleanup_error:
            raise RuntimeError(config.cleanup_error)
        return False

    def base_mapping(session):
        return {
            "CPU_fixture": True,
            "cleanup_verified": session._closed,
            "camera_thread_alive": config.camera_alive,
            "flight_authority": False,
        }

    monkeypatch.setattr(probe.OwnedTeacherGazeboSessionV5, "__init__", initialize)
    monkeypatch.setattr(probe.OwnedTeacherGazeboSessionV5, "__exit__", base_exit)
    monkeypatch.setattr(probe.OwnedTeacherGazeboSessionV5, "to_mapping", base_mapping)
    calls = []

    def runner(command, *, timeout_s, prefix_bytes, output_limit_bytes):
        assert timeout_s == 10 and prefix_bytes == 128 * 1024
        assert output_limit_bytes == 256 * 1024
        events.append("probe-native")
        calls.append(command)
        return native_result()

    intent = probe.DisposableSimOperatorIntent(
        "old-CPU-experiment",
        "CPU-container",
        tuple(SourceBinding("unused", 0, hashlib.sha256(b"").hexdigest()) for _ in range(5)),
        True,
    )
    return SimpleNamespace(
        root=tmp_path,
        output=tmp_path / "output",
        plan=plan,
        events=events,
        instances=instances,
        config=config,
        calls=calls,
        runner=runner,
        intent=intent,
    )


def session(h, runner=None):
    return probe.NavrefProbeSession(
        h.plan,
        execute=True,
        probe_directory=h.output,
        probe_runner=h.runner if runner is None else runner,
    )


def test_normal_all_fixed_native_reads_precede_real_cleanup(h):
    selected = session(h)
    assert selected.__exit__(None, None, None) is False
    assert selected.navref_probe["status"] == "recorded"
    assert len(h.calls) == len(probe.SPECS) == 18
    assert h.events[-1] == "real-base-cleanup"
    assert h.events.count("inspect-owned") == 36
    assert len(list(h.output.glob("attempt-*.json"))) == 18
    for row, (name, args) in zip(selected.navref_probe["commands"], probe.SPECS, strict=True):
        assert row["command"] == ["CPU-docker-not-called", "exec", "d" * 64, *args]
        assert row["name"] == name and row["API_attempted"] and row["API_returned"]
        assert row["clock_id"] == "cpu-same-clock"
        assert row["finished_at_s"] >= row["started_at_s"]
        assert row["origin_qualified"] is False
        assert (
            json.loads(Path(row["before_API_receipt"]["path"]).read_bytes())["API_attempted"]
            is False
        )
        for item in row["streams"].values():
            binding = item["original_bytes"]
            raw = Path(binding["path"]).read_bytes()
            assert len(raw) == binding["bytes"]
            assert hashlib.sha256(raw).hexdigest() == binding["sha256"]


def test_body_memory_error_skips_probe_and_preserves_primary_even_cleanup_fails(h):
    selected = session(h)
    original = MemoryError("original body")
    h.config.cleanup_error = "secondary cleanup"
    assert selected.__exit__(MemoryError, original, None) is False
    assert selected._closed and not h.calls and not h.output.exists()
    assert selected.navref_probe["status"] == "skipped-body-failure"
    assert "secondary cleanup" in " ".join(original.__notes__)


@pytest.mark.parametrize(
    "updates",
    [
        {"native_exit_code": 1},
        {"native_exit_code": False},
        {"complete_output_observed": False},
        {"reader_threads_exited": False},
        {"owned_process_exited": False},
        {"timeout": True},
    ],
)
def test_failed_native_retains_original_bytes_stops_and_cleans(h, updates):
    selected = session(h, lambda *a, **k: native_result(b"failed-original\xff", **updates))
    with pytest.raises(ValueError, match="failed/incomplete"):
        selected.__exit__(None, None, None)
    assert selected._closed and len(selected.navref_probe["commands"]) == 1
    assert (h.output / "native-00.stdout.bin").read_bytes() == b"failed-original\xff"
    assert selected.navref_probe["status"] == "failed"


@pytest.mark.parametrize(
    "key,value",
    [
        ("all_bytes_retained", False),
        ("EOF", False),
        ("error", "readerfailed"),
        ("observed_bytes", True),
        ("observed_bytes", 99),
        ("full_output_sha256", "0" * 64),
    ],
)
def test_stream_identity_drift_refuses_after_saving_original(h, key, value):
    result = native_result()
    result["streams"]["stdout"][key] = value
    selected = session(h, lambda *a, **k: result)
    with pytest.raises(ValueError, match="stream identity"):
        selected.__exit__(None, None, None)
    assert selected._closed and (h.output / "native-00.stdout.bin").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_changed", True),
        ("receiver_alive", True),
        ("queued", 1),
        ("camera_alive", True),
    ],
)
def test_owner_or_unclosed_rx_camera_refuses_before_native(h, field, value):
    setattr(h.config, field, value)
    selected = session(h)
    with pytest.raises(ValueError):
        selected.__exit__(None, None, None)
    assert not h.calls and selected._closed


def test_readonly_allowlist_rejects_mutation_or_extra_argument(h):
    selected = session(h)
    with pytest.raises(ValueError, match="fixed readonly"):
        selected._probe_one(0, "EKF2_IMU_POS_X", ("px4-param", "set", "EKF2_IMU_POS_X", "0"))
    with pytest.raises(ValueError, match="fixed readonly"):
        selected._probe_one(0, probe.SPECS[0][0], (*probe.SPECS[0][1], "--extra"))
    assert not h.calls


def test_probe_primary_exception_survives_clock_and_cleanup_secondary(h):
    def failing(*args, **kwargs):
        h.instances[-1].clock.monotonic = lambda: float("nan")
        raise MemoryError("native failure")

    selected = session(h, failing)
    h.config.cleanup_error = "cleanup also failed"
    with pytest.raises(MemoryError, match="native failure") as caught:
        selected.__exit__(None, None, None)
    assert selected._closed
    assert "Probe" not in str(caught.value)
    assert len(caught.value.__notes__) == 2


def test_probe_fsync_failure_preserves_partial_and_always_cleans(h, monkeypatch):
    original_write = probe._write_new

    def fail_after_write(path, raw):
        result = original_write(path, raw)
        if str(path).endswith("stdout.bin"):
            raise OSError("fsync diagnostic failure")
        return result

    monkeypatch.setattr(probe, "_write_new", fail_after_write)
    selected = session(h)
    with pytest.raises(OSError, match="fsync diagnostic"):
        selected.__exit__(None, None, None)
    assert selected._closed and (h.output / "native-00.stdout.bin").exists()
    assert len(h.calls) == 1


def test_attempt_persistence_failure_never_calls_native_but_cleans(h, monkeypatch):
    monkeypatch.setattr(probe, "_json_new", lambda *a: (_ for _ in ()).throw(OSError("attempt")))
    selected = session(h)
    with pytest.raises(OSError, match="attempt"):
        selected.__exit__(None, None, None)
    assert selected._closed and not h.calls


def test_cleanup_failure_after_reads_is_not_success(h):
    selected = session(h)
    h.config.cleanup_error = "cleanup incomplete"
    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        selected.__exit__(None, None, None)
    assert selected.navref_probe["status"] == "recorded"


@pytest.mark.parametrize("raw", [b"NaN", b"inf", b"true", b"0\n1", b"", b"\xff", b"1e999"])
def test_invalid_parameter_no_default_repair(raw):
    with pytest.raises((ValueError, UnicodeDecodeError)):
        probe.parse_quiet_value(raw, "EKF2_IMU_POS_X")


@pytest.mark.parametrize("raw,expected", [(b"-0.25\n", -0.25), (b"1e3", 1000), (b".125", 0.125)])
def test_actual_numeric_parameter_value(raw, expected):
    assert probe.parse_quiet_value(raw, "EKF2_IMU_POS_X") == expected


def fake_observer(h, *, status="SIM-operator-observations-recorded", mutate=None):
    def run(plan, directory, *, session_factory, operator_intent, **kwargs):
        assert operator_intent.experiment_id == probe.EXPERIMENT_ID
        assert operator_intent.sources == h.intent.sources
        assert operator_intent.owned_container_name == h.intent.owned_container_name
        Path(directory).mkdir()
        selected = session_factory(plan, execute=True)
        selected.__exit__(None, None, None)
        if mutate:
            mutate()
        return {
            "status": status,
            "injected_dependencies_used": True,
            "external_SIM_observed_here": False,
            "context_qualified": False,
        }

    return run


def test_plan_only_no_observer_session_native_output(h, monkeypatch):
    monkeypatch.setattr(probe, "source_inventory", lambda: [{"CPU_fixture": True}])
    result = probe.probe_sim_operator(
        h.plan, h.output, operator_intent=h.intent, artifact_root=h.root
    )
    assert result["status"] == "planned" and not h.output.exists()
    assert not h.instances and not h.calls and not result["origin_qualified"]
    assert h.intent.experiment_id == "old-CPU-experiment"


def run_wrapper(h, **kwargs):
    return probe.probe_sim_operator(
        h.plan,
        h.output,
        execute=True,
        operator_intent=h.intent,
        artifact_root=h.root,
        observer=kwargs.pop("observer", fake_observer(h)),
        native_runner=h.runner,
        **kwargs,
    )


def test_wrapper_records_experiment_and_preserves_legacy_injected_flags(h, monkeypatch):
    monkeypatch.setattr(probe, "source_inventory", lambda: [{"CPU_fixture": True}])
    result = run_wrapper(h)
    assert result["status"] == "navref-probe-recorded"
    assert result["selected_sources_unchanged_during_run"]
    assert not result["native_probe_observed_here"] and not result["origin_qualified"]
    assert not any(result["claims"].values())
    legacy = result["legacy_observation"]
    assert legacy["injected_dependencies_used"] and not legacy["external_SIM_observed_here"]
    persisted = json.loads(Path(result["receipt"]["path"]).read_bytes())
    assert persisted["session"]["cleanup_verified"]
    assert persisted["probe"]["diagnostic_after_telemetry_seal"]
    assert persisted["actual_operator_intent"]["experiment_id"] == probe.EXPERIMENT_ID
    assert h.intent.experiment_id == "old-CPU-experiment"


def test_late_source_drift_retains_raw_but_rejects_report(h, monkeypatch):
    inventory = [{"version": 1}]
    monkeypatch.setattr(probe, "source_inventory", lambda: copy.deepcopy(inventory))
    result = run_wrapper(
        h, observer=fake_observer(h, mutate=lambda: inventory.append({"drift": 1}))
    )
    assert result["status"] == "diagnostics-failed"
    assert not result["selected_sources_unchanged_during_run"]
    assert not result["native_probe_observed_here"]
    assert len(result["probe"]["commands"]) == 18 and result["session"]["cleanup_verified"]
    assert (h.output / "origin-probe.json").exists()


def test_existing_output_refused_no_overwrite(h):
    h.output.mkdir()
    marker = h.output / "old"
    marker.write_bytes(b"preserved")
    with pytest.raises(ValueError, match="Fresh contained"):
        run_wrapper(h)
    assert marker.read_bytes() == b"preserved" and not h.instances


def test_wrapper_failed_legacy_does_not_admit_probes(h, monkeypatch):
    monkeypatch.setattr(probe, "source_inventory", lambda: [])
    result = run_wrapper(h, observer=fake_observer(h, status="diagnostics-failed"))
    assert result["status"] == "diagnostics-failed" and not result["native_probe_observed_here"]


def test_wrapper_native_failure_retains_failure_receipt_and_real_cleanup(h, monkeypatch):
    monkeypatch.setattr(probe, "source_inventory", lambda: [])
    h.runner = lambda *a, **k: native_result(native_exit_code=1)
    result = run_wrapper(h)
    assert result["status"] == "diagnostics-failed"
    assert result["session"]["cleanup_verified"]
    assert result["probe"]["status"] == "failed"
    assert (h.output / "origin-probe.json").exists()


def test_original_output_late_mutation_refuses_success_and_still_cleans(h):
    def runner(*args, **kwargs):
        if (h.output / "native-00.stdout.bin").exists():
            (h.output / "native-00.stdout.bin").write_bytes(b"changed")
        return h.runner(*args, **kwargs)

    selected = session(h, runner)
    with pytest.raises(ValueError, match="changed before completion"):
        selected.__exit__(None, None, None)
    assert selected._closed and selected.navref_probe["status"] == "failed"


def test_intent_file_late_drift_is_not_an_admitted_probe(h, monkeypatch):
    monkeypatch.setattr(probe, "source_inventory", lambda: [])
    path = h.root / "input-intent.json"
    path.write_bytes(b"original-source")
    binding = probe._record(path)
    result = run_wrapper(
        h,
        operator_intent_binding=binding,
        observer=fake_observer(h, mutate=lambda: path.write_bytes(b"different")),
    )
    assert result["status"] == "diagnostics-failed"
    assert "intent changed" in result["source_error"]
    assert result["session"]["cleanup_verified"]


def test_wrapper_missing_intent_and_nonboolean_execute_refuse_before_session(h):
    with pytest.raises(ValueError, match="boolean"):
        probe.probe_sim_operator(
            h.plan, h.output, execute=1, operator_intent=h.intent, artifact_root=h.root
        )
    with pytest.raises(ValueError, match="operator intent"):
        probe.probe_sim_operator(h.plan, h.output, artifact_root=h.root)
    assert not h.instances and not h.output.exists()


def test_inventory_is_small_and_contains_only_explicit_entrypoints():
    rows = probe.source_inventory()
    assert len(rows) == len(probe.SOURCE_PATHS)
    assert len({r["path"] for r in rows}) == len(rows)
    assert all(0 < row["bytes"] <= 8 * 1024 * 1024 for row in rows)
    assert not any("dataset" in row["path"] or "weights" in row["path"] for row in rows)


def test_fresh_import_is_no_optional_model_camera_or_mavlink_runtime():
    root = Path(__file__).resolve().parents[1]
    code = (
        f"import sys;sys.path[:0]={[str(root), str(root / 'src')]!r};"
        "from scripts import probe_onboard_teacher_gazebo_navref;"
        "assert not any(k.split('.')[0] in {'torch','PIL','numpy','pymavlink','cv2'} "
        "for k in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, timeout=20, check=False
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
