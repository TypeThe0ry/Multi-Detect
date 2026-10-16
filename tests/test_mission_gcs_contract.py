"""Cross-language preflight admission; run against the native Qt self-test binary."""

from __future__ import annotations

import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_security import AuthenticationKey, canonical_auth_bytes, sign_document, verify_document
from multidetect.autonomy.serialization import strict_json_loads


def _cases():
    example = json.loads(
        Path("configs/onboard/mission-package-example.json").read_text(encoding="utf-8")
    )
    yield "example", example
    task_only = deepcopy(example)
    del task_only["plan"]
    yield "onboard_compilation", task_only
    for name, path, value in (
        ("equal_reserve", ("budget", "reserve_time_s"), 900),
        ("outside_home", ("home",), [1000, 0]),
        ("unknown_risk", ("risk_level",), "unknown"),
        ("no_goal", ("task_text",), ""),
        ("wrong_heartbeat", ("heartbeat_timeout_s",), True),
        ("zero_heartbeat", ("heartbeat_timeout_s",), 0),
        ("bad_zone_radius", ("zones", "zone:people", "radius_m"), 1000),
        ("bad_zone_tag", ("zones", "zone:people", "risk_tag"), True),
        ("bad_zone_oversight", ("zones", "zone:people", "oversight_required"), 1),
        ("unknown_step", ("oversight_steps",), ["unknown"]),
        ("timed_without_timer", ("link_loss_policy",), {"action": "hold_then_return"}),
        ("bool_timer", ("link_loss_policy", "timeout_s"), False),
        ("bad_policy", ("link_loss_policy", "action"), "manual"),
        ("bad_ref_labels", ("ref_labels", "poi:unknown"), "unknown"),
        ("unknown_top_level", ("extra",), 1),
        ("collapsed_altitude", ("operating_area", "max_altitude_m"), 5),
        ("inverted_area", ("operating_area", "north_min_m"), 101),
        ("outside_route", ("routes", "route:patrol"), [[1000, 0]]),
        ("empty_route", ("routes", "route:patrol"), []),
        ("bad_place", ("places", "poi:gate"), ["40", 0]),
        ("null_task_with_plan", ("task_text",), None),
    ):
        case = deepcopy(example)
        node = case
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        yield name, case
    for name, step_path, value in (
        ("step_altitude_limit", ("params", "altitude_m"), 41),
        ("step_altitude_type", ("params", "altitude_m"), True),
        ("step_unknown_reference", ("params", "target_ref"), "poi:unknown"),
        ("step_route_as_place", ("params", "target_ref"), "route:patrol"),
        ("step_dynamic_finding", ("params", "target_ref"), "finding:1"),
        ("step_bad_dynamic_finding", ("params", "target_ref"), "finding:+1"),
        ("step_bad_track", ("params", "target_ref"), "track:1"),
        ("step_unknown_success", ("on_success",), "unknown"),
        ("step_bad_skill", ("skill",), "fly"),
        ("step_unknown_param", ("params", "extra"), 1),
        ("step_cycle", ("on_success",), "visit_gate"),
        ("trimmed_reference", ("params", "target_ref"), " poi:gate "),
        ("trimmed_skill", ("skill",), " goto "),
        ("trimmed_step_id", ("id",), " visit_gate "),
        ("unicode_step_id", ("id",), "🛩" * 25),
    ):
        case = deepcopy(example)
        node = case["plan"]["steps"][0]
        for key in step_path[:-1]:
            node = node[key]
        node[step_path[-1]] = value
        yield name, case
    case = deepcopy(example)
    case["plan"]["steps"][1]["id"] = "home "
    case["oversight_steps"] = ["home "]
    yield "normalized_oversight_id_mismatch", case
    case = deepcopy(example)
    case["plan"]["plan_version"] = 1.0
    yield "integer_version_not_float", case


@pytest.mark.parametrize("name,document", list(_cases()))
def test_native_package_admission_matches_python(name, document, tmp_path):
    executable = os.environ.get("MULTIDETECT_MISSION_VALIDATOR")
    if not executable:
        pytest.skip("set MULTIDETECT_MISSION_VALIDATOR to MultiDetectMissionSelfTest.exe")
    try:
        load_mission_package(document)
        expected = 0
    except (ValueError, TypeError, KeyError):
        expected = 1
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [executable, "--validate", str(path)], capture_output=True, timeout=10, check=False
    )
    assert result.returncode == expected, result.stdout.decode("utf-8", errors="replace")


@pytest.mark.parametrize(
    "content",
    (
        '{"mission_id":"one","mission_id":"two"}',
        '{"mission_id":"one","mission_\\u0069d":"two"}',
        '{"mission_id":"one","places":{"poi:x":[0,0],"poi:x":[1,1]}}',
        '{"mission_id":NaN}',
    ),
)
def test_native_and_python_reject_ambiguous_json(content, tmp_path):
    executable = os.environ.get("MULTIDETECT_MISSION_VALIDATOR")
    if not executable:
        pytest.skip("set MULTIDETECT_MISSION_VALIDATOR to MultiDetectMissionSelfTest.exe")
    with pytest.raises(ValueError):
        strict_json_loads(content, name="mission package")
    path = tmp_path / "ambiguous.json"
    path.write_text(content, encoding="utf-8")
    result = subprocess.run([executable, "--validate", str(path)], capture_output=True, timeout=10)
    assert result.returncode == 1


def test_gcs_ground_command_golden_vector_matches_onboard_verifier():
    key = AuthenticationKey("ed25519", bytes(range(32)), "gcs-test")
    command = {
        "mission_id": "golden-mission",
        "type": "abort",
        "sequence": 7,
        "timestamp": 1700000000.25,
        "timestamp_version": 1,
        "timestamp_domain": "unix",
    }
    signed = sign_document(command, key)
    assert canonical_auth_bytes({**command, "key_id": "gcs-test"}, "signature", "signature_alg") == (
        b'{"key_id":"gcs-test","mission_id":"golden-mission","sequence":7,'
        b'"timestamp":1700000000.25,"timestamp_domain":"unix",'
        b'"timestamp_version":1,"type":"abort"}'
    )
    assert signed["signature"] == (
        "Ow/0p9a6YI5GDT/z2OZEdSwhslBXBgya64HVaqMCck1KuLW9OQhpx1vtSfBc1xeXxfU2hckAA5DPpvzq1rSmDQ=="
    )
    public = AuthenticationKey("ed25519", Ed25519PrivateKey.from_private_bytes(bytes(range(32))).public_key().public_bytes_raw(), "gcs-test")
    assert verify_document(signed, public)


def test_gcs_canonical_numbers_and_unicode_golden_vector():
    key = AuthenticationKey("ed25519", bytes(range(32)), "gcs-test")
    command = {
        "A": 1.0,
        "a": 1700000000.0,
        "arr": [0.1, 1e-7, 1e21, -2.5, 123456789.125, ["\u96ea", "\U0001f600"]],
        "negative": -42.0,
        "zero": -0.0,
        "\u00e9": "\u96ea\U0001f600",
        "\U0001f600": "\u00e9",
    }
    signed = sign_document(command, key)
    assert canonical_auth_bytes({**command, "key_id": "gcs-test"}, "signature", "signature_alg").hex() == (
        "7b2241223a312c2261223a313730303030303030302c22617272223a5b302e312c31652d30372c31652b32312c"
        "2d322e352c3132333435363738392e3132352c5b22e99baa222c22f09f9880225d5d2c226b65795f6964223a"
        "226763732d74657374222c226e65676174697665223a2d34322c227a65726f223a302c22c3a9223a22e99baa"
        "f09f9880222c22f09f9880223a22c3a9227d"
    )
    assert signed["signature"] == (
        "9SZ0kBIOWhWtHWv4odkBYfpKBpuDBZ/1yKauYtmcD72DG8DqYi9djX+TQM0Yrl05xCxvUAaLRDIEF/tzmAQCAA=="
    )
