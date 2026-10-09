"""CPU math/report seams only; no actual permission, runtime or flight proof."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_onboard_teacher_mission_execution_v1 as old_execution
import test_teacher_live_navigation_checkpoint as checkpoint_tests
from test_sim_navigation_reference import binding, document
from test_sim_teacher_compilation import _inputs
from test_sim_teacher_environment import GROUND, VEHICLE

from multidetect.autonomy.contracts import ValidationFinding, ValidationVerdict
from multidetect.autonomy.serialization import canonical_json
from multidetect.autonomy.sim_teacher_compilation import compile_qualified_sim_teacher_held_mission
from multidetect.autonomy.sim_teacher_environment import load_static_teacher_environment
from multidetect.autonomy.teacher_live_navigation_checkpoint import (
    qualify_teacher_live_navigation_checkpoint,
)
from multidetect.mission_journal import MissionJournalIdentity
from multidetect.px4_mission_acceptance import _build_transaction_documents
from scripts import onboard_teacher_mission_execution_v2 as execution_api
from scripts import onboard_teacher_mission_live_revalidation_v1 as api


@pytest.fixture
def teacher(tmp_path):
    inputs = _inputs(tmp_path)
    draft, qualification = compile_qualified_sim_teacher_held_mission(**inputs)
    return SimpleNamespace(draft=draft, qualification=qualification, inputs=inputs)


def test_current_budget_keeps_fixed_return_reserve_and_original_artifact(teacher):
    h = teacher
    before = h.draft.to_mapping()
    at_check = api.recompute_teacher_live_budget(
        h.draft, h.draft.context, h.qualification, now_s=11
    )
    later = api.recompute_teacher_live_budget(h.draft, h.draft.context, h.qualification, now_s=11.5)
    battery = h.draft.context.battery_remaining_pct
    reserve = (
        h.draft.task.constraints.minimum_return_battery_pct
        + h.draft.planner_policy.minimum_battery_margin_pct
    )
    expected = h.qualification.modeled_endurance_before_age_s * (battery - reserve) / battery - 1
    assert at_check["available_above_fixed_return_reserve_s"] == pytest.approx(expected)
    assert later["available_above_fixed_return_reserve_s"] == pytest.approx(expected - 0.5)
    assert at_check["full_route_estimated_duration_s"] == pytest.approx(
        h.draft.budget_review["total_estimated_duration_s"]
    )
    assert at_check["reserve_deducted_once"] is True
    assert h.draft.to_mapping() == before


@pytest.mark.parametrize("now", [9.9, 12.0, 15.0])
def test_current_budget_rejects_future_or_expired_current_sources(teacher, now):
    with pytest.raises(ValueError, match="window binding"):
        api.recompute_teacher_live_budget(
            teacher.draft, teacher.draft.context, teacher.qualification, now_s=now
        )


@pytest.mark.parametrize(
    "updates,error",
    [
        ({"gps_fix_type": 2}, "GPS"),
        ({"gps_horizontal_accuracy_m": 10}, "GPS"),
        ({"ground_speed_mps": 1}, "ground motion"),
        ({"battery_remaining_pct": 30}, "reserve"),
        ({"airframe_profile_hash": "d" * 64}, "profile"),
    ],
)
def test_current_budget_rejects_new_unsafe_inputs(teacher, updates, error):
    context = replace(teacher.draft.context, **updates)
    qualification = replace(teacher.qualification, qualified_context_sha256=context.canonical_hash)
    with pytest.raises((ValueError, TypeError), match=error):
        api.recompute_teacher_live_budget(teacher.draft, context, qualification, now_s=11)


def test_current_wind_math_is_recomputed_not_borrowed_from_old_plan(teacher):
    context = replace(teacher.draft.context, wind_east_mps=10)
    qualification = replace(teacher.qualification, qualified_context_sha256=context.canonical_hash)
    with pytest.raises(ValueError):
        api.recompute_teacher_live_budget(teacher.draft, context, qualification, now_s=11)


def test_current_budget_does_not_set_legacy_resource_flags(teacher):
    context = replace(teacher.draft.context, map_cache_available=False, dem_available=False)
    qualification = replace(teacher.qualification, qualified_context_sha256=context.canonical_hash)
    result = api.recompute_teacher_live_budget(teacher.draft, context, qualification, now_s=11)
    assert result["full_route_estimated_duration_s"] > 0
    assert context.map_cache_available is False and context.dem_available is False
    # This helper's math success is deliberately NOT environment admission.


@pytest.fixture
def report_case(teacher):
    h = teacher
    # Synthetic immutable source labels for ONLY the finding parser test.
    # Full revalidator rejects these stand-ins; they never become live leases.
    original = api.TeacherMissionOriginalInputs(
        h.draft,
        SimpleNamespace(canonical_hash="1" * 64),
        h.qualification,
        SimpleNamespace(
            canonical_hash="2" * 64,
            to_mapping=lambda: {"scene_sha256": "3" * 64, "airframe_envelope_sha256": "4" * 64},
        ),
        SimpleNamespace(canonical_hash="5" * 64),
    )
    report, bindings, grant = _build_transaction_documents(
        vehicle_class="multirotor",
        compiled=h.draft.compiled_mission,
        context_snapshot=h.draft.context,
        platform_plan=h.draft.compilation.plan,
        policy=h.draft.safety_policy,
        transaction_id="CPU-report-parser-not-actual-authority",
        now_utc=old_execution.fixtures.NOW,
    )
    del grant
    bindings = replace(bindings, task_spec_hash=h.draft.task.canonical_hash)
    finding = ValidationFinding(
        api.PLAN_ADMISSION_RULE,
        ValidationVerdict.PASS,
        canonical_json(original.admission_mapping()),
    )
    report = replace(report, findings=(*report.findings, finding))
    identity = MissionJournalIdentity(
        "CPU-report-parser-not-actual-authority",
        h.draft.compiled_mission.executable_mission.canonical_hash,
        h.draft.compiled_mission.wire_hash,
        report.canonical_hash,
        bindings,
        len(h.draft.compiled_mission.wire_items),
        h.draft.compiled_mission.executable_mission.airframe_adapter_id,
    )
    return original, identity, report


def test_source_finding_exact_canonical_identity_is_checked(report_case):
    api.validate_teacher_plan_source_admission(*report_case)


@pytest.mark.parametrize(
    "change", ["missing", "duplicate", "different-source", "noncanonical", "different-journal"]
)
def test_source_finding_cannot_be_missing_replayed_or_changed(report_case, change):
    original, identity, report = report_case
    finding = report.findings[-1]
    if change == "missing":
        report = replace(report, findings=report.findings[:-1])
    elif change == "duplicate":
        report = replace(report, findings=(*report.findings, finding))
    elif change == "different-source":
        value = original.admission_mapping()
        value["original_navigation_checkpoint_sha256"] = "e" * 64
        report = replace(
            report, findings=(*report.findings[:-1], replace(finding, reason=canonical_json(value)))
        )
    elif change == "noncanonical":
        report = replace(
            report,
            findings=(
                *report.findings[:-1],
                replace(finding, reason=finding.reason.replace('":', '": ', 1)),
            ),
        )
    if change != "different-journal":
        identity = replace(identity, validation_report_hash=report.canonical_hash)
    else:
        identity = replace(identity, validation_report_hash="f" * 64)
    with pytest.raises(ValueError):
        api.validate_teacher_plan_source_admission(original, identity, report)


@pytest.fixture
def harness(tmp_path):
    yield from old_execution.harness.__wrapped__(tmp_path)


def test_V8_does_not_accept_fixture_generic_PASS_or_missing_real_plan_admission(harness, teacher):
    h = harness
    original = api.TeacherMissionOriginalInputs(
        teacher.draft, None, teacher.qualification, None, None
    )
    before = list(h.connection.mav.calls)
    with pytest.raises(ValueError, match="binding differs"):
        execution_api.TeacherMissionExecutionV2(
            original_inputs=original,
            live_input_provider=lambda **_: (_ for _ in ()).throw(AssertionError("must not run")),
            **h.inputs,
        )
    assert h.connection.mav.calls == before
    assert not (h.tmp_path / "execution").exists()


def test_new_production_modules_do_not_create_grants_force_or_new_transport_facades():
    from pathlib import Path

    for module in (api, execution_api):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "_build_transaction_documents" not in source
        assert "21196" not in source
        assert "recv_match(" not in source
        assert "OperatorGrant(" not in source
    assert (
        execution_api.OwnedTeacherGazeboSessionV8.start_receiver
        is execution_api.frozen.OwnedTeacherGazeboSessionV7.start_receiver
    )


@pytest.fixture
def current_source_case(tmp_path):
    # Independent CRC-encoded packets and immutable temporary source files.
    # No report, transaction, Grant or execution object is created here.
    fixture = checkpoint_tests.live_fixture.__wrapped__(tmp_path)
    args, cap, clock, native = next(fixture)
    root = args["artifact_root"]
    xml = (root / "world.sdf").read_text(encoding="utf-8").replace("</world>", GROUND + "</world>")
    args["world_source"] = binding(root, "world.sdf", xml.encode())
    checkpoint = qualify_teacher_live_navigation_checkpoint(**args)
    reply = ("data: " + json.dumps(xml.replace("</world>", VEHICLE + "</world>")) + "\n").encode()
    loaded = native(
        "current-loaded-world",
        reply,
        100.46,
        100.50,
        ("docker", "exec", args["container_id"], *api.WORLD_ARGS),
    )
    loaded_source = binding(root, "native-current-loaded-world.bin")
    environment = load_static_teacher_environment(
        artifact_root=root,
        world_source=args["world_source"],
        generated_sdf_source=loaded_source,
        include_model_sources={},
    )
    live = api.TeacherMissionLiveInputs(
        checkpoint, environment, document(root, "loaded-native.json", loaded)
    )
    check = dict(
        expected_runtime=checkpoint.runtime_attestation,
        expected_world=checkpoint.world_source,
        expected_scene=environment.scene,
        expected_origin_continuity_token=checkpoint.origin_continuity_token,
        root=root,
        clock=clock.session_id,
        start=100.0,
        now=clock.monotonic(),
    )
    try:
        yield SimpleNamespace(
            live=live, check=check, cap=cap, clock=clock, root=root, loaded=loaded
        )
    finally:
        fixture.close()


def test_current_source_subcheck_reopens_crc_camera_native_and_static_sources(current_source_case):
    h = current_source_case
    checkpoint, loaded_start = api._current_phase_sources(h.live, **h.check)
    assert checkpoint is h.live.navigation_checkpoint and loaded_start == 100.46
    assert not any(checkpoint.to_mapping()["claims"].values())
    assert checkpoint.observed_context.geofence_valid is False
    assert checkpoint.observed_context.map_cache_available is False
    assert checkpoint.observed_context.dem_available is False
    # Only the source subcheck passed: it issues no complete live Mission
    # receipt, original-plan finding, persistent transaction or Grant.


@pytest.mark.parametrize(
    "change", ["expired", "pre-phase", "reset-token", "foreign-runtime", "foreign-world", "closed"]
)
def test_current_phase_navigation_cannot_be_borrowed_or_renewed(current_source_case, change):
    h = current_source_case
    check = dict(h.check)
    if change == "expired":
        h.clock.value = check["now"] = h.live.navigation_checkpoint.expires_at_s
    elif change == "pre-phase":
        check["start"] = 100.37
    elif change == "reset-token":
        check["expected_origin_continuity_token"] = dict(check["expected_origin_continuity_token"])
        check["expected_origin_continuity_token"]["ref_timestamp"] += 1
    elif change == "foreign-runtime":
        check["expected_runtime"] = None
    elif change == "foreign-world":
        check["expected_world"] = replace(check["expected_world"], sha256="f" * 64)
    else:
        h.cap.abort("CPU closed-writer source case")
    with pytest.raises((ValueError, RuntimeError)):
        api._current_phase_sources(h.live, **check)


@pytest.mark.parametrize("path", ["rgb-1.bin", "world.sdf", "native-current-loaded-world.bin"])
def test_current_source_byte_replacement_is_rejected_without_source_doubles(
    current_source_case, path
):
    h = current_source_case
    target = h.root / path
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(ValueError):
        api._current_phase_sources(h.live, **h.check)


@pytest.mark.parametrize(
    "change", ["container", "argv", "native-exit", "native-future", "native-pre-phase"]
)
def test_loaded_world_must_be_actual_same_runtime_finite_phase_output(current_source_case, change):
    h = current_source_case
    row = dict(h.loaded)
    if change == "container":
        row["container_id"] = "f" * 64
    elif change == "argv":
        row["command"] = ["docker", "exec", "f" * 64, *api.WORLD_ARGS]
    elif change == "native-exit":
        row["native_exit_code"] = 1
    elif change == "native-future":
        row["finished_at_s"] = 100.52
    else:
        row["started_at_s"] = 99.9
    live = replace(h.live, loaded_world_native_source=document(h.root, "bad-native.json", row))
    with pytest.raises(ValueError):
        api._current_phase_sources(live, **h.check)


def _on_independent_sender_thread(callback):
    outcome = []

    def run():
        try:
            outcome.append((True, callback()))
        except BaseException as error:
            outcome.append((False, error))

    thread = threading.Thread(target=run, name="CPU-V8-actual-readonly-sender")
    thread.start()
    thread.join(2)
    assert not thread.is_alive() and len(outcome) == 1
    valid, value = outcome[0]
    if not valid:
        raise value
    return value


def test_main_current_check_and_RX_readonly_reproduction_have_distinct_original_times(
    current_source_case,
):
    h = current_source_case
    cp = h.live.navigation_checkpoint
    assert cp.qualified_at_s == 100.51
    h.clock.value = 100.60  # Phase check is NOT the checkpoint qualification.
    phase_check = {**h.check, "now": h.clock.value}
    assert api._current_phase_sources(h.live, **phase_check)[0] is cp
    before = cp.to_mapping()
    # Re-reading after 200ms exceeds the CP current-time clock slack, but is
    # still within the EXISTING 2-second phase/source lease. This is source
    # identity reproduction only, never a fresh qualification or authority.
    h.clock.value = 100.80
    result = _on_independent_sender_thread(
        lambda: api._current_phase_sources(
            h.live, **phase_check, _reproduce_checkpoint_identity_only=True
        )
    )
    assert result[0] is cp and cp.to_mapping() == before
    assert not h.cap._closed


@pytest.mark.parametrize("same_checkpoint_time", [False, True])
def test_sender_cannot_use_main_current_qualification_API(
    current_source_case, same_checkpoint_time
):
    h = current_source_case
    h.clock.value = 100.51 if same_checkpoint_time else 100.60
    with pytest.raises(RuntimeError, match="another"):
        _on_independent_sender_thread(
            lambda: api._current_phase_sources(h.live, **{**h.check, "now": h.clock.value})
        )


def test_readonly_reproduction_still_rejects_replaced_original_source_on_RX(current_source_case):
    h = current_source_case
    h.clock.value = 100.80
    target = h.root / "rgb-1.bin"
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(ValueError):
        _on_independent_sender_thread(
            lambda: api._current_phase_sources(
                h.live, **{**h.check, "now": 100.60}, _reproduce_checkpoint_identity_only=True
            )
        )


def test_current_public_entry_cannot_fake_now_equal_to_checkpoint_time(current_source_case):
    h = current_source_case
    h.clock.value = 100.80
    with pytest.raises(ValueError, match="actual current clock"):
        api._current_phase_sources(h.live, **h.check)


def test_append_is_valid_history_but_invalidates_the_execution_phase_generation(
    current_source_case,
):
    h = current_source_case
    execution = execution_api.TeacherMissionExecutionV2.__new__(
        execution_api.TeacherMissionExecutionV2
    )
    execution._transport = SimpleNamespace(_capture=h.cap, _clock=h.clock.monotonic)
    execution.intent = SimpleNamespace(clock_id=h.clock.session_id)
    execution._owner = threading.get_ident()
    execution._check_live_writer(h.live)
    h.clock.value = 100.60
    message = checkpoint_tests.health_messages()[0]
    message.pack(checkpoint_tests.Sender(None))
    h.cap.append_batch([(message, 100.59)])
    # The old source prefix is still reproducible, but cannot silently ignore
    # this known newer committed observation for execution.
    assert (
        api._current_phase_sources(
            h.live, **{**h.check, "now": 100.60}, _reproduce_checkpoint_identity_only=True
        )[0]
        is h.live.navigation_checkpoint
    )
    with pytest.raises(ValueError, match="dual-committed source generation changed"):
        _on_independent_sender_thread(lambda: execution._check_live_writer(h.live))
