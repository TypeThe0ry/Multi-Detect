from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.simulator.px4_launch import (
    Px4LaunchError,
    build_px4_launch_plan,
    patch_rc_mavlink,
    patch_rcS,
    verify_px4_launch_plan,
)
from multidetect.simulator.stack import stack_plan_from_scenario

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"


def _stack_plan(*, px4: bool = False):
    return stack_plan_from_scenario(
        SCENARIO,
        repo_root=ROOT,
        tool_capabilities={"px4": px4, "gazebo": False},
    )


def _fake_px4_tree(tmp_path: Path) -> tuple[Path, bytes, bytes]:
    workdir = tmp_path / "px4-build"
    init = workdir / "etc" / "init.d-posix"
    init.mkdir(parents=True)
    px4 = workdir / "bin" / "px4"
    px4.parent.mkdir(parents=True)
    # The planner only resolves and records the binary.  A shell stub keeps
    # the fixture platform-neutral and avoids starting a process in tests.
    px4.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    px4.chmod(0o755)
    rc_s = b"#!/bin/sh\n. px4-rc.mavlink\n"
    rc_mavlink = (
        b"#!/bin/sh\n"
        b"udp_gcs_port_local=18570\n"
        b"mavlink start -x -u $udp_gcs_port_local -r 4000000 -f\n"
        b"mavlink stream -r 50 -s GLOBAL_POSITION_INT -u $udp_gcs_port_local\n"
    )
    (init / "rcS").write_bytes(rc_s)
    (init / "px4-rc.mavlink").write_bytes(rc_mavlink)
    return workdir, rc_s, rc_mavlink


def _plan_with_fake_px4(tmp_path: Path):
    workdir, rc_s, rc_mavlink = _fake_px4_tree(tmp_path)
    px4 = workdir / "bin" / "px4"
    plan = _stack_plan(px4=True)
    # build_stack_plan deliberately restricts PX4_WORKDIR to the repository;
    # replacing the immutable environment here models a caller-provided stock
    # checkout while preserving the original hash-bound scenario/assets.
    plan = replace(
        plan,
        px4_executable=str(px4),
        px4_command=(str(px4), "-i", "0", "-s", "etc/init.d-posix/rcS"),
        environment={**plan.environment_mapping, "PX4_WORKDIR": str(workdir)},
    )
    return plan, workdir, rc_s, rc_mavlink


def test_dry_run_generates_hash_bound_template_and_evidence_without_px4(
    tmp_path: Path,
) -> None:
    plan = build_px4_launch_plan(
        _stack_plan(),
        job_dir=tmp_path / "job",
        dry_run=True,
    )

    assert plan.dry_run is True
    assert plan.ready_to_launch is False
    assert plan.generated_rc_mavlink.is_file()
    assert plan.wrapper_path.is_file()
    assert plan.verify() is True
    assert verify_px4_launch_plan(plan) is True
    assert plan.to_mapping()["truth_free"] is True
    assert plan.to_mapping()["runtime"]["external_process_started"] is False
    assert plan.to_mapping()["ports"] == {
        "host": "127.0.0.1",
        "control_port": 14540,
        "telemetry_port": 14541,
        "control_endpoint": "udpout:127.0.0.1:14540",
        "telemetry_endpoint": "udp://127.0.0.1:14541",
        "loopback_only": True,
        "distinct": True,
    }
    document = json.loads((plan.job_dir / "px4-launch.json").read_text(encoding="utf-8"))
    assert document["sha256"] == plan.evidence_hash
    assert document["source_stack_plan_sha256"] == plan.source_stack_plan_sha256
    assert document["scenario_manifest_sha256"] == plan.scenario_manifest_sha256
    assert document["readiness"]["gate_passed"] is False


def test_valid_fake_px4_tree_is_strict_ready_and_does_not_modify_source(
    tmp_path: Path,
) -> None:
    plan, workdir, original_rc_s, original_rc_mavlink = _plan_with_fake_px4(tmp_path)
    source_before = (workdir / "etc" / "init.d-posix" / "px4-rc.mavlink").read_bytes()
    rc_s_before = (workdir / "etc" / "init.d-posix" / "rcS").read_bytes()

    launch = build_px4_launch_plan(
        plan,
        job_dir=tmp_path / "job",
        dry_run=False,
        strict=True,
    )

    assert launch.ready_to_launch is True
    assert launch.startup_patch_applied is True
    assert launch.source_is_template is False
    assert launch.px4_workdir == workdir.resolve()
    assert launch.command == (str(launch.wrapper_path),)
    assert launch.environment_mapping["MD_SIM_STACK_PLAN_SHA256"] == plan.evidence_hash
    assert launch.environment_mapping["MD_SIM_SCENARIO_SHA256"] == plan.scenario_manifest_hash
    generated = launch.generated_rc_mavlink.read_text(encoding="utf-8")
    generated_rcs = launch.generated_rcS.read_text(encoding="utf-8")
    wrapper = launch.wrapper_path.read_text(encoding="utf-8")
    assert generated.count("BEGIN MULTIDETECT SIM MAVLINK OVERRIDE") == 1
    assert generated.count("mavlink start -x") == 1
    assert "mavlink start -x -t 127.0.0.1 -u 14540 -o 14541" in generated
    assert "PATH=\"$MD_SIM_PX4_OVERLAY:$PATH\"" in wrapper
    assert "export PX4_NET_INTERFACE=" in wrapper
    assert "-i 0 -s etc/init.d-posix/rcS" in wrapper
    assert "PX4_MAVLINK_CONTROL_PORT=14540" in wrapper
    assert "PX4_MAVLINK_TELEMETRY_PORT=14541" in wrapper
    assert launch.generated_rcS.is_file()
    assert launch.readiness_mapping["explicit_rc_mavlink_reference"] is True
    assert launch.readiness_mapping["startup_rcS_patch_applied"] is True
    assert '. "$MD_SIM_PX4_RC_MAVLINK"' in generated_rcs
    assert generated_rcs.count(". px4-rc.mavlink") == 0
    assert '-s "$MD_SIM_PX4_RCS"' in wrapper
    assert 'command -v px4-rc.mavlink' in wrapper
    assert (workdir / "etc" / "init.d-posix" / "px4-rc.mavlink").read_bytes() == source_before
    assert (workdir / "etc" / "init.d-posix" / "rcS").read_bytes() == rc_s_before
    assert original_rc_s == rc_s_before
    assert original_rc_mavlink == source_before
    assert launch.verify() is True


def test_patch_rc_mavlink_is_idempotent_and_preserves_non_endpoint_arguments() -> None:
    plan = _stack_plan()
    source = (
        "#!/bin/sh\r\n"
        "udp_gcs_port_local=18570\r\n"
        "mavlink start -x -u $udp_gcs_port_local -t 0.0.0.0 -o 18571 -r 4000000 -f\r\n"
        "mavlink stream -r 50 -s GLOBAL_POSITION_INT -u $udp_gcs_port_local\r\n"
    )
    first = patch_rc_mavlink(
        source,
        control_port=14540,
        telemetry_port=14541,
        stack_plan_sha256=plan.evidence_hash,
        scenario_manifest_sha256=plan.scenario_manifest_hash,
    )
    second = patch_rc_mavlink(
        first,
        control_port=14540,
        telemetry_port=14541,
        stack_plan_sha256=plan.evidence_hash,
        scenario_manifest_sha256=plan.scenario_manifest_hash,
    )

    assert first == second
    assert first.count("BEGIN MULTIDETECT SIM MAVLINK OVERRIDE") == 1
    assert first.count("mavlink start -x") == 1
    assert "-t 0.0.0.0" not in first
    assert "-o 18571" not in first
    assert "-r 4000000 -f" in first
    assert "\r" not in first


def test_patch_rcs_replaces_stock_source_with_hash_bound_job_file() -> None:
    plan = _stack_plan()
    source = "#!/bin/sh\nset +e\n. px4-rc.mavlink\n"
    first = patch_rcS(
        source,
        patched_rc_mavlink="/tmp/job/px4-init/px4-rc.mavlink",
        stack_plan_sha256=plan.evidence_hash,
        scenario_manifest_sha256=plan.scenario_manifest_hash,
    )
    second = patch_rcS(
        first,
        patched_rc_mavlink="/tmp/job/px4-init/px4-rc.mavlink",
        stack_plan_sha256=plan.evidence_hash,
        scenario_manifest_sha256=plan.scenario_manifest_hash,
    )

    assert first == second
    assert first.count("BEGIN MULTIDETECT SIM PX4 RCS OVERRIDE") == 1
    assert first.count(". px4-rc.mavlink") == 0
    assert first.count('. "$MD_SIM_PX4_RC_MAVLINK"') == 1
    assert "MD_SIM_STACK_PLAN_SHA256=" + plan.evidence_hash in first
    assert "MD_SIM_SCENARIO_SHA256=" + plan.scenario_manifest_hash in first


def test_patch_rcs_rejects_missing_source_anchor() -> None:
    plan = _stack_plan()
    with pytest.raises(Px4LaunchError, match="source statement"):
        patch_rcS(
            "#!/bin/sh\ntrue\n",
            patched_rc_mavlink="/tmp/job/px4-init/px4-rc.mavlink",
            stack_plan_sha256=plan.evidence_hash,
            scenario_manifest_sha256=plan.scenario_manifest_hash,
        )


def test_strict_missing_px4_files_reports_all_failed_readiness(tmp_path: Path) -> None:
    with pytest.raises(Px4LaunchError, match="strict PX4 launch readiness failed") as caught:
        build_px4_launch_plan(
            _stack_plan(),
            job_dir=tmp_path / "strict",
            dry_run=False,
            strict=True,
        )
    message = str(caught.value)
    assert "px4_executable_available" in message
    assert "px4_workdir_resolved" in message
    assert "rcS_present" in message
    assert "startup_patch_applied" in message


def test_endpoint_overrides_and_stale_environment_are_rejected(tmp_path: Path) -> None:
    plan = _stack_plan()
    with pytest.raises(Px4LaunchError, match="control_port conflicts"):
        build_px4_launch_plan(plan, job_dir=tmp_path / "control", control_port=14542)
    with pytest.raises(Px4LaunchError, match="telemetry_port conflicts"):
        build_px4_launch_plan(plan, job_dir=tmp_path / "telemetry", telemetry_port=14542)
    stale = replace(
        plan,
        environment={**plan.environment_mapping, "PX4_MAVLINK_UDP_PORT": "19999"},
    )
    with pytest.raises(Px4LaunchError, match="PX4_MAVLINK_UDP_PORT conflicts"):
        build_px4_launch_plan(stale, job_dir=tmp_path / "stale")


def test_non_loopback_target_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(Px4LaunchError, match="literal loopback"):
        build_px4_launch_plan(_stack_plan(), job_dir=tmp_path / "unsafe", target_host="0.0.0.0")


def test_verify_detects_tampered_job_artifact(tmp_path: Path) -> None:
    launch = build_px4_launch_plan(_stack_plan(), job_dir=tmp_path / "job")
    launch.wrapper_path.write_text(
        launch.wrapper_path.read_text(encoding="utf-8") + "# tampered\n",
        encoding="utf-8",
    )
    assert launch.verify() is False
