from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from multidetect.autonomy.mission_executor import plan_for_find_task
from multidetect.autonomy.near_depth import NearDepthConfig, NearDepthController
from multidetect.autonomy.onboard_loop import LoopConfig, PositionEstimate
from scripts.onboard_policy import OnboardPolicy


class FakeEstimator:
    def __init__(self, distance=4.0, sigma=0.2, captured=0.0):
        self.distance, self.sigma, self.captured = distance, sigma, captured
        self.calls = 0

    def estimate(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            slant_range_m=self.distance,
            sigma_m=self.sigma,
            captured_at_s=self.captured,
            produced_at_s=self.captured,
        )


def _track():
    return SimpleNamespace(
        track_id="t1",
        bbox=(40.0, 40.0, 60.0, 60.0),
        ground=(4.0, 0.0),
        position_estimate=PositionEstimate((5.0, 0.0, 0.0), 2.0, 0.0, "triangulation"),
    )


def _controller(estimator, tmp_path):
    model = tmp_path / "fake.onnx"
    model.write_bytes(b"test-only fake model")
    return NearDepthController(
        NearDepthConfig(
            enabled=True,
            model_path=model,
            max_range_m=10.0,
            min_interval_s=0.2,
            max_age_s=1.0,
            sigma_scale=2.0,
        ),
        estimator=estimator,
    )


def test_default_disabled_is_lazy_and_loop_legacy_unchanged():
    controller = NearDepthController(NearDepthConfig())
    assert controller.enabled is False
    assert controller.stats.inference_count == 0
    assert LoopConfig().near_depth is False


def test_gate_stage_distance_interval_and_once_per_step(tmp_path):
    fake = FakeEstimator()
    controller = _controller(fake, tmp_path)
    args = dict(
        track=_track(),
        image_bgr=np.zeros((100, 100, 3), np.uint8),
        frame_id="f",
        captured_at_s=0.0,
        now_s=0.0,
        camera_ray=lambda u, v: ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        current_range_m=5.0,
        calibration=object(),
        image_size=(100, 100),
        stage_allowed=False,
        focus=True,
        step_token=1,
    )
    assert not controller.run(**args)
    args["stage_allowed"] = True
    assert controller.run(**args)
    assert fake.calls == 1
    assert not controller.run(**args)
    args.update(now_s=0.3, step_token=2, current_range_m=11.0)
    assert not controller.run(**args)
    args.update(current_range_m=5.0, step_token=3)
    assert controller.run(**args)
    assert fake.calls == 2


def test_expired_and_invalid_results_are_discarded(tmp_path):
    fake = FakeEstimator(captured=0.0)
    controller = _controller(fake, tmp_path)
    args = dict(
        track=_track(),
        image_bgr=np.zeros((100, 100, 3), np.uint8),
        frame_id="f",
        captured_at_s=0.0,
        now_s=2.0,
        camera_ray=lambda u, v: ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        current_range_m=5.0,
        calibration=object(),
        image_size=(100, 100),
        stage_allowed=True,
        focus=True,
        step_token=1,
    )
    assert not controller.run(**args)
    assert controller.stats.skip_counts["expired"] == 1
    fake.distance = float("nan")
    args.update(now_s=2.3, captured_at_s=2.3, step_token=2)
    assert not controller.run(**args)
    assert controller.stats.skip_counts["invalid"] == 1


def test_depth_measurement_uses_conservative_sigma_and_existing_fusion_gate(tmp_path):
    fake = FakeEstimator(distance=4.0, sigma=0.2, captured=0.0)
    controller = _controller(fake, tmp_path)
    track = _track()
    args = dict(
        track=track,
        image_bgr=np.zeros((100, 100, 3), np.uint8),
        frame_id="f",
        captured_at_s=0.0,
        now_s=0.0,
        camera_ray=lambda u, v: ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        current_range_m=5.0,
        calibration=object(),
        image_size=(100, 100),
        stage_allowed=True,
        focus=True,
        step_token=1,
    )
    assert controller.run(**args)
    assert track.position_estimate is not None
    assert track.position_estimate.source == "fused"
    # <10 m empirical P95 (1.6 m) dominates the model sigma, then config scale.
    assert track.position_estimate.sigma_m > 1.0
    assert track.position_estimate.consistent is True


def test_configuration_validation():
    # Disabled mode is allowed to retain a deployment path that is absent on
    # the developer machine and must not touch/load that artifact.
    NearDepthConfig(enabled=False, model_path=Path("missing.onnx"))
    with pytest.raises(ValueError):
        NearDepthConfig(enabled=True, model_path=__file__, backend="cuda")
    with pytest.raises(ValueError, match=".onnx"):
        NearDepthConfig(enabled=True, model_path=__file__)
    with pytest.raises(ValueError, match="does not exist"):
        NearDepthConfig(enabled=True, model_path=Path("missing.onnx"))
    with pytest.raises(ValueError, match=".engine"):
        NearDepthConfig(enabled=True, model_path=__file__, backend="tensorrt")


def test_policy_serializes_slow_identity_before_depth_call():
    events = []
    candidate = SimpleNamespace(track_id="t1", bbox=(10.0, 20.0, 40.0, 60.0), verdict=None)
    plan = plan_for_find_task("backpack")
    report = SimpleNamespace(evidence_frame_ids=(), state_answers=(), alerts=(), positions=())
    executor = SimpleNamespace(
        findings=[],
        current_tracks=(candidate,),
        current_step=plan.steps[0],
        current_task_text="Find a backpack",
        history=[],
        counts={},
        alert_vote_clusters=(),
        observe=lambda *args, **kwargs: [candidate],
        apply_verdict=lambda *args, **kwargs: None,
        next_action=lambda **kwargs: (None, {}),
        report=lambda: report,
    )
    policy = SimpleNamespace(
        task_text="Find a backpack",
        executor=executor,
        mission_plan=None,
        mission_plan_report=None,
        _mission_plan_labeled=True,
        _step=1,
        _slow_identity=lambda *args: (
            events.append("vlm") or {"decision": "approach_track", "confidence": 0.8}
        ),
        _run_near_depth=lambda **kwargs: events.append("depth"),
    )
    OnboardPolicy._act_mission(
        policy,
        Path("frame.png"),
        [],
        0.0,
        pose_ned=(0.0, 0.0, -5.0),
        yaw_deg=0.0,
        ground_point=None,
        frame_rgb=np.zeros((10, 10, 3), np.uint8),
        camera_info={},
        camera_ray=None,
        timestamp_s=0.0,
    )
    assert events == ["vlm", "depth"]
    with pytest.raises(ValueError):
        LoopConfig(near_depth="false")


@pytest.mark.onboard
def test_real_model_cpu_smoke_when_artifact_exists():
    from pathlib import Path

    from multidetect.domain import BoundingBox
    from multidetect.metric_depth import MetricDepthConfig, MetricDepthEstimator
    from multidetect.multimodal_ranging import CameraCalibration

    model = Path(
        "D:/Projects/Multi-Detect/artifacts/evaluation/depth-anything-v2-metric-outdoor-small.onnx"
    )
    if not model.is_file():
        pytest.skip("optional pinned depth model is absent")
    estimator = MetricDepthEstimator(
        MetricDepthConfig(
            model_path=model,
            providers=("CPUExecutionProvider",),
            intra_op_num_threads=2,
            inter_op_num_threads=2,
        )
    )
    try:
        result = estimator.estimate(
            image_bgr=np.zeros((480, 640, 3), dtype=np.uint8),
            target_id="smoke",
            bbox=BoundingBox(0.4, 0.3, 0.6, 0.7),
            target_label="manual",
            frame_id="smoke-frame",
            captured_at_s=1.0,
            calibration=CameraCalibration(
                calibration_id="smoke",
                width_px=640,
                height_px=480,
                fx_px=500.0,
                fy_px=500.0,
                cx_px=320.0,
                cy_px=240.0,
            ),
        )
        assert np.isfinite(result.slant_range_m) and result.slant_range_m > 0
        assert np.isfinite(result.sigma_m) and result.processing_time_ms > 0
    finally:
        estimator.close()
