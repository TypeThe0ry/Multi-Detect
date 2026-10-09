"""CPU read-spec fixtures; not actual Docker/PX4 settings or flight evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import onboard_teacher_sim_operator_v2 as operator

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "_operator_v2_cpu_fixture_helpers", ROOT / "tests/test_onboard_teacher_sim_operator.py"
)
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)
template, plan, intent_mapping = old.template, old.plan, old.intent_mapping


class QuietOperations(old.NativeOperations):
    def __call__(self, argv, *, timeout_s):
        if argv[3:6] != ["/opt/px4-gazebo/bin/px4-param", "show", "-q"]:
            return super().__call__(argv, timeout_s=timeout_s)
        assert len(argv) == 7 and argv[6] in operator.PARAMETERS
        assert 0 < timeout_s <= 10
        self.calls.append(argv)
        if self.callback:
            self.callback(argv)
        name = argv[6]
        if name == self.missing:
            return subprocess.CompletedProcess(argv, 1, b"", b"missing-actual-parameter")
        return subprocess.CompletedProcess(argv, 0, f"{self.values[name]}\n".encode(), b"")


def make_operator(plan, intent_mapping, tmp_path, native):
    harness = old.owned(plan)
    instance = operator.SimOperator(
        harness.session,
        operator.DisposableSimOperatorIntent.from_mapping(intent_mapping),
        tmp_path / "operator-v2",
        repository_root=tmp_path,
        native_runner=native,
    )
    return harness, instance


@pytest.mark.parametrize("raw,expected", [(b"1\n", 1), (b"4\n", 4), (b"10.0000\n", 10)])
def test_actual_numeric_quiet_response(raw, expected):
    assert operator.parse_quiet_parameter_output(raw, "COM_RC_IN_MODE") == expected


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"No such parameter\n",
        b"nan\n",
        b"inf\n",
        b"1e9999\n",
        b"1\n1\n",
        b"1 trailing-repair\n",
        b"x COM_RC_IN_MODE [1,2] : 1\n",
        b"\xff\n",
        b"1" * (128 * 1024 + 1),
    ],
    ids=[
        "empty",
        "missing",
        "nan",
        "inf",
        "overflow",
        "multiple",
        "trailing",
        "named",
        "ascii",
        "cap",
    ],
)
def test_missing_nonfinite_multiple_named_or_oversized_value_is_not_repaired(raw):
    with pytest.raises(ValueError):
        operator.parse_quiet_parameter_output(raw, "COM_RC_IN_MODE")


def test_other_parameter_cannot_acquire_read_or_write_authorization():
    with pytest.raises(ValueError, match="fixed"):
        operator.parse_quiet_parameter_output(b"1\n", "COM_ARM_AUTH_REQ")


def test_quiet_values_unused_or_used_have_exact_actual_argv_and_raw_identity(
    plan, intent_mapping, tmp_path
):
    native = QuietOperations()
    harness, instance = make_operator(plan, intent_mapping, tmp_path, native)
    with harness.session:
        result = instance.apply()
    assert result["contract"] == operator.CONTRACT
    assert result["status"] == "SIM-configuration-readback-verified"
    assert result["parameter_change_attempts"] == 1
    assert result["parameter_readback_verified"] is True
    assert result["saved_outputs_unchanged"] is True
    assert result["sources_unchanged"] is True
    assert len(native.calls) == 20 and sum("set" in c for c in native.calls) == 1
    reads = [r for r in result["native_attempts"] if r.get("requested_parameter")]
    assert len(reads) == 18
    assert [r["requested_parameter"] for r in reads] == list(operator.PARAMETERS) * 2
    for row in reads:
        assert row["command"][3:] == [
            "/opt/px4-gazebo/bin/px4-param",
            "show",
            "-q",
            row["requested_parameter"],
        ]
        before_api = json.loads(Path(row["before_API_receipt"]["path"]).read_bytes())
        assert before_api["command"] == row["command"]
        assert before_api["requested_parameter"] == row["requested_parameter"]
        assert before_api["API_attempted"] is False
        raw = Path(row["stdout"]["path"]).read_bytes()
        assert len(raw) == row["stdout"]["bytes"]
        assert hashlib.sha256(raw).hexdigest() == row["stdout"]["sha256"]
        assert b"Symbols" not in raw and b":" not in raw
    assert all(value is False for value in result["claims"].values())
    assert result["parameter_read_specification"]["all_table_fallback"] is False
    assert harness.connection.closed and harness.docker.current is None


def test_actual_missing_unused_parameter_fails_before_any_set(plan, intent_mapping, tmp_path):
    native = QuietOperations()
    native.missing = "COM_ARM_WO_GPS"
    harness, instance = make_operator(plan, intent_mapping, tmp_path, native)
    with harness.session:
        with pytest.raises(ValueError, match="quiet-read failed"):
            instance.apply()
        with pytest.raises(ValueError, match="single-use"):
            instance.apply()
    assert instance.to_mapping()["parameter_change_attempts"] == 0
    assert not any("set" in c for c in native.calls)


@pytest.mark.parametrize("value", [0, 4])
def test_actual_current_target_must_still_be1(plan, intent_mapping, tmp_path, value):
    native = QuietOperations()
    native.values["COM_RC_IN_MODE"] = value
    harness, instance = make_operator(plan, intent_mapping, tmp_path, native)
    with harness.session:
        with pytest.raises(ValueError, match="actually be1"):
            instance.apply()
    assert not any("set" in c for c in native.calls)


def test_protected_after_mutation_still_refused(plan, intent_mapping, tmp_path):
    native = QuietOperations()
    native.protected_mutation = True
    harness, instance = make_operator(plan, intent_mapping, tmp_path, native)
    with harness.session:
        with pytest.raises(ValueError, match="protected safety"):
            instance.apply()
    assert instance.to_mapping()["parameter_change_uncertain"] is True
    assert sum("set" in c for c in native.calls) == 1


@pytest.mark.parametrize(
    "primary", [MemoryError("unknown-set"), KeyboardInterrupt("interrupt-set")]
)
def test_set_error_preserves_inherited_single_reservation_and_no_retry(
    plan, intent_mapping, tmp_path, primary
):
    native = QuietOperations()
    native.primary = primary
    harness, instance = make_operator(plan, intent_mapping, tmp_path, native)
    with harness.session:
        with pytest.raises(type(primary)) as caught:
            instance.apply()
        assert caught.value is primary
        with pytest.raises(ValueError, match="single-use"):
            instance.apply()
    result = instance.to_mapping()
    assert result["parameter_change_attempts"] == 1
    assert result["parameter_change_API_attempted"] is True
    assert result["parameter_change_uncertain"] is True
    assert result["parameter_change_returned"] is False
    assert sum("set" in c for c in native.calls) == 1


def test_quiet_saved_output_late_mutation_cannot_succeed(plan, intent_mapping, tmp_path):
    native = QuietOperations()
    harness, instance = make_operator(plan, intent_mapping, tmp_path, native)

    def mutate(argv):
        if len(native.calls) == 20:
            path = instance.directory / "native-001.stdout.bin"
            path.write_bytes(b"9\n")

    native.callback = mutate
    with harness.session:
        with pytest.raises(ValueError, match="SHA"):
            instance.apply()
    assert instance.to_mapping()["saved_outputs_unchanged"] is False
    assert instance.to_mapping()["status"] == "failed"


@pytest.mark.parametrize(
    "args",
    [
        ["/opt/px4-gazebo/bin/px4-param", "show", "COM_ARM_AUTH_REQ"],
        ["/opt/px4-gazebo/bin/px4-param", "show", "-a"],
        ["/opt/px4-gazebo/bin/px4-param", "set", "COM_ARM_WO_GPS", "0"],
    ],
)
def test_alternative_native_specs_are_not_allowed(plan, intent_mapping, tmp_path, args):
    native = QuietOperations()
    harness, instance = make_operator(plan, intent_mapping, tmp_path, native)
    with harness.session:
        with pytest.raises(ValueError):
            instance._native(args)
    assert native.calls == []


def test_closed_r1_missing_namedline_is_not_a_value_or_default():
    path = ROOT / (
        "artifacts/diagnostics/onboard-teacher-SIM-operator-20261008-r1/"
        "sim-operator/native-002.stdout.bin"
    )
    if not path.exists():
        pytest.skip("Closed actual R1 runtime artifact is absent in clean clone")
    raw = path.read_bytes()
    assert len(raw) == 81 and b"COM_ARM_WO_GPS" not in raw
    assert hashlib.sha256(raw).hexdigest() == (
        "df7bac41cca086a61f49cc67651865070abe8d4afbf1e267d34828d2d3d29936"
    )
    with pytest.raises(ValueError, match="no default"):
        operator.v1.parse_parameter_output(raw, "COM_ARM_WO_GPS")
    with pytest.raises(ValueError, match="numeric-only"):
        operator.parse_quiet_parameter_output(raw, "COM_ARM_WO_GPS")


def test_frozen_sources_and_import_quiet():
    operator.authenticate_v1()
    code = (
        f"import sys;sys.path[:0]=[{str(ROOT)!r},{str(ROOT / 'src')!r}];"
        "import scripts.onboard_teacher_sim_operator_v2;"
        "blocked=('torch','PIL','numpy','pymavlink');"
        "assert not any(n.split('.')[0] in blocked for n in sys.modules)"
    )
    completed = subprocess.run([sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True)
    assert completed.returncode == 0, completed.stderr
