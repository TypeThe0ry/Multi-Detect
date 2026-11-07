"""Disposable SIM operator v2: exact unused-inclusive, numeric-only reads.

Pinned PX4 83c4f4e580558816f0e398918f66358988004696 param.cpp:268-270,
640-669 implements ``param show -q NAME`` using param_find_no_notification,
not the ordinary used-only show traversal. Real numeric stdout is retained;
no named line, default, all-table fallback or startup retry is synthesized.
Only this read specification differs from the frozen operator. Configuration
readback and GCS API returns do not grant arming, Mission or flight authority.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V1_PINS = {
    "scripts/onboard_teacher_sim_operator.py": (
        26803,
        "286e883fd3aafb82714a84fdfe53af1bd3b20ddcd90a7f97e42e7715ea827ce9",
    ),
    "tests/test_onboard_teacher_sim_operator.py": (
        23498,
        "dfbefb0ec6e80b66a69354168051f4f8911853507030d056e590cfb7aa9b1d6f",
    ),
}


def authenticate_v1():
    for relative, (size, digest) in V1_PINS.items():
        raw = (ROOT / relative).read_bytes()
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Frozen SIM operator identity differs")


authenticate_v1()
from scripts import onboard_teacher_sim_operator as v1  # noqa: E402

authenticate_v1()
CONTRACT = "multidetect.teacher-disposable-SIM-operator-v2"
DisposableSimOperatorIntent = v1.DisposableSimOperatorIntent
OwnedTeacherGazeboSessionV5 = v1.OwnedTeacherGazeboSessionV5
PROTECTED_PARAMETERS = v1.PROTECTED_PARAMETERS
PARAMETERS = ("COM_RC_IN_MODE", *PROTECTED_PARAMETERS)
PINNED_PARAM_SOURCE = (
    "https://raw.githubusercontent.com/PX4/PX4-Autopilot/"
    "83c4f4e580558816f0e398918f66358988004696/src/systemcmds/param/param.cpp"
)


def parse_quiet_parameter_output(raw, name):
    """Actual command name, not stdout text, binds this numeric-only value."""
    v1._require(name in PARAMETERS, "Only fixed parameter names admitted")
    v1._require(type(raw) is bytes and len(raw) <= 128 * 1024, "Bounded original bytes required")
    text = raw.decode("ascii", errors="strict").strip()
    v1._require(
        re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text) is not None,
        "One actual numeric-only value required; no default or named-line repair",
    )
    value = float(text)
    v1._require(math.isfinite(value), "Actual numeric parameter must be finite")
    return value


class SimOperator(v1.SimOperator):
    def __init__(self, *args, **kwargs):
        authenticate_v1()
        super().__init__(*args, **kwargs)
        self.report.update(
            contract=CONTRACT,
            inherited_operator_source=dict(V1_PINS),
            parameter_read_specification={
                "command": "px4-param show -q NAME",
                "parameter_name_source": "actual_argv_and_requested_parameter",
                "stdout_schema": "one_finite_numeric_value_only",
                "used_parameter_filter": False,
                "all_table_fallback": False,
                "named_line_synthesized": False,
                "pinned_primary_source": PINNED_PARAM_SOURCE,
            },
        )

    def _native(self, args):
        # The inherited apply-facing read is projected before any receipt/API.
        # Version and the single permitted set keep the frozen native path.
        if (
            type(args) is not list
            or len(args) != 3
            or args[:2]
            != [
                "/opt/px4-gazebo/bin/px4-param",
                "show",
            ]
        ):
            return super()._native(args)
        name = args[2]
        v1._require(type(name) is str and name in PARAMETERS, "Fixed quiet-read allowlist required")
        identity = self._owner()
        argv = [
            self.session.plan.docker_plan.docker_executable,
            "exec",
            identity,
            "/opt/px4-gazebo/bin/px4-param",
            "show",
            "-q",
            name,
        ]
        row = {
            "attempt_index": len(self.report["native_attempts"]),
            "command": argv,
            "requested_parameter": name,
            "numeric_value_bound_to_actual_argv": True,
            "used_parameter_filter": False,
            "container_id": identity,
            "clock_id": self.session.clock.session_id,
            "started_at_s": self._time(),
            "API_attempted": False,
            "API_returned": False,
            "native_exit_code": None,
            "error": None,
        }
        self.report["native_attempts"].append(row)
        row["before_API_receipt"] = self._save_json(
            self.directory / f"attempt-{row['attempt_index']:03d}.json", row
        )
        primary = None
        try:
            timeout = min(10, self.session._remaining())
            row["API_attempted"] = True
            result = (
                subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
                if self.runner is None
                else self.runner(argv, timeout_s=timeout)
            )
            row["API_returned"] = True
            v1._require(
                type(result.returncode) is int
                and type(result.stdout) is bytes
                and type(result.stderr) is bytes
                and len(result.stdout) + len(result.stderr) <= 128 * 1024,
                "Bounded original native bytes and exit required",
            )
            row["native_exit_code"] = result.returncode
            row["stdout"] = self._save(
                self.directory / f"native-{row['attempt_index']:03d}.stdout.bin", result.stdout
            )
            row["stderr"] = self._save(
                self.directory / f"native-{row['attempt_index']:03d}.stderr.bin", result.stderr
            )
            v1._require(result.returncode == 0, "Native exact quiet-read failed")
            self._owner()
            return result.stdout
        except BaseException as error:
            primary = error
            row["error"] = f"{type(error).__name__}: {error}"[:1024]
            raise
        finally:
            try:
                row["finished_at_s"] = self._time()
            except BaseException as secondary:
                row["finished_at_s"] = None
                row["secondary_error"] = f"{type(secondary).__name__}: {secondary}"[:1024]
                if primary is not None:
                    primary.add_note(row["secondary_error"])
                else:
                    raise

    def _read_parameters(self):
        return {
            name: parse_quiet_parameter_output(
                self._native(["/opt/px4-gazebo/bin/px4-param", "show", name]), name
            )
            for name in PARAMETERS
        }

    def apply(self):
        v1._require(not self._used, "Operator is single-use; unknown/failed attempts never retry")
        self._used = True
        self.directory.mkdir(parents=False, exist_ok=False)
        primary = None
        try:
            expected = v1.admit_intent(self.intent, self.root)
            self._save_json(self.directory / "intent.json", self.intent.to_mapping())
            current = v1.parse_firmware_output(self._native(["/opt/px4-gazebo/bin/px4-ver", "all"]))
            v1._require(
                current == expected, "Current runtime firmware differs from admitted original"
            )
            before = self._read_parameters()
            self.report["protected_parameters_before"] = before
            v1._require(
                before["COM_RC_IN_MODE"] == 1, "Current mode must actually be1; no default/reset"
            )
            self.report["parameter_change_attempts"] = 1
            self._native(["/opt/px4-gazebo/bin/px4-param", "set", "COM_RC_IN_MODE", "4"])
            self.report["parameter_change_returned"] = True
            after = self._read_parameters()
            self.report["protected_parameters_after"] = after
            v1._require(
                after["COM_RC_IN_MODE"] == 4
                and all(before[name] == after[name] for name in PROTECTED_PARAMETERS),
                "Readback target/protected safety settings differ",
            )
            self.report.update(parameter_readback_verified=True, parameter_change_uncertain=False)
            v1.admit_intent(self.intent, self.root)
            self.report["sources_unchanged"] = True
            for binding in self._outputs:
                v1.SourceBinding.from_mapping(binding).read(self.root)
            authenticate_v1()
            self.report.update(
                saved_outputs_unchanged=True,
                outputs_reverified=copy.deepcopy(self._outputs),
                status="SIM-configuration-readback-verified",
            )
        except BaseException as error:
            primary = error
            self.report.update(status="failed", error=f"{type(error).__name__}: {error}"[:1024])
            raise
        finally:
            try:
                self.report["receipt"] = v1._json_new(
                    self.directory / "operator-report.json", self.report
                )
            except BaseException as error:
                self.report["secondary_errors"].append(f"{type(error).__name__}: {error}"[:1024])
                self.report["status"] = "failed"
                if primary is not None:
                    primary.add_note(self.report["secondary_errors"][-1])
                else:
                    raise
        return self.to_mapping()
