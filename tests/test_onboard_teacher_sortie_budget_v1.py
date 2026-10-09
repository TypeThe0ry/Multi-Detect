"""CPU arithmetic only, not a measured rate, Grant or flight proof."""

from dataclasses import replace

import pytest

from scripts.onboard_teacher_sortie_budget_v1 import TeacherSortieLifecycleBudget


@pytest.fixture
def budget():
    return TeacherSortieLifecycleBudget(
        clock_id="CPU-once-bound",
        started_at_s=100,
        preflight_s=30,
        transfer_s=20,
        mission_mode_s=5,
        normal_arm_s=10,
        route_s=120,
        rtl_land_s=30,
        cleanup_s=20,
        packet_rate_ceiling_hz=220,
        protocol_packet_reserve=128,
        maximum_raw_record_bytes=4608,
        maximum_projection_record_bytes=8192,
        maximum_receipt_record_bytes=8192,
        snapshot_bytes=2 * 1024 * 1024,
        camera_bytes=64 * 1024 * 1024,
        evidence_bytes=2 * 1024 * 1024,
        start_camera_reserve_bytes=32 * 1024 * 1024,
        start_evidence_reserve_bytes=1024 * 1024,
        queue_capacity=512,
        maximum_consumer_blocked_s=1,
        maximum_dual_commit_s=0.5,
    )


def start_inputs(budget):
    return dict(
        now_s=140,
        clock_id=budget.clock_id,
        route_duration_s=100,
        used_records=2000,
        used_telemetry_bytes=20 * 1024 * 1024,
        remaining_camera_bytes=budget.start_camera_reserve_bytes,
        remaining_evidence_bytes=budget.start_evidence_reserve_bytes,
    )


def test_once_bound_lifecycle_is_canonical_and_exceeds_old_soft_caps(budget):
    assert budget.duration_s == 235 and budget.deadline_s == 335
    assert budget.record_limit == 51828 > 8192
    assert budget.telemetry_byte_limit > 64 * 1024 * 1024
    assert budget.phase_deadline_s("route") == 285
    assert TeacherSortieLifecycleBudget.from_mapping(budget.to_mapping()) == budget
    budget.assert_start_capacity(**start_inputs(budget))


@pytest.mark.parametrize(
    "field,value",
    [
        ("clock_id", "other"),
        ("now_s", 150.01),
        ("route_duration_s", 121),
        ("used_records", 50000),
        ("used_telemetry_bytes", 500 * 1024 * 1024),
        ("remaining_camera_bytes", 0),
        ("remaining_evidence_bytes", 0),
    ],
)
def test_no_ARM_without_whole_return_and_cleanup_reserve(budget, field, value):
    inputs = start_inputs(budget)
    inputs[field] = value
    with pytest.raises(ValueError):
        budget.assert_start_capacity(**inputs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("route_s", 600),
        ("cleanup_s", 0),
        ("rtl_land_s", 0),
        ("preflight_s", True),
        ("packet_rate_ceiling_hz", float("nan")),
        ("queue_capacity", 16),
        ("maximum_projection_record_bytes", True),
        ("start_camera_reserve_bytes", 100 * 1024 * 1024),
    ],
)
def test_invalid_or_uncoverable_contract_is_refused(budget, field, value):
    with pytest.raises(ValueError):
        replace(budget, **{field: value})


def test_phase_reads_cannot_renew_the_lifecycle(budget):
    before = budget.to_mapping()
    budget.assert_phase("transfer", now_s=149.9, clock_id=budget.clock_id)
    with pytest.raises(ValueError):
        budget.assert_phase("transfer", now_s=150, clock_id=budget.clock_id)
    assert budget.remaining_s(335, clock_id=budget.clock_id) == 0
    assert budget.to_mapping() == before
    with pytest.raises(ValueError):
        budget.remaining_s(99, clock_id=budget.clock_id)
