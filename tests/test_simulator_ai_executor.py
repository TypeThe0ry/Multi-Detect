from __future__ import annotations

import pytest

from multidetect.simulator.ai_executor import (
    DeterministicSimulatorAiPolicy,
    SimulatorAiExecutor,
    SimulatorAiExecutorConfig,
    SimulatorAiMode,
    SimulatorTelemetrySnapshot,
)
from multidetect.simulator.control import ControlAuthorityState, SimulatorControlAuthority
from multidetect.simulator.mavlink_control import (
    SimulatorMavlinkControlAdapter,
    SimulatorMavlinkControlConfig,
)


class FakeMav:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int, int, int, int, int]] = []

    def manual_control_send(self, *args: int) -> None:
        self.sent.append(tuple(args))


class FakeConnection:
    def __init__(self) -> None:
        self.mav = FakeMav()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def make_executor(
    *,
    watchdog_s: float = 0.5,
    config: SimulatorAiExecutorConfig | None = None,
) -> tuple[SimulatorAiExecutor, SimulatorControlAuthority, FakeConnection]:
    authority = SimulatorControlAuthority()
    connection = FakeConnection()
    adapter = SimulatorMavlinkControlAdapter(
        authority,
        config=SimulatorMavlinkControlConfig(watchdog_s=watchdog_s),
        connection=connection,
    )
    executor = SimulatorAiExecutor(adapter, config=config)
    return executor, authority, connection


def snapshot(**overrides: object) -> SimulatorTelemetrySnapshot:
    values: dict[str, object] = {
        "observed_at_s": 1.0,
        "altitude_agl_m": 30.0,
        "heading_deg": 0.0,
        "ground_speed_mps": 8.0,
        "link_healthy": True,
        "armed": True,
        # A return decision requires a positively observed satellite count;
        # omitting it is intentionally treated as unknown/fail-closed by the
        # production GPS quality gate.
        "satellites_visible": 12,
        # Keep the dedicated raw-GPS sample cursor explicit in direct policy
        # fixtures.  Fused telemetry freshness alone must not qualify RTL.
        "gps_observed_at_s": 1.0,
    }
    values.update(overrides)
    return SimulatorTelemetrySnapshot(**values)


def test_policy_is_deterministic_and_bounded_without_truth_fields() -> None:
    policy = DeterministicSimulatorAiPolicy()
    observed = snapshot(
        target_visible=True,
        target_bearing_deg=90.0,
        target_distance_m=90.0,
        target_confidence=0.91,
    )
    first = policy.decide(observed, now_s=2.0)
    second = policy.decide(observed, now_s=2.0)
    assert first == second
    assert first.mode is SimulatorAiMode.TRACK
    assert first.command is not None
    assert -0.5 <= first.command.roll <= 0.5
    assert -0.5 <= first.command.pitch <= 0.5
    assert -0.6 <= first.command.yaw <= 0.6
    assert 0.15 <= first.command.throttle <= 0.8
    mapping = observed.to_mapping()
    assert mapping["truth_free"] is True
    assert "ground_truth_target" not in mapping
    assert "target_latitude_deg" not in mapping


def test_executor_requires_explicit_enable() -> None:
    executor, authority, connection = make_executor()
    decision = executor.step(snapshot(), now_s=1.0)
    assert decision.mode is SimulatorAiMode.PAUSED
    assert decision.reason == "ai_not_enabled"
    assert decision.accepted is False
    assert authority.state is ControlAuthorityState.PAUSED
    assert connection.mav.sent == []


@pytest.mark.parametrize(
    ("field", "value", "reason", "flag"),
    (
        ("armed", None, "vehicle_armed_state_unknown", "armed_unknown"),
        ("link_healthy", None, "flight_controller_link_unknown", "link_unknown"),
    ),
)
def test_policy_fails_closed_when_lifecycle_observation_is_unknown(
    field: str, value: object, reason: str, flag: str
) -> None:
    policy = DeterministicSimulatorAiPolicy()
    observed = snapshot(**{field: value})
    decision = policy.decide(observed, now_s=1.0)
    assert decision.mode is SimulatorAiMode.FAILSAFE
    assert decision.reason == reason
    assert flag in decision.safety_flags


def test_enable_executes_hover_through_shared_adapter() -> None:
    executor, authority, connection = make_executor()
    executor.enable_ai()
    assert authority.state is ControlAuthorityState.AI
    decision = executor.step(snapshot(), now_s=1.0)
    assert decision.mode is SimulatorAiMode.HOVER
    assert decision.accepted is True
    assert decision.receipt is not None
    # Adapter maps normalized hover throttle 0.5 to PX4/QGC z=500.
    assert connection.mav.sent[-1] == (1, 0, 0, 500, 0, 0)


def test_manual_takeover_suppresses_ai_until_explicit_release() -> None:
    executor, authority, connection = make_executor()
    executor.enable_ai()
    executor.step(snapshot(), now_s=1.0)
    before = len(connection.mav.sent)
    executor.adapter.submit_manual(
        roll=0.2,
        pitch=0.0,
        yaw=0.0,
        throttle=0.6,
        timestamp_s=1.1,
    )
    suppressed = executor.step(snapshot(observed_at_s=1.2), now_s=1.2)
    assert suppressed.mode is SimulatorAiMode.SUPPRESSED
    assert suppressed.reason == "manual_control_priority"
    assert len(connection.mav.sent) == before + 1
    assert authority.state is ControlAuthorityState.MANUAL
    executor.release_manual_control()
    resumed = executor.step(snapshot(observed_at_s=1.3), now_s=1.3)
    assert resumed.mode is SimulatorAiMode.HOVER
    assert resumed.accepted is True
    assert authority.state is ControlAuthorityState.AI


def test_track_and_return_are_explainable_and_use_only_axes() -> None:
    executor, authority, connection = make_executor()
    executor.enable_ai()
    tracked = executor.step(
        snapshot(
            target_visible=True,
            target_bearing_deg=20.0,
            target_distance_m=90.0,
            target_confidence=0.8,
        ),
        now_s=1.0,
    )
    assert tracked.mode is SimulatorAiMode.TRACK
    assert tracked.reason == "target_observation_qualified"
    assert tracked.command is not None
    assert tracked.command.yaw > 0.0
    returned = executor.step(
        snapshot(
            observed_at_s=1.1,
            return_requested=True,
            home_bearing_deg=-30.0,
            home_distance_m=120.0,
            latitude_deg=1.3,
            longitude_deg=103.8,
            gps_fix_type=3,
            gps_horizontal_accuracy_m=2.0,
            gps_vertical_accuracy_m=2.0,
            satellites_visible=12,
            gps_observed_at_s=1.1,
        ),
        now_s=1.1,
    )
    assert returned.mode is SimulatorAiMode.RETURN
    assert returned.reason == "return_requested"
    assert returned.accepted is True
    assert returned.command is not None
    assert len(connection.mav.sent) == 2
    # No mode/mission/arm methods are exposed by this executor; only the
    # MANUAL_CONTROL tuple reaches the fake MAVLink transport.
    assert all(len(message) == 6 for message in connection.mav.sent)
    assert authority.state is ControlAuthorityState.AI


def test_bad_return_gps_enters_failsafe_and_neutralizes_active_stream() -> None:
    executor, authority, connection = make_executor()
    executor.enable_ai()
    executor.step(snapshot(), now_s=1.0)
    decision = executor.step(
        snapshot(
            observed_at_s=1.1,
            return_requested=True,
            home_bearing_deg=0.0,
            home_distance_m=100.0,
            gps_fix_type=2,
            gps_horizontal_accuracy_m=30.0,
        ),
        now_s=1.1,
    )
    assert decision.mode is SimulatorAiMode.FAILSAFE
    assert decision.reason == "return_gps_quality_insufficient"
    assert decision.accepted is True
    assert authority.state is ControlAuthorityState.FAILSAFE
    assert connection.mav.sent[-1] == (1, 0, 0, 500, 0, 0)


@pytest.mark.parametrize("gps_observed_at_s", [None, 0.0, 3.0])
def test_return_requires_a_fresh_dedicated_gps_cursor(
    gps_observed_at_s: float | None,
) -> None:
    policy = DeterministicSimulatorAiPolicy()
    observed = snapshot(
        return_requested=True,
        home_bearing_deg=0.0,
        home_distance_m=100.0,
        latitude_deg=1.3,
        longitude_deg=103.8,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=2.0,
        gps_vertical_accuracy_m=2.0,
        gps_observed_at_s=gps_observed_at_s,
    )

    decision = policy.decide(observed, now_s=2.0)

    assert decision.mode is SimulatorAiMode.FAILSAFE
    assert decision.reason == "return_gps_quality_insufficient"
    assert "gps_unqualified_for_return" in decision.safety_flags


def test_return_accepts_gps_at_the_freshness_boundary() -> None:
    policy = DeterministicSimulatorAiPolicy()
    observed = snapshot(
        return_requested=True,
        home_bearing_deg=0.0,
        home_distance_m=100.0,
        latitude_deg=1.3,
        longitude_deg=103.8,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=2.0,
        gps_vertical_accuracy_m=2.0,
        gps_observed_at_s=2.0,
    )

    decision = policy.decide(observed, now_s=3.5)

    assert decision.mode is SimulatorAiMode.RETURN


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("gps_horizontal_accuracy_m", 6.1),
        ("gps_vertical_accuracy_m", 10.1),
    ),
)
def test_snapshot_gps_quality_enforces_canonical_accuracy_ceilings(
    field: str, value: float
) -> None:
    values: dict[str, object] = {
        "latitude_deg": 1.3,
        "longitude_deg": 103.8,
        "gps_fix_type": 3,
        "gps_horizontal_accuracy_m": 2.0,
        "gps_vertical_accuracy_m": 2.0,
    }
    values[field] = value
    observed = snapshot(**values)

    assert observed.gps_quality_valid is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("gps_horizontal_accuracy_m", 6.1),
        ("gps_vertical_accuracy_m", 10.1),
    ),
)
def test_return_never_relaxes_canonical_accuracy_with_legacy_config_knob(
    field: str, value: float
) -> None:
    policy = DeterministicSimulatorAiPolicy(
        SimulatorAiExecutorConfig(maximum_return_gps_accuracy_m=100.0)
    )
    values: dict[str, object] = {
        "return_requested": True,
        "home_bearing_deg": 0.0,
        "home_distance_m": 100.0,
        "latitude_deg": 1.3,
        "longitude_deg": 103.8,
        "gps_fix_type": 3,
        "gps_horizontal_accuracy_m": 2.0,
        "gps_vertical_accuracy_m": 2.0,
        "gps_observed_at_s": 2.0,
    }
    values[field] = value
    observed = snapshot(**values)

    decision = policy.decide(observed, now_s=2.5)

    assert decision.mode is SimulatorAiMode.FAILSAFE
    assert decision.reason == "return_gps_quality_insufficient"


def test_executor_config_rejects_non_positive_return_gps_age() -> None:
    with pytest.raises(ValueError):
        SimulatorAiExecutorConfig(maximum_return_gps_age_s=0.0)


def test_executor_config_rejects_two_dimensional_return_fix_threshold() -> None:
    with pytest.raises(ValueError, match="minimum_return_fix_type"):
        SimulatorAiExecutorConfig(minimum_return_fix_type=2)


@pytest.mark.parametrize(
    "overrides",
    [
        {"gps_fix_type": 2},
        {"satellites_visible": 5},
        {"gps_horizontal_accuracy_m": float("nan")},
        {"gps_vertical_accuracy_m": float("nan")},
        {"gps_observed_at_s": None},
    ],
)
def test_snapshot_gps_quality_requires_a_complete_qualified_sample(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "latitude_deg": 1.3,
        "longitude_deg": 103.8,
        "gps_fix_type": 3,
        "gps_horizontal_accuracy_m": 2.0,
        "gps_vertical_accuracy_m": 2.0,
        "satellites_visible": 12,
        "gps_observed_at_s": 1.0,
    }
    values.update(overrides)
    observed = snapshot(**values)

    assert observed.gps_quality_valid is False


@pytest.mark.parametrize("latitude_deg", [-90.0, 90.0])
def test_snapshot_rejects_exact_pole_coordinates(latitude_deg: float) -> None:
    with pytest.raises(ValueError, match="strictly within"):
        SimulatorTelemetrySnapshot(
            observed_at_s=1.0,
            latitude_deg=latitude_deg,
            longitude_deg=0.0,
        )


def test_first_unsafe_observation_emits_neutral_failsafe_frame() -> None:
    executor, authority, connection = make_executor()
    executor.enable_ai()
    decision = executor.step(
        snapshot(
            return_requested=True,
            home_bearing_deg=0.0,
            home_distance_m=100.0,
            gps_fix_type=2,
            gps_horizontal_accuracy_m=30.0,
        ),
        now_s=1.0,
    )
    assert decision.mode is SimulatorAiMode.FAILSAFE
    assert decision.accepted is True
    assert decision.receipt is not None
    assert authority.state is ControlAuthorityState.FAILSAFE
    assert connection.mav.sent[-1] == (1, 0, 0, 500, 0, 0)


def test_watchdog_and_stale_telemetry_fail_closed() -> None:
    executor, authority, connection = make_executor(watchdog_s=0.5)
    executor.enable_ai()
    executor.step(snapshot(), now_s=1.0)
    decision = executor.step(snapshot(observed_at_s=0.0), now_s=2.0)
    assert decision.mode is SimulatorAiMode.FAILSAFE
    assert decision.reason == "telemetry_stale"
    assert authority.state is ControlAuthorityState.FAILSAFE
    assert connection.mav.sent[-1] == (1, 0, 0, 500, 0, 0)
    evidence = executor.evidence()
    assert evidence["truth_free_input_contract"] is True
    assert evidence["arbitrary_flight_commands"] is False
    assert evidence["watchdog_failsafe"] == 1


def test_evidence_counters_and_mapping_alias() -> None:
    executor, _, _ = make_executor()
    mapped = SimulatorTelemetrySnapshot.from_mapping(
        {
            "timestamp_s": 3.0,
            "altitude_agl_m": 10.0,
            "link_healthy": True,
            "armed": True,
        }
    )
    assert mapped.observed_at_s == 3.0
    assert mapped.timestamp_s == 3.0
    executor.step(mapped, now_s=3.0)
    executor.enable_ai()
    executor.step(mapped, now_s=3.0)
    evidence = executor.evidence()
    assert evidence["decisions"] == 2
    assert evidence["mode_counts"]["paused"] == 1
    assert evidence["mode_counts"]["hover"] == 1
    assert evidence["last_decision"]["mode"] == "hover"


def test_snapshot_gps_timestamp_round_trips_separately_from_position_timestamp() -> None:
    snapshot = SimulatorTelemetrySnapshot(
        observed_at_s=4.0,
        latitude_deg=31.2,
        longitude_deg=121.5,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.2,
        gps_vertical_accuracy_m=2.0,
        satellites_visible=12,
        battery_remaining_pct=80.0,
        position_observed_at_s=3.5,
        gps_observed_at_s=3.75,
    )

    mapping = snapshot.to_mapping()
    assert mapping["position_observed_at_s"] == pytest.approx(3.5)
    assert mapping["gps_observed_at_s"] == pytest.approx(3.75)
    restored = SimulatorTelemetrySnapshot.from_mapping(mapping)
    assert restored == snapshot


@pytest.mark.parametrize("value", [True, -0.1, float("nan"), float("inf")])
def test_snapshot_rejects_invalid_gps_timestamp(value: object) -> None:
    with pytest.raises(ValueError, match="gps_observed_at_s"):
        SimulatorTelemetrySnapshot(observed_at_s=1.0, gps_observed_at_s=value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"observed_at_s": -1.0},
        {"observed_at_s": 1.0, "latitude_deg": 91.0},
        {"observed_at_s": 1.0, "target_confidence": 1.1},
        {"observed_at_s": 1.0, "home_distance_m": -1.0},
    ],
)
def test_snapshot_rejects_invalid_sensor_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SimulatorTelemetrySnapshot(**kwargs)


def test_config_rejects_invalid_history_limit() -> None:
    with pytest.raises(ValueError):
        SimulatorAiExecutorConfig(history_limit=0)
