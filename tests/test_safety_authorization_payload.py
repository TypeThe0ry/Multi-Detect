from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from multidetect.authorization import (
    AuthorizationBindingError,
    AuthorizationConsumed,
    AuthorizationDenied,
    AuthorizationError,
    AuthorizationExpired,
    AuthorizationService,
    AuthorizationStatus,
)
from multidetect.config import MissionConfig, MissionType, PayloadSpec, PlatformMode
from multidetect.domain import (
    BoundingBox,
    Detection,
    FrameObservation,
    PayloadState,
    SensorKind,
    StateTransitionError,
    TrackSnapshot,
    VehicleTelemetry,
    Verdict,
)
from multidetect.payload import (
    FakePayloadPort,
    PayloadController,
    PayloadFeedbackError,
    PayloadInterlockError,
)
from multidetect.safety import SafetyRuleEngine


def make_config() -> MissionConfig:
    return MissionConfig(
        mission_id="mission-1",
        mission_type=MissionType.FIRE_SUPPRESSION,
        platform_mode=PlatformMode.MULTI_DEPLOYMENT,
        payloads=(
            PayloadSpec("slot-1", "fire_suppression_agent"),
            PayloadSpec("slot-2", "fire_suppression_ball"),
        ),
        target_classes=("flame", "smoke"),
    )


def make_track(**changes: object) -> TrackSnapshot:
    track = TrackSnapshot(
        track_id="track-7",
        revision=4,
        label="flame",
        bbox=BoundingBox(0.40, 0.40, 0.60, 0.60),
        first_seen_at_s=96.0,
        last_seen_at_s=100.0,
        observation_count=12,
        consecutive_observations=12,
        confidence_floor=0.88,
        confidence_mean=0.93,
        maximum_gap_s=0.2,
        area_growth_rate=0.01,
        thermal_corroborated=True,
        confirmed=True,
        independent_rgb_corroborated=True,
    )
    return replace(track, **changes)


def make_telemetry(**changes: object) -> VehicleTelemetry:
    telemetry = VehicleTelemetry(
        altitude_agl_m=20.0,
        roll_deg=1.0,
        pitch_deg=-1.0,
        ground_speed_mps=1.2,
        in_allowed_zone=True,
        geofence_healthy=True,
        position_healthy=True,
        link_healthy=True,
        flight_mode_allows_deploy=True,
        release_zone_clear=True,
        person_detector_healthy=True,
    )
    return replace(telemetry, **changes)


def make_frame(
    *,
    detections: tuple[Detection, ...] | None = None,
    telemetry: VehicleTelemetry | None = None,
    captured_at_s: float = 100.0,
    frame_id: str = "frame-a",
) -> FrameObservation:
    if detections is None:
        detections = (
            Detection(
                "flame",
                0.94,
                BoundingBox(0.40, 0.40, 0.60, 0.60),
                SensorKind.RGB,
                "rgb-v1",
            ),
            Detection(
                "smoke",
                0.91,
                BoundingBox(0.68, 0.10, 0.85, 0.30),
                SensorKind.RGB,
                "rgb-v1",
            ),
        )
    return FrameObservation(
        frame_id=frame_id,
        captured_at_s=captured_at_s,
        detections=detections,
        telemetry=telemetry or make_telemetry(),
    )


def make_decision(
    config: MissionConfig | None = None,
    *,
    track: TrackSnapshot | None = None,
    frame: FrameObservation | None = None,
    now_s: float = 100.2,
):
    config = config or make_config()
    return SafetyRuleEngine(config).evaluate(
        track=track or make_track(),
        frame=frame or make_frame(),
        now_s=now_s,
    )


def consume_authorization(
    config: MissionConfig,
    decision,
    *,
    payload_slot_id: str = "slot-1",
    created_at_s: float = 100.2,
):
    service = AuthorizationService.from_config(config)
    challenge = service.create_challenge(
        mission_id=config.mission_id,
        payload_slot_id=payload_slot_id,
        decision=decision,
        now_s=created_at_s,
    )
    service.approve(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        operator_id="operator-1",
        now_s=created_at_s + 0.1,
    )
    return service.consume(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        mission_id=config.mission_id,
        target_id=decision.target_id,
        target_revision=decision.target_revision,
        payload_slot_id=payload_slot_id,
        scene_digest=decision.scene_digest,
        ruleset_version=decision.ruleset_version,
        now_s=created_at_s + 0.2,
    )


def check_for(decision, rule_id: str):
    return next(check for check in decision.checks if check.rule_id == rule_id)


def test_safe_scene_passes_and_scene_digest_is_order_independent() -> None:
    config = make_config()
    first_frame = make_frame()
    second_frame = make_frame(
        detections=tuple(reversed(first_frame.detections)),
        captured_at_s=100.1,
        frame_id="frame-b",
    )

    first = make_decision(config, frame=first_frame)
    second = make_decision(config, frame=second_frame)

    assert first.allowed is True
    assert all(check.verdict is Verdict.PASS for check in first.checks)
    assert first.scene_digest == second.scene_digest
    assert len(first.scene_digest) == 64


@pytest.mark.parametrize(
    ("field", "value", "rule_id", "expected_verdict"),
    (
        ("in_allowed_zone", False, "navigation.allowed_zone", Verdict.DENY),
        ("geofence_healthy", False, "navigation.geofence_health", Verdict.DENY),
        ("position_healthy", None, "navigation.position_health", Verdict.UNKNOWN),
        ("link_healthy", False, "communications.link_health", Verdict.DENY),
        ("flight_mode_allows_deploy", False, "flight.allowed_mode", Verdict.DENY),
        ("release_zone_clear", False, "deployment.release_zone_clear", Verdict.DENY),
        (
            "person_detector_healthy",
            None,
            "sensor.person_detector_health",
            Verdict.UNKNOWN,
        ),
        ("altitude_agl_m", 61.0, "flight.altitude", Verdict.DENY),
        ("roll_deg", 13.0, "flight.roll", Verdict.DENY),
        ("pitch_deg", -13.0, "flight.pitch", Verdict.DENY),
        ("ground_speed_mps", 3.1, "flight.ground_speed", Verdict.DENY),
    ),
)
def test_any_navigation_or_flight_failure_denies(
    field: str, value: object, rule_id: str, expected_verdict: Verdict
) -> None:
    decision = make_decision(frame=make_frame(telemetry=make_telemetry(**{field: value})))

    assert decision.allowed is False
    assert check_for(decision, rule_id).verdict is expected_verdict


@pytest.mark.parametrize(
    ("changes", "rule_id"),
    (
        ({"confirmed": False}, "target.confirmed_track"),
        ({"label": "vehicle"}, "target.allowed_class"),
        ({"confidence_floor": 0.81}, "target.minimum_confidence"),
        (
            {"independent_rgb_corroborated": False},
            "sensor.independent_rgb_fire_consistency",
        ),
    ),
)
def test_target_evidence_is_fail_closed(changes: dict[str, object], rule_id: str) -> None:
    decision = make_decision(track=make_track(**changes))

    assert decision.allowed is False
    assert check_for(decision, rule_id).verdict is Verdict.DENY


def test_legacy_thermal_gate_is_only_present_when_explicitly_enabled() -> None:
    rgb_only = make_decision()
    assert all(check.rule_id != "sensor.thermal_consistency" for check in rgb_only.checks)

    thermal_required = replace(make_config(), require_thermal_corroboration=True)
    denied = make_decision(
        thermal_required,
        track=make_track(thermal_corroborated=False),
    )
    assert check_for(denied, "sensor.thermal_consistency").verdict is Verdict.DENY


def test_stale_frame_and_stale_track_each_deny() -> None:
    stale_frame = make_decision(frame=make_frame(captured_at_s=98.0), now_s=100.2)
    stale_track = make_decision(track=make_track(last_seen_at_s=98.0), now_s=100.2)

    assert check_for(stale_frame, "sensor.frame_freshness").verdict is Verdict.DENY
    assert check_for(stale_track, "sensor.track_freshness").verdict is Verdict.DENY
    assert not stale_frame.allowed and not stale_track.allowed


def test_person_intersecting_expanded_target_zone_denies() -> None:
    person = Detection(
        "firefighter",
        0.51,
        BoundingBox(0.59, 0.45, 0.65, 0.58),
        SensorKind.RGB,
        "safety-v1",
    )
    decision = make_decision(frame=make_frame(detections=(person,)))

    assert decision.allowed is False
    assert check_for(decision, "deployment.person_exclusion").verdict is Verdict.DENY


def test_authorization_requires_allowed_decision_and_correct_nonce() -> None:
    config = make_config()
    denied = make_decision(config, track=make_track(confirmed=False))
    service = AuthorizationService.from_config(config)
    with pytest.raises(AuthorizationError):
        service.create_challenge(
            mission_id=config.mission_id,
            payload_slot_id="slot-1",
            decision=denied,
            now_s=100.2,
        )

    allowed = make_decision(config)
    challenge = service.create_challenge(
        mission_id=config.mission_id,
        payload_slot_id="slot-1",
        decision=allowed,
        now_s=100.2,
    )
    with pytest.raises(AuthorizationBindingError):
        service.approve(
            challenge_id=challenge.challenge_id,
            nonce="wrong-nonce",
            operator_id="operator-1",
            now_s=100.3,
        )
    assert service.status(challenge.challenge_id) is AuthorizationStatus.PENDING


def test_authorization_binding_mismatch_does_not_consume_then_consumes_once() -> None:
    config = make_config()
    decision = make_decision(config)
    service = AuthorizationService.from_config(config)
    challenge = service.create_challenge(
        mission_id=config.mission_id,
        payload_slot_id="slot-1",
        decision=decision,
        now_s=100.2,
    )
    service.approve(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        operator_id="operator-1",
        now_s=100.3,
    )
    consume_args = dict(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        mission_id=config.mission_id,
        target_id=decision.target_id,
        target_revision=decision.target_revision,
        payload_slot_id="slot-1",
        scene_digest=decision.scene_digest,
        ruleset_version=decision.ruleset_version,
        now_s=100.4,
    )

    with pytest.raises(AuthorizationBindingError):
        service.consume(**{**consume_args, "target_revision": decision.target_revision + 1})
    consumed = service.consume(**consume_args)
    assert consumed.grant.approved is True
    assert service.status(challenge.challenge_id) is AuthorizationStatus.CONSUMED
    with pytest.raises(AuthorizationConsumed):
        service.consume(**consume_args)


def test_authorization_consumption_is_atomic_under_concurrency() -> None:
    config = make_config()
    decision = make_decision(config)
    service = AuthorizationService.from_config(config)
    challenge = service.create_challenge(
        mission_id=config.mission_id,
        payload_slot_id="slot-1",
        decision=decision,
        now_s=100.2,
    )
    service.approve(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        operator_id="operator-1",
        now_s=100.3,
    )

    def attempt_consume():
        try:
            return service.consume(
                challenge_id=challenge.challenge_id,
                nonce=challenge.nonce,
                mission_id=config.mission_id,
                target_id=decision.target_id,
                target_revision=decision.target_revision,
                payload_slot_id="slot-1",
                scene_digest=decision.scene_digest,
                ruleset_version=decision.ruleset_version,
                now_s=100.4,
            )
        except AuthorizationConsumed as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: attempt_consume(), range(2)))
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, AuthorizationConsumed) for result in results) == 1


def test_authorization_denial_and_expiration_cannot_be_consumed() -> None:
    config = make_config()
    decision = make_decision(config)
    service = AuthorizationService.from_config(config)
    denied = service.create_challenge(
        mission_id=config.mission_id,
        payload_slot_id="slot-1",
        decision=decision,
        now_s=100.2,
    )
    service.deny(
        challenge_id=denied.challenge_id,
        nonce=denied.nonce,
        operator_id="operator-1",
        now_s=100.3,
    )
    with pytest.raises(AuthorizationDenied):
        service.consume(
            challenge_id=denied.challenge_id,
            nonce=denied.nonce,
            mission_id=config.mission_id,
            target_id=decision.target_id,
            target_revision=decision.target_revision,
            payload_slot_id="slot-1",
            scene_digest=decision.scene_digest,
            ruleset_version=decision.ruleset_version,
            now_s=100.4,
        )

    expiring = service.create_challenge(
        mission_id=config.mission_id,
        payload_slot_id="slot-1",
        decision=decision,
        now_s=200.0,
    )
    service.approve(
        challenge_id=expiring.challenge_id,
        nonce=expiring.nonce,
        operator_id="operator-1",
        now_s=200.1,
    )
    with pytest.raises(AuthorizationExpired):
        service.consume(
            challenge_id=expiring.challenge_id,
            nonce=expiring.nonce,
            mission_id=config.mission_id,
            target_id=decision.target_id,
            target_revision=decision.target_revision,
            payload_slot_id="slot-1",
            scene_digest=decision.scene_digest,
            ruleset_version=decision.ruleset_version,
            now_s=expiring.expires_at_s,
        )


def test_payload_strict_state_sequence_requires_both_confirmations() -> None:
    config = make_config()
    decision = make_decision(config)
    authorization = consume_authorization(config, decision)
    port = FakePayloadPort()
    controller = PayloadController(config, port)

    assert controller.get_slot("slot-1").state is PayloadState.LOCKED
    assert (
        controller.arm(payload_slot_id="slot-1", authorization=authorization, now_s=100.5).state
        is PayloadState.ARMED
    )
    release_id = controller.request_release(
        payload_slot_id="slot-1",
        decision=decision,
        now_s=100.6,
        release_id="release-1",
    )
    assert controller.get_slot("slot-1").state is PayloadState.RELEASE_REQUESTED
    assert port.request_count == 1

    executed = controller.report_execution(
        release_id=release_id, payload_slot_id="slot-1", now_s=100.7
    )
    assert executed.state is PayloadState.RELEASED
    assert controller.confirmed_release_count == 0

    confirmed = controller.report_independent_confirmation(
        release_id=release_id,
        payload_slot_id="slot-1",
        source_id="departure-observer",
        now_s=100.8,
    )
    assert confirmed.state is PayloadState.RELEASE_CONFIRMED
    assert controller.confirmed_release_count == 1
    assert controller.remaining_payload_count == 1
    assert controller.active_slot_id is None


def test_independent_confirmation_may_arrive_first_but_cannot_confirm_alone() -> None:
    config = make_config()
    decision = make_decision(config)
    controller = PayloadController(config, FakePayloadPort())
    controller.arm(
        payload_slot_id="slot-1",
        authorization=consume_authorization(config, decision),
        now_s=100.5,
    )
    release_id = controller.request_release(
        payload_slot_id="slot-1", decision=decision, now_s=100.6
    )

    pending = controller.report_independent_confirmation(
        release_id=release_id,
        payload_slot_id="slot-1",
        source_id="visual-observer",
        now_s=100.7,
    )
    assert pending.state is PayloadState.RELEASE_REQUESTED
    completed = controller.report_execution(
        release_id=release_id, payload_slot_id="slot-1", now_s=100.8
    )
    assert completed.state is PayloadState.RELEASE_CONFIRMED


def test_payload_controller_accepts_only_fake_port_and_enforces_single_slot() -> None:
    config = make_config()
    decision = make_decision(config)
    with pytest.raises(TypeError):
        PayloadController(config, object())  # type: ignore[arg-type]

    controller = PayloadController(config, FakePayloadPort())
    controller.arm(
        payload_slot_id="slot-1",
        authorization=consume_authorization(config, decision, payload_slot_id="slot-1"),
        now_s=100.5,
    )
    with pytest.raises(PayloadInterlockError):
        controller.arm(
            payload_slot_id="slot-2",
            authorization=consume_authorization(config, decision, payload_slot_id="slot-2"),
            now_s=100.5,
        )


def test_release_id_is_idempotent_and_never_submitted_twice() -> None:
    config = make_config()
    decision = make_decision(config)
    port = FakePayloadPort()
    controller = PayloadController(config, port)
    controller.arm(
        payload_slot_id="slot-1",
        authorization=consume_authorization(config, decision),
        now_s=100.5,
    )

    first = controller.request_release(
        payload_slot_id="slot-1",
        decision=decision,
        now_s=100.6,
        release_id="stable-release-id",
    )
    second = controller.request_release(
        payload_slot_id="slot-1",
        decision=decision,
        now_s=100.7,
        release_id="stable-release-id",
    )
    assert first == second
    assert port.request_count == 1


def test_changed_or_stale_scene_cannot_leave_armed_state() -> None:
    config = make_config()
    decision = make_decision(config)
    controller = PayloadController(config, FakePayloadPort())
    controller.arm(
        payload_slot_id="slot-1",
        authorization=consume_authorization(config, decision),
        now_s=100.5,
    )

    with pytest.raises(PayloadInterlockError):
        controller.request_release(
            payload_slot_id="slot-1",
            decision=replace(decision, scene_digest="changed-scene"),
            now_s=100.6,
        )
    assert controller.get_slot("slot-1").state is PayloadState.ARMED
    with pytest.raises(PayloadInterlockError):
        controller.request_release(payload_slot_id="slot-1", decision=decision, now_s=101.3)
    assert controller.get_slot("slot-1").state is PayloadState.ARMED


def test_confirmation_timeout_is_terminal_and_never_retries() -> None:
    config = make_config()
    decision = make_decision(config)
    port = FakePayloadPort()
    controller = PayloadController(config, port)
    controller.arm(
        payload_slot_id="slot-1",
        authorization=consume_authorization(config, decision),
        now_s=100.5,
    )
    release_id = controller.request_release(
        payload_slot_id="slot-1",
        decision=decision,
        now_s=100.6,
        release_id="timeout-release",
    )

    timed_out = controller.check_timeouts(now_s=105.6)
    assert len(timed_out) == 1
    assert timed_out[0].state is PayloadState.FAILED
    assert timed_out[0].uncertain_release is True
    assert controller.faulted is True
    assert (
        controller.request_release(
            payload_slot_id="slot-1",
            decision=decision,
            now_s=105.7,
            release_id=release_id,
        )
        == release_id
    )
    assert port.request_count == 1
    with pytest.raises(PayloadInterlockError):
        controller.arm(
            payload_slot_id="slot-2",
            authorization=consume_authorization(config, decision, payload_slot_id="slot-2"),
            now_s=105.7,
        )


def test_wrong_slot_feedback_faults_the_active_transaction() -> None:
    config = make_config()
    decision = make_decision(config)
    controller = PayloadController(config, FakePayloadPort())
    controller.arm(
        payload_slot_id="slot-1",
        authorization=consume_authorization(config, decision),
        now_s=100.5,
    )
    release_id = controller.request_release(
        payload_slot_id="slot-1", decision=decision, now_s=100.6
    )

    with pytest.raises(PayloadFeedbackError):
        controller.report_execution(release_id=release_id, payload_slot_id="slot-2", now_s=100.7)
    failed = controller.get_slot("slot-1")
    assert failed.state is PayloadState.FAILED
    assert failed.uncertain_release is True
    assert controller.faulted is True


def test_release_cannot_be_requested_without_arm() -> None:
    config = make_config()
    controller = PayloadController(config, FakePayloadPort())
    with pytest.raises(StateTransitionError):
        controller.request_release(
            payload_slot_id="slot-1",
            decision=make_decision(config),
            now_s=100.5,
        )
