"""Cross-language preflight admission; run against the native Qt self-test binary."""

from __future__ import annotations

import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from multidetect.autonomy.mission_package import load_mission_package
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
