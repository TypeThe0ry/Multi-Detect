"""CPU fixtures only; no Docker, socket, real parameter changes, flight or ML."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.clock import HighResolutionSystemClock
from scripts import onboard_teacher_sim_operator as operator

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "_sim_operator_frozen_fixture_helpers", ROOT / "tests/test_onboard_teacher_gazebo_session.py"
)
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
FW = b"HW arch: PX4_SITL\nPX4 git-hash: " + b"a" * 40 + b"\nPX4 version: CPU-fixture-not-runtime\n"


def output(name, value):
    return f"Symbols: x = used\nx     {name} [1,2] : {value}\n\n9/10 parameters used.\n".encode()


def write_binding(path, raw):
    path.write_bytes(raw)
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return helpers.docker_template.__wrapped__(tmp_path_factory)


@pytest.fixture
def plan(tmp_path, template):
    return helpers.plan.__wrapped__(tmp_path, template)


@pytest.fixture
def intent_mapping(tmp_path, plan):
    metadata = write_binding(
        tmp_path / "metadata.json",
        json.dumps(
            {
                "parameters": [
                    {
                        "name": "COM_RC_IN_MODE",
                        "type": "Int32",
                        "values": [{"value": 4, "description": "Disable manual control"}],
                    }
                ]
            }
        ).encode(),
    )
    extraction = {
        "contract": "multidetect.teacher-pinned-PX4-metadata-extraction-v1",
        "image": operator.PINNED_IMAGE,
        "status": "pinned-image-metadata-extracted",
        "injected_runner": False,
        "final_metadata_sources_reverified": True,
        "cleanup_verified": True,
        "files": {"parameters.json.xz": {"expanded": metadata}},
    }
    evidence = {
        "contract": "multidetect.teacher-PX4-readonly-diagnostic-evidence-v1",
        "native_version_and_parameter_exit_codes_all_zero": True,
        "reported_PX4_git_hash": "a" * 40,
        "reported_PX4_version_raw": "CPU-fixture-not-runtime",
        "observed_parameters": {"COM_RC_IN_MODE": 1},
    }
    sources = {
        "metadata": metadata,
        "metadata_extraction": write_binding(
            tmp_path / "extraction.json", json.dumps(extraction).encode()
        ),
        "firmware_output": write_binding(tmp_path / "firmware.bin", FW),
        "parameter_output": write_binding(tmp_path / "parameter.bin", output("COM_RC_IN_MODE", 1)),
        "prior_evidence": write_binding(tmp_path / "evidence.json", json.dumps(evidence).encode()),
    }
    return {
        "contract": operator.CONTRACT,
        "experiment_id": "CPU-fixture",
        "owned_container_name": plan.docker_plan.container_name,
        "scope": "owned-disposable-software-SIM-only",
        "explicitly_authorized": True,
        "image": operator.PINNED_IMAGE,
        "change": {"name": "COM_RC_IN_MODE", "expected_before": 1, "requested_after": 4},
        "sources": sources,
    }


class Mav:
    def __init__(self):
        self.srcSystem, self.srcComponent = 255, 190
        self.calls = []
        self.on_heartbeat = None

    def heartbeat_send(self, *args):
        self.calls.append(("heartbeat", args, threading.get_ident()))
        if self.on_heartbeat:
            self.on_heartbeat()

    def command_long_send(self, *args):
        self.calls.append(("command", args, threading.get_ident()))

    def param_request_read_send(self, *args):
        self.calls.append(("read-param", args, threading.get_ident()))


class Connection:
    def __init__(self):
        self.mav, self.closed, self.inbox = Mav(), False, queue.Queue()
        self.read_threads = []

    def recv_match(self, *, blocking, timeout):
        assert blocking is True and 0 < timeout <= 0.05
        self.read_threads.append(threading.get_ident())
        try:
            return self.inbox.get(timeout=min(timeout, 0.005))
        except queue.Empty:
            return None

    def close(self):
        self.closed = True


class NativeOperations:
    def __init__(self):
        self.values = {"COM_RC_IN_MODE": 1, **dict.fromkeys(operator.PROTECTED_PARAMETERS, 1)}
        self.calls, self.callback = [], None
        self.missing = None
        self.primary = None
        self.protected_mutation = False

    def __call__(self, argv, *, timeout_s):
        assert 0 < timeout_s <= 10
        self.calls.append(argv)
        if self.callback:
            self.callback(argv)
        command = argv[3:]
        if command == ["/opt/px4-gazebo/bin/px4-ver", "all"]:
            raw = FW
        elif command[1] == "show":
            raw = (
                b"No such parameter\n"
                if command[2] == self.missing
                else output(command[2], self.values[command[2]])
            )
        elif command == ["/opt/px4-gazebo/bin/px4-param", "set", "COM_RC_IN_MODE", "4"]:
            self.values["COM_RC_IN_MODE"] = 4
            if self.protected_mutation:
                self.values[operator.PROTECTED_PARAMETERS[0]] = 0
            if self.primary:
                raise self.primary
            raw = b"CPU-fixture set returned\n"
        else:
            raise AssertionError("No alternate setting or flight operation allowed")
        return subprocess.CompletedProcess(argv, 0, raw, b"")


def owned(plan):
    clock, connection, docker = (
        HighResolutionSystemClock(),
        Connection(),
        helpers.NativeDocker(plan),
    )
    backend = helpers.Backend(plan, clock)
    connect_calls = []

    def connect(endpoint, **kwargs):
        connect_calls.append((endpoint, kwargs))
        return connection

    session = operator.OwnedTeacherGazeboSessionV5(
        plan,
        execute=True,
        clock=clock,
        runner=docker,
        connection_factory=connect,
        camera_factory=lambda *_: backend,
        port_probe=lambda _: None,
    )
    return SimpleNamespace(
        session=session, connection=connection, docker=docker, connect_calls=connect_calls
    )


def test_intent_roundtrip_and_five_actual_bindings(intent_mapping, tmp_path):
    intent = operator.DisposableSimOperatorIntent.from_mapping(intent_mapping)
    assert intent.to_mapping() == intent_mapping
    assert operator.admit_intent(intent, tmp_path) == {
        "git_hash": "a" * 40,
        "version_raw": "CPU-fixture-not-runtime",
    }


def test_direct_typed_intent_cannot_skip_explicit_authorization(intent_mapping):
    admitted = operator.DisposableSimOperatorIntent.from_mapping(intent_mapping)
    with pytest.raises(ValueError, match="Explicit caller SIM intent"):
        operator.DisposableSimOperatorIntent(
            admitted.experiment_id, admitted.owned_container_name, admitted.sources, False
        )


@pytest.mark.parametrize(
    "change",
    (
        "real",
        "hil",
        "authority-false",
        "authority-bool",
        "mode-zero",
        "different-name",
        "before-bool",
        "after-bool",
        "image",
        "missing-source",
        "extra-key",
    ),
)
def test_only_explicit_pinned_disposable_sim_change_admitted(intent_mapping, change):
    value = copy.deepcopy(intent_mapping)
    if change in {"real", "hil"}:
        value["scope"] = change
    elif change == "authority-false":
        value["explicitly_authorized"] = False
    elif change == "authority-bool":
        value["explicitly_authorized"] = 1
    elif change == "mode-zero":
        value["change"]["requested_after"] = 0
    elif change == "different-name":
        value["change"]["name"] = "COM_ARM_WO_GPS"
    elif change == "before-bool":
        value["change"]["expected_before"] = True
    elif change == "after-bool":
        value["change"]["requested_after"] = True
    elif change == "image":
        value["image"] = "unpinned:latest"
    elif change == "missing-source":
        del value["sources"]["firmware_output"]
    else:
        value["arming_permission"] = True
    with pytest.raises(ValueError):
        operator.DisposableSimOperatorIntent.from_mapping(value)


@pytest.mark.parametrize(
    "raw",
    (b"", b"No such parameter\n", output("COM_RC_IN_MODE", "nan"), output("COM_RC_IN_MODE", 1) * 2),
)
def test_missing_duplicate_or_nonfinite_actual_parameter_has_no_default(raw):
    with pytest.raises(ValueError):
        operator.parse_parameter_output(raw, "COM_RC_IN_MODE")


def test_actual_before_not_metadata_default(intent_mapping, tmp_path):
    metadata = intent_mapping["sources"]["parameter_output"]
    intent_mapping["sources"]["parameter_output"] = write_binding(
        Path(metadata["path"]), output("COM_RC_IN_MODE", 4)
    )
    with pytest.raises(ValueError, match="Historical"):
        operator.admit_intent(
            operator.DisposableSimOperatorIntent.from_mapping(intent_mapping), tmp_path
        )


def test_normal_one_change_all_protected_readbacks_and_closed_outputs(
    plan, intent_mapping, tmp_path
):
    harness, native = owned(plan), NativeOperations()
    intent = operator.DisposableSimOperatorIntent.from_mapping(intent_mapping)
    with harness.session as session:
        op = operator.SimOperator(
            session, intent, tmp_path / "operator", repository_root=tmp_path, native_runner=native
        )
        report = op.apply()
        assert report["status"] == "SIM-configuration-readback-verified"
        assert (
            report["parameter_change_attempts"] == 1
            and report["parameter_change_API_attempted"] is True
        )
        assert (
            report["parameter_change_returned"] is True
            and report["parameter_change_uncertain"] is False
        )
        assert report["saved_outputs_unchanged"] is True and report["sources_unchanged"] is True
        assert len(native.calls) == 20 and sum("set" in c for c in native.calls) == 1
        assert all(value is False for value in report["claims"].values())
        for name in operator.PROTECTED_PARAMETERS:
            assert (
                report["protected_parameters_before"][name]
                == report["protected_parameters_after"][name]
            )
        assert len(report["outputs_reverified"]) == 61
        with pytest.raises(ValueError, match="single-use"):
            op.apply()
        assert len(native.calls) == 20
    assert harness.connection.closed and harness.docker.current is None
    assert harness.connect_calls[0][1] == {
        "source_system": 255,
        "source_component": 190,
        "autoreconnect": False,
    }


def test_fresh_current_mode_not1_refuses_without_any_write(plan, intent_mapping, tmp_path):
    harness, native = owned(plan), NativeOperations()
    native.values["COM_RC_IN_MODE"] = 4
    with harness.session as session:
        op = operator.SimOperator(
            session,
            operator.DisposableSimOperatorIntent.from_mapping(intent_mapping),
            tmp_path / "operator",
            repository_root=tmp_path,
            native_runner=native,
        )
        with pytest.raises(ValueError, match="actually be1"):
            op.apply()
        assert op.to_mapping()["parameter_change_attempts"] == 0
        assert not any("set" in c for c in native.calls)


def test_native_exit0_missing_protected_parameter_refuses_before_write(
    plan, intent_mapping, tmp_path
):
    harness, native = owned(plan), NativeOperations()
    native.missing = "COM_ARM_HFLT_CHK"
    with harness.session as session:
        op = operator.SimOperator(
            session,
            operator.DisposableSimOperatorIntent.from_mapping(intent_mapping),
            tmp_path / "operator",
            repository_root=tmp_path,
            native_runner=native,
        )
        with pytest.raises(ValueError, match="no default"):
            op.apply()
        assert not any("set" in c for c in native.calls)


def test_protected_setting_change_is_failure_not_arming_permission(plan, intent_mapping, tmp_path):
    harness, native = owned(plan), NativeOperations()
    native.protected_mutation = True
    with harness.session as session:
        op = operator.SimOperator(
            session,
            operator.DisposableSimOperatorIntent.from_mapping(intent_mapping),
            tmp_path / "operator",
            repository_root=tmp_path,
            native_runner=native,
        )
        with pytest.raises(ValueError, match="protected safety"):
            op.apply()
        assert op.to_mapping()["parameter_change_uncertain"] is True
        assert op.to_mapping()["status"] == "failed"
        assert sum("set" in c for c in native.calls) == 1


@pytest.mark.parametrize(
    "primary", (MemoryError("write-unknown"), KeyboardInterrupt("write-interrupted"))
)
def test_failed_set_keeps_reservation_uncertainty_no_refund_or_rollback(
    plan, intent_mapping, tmp_path, primary
):
    harness, native = owned(plan), NativeOperations()
    native.primary = primary
    with harness.session as session:
        op = operator.SimOperator(
            session,
            operator.DisposableSimOperatorIntent.from_mapping(intent_mapping),
            tmp_path / "operator",
            repository_root=tmp_path,
            native_runner=native,
        )
        with pytest.raises(type(primary)) as caught:
            op.apply()
        assert caught.value is primary
        report = op.to_mapping()
        assert (
            report["parameter_change_attempts"] == 1
            and report["parameter_change_API_attempted"] is True
        )
        assert (
            report["parameter_change_uncertain"] is True
            and report["parameter_change_returned"] is False
        )
        with pytest.raises(ValueError, match="single-use"):
            op.apply()
        assert sum("set" in c for c in native.calls) == 1


def test_pre_api_receipt_failure_keeps_reservation_but_no_set_api(
    plan, intent_mapping, tmp_path, monkeypatch
):
    harness, native = owned(plan), NativeOperations()
    with harness.session as session:
        op = operator.SimOperator(
            session,
            operator.DisposableSimOperatorIntent.from_mapping(intent_mapping),
            tmp_path / "operator",
            repository_root=tmp_path,
            native_runner=native,
        )
        original = op._save_json

        def fail_set_receipt(path, value):
            if value.get("command", [])[-3:] == ["set", "COM_RC_IN_MODE", "4"]:
                raise MemoryError("before-set-receipt")
            return original(path, value)

        monkeypatch.setattr(op, "_save_json", fail_set_receipt)
        with pytest.raises(MemoryError, match="before-set-receipt"):
            op.apply()
        report = op.to_mapping()
        assert report["parameter_change_attempts"] == 1
        assert report["parameter_change_API_attempted"] is False
        assert report["parameter_change_uncertain"] is False
        assert not any("set" in row for row in native.calls)
        with pytest.raises(ValueError, match="single-use"):
            op.apply()


def test_native_primary_failure_survives_secondary_clock_failure(
    plan, intent_mapping, tmp_path, monkeypatch
):
    harness, native = owned(plan), NativeOperations()
    primary = MemoryError("set-unknown-primary")
    native.primary = primary
    with harness.session as session:
        op = operator.SimOperator(
            session,
            operator.DisposableSimOperatorIntent.from_mapping(intent_mapping),
            tmp_path / "operator",
            repository_root=tmp_path,
            native_runner=native,
        )
        original = op._time

        def time_or_fail():
            if native.values["COM_RC_IN_MODE"] == 4:
                raise ValueError("secondary-clock-read")
            return original()

        monkeypatch.setattr(op, "_time", time_or_fail)
        with pytest.raises(MemoryError) as caught:
            op.apply()
        assert caught.value is primary
        assert any("secondary-clock-read" in note for note in primary.__notes__)
        assert op.to_mapping()["native_attempts"][-1]["finished_at_s"] is None
        assert op.to_mapping()["parameter_change_uncertain"] is True


def test_saved_output_late_same_length_mutation_refuses_success(plan, intent_mapping, tmp_path):
    harness, native = owned(plan), NativeOperations()
    directory = tmp_path / "operator"

    def mutate(argv):
        if len(native.calls) == 20:
            path = directory / "native-000.stdout.bin"
            raw = path.read_bytes()
            path.write_bytes(b"X" + raw[1:])

    native.callback = mutate
    with harness.session as session:
        op = operator.SimOperator(
            session,
            operator.DisposableSimOperatorIntent.from_mapping(intent_mapping),
            directory,
            repository_root=tmp_path,
            native_runner=native,
        )
        with pytest.raises(ValueError, match="SHA"):
            op.apply()
        assert op.to_mapping()["saved_outputs_unchanged"] is False
        assert op.to_mapping()["status"] == "failed"


def test_history_late_drift_refuses_success(plan, intent_mapping, tmp_path):
    harness, native = owned(plan), NativeOperations()

    def mutate(argv):
        if len(native.calls) == 20:
            path = Path(intent_mapping["sources"]["firmware_output"]["path"])
            raw = path.read_bytes()
            path.write_bytes(b"X" + raw[1:])

    native.callback = mutate
    with harness.session as session:
        op = operator.SimOperator(
            session,
            operator.DisposableSimOperatorIntent.from_mapping(intent_mapping),
            tmp_path / "operator",
            repository_root=tmp_path,
            native_runner=native,
        )
        with pytest.raises(ValueError, match="SHA"):
            op.apply()
        assert op.to_mapping()["sources_unchanged"] is False


def test_operator_cannot_run_after_receive_transfer_or_reuse_output(plan, intent_mapping, tmp_path):
    harness = owned(plan)
    intent = operator.DisposableSimOperatorIntent.from_mapping(intent_mapping)
    with harness.session as session:
        rx = session.start_receiver()
        op = operator.SimOperator(
            session,
            intent,
            tmp_path / "operator",
            repository_root=tmp_path,
            native_runner=NativeOperations(),
        )
        with pytest.raises(ValueError, match="before RX transfer"):
            op.apply()
        rx.stop()
        with pytest.raises(ValueError, match="Fresh contained"):
            operator.SimOperator(session, intent, tmp_path / "operator", repository_root=tmp_path)


def test_native_fixed_allowlist_never_accepts_arm_or_other_parameter(
    plan, intent_mapping, tmp_path
):
    harness = owned(plan)
    with harness.session as session:
        op = operator.SimOperator(
            session,
            operator.DisposableSimOperatorIntent.from_mapping(intent_mapping),
            tmp_path / "operator",
            repository_root=tmp_path,
            native_runner=NativeOperations(),
        )
        with pytest.raises(ValueError, match="fixed scoped"):
            op._native(["/opt/px4-gazebo/bin/px4-param", "set", "COM_ARM_WO_GPS", "2"])
        assert not op.directory.exists()


def test_gcs_ticker_exact_role_1hz_and_no_delivery_claim():
    connection, now = Connection(), [10.0]
    ticker = operator.GcsHeartbeatTicker(connection, lambda: now[0], "CPU-QPC", lambda: 1)
    ticker.tick()
    now[0] = 10.99
    ticker.tick()
    assert len(connection.mav.calls) == 1
    now[0] = 11
    ticker.tick()
    rows = ticker.to_mapping()
    assert len(rows) == 2 and all(r["API_returned"] is True for r in rows)
    assert all(
        r["args"] == [6, 8, 0, 0, 4, 3] and r["datagram_delivery_verified"] is False for r in rows
    )
    assert all(r["source_system_id"] == 255 and r["source_component_id"] == 190 for r in rows)
    assert all(r["flight_authority"] is False for r in rows)


@pytest.mark.parametrize("identity", ((1, 1), (250, 191), (True, 190)))
def test_wrong_or_bool_actual_gcs_source_refuses_send(identity):
    connection = Connection()
    connection.mav.srcSystem, connection.mav.srcComponent = identity
    ticker = operator.GcsHeartbeatTicker(connection, time.monotonic, "CPU-QPC", lambda: 1)
    with pytest.raises(ValueError, match="Actual GCS"):
        ticker.tick()
    assert connection.mav.calls == []


def test_gcs_clock_regression_after_api_preserves_actual_attempt():
    connection, now = Connection(), [10.0]
    connection.mav.on_heartbeat = lambda: now.__setitem__(0, 9.0)
    ticker = operator.GcsHeartbeatTicker(connection, lambda: now[0], "CPU-QPC", lambda: 1)
    with pytest.raises(ValueError, match="regressed"):
        ticker.tick()
    row = ticker.to_mapping()[0]
    assert row["API_returned"] is True and "regressed" in row["error"]


def test_v5_unique_rx_sends_gcs_and_read_requests_same_thread_then_cleanup(plan):
    harness = owned(plan)
    from scripts.onboard_teacher_gazebo_session_v4 import DiagnosticRequest

    with harness.session as session:
        rx = session.start_receiver()
        rows = rx.request_diagnostics([DiagnosticRequest("parameter-read", "SYS_AUTOSTART")])
        assert rows[0]["API_returned"] is True
        rx.stop()
        rx_thread = rx._thread.ident
        assert rx_thread != threading.get_ident()
        assert {c[2] for c in harness.connection.mav.calls} == {rx_thread}
        assert set(harness.connection.read_threads) == {rx_thread}
    report = harness.session.to_mapping()
    assert report["GCS_heartbeat_API_attempts"] and report["receiver"]["thread_alive"] is False
    assert report["GCS_connection_factory_injected"] is True
    assert harness.connection.closed and harness.docker.current is None


def test_heartbeat_failure_latches_rx_and_owned_cleanup(plan):
    harness = owned(plan)

    def fail():
        raise MemoryError("GCS-actual-send-unknown")

    harness.connection.mav.on_heartbeat = fail
    with harness.session as session:
        rx = session.start_receiver()
        rx._thread.join(1)
        with pytest.raises(RuntimeError, match="GCS-actual"):
            rx.raise_if_failed()
        rx.stop()
    assert harness.connection.closed and harness.docker.current is None
    assert harness.session.to_mapping()["GCS_heartbeat_API_attempts"][0]["API_returned"] is False


def test_import_quiet_no_ml_mavlink_or_frozen_v4_edits():
    raw = (ROOT / "scripts/onboard_teacher_gazebo_session_v4.py").read_bytes()
    assert (
        len(raw) == 12186
        and hashlib.sha256(raw).hexdigest()
        == "dd696b9ba0ef905d2e2fd2b49986bdbe16c156380fab2543258201763ed8015d"
    )
    code = (
        f"import sys;sys.path[:0]=[{str(ROOT)!r},{str(ROOT / 'src')!r}];"
        "import scripts.onboard_teacher_sim_operator;"
        "blocked=('torch','transformers','peft','pymavlink','PIL','numpy');"
        "assert not any(n.split('.')[0] in blocked for n in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
