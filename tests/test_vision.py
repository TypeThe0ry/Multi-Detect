from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

import multidetect.tensorrt_session as tensorrt_session_module
import multidetect.vision as vision_module
from multidetect.domain import BoundingBox, Detection, SensorKind
from multidetect.monocular_avoidance import (
    CollisionRiskState,
    MonocularAvoidanceConfig,
    OpenCVSparseFlowAvoidance,
)
from multidetect.vision import (
    BrightNeutralLightVetoFilter,
    CameraReadError,
    CaptureConfig,
    ClassConfidenceFilter,
    DetectorEnsemble,
    FrameCadencedDetector,
    LabelAllowListFilter,
    LabelRemapDetector,
    LetterboxTransform,
    MultiSourceConfidenceFilter,
    OnnxNx6Config,
    OnnxNx6Detector,
    OnnxOutputContractError,
    OnnxRawYoloConfig,
    OnnxRawYoloDetector,
    OpenCVFrameSource,
    PersonOverlapVetoFilter,
    SameLabelDetectionFusion,
    SyntheticFrameSource,
    TemporalDetectionFilter,
    TiledDetectionConfig,
    TiledDetectionFusion,
    VehicleFurnitureOverlapVetoFilter,
    frame_source_from_config,
)


def test_class_confidence_filter_uses_stricter_flame_threshold() -> None:
    class _Detector:
        def detect(self, _image):
            return (
                Detection("flame", 0.24, BoundingBox(0.1, 0.1, 0.2, 0.2), SensorKind.RGB),
                Detection("flame", 0.42, BoundingBox(0.2, 0.2, 0.3, 0.3), SensorKind.RGB),
                Detection("smoke", 0.24, BoundingBox(0.3, 0.3, 0.4, 0.4), SensorKind.RGB),
            )

        def covers_labels(self, labels):
            return set(labels).issubset({"flame", "smoke"})

    filtered = ClassConfidenceFilter(_Detector(), {"flame": 0.35, "smoke": 0.20})

    results = filtered.detect(object())

    assert tuple((item.label, item.confidence) for item in results) == (
        ("flame", 0.42),
        ("smoke", 0.24),
    )
    assert filtered.covers_labels(("flame",)) is True


def test_label_allow_list_hides_furniture_but_keeps_operational_candidates() -> None:
    class _Detector:
        class_names = ("chair", "couch", "person", "car", "flame", "smoke")
        provider_names = ("TensorrtExecutionProvider",)

        def __init__(self) -> None:
            self.warmup_iterations = None

        def warmup(self, *, iterations: int = 1) -> None:
            self.warmup_iterations = iterations

        def detect(self, _image):
            return (
                Detection("chair", 0.91, BoundingBox(0.05, 0.05, 0.20, 0.35)),
                Detection("couch", 0.86, BoundingBox(0.20, 0.20, 0.45, 0.55)),
                Detection("person", 0.94, BoundingBox(0.45, 0.10, 0.65, 0.85)),
                Detection("car", 0.82, BoundingBox(0.62, 0.55, 0.90, 0.82)),
                Detection("flame", 0.78, BoundingBox(0.08, 0.60, 0.18, 0.82)),
                Detection("smoke", 0.73, BoundingBox(0.20, 0.55, 0.38, 0.75)),
            )

        def covers_labels(self, required_labels):
            return set(required_labels).issubset(set(self.class_names))

    source = _Detector()
    filtered = LabelAllowListFilter(
        source,
        labels=frozenset({"person", "car", "flame", "smoke"}),
    )

    filtered.warmup(iterations=3)

    assert tuple(item.label for item in filtered.detect(object())) == (
        "person",
        "car",
        "flame",
        "smoke",
    )
    assert filtered.class_names == ("person", "car", "flame", "smoke")
    assert filtered.provider_names == ("TensorrtExecutionProvider",)
    assert filtered.covers_labels(("person", "flame")) is True
    assert filtered.covers_labels(("chair",)) is False
    assert source.warmup_iterations == 3


def test_complete_auxiliary_threshold_map_keeps_vehicle_and_environment_candidates() -> None:
    class _AuxiliaryDetector:
        def detect(self, _image):
            return (
                Detection("car", 0.51, BoundingBox(0.1, 0.1, 0.2, 0.2)),
                Detection("building", 0.61, BoundingBox(0.2, 0.2, 0.4, 0.4)),
                Detection("power_line", 0.39, BoundingBox(0.5, 0.1, 0.6, 0.8)),
            )

        def covers_labels(self, required_labels):
            return set(required_labels).issubset({"car", "building", "power_line"})

    filtered = ClassConfidenceFilter(
        _AuxiliaryDetector(),
        {"car": 0.30, "building": 0.40, "power_line": 0.40},
        default_threshold=None,
    )

    assert [item.label for item in filtered.detect(None)] == ["car", "building"]


def test_person_overlap_veto_suppresses_ambiguous_flame_but_keeps_person() -> None:
    class _Detector:
        def detect(self, _image):
            return (
                Detection("person", 0.9, BoundingBox(0.1, 0.1, 0.5, 0.9)),
                Detection("flame", 0.8, BoundingBox(0.2, 0.2, 0.4, 0.6)),
                Detection("smoke", 0.7, BoundingBox(0.7, 0.1, 0.9, 0.3)),
            )

        def covers_labels(self, _labels):
            return True

    filtered = PersonOverlapVetoFilter(_Detector())

    results = filtered.detect(object())

    assert tuple(item.label for item in results) == ("person", "smoke")


def test_vehicle_furniture_overlap_veto_rejects_chair_base_car_false_positive() -> None:
    class _Detector:
        def detect(self, _image):
            return (
                Detection("chair", 0.76, BoundingBox(0.20, 0.18, 0.76, 0.92)),
                Detection("car", 0.83, BoundingBox(0.39, 0.60, 0.62, 0.78)),
                Detection("person", 0.91, BoundingBox(0.78, 0.12, 0.96, 0.88)),
            )

        def covers_labels(self, _labels):
            return True

    results = VehicleFurnitureOverlapVetoFilter(_Detector()).detect(object())

    assert tuple(item.label for item in results) == ("chair", "person")


def test_vehicle_furniture_overlap_veto_keeps_nearby_vehicle() -> None:
    class _Detector:
        def detect(self, _image):
            return (
                Detection("chair", 0.76, BoundingBox(0.02, 0.12, 0.26, 0.90)),
                Detection("car", 0.83, BoundingBox(0.44, 0.50, 0.76, 0.78)),
            )

        def covers_labels(self, _labels):
            return True

    results = VehicleFurnitureOverlapVetoFilter(_Detector()).detect(object())

    assert tuple(item.label for item in results) == ("chair", "car")


def test_multi_source_confidence_filter_rejects_weak_single_source_car() -> None:
    class _Detector:
        def detect(self, _image):
            return (
                Detection(
                    "car",
                    0.74,
                    BoundingBox(0.30, 0.50, 0.48, 0.66),
                    model_version="visdrone-priority",
                ),
                Detection(
                    "person",
                    0.92,
                    BoundingBox(0.60, 0.12, 0.82, 0.88),
                    model_version="coco-common",
                ),
            )

        def covers_labels(self, _labels):
            return True

    results = MultiSourceConfidenceFilter(_Detector(), labels=frozenset({"car"})).detect(object())

    assert tuple(item.label for item in results) == ("person",)


def test_multi_source_confidence_filter_keeps_agreed_or_high_confidence_car() -> None:
    class _Detector:
        def detect(self, _image):
            return (
                Detection(
                    "car",
                    0.63,
                    BoundingBox(0.30, 0.50, 0.50, 0.68),
                    model_version="visdrone-priority",
                ),
                Detection(
                    "car",
                    0.61,
                    BoundingBox(0.32, 0.51, 0.52, 0.69),
                    model_version="coco-common",
                ),
                Detection(
                    "car",
                    0.84,
                    BoundingBox(0.66, 0.48, 0.82, 0.63),
                    model_version="visdrone-priority",
                ),
            )

        def covers_labels(self, _labels):
            return True

    results = MultiSourceConfidenceFilter(_Detector(), labels=frozenset({"car"})).detect(object())

    assert tuple(item.confidence for item in results) == (0.63, 0.61, 0.84)


def test_bright_neutral_light_veto_rejects_white_lamp_but_keeps_colored_flame() -> None:
    class _Detector:
        def detect(self, _image):
            return (
                Detection("flame", 0.8, BoundingBox(0.0, 0.0, 0.5, 1.0)),
                Detection("flame", 0.8, BoundingBox(0.5, 0.0, 1.0, 1.0)),
            )

        def covers_labels(self, _labels):
            return True

    image = np.zeros((40, 80, 3), dtype=np.uint8)
    image[:, :40] = (255, 255, 255)
    image[:, 40:] = (0, 100, 255)
    filtered = BrightNeutralLightVetoFilter(_Detector())

    results = filtered.detect(image)

    assert len(results) == 1
    assert results[0].bbox == BoundingBox(0.5, 0.0, 1.0, 1.0)
    assert results[0].metadata["fire_rgb_bright_neutral_fraction"] == pytest.approx(0.0)
    assert results[0].metadata["fire_rgb_colorful_fraction"] == pytest.approx(1.0)
    assert results[0].metadata["fire_rgb_warm_fraction"] == pytest.approx(1.0)
    assert results[0].metadata["fire_rgb_bright_warm_fraction"] == pytest.approx(1.0)
    assert results[0].metadata["fire_rgb_bbox_aspect_ratio"] == pytest.approx(2.0)


def test_bright_neutral_light_veto_can_require_a_small_bright_warm_signal() -> None:
    class _Detector:
        def detect(self, _image):
            return (
                Detection("flame", 0.8, BoundingBox(0.0, 0.0, 0.5, 1.0)),
                Detection("flame", 0.8, BoundingBox(0.5, 0.0, 1.0, 1.0)),
            )

        def covers_labels(self, _labels):
            return True

    image = np.zeros((40, 80, 3), dtype=np.uint8)
    image[:, :40] = (0, 32, 110)  # Colorful/warm, but not a bright flame source.
    image[:, 40:] = (0, 100, 255)

    results = BrightNeutralLightVetoFilter(
        _Detector(),
        minimum_bright_warm_fraction=0.001,
    ).detect(image)

    assert len(results) == 1
    assert results[0].bbox == BoundingBox(0.5, 0.0, 1.0, 1.0)
    assert results[0].metadata["fire_rgb_bright_warm_fraction"] == pytest.approx(1.0)


def test_bright_neutral_light_veto_skips_hsv_work_without_fire_candidates(monkeypatch) -> None:
    class _Detector:
        def detect(self, _image):
            return (Detection("person", 0.9, BoundingBox(0.1, 0.1, 0.3, 0.8)),)

        def covers_labels(self, _labels):
            return True

    monkeypatch.setattr(
        vision_module,
        "_require_cv2",
        lambda: pytest.fail("HSV conversion must not run without a fire candidate"),
    )

    results = BrightNeutralLightVetoFilter(_Detector()).detect(
        np.zeros((40, 80, 3), dtype=np.uint8)
    )

    assert tuple(item.label for item in results) == ("person",)


def test_temporal_filter_requires_three_spatially_consistent_fire_frames() -> None:
    class _Detector:
        def __init__(self):
            self.frame = 0

        def detect(self, _image):
            self.frame += 1
            return (
                Detection("person", 0.9, BoundingBox(0.6, 0.1, 0.9, 0.9)),
                Detection(
                    "flame",
                    0.8,
                    BoundingBox(0.1 + self.frame * 0.001, 0.1, 0.3, 0.4),
                ),
            )

        def covers_labels(self, _labels):
            return True

    filtered = TemporalDetectionFilter(
        _Detector(),
        labels=frozenset({"flame", "smoke"}),
        minimum_consecutive_frames=3,
    )

    assert tuple(item.label for item in filtered.detect(object())) == ("person",)
    assert tuple(item.label for item in filtered.detect(object())) == ("person",)
    assert tuple(item.label for item in filtered.detect(object())) == ("person", "flame")


def test_temporal_filter_tracks_flickering_fire_aliases_by_bounded_center_motion() -> None:
    class _Detector:
        def __init__(self) -> None:
            self.frames = iter(
                (
                    (Detection("fire", 0.88, BoundingBox(0.10, 0.10, 0.26, 0.35)),),
                    (Detection("flame", 0.84, BoundingBox(0.16, 0.12, 0.32, 0.37)),),
                    (Detection("fire", 0.86, BoundingBox(0.19, 0.11, 0.35, 0.36)),),
                )
            )

        def detect(self, _image):
            return next(self.frames)

        def covers_labels(self, _labels):
            return True

    filtered = TemporalDetectionFilter(
        _Detector(),
        labels=frozenset({"fire", "flame", "smoke"}),
        minimum_consecutive_frames=3,
        iou_threshold=0.70,
        label_aliases={"fire": "flame"},
        maximum_center_distance=0.10,
        minimum_area_ratio=0.50,
    )

    assert filtered.detect(object()) == ()
    assert filtered.detect(object()) == ()
    result = filtered.detect(object())

    assert len(result) == 1
    assert result[0].label == "fire"


def test_temporal_filter_does_not_join_distant_fire_candidates() -> None:
    class _Detector:
        def __init__(self) -> None:
            self.frames = iter(
                (
                    (Detection("flame", 0.88, BoundingBox(0.10, 0.10, 0.26, 0.35)),),
                    (Detection("flame", 0.84, BoundingBox(0.48, 0.12, 0.64, 0.37)),),
                    (Detection("flame", 0.86, BoundingBox(0.12, 0.11, 0.28, 0.36)),),
                )
            )

        def detect(self, _image):
            return next(self.frames)

        def covers_labels(self, _labels):
            return True

    filtered = TemporalDetectionFilter(
        _Detector(),
        labels=frozenset({"flame"}),
        minimum_consecutive_frames=2,
        iou_threshold=0.70,
        maximum_center_distance=0.10,
        minimum_area_ratio=0.50,
        maximum_missed_frames=0,
    )

    assert filtered.detect(object()) == ()
    assert filtered.detect(object()) == ()
    # Frame three is a fresh local candidate rather than proof for the distant frame two box.
    assert filtered.detect(object()) == ()


def test_temporal_filter_preserves_two_adjacent_flame_histories_with_global_assignment() -> None:
    class _Detector:
        def __init__(self) -> None:
            self.frames = iter(
                (
                    (
                        Detection("flame", 0.90, BoundingBox(0.00, 0.10, 0.40, 0.50)),
                        Detection("flame", 0.80, BoundingBox(0.20, 0.10, 0.60, 0.50)),
                    ),
                    # The high-confidence contour is closest to the second prior
                    # candidate, but the lower-confidence contour can only extend
                    # that candidate.  Greedy matching would reset one history.
                    (
                        Detection("flame", 0.95, BoundingBox(0.16, 0.10, 0.56, 0.50)),
                        Detection("flame", 0.70, BoundingBox(0.38, 0.10, 0.78, 0.50)),
                    ),
                )
            )

        def detect(self, _image):
            return next(self.frames)

        def covers_labels(self, _labels):
            return True

    filtered = TemporalDetectionFilter(
        _Detector(),
        labels=frozenset({"flame"}),
        minimum_consecutive_frames=2,
        iou_threshold=0.05,
        maximum_missed_frames=0,
    )

    assert filtered.detect(object()) == ()
    stable = filtered.detect(object())

    assert len(stable) == 2
    assert all(detection.label == "flame" for detection in stable)


def test_temporal_filter_preserves_vehicle_history_across_subtype_flips() -> None:
    class _Detector:
        def __init__(self) -> None:
            self.frames = iter(
                (
                    (Detection("van", 0.88, BoundingBox(0.10, 0.20, 0.34, 0.64)),),
                    (Detection("car", 0.86, BoundingBox(0.12, 0.20, 0.36, 0.64)),),
                    (Detection("truck", 0.84, BoundingBox(0.14, 0.20, 0.38, 0.64)),),
                )
            )

        def detect(self, _image):
            return next(self.frames)

        def covers_labels(self, _labels):
            return True

    filtered = TemporalDetectionFilter(
        _Detector(),
        labels=frozenset({"car", "van", "truck"}),
        minimum_consecutive_frames=3,
        label_aliases={"car": "vehicle", "van": "vehicle", "truck": "vehicle"},
    )

    assert filtered.detect(object()) == ()
    assert filtered.detect(object()) == ()
    stable = filtered.detect(object())

    assert len(stable) == 1
    assert stable[0].label == "truck"


class _Input:
    name = "images"
    shape = [1, 3, 640, 640]


class _Session:
    def __init__(self, output: object) -> None:
        self.output = output
        self.received = None

    def get_inputs(self):
        return [_Input()]

    def get_providers(self):
        return ["CPUExecutionProvider"]

    def run(self, _outputs, feeds):
        self.received = feeds
        return [self.output]


def detector(output: object, *, threshold: float = 0.25) -> OnnxNx6Detector:
    return OnnxNx6Detector(
        OnnxNx6Config(
            model_path=Path("fake.onnx"),
            class_names=("fire", "smoke"),
            input_width=640,
            input_height=640,
            confidence_threshold=threshold,
        ),
        session=_Session(output),
    )


def test_post_nms_nx6_is_adapted_to_canonical_detection() -> None:
    model = detector(np.array([[[64, 128, 320, 512, 0.9, 0]]], dtype=np.float32))
    image = np.zeros((640, 640, 3), dtype=np.uint8)

    (result,) = model.detect(image)

    assert result.label == "flame"
    assert result.confidence == pytest.approx(0.9)
    assert result.bbox == BoundingBox(0.1, 0.2, 0.5, 0.8)
    assert model.provider_names == ("CPUExecutionProvider",)


def test_detector_warmup_initializes_provider_with_static_input() -> None:
    model = detector(np.empty((1, 0, 6), dtype=np.float32))

    model.warmup(iterations=2)

    received = model._session.received
    assert received is not None
    assert received["images"].shape == (1, 3, 640, 640)
    assert received["images"].dtype == np.float32


def test_tensor_engine_path_uses_direct_tensorrt_session(monkeypatch) -> None:
    created: list[Path] = []

    class _TensorRtSession(_Session):
        def __init__(self, path: Path) -> None:
            created.append(path)
            super().__init__(np.empty((1, 0, 6), dtype=np.float32))

        def get_providers(self):
            return ["TensorrtExecutionProvider"]

    monkeypatch.setattr(tensorrt_session_module, "TensorRtNx6Session", _TensorRtSession)
    model = OnnxNx6Detector(
        OnnxNx6Config(
            model_path=Path("fire.engine"),
            class_names=("fire", "smoke"),
            input_width=640,
            input_height=640,
        )
    )

    assert created == [Path("fire.engine")]
    assert model.provider_names == ("TensorrtExecutionProvider",)
    assert model.detect(np.zeros((640, 640, 3), dtype=np.uint8)) == ()


def test_confidence_filter_runs_before_legacy_adapter() -> None:
    model = detector(np.array([[64, 128, 320, 512, 0.2, 0]], dtype=np.float32), threshold=0.25)

    assert model.detect(np.zeros((640, 640, 3), dtype=np.uint8)) == ()


def test_non_nx6_onnx_output_is_rejected() -> None:
    model = detector(np.zeros((1, 3, 7), dtype=np.float32))

    with pytest.raises(OnnxOutputContractError, match="Nx6"):
        model.detect(np.zeros((640, 640, 3), dtype=np.uint8))


def test_raw_yolo_output_runs_class_aware_nms_and_maps_boxes() -> None:
    # Three anchors in 4+2 by N form: two overlapping fire boxes and one smoke box.
    output = np.array(
        [
            [
                [320, 325, 100],
                [320, 325, 100],
                [200, 200, 50],
                [200, 200, 50],
                [0.90, 0.80, 0.10],
                [0.10, 0.20, 0.85],
            ]
        ],
        dtype=np.float32,
    )
    model = OnnxRawYoloDetector(
        OnnxRawYoloConfig(
            model_path=Path("raw.onnx"),
            class_names=("fire", "smoke"),
            input_width=640,
            input_height=640,
            confidence_threshold=0.25,
            iou_threshold=0.45,
        ),
        session=_Session(output),
    )

    results = model.detect(np.zeros((640, 640, 3), dtype=np.uint8))

    assert tuple(item.label for item in results) == ("flame", "smoke")
    assert results[0].confidence == pytest.approx(0.90)
    assert results[0].bbox == BoundingBox(220 / 640, 220 / 640, 420 / 640, 420 / 640)
    assert results[1].bbox == BoundingBox(75 / 640, 75 / 640, 125 / 640, 125 / 640)


def test_raw_yolo_output_rejects_wrong_feature_count() -> None:
    model = OnnxRawYoloDetector(
        OnnxRawYoloConfig(
            model_path=Path("raw.onnx"),
            class_names=("fire", "smoke"),
            input_width=640,
            input_height=640,
        ),
        session=_Session(np.zeros((1, 7, 10), dtype=np.float32)),
    )

    with pytest.raises(OnnxOutputContractError, match="4 box values"):
        model.detect(np.zeros((640, 640, 3), dtype=np.uint8))


def test_tiled_detection_recovers_small_objects_and_respects_scan_interval() -> None:
    class _TiledDetector:
        class_names = ("person",)
        provider_names = ("CPUExecutionProvider",)

        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []
            self.warmup_iterations = 0

        def detect(self, image):
            height, width = image.shape[:2]
            self.calls.append((height, width))
            if width == 200:
                return ()
            return (
                Detection(
                    "person",
                    0.8,
                    BoundingBox(0.25, 0.2, 0.75, 0.8),
                    model_version="fake-coco",
                ),
            )

        def warmup(self, *, iterations: int = 1) -> None:
            self.warmup_iterations += iterations

        def covers_labels(self, labels):
            return set(labels).issubset(self.class_names)

    base = _TiledDetector()
    detector = TiledDetectionFusion(
        base,
        TiledDetectionConfig(
            columns=2,
            rows=1,
            overlap_fraction=0.0,
            scan_interval_frames=2,
            maximum_tile_box_area=1.0,
        ),
    )
    image = np.zeros((100, 200, 3), dtype=np.uint8)

    first = detector.detect(image)
    second = detector.detect(image)
    detector.warmup(iterations=2)

    assert base.calls == [(100, 200), (100, 100), (100, 100), (100, 200)]
    assert tuple(item.bbox for item in first) == (
        BoundingBox(0.125, 0.2, 0.375, 0.8),
        BoundingBox(0.625, 0.2, 0.875, 0.8),
    )
    assert all(item.metadata["tiled_detection"] is True for item in first)
    assert second == ()
    assert detector.class_names == ("person",)
    assert detector.provider_names == ("CPUExecutionProvider",)
    assert detector.covers_labels(("person",)) is True
    assert base.warmup_iterations == 2


def test_tiled_detection_fuses_full_frame_and_tile_duplicates() -> None:
    class _Detector:
        class_names = ("car",)

        def __init__(self) -> None:
            self.call = 0

        def detect(self, _image):
            self.call += 1
            if self.call == 1:
                return (Detection("car", 0.7, BoundingBox(0.2, 0.2, 0.4, 0.4)),)
            if self.call == 2:
                return (Detection("car", 0.9, BoundingBox(0.4, 0.2, 0.8, 0.4)),)
            return ()

        def warmup(self, *, iterations=1):
            return None

        def covers_labels(self, labels):
            return set(labels).issubset(self.class_names)

    detector = TiledDetectionFusion(
        _Detector(),
        TiledDetectionConfig(columns=2, rows=1, overlap_fraction=0.0),
    )

    (result,) = detector.detect(np.zeros((100, 200, 3), dtype=np.uint8))

    assert result.confidence == 0.9
    assert result.bbox == BoundingBox(0.2, 0.2, 0.4, 0.4)
    assert result.metadata["tiled_fusion_count"] == 2
    assert result.metadata["tiled_detection"] is True


def test_tiled_detection_filters_low_confidence_and_nonpriority_tile_results() -> None:
    class _Detector:
        class_names = ("person", "car", "chair")

        def __init__(self) -> None:
            self.call = 0

        def detect(self, _image):
            self.call += 1
            if self.call == 1:
                return (Detection("chair", 0.8, BoundingBox(0.1, 0.1, 0.2, 0.2)),)
            return (
                Detection("person", 0.39, BoundingBox(0.1, 0.1, 0.2, 0.2)),
                Detection("car", 0.50, BoundingBox(0.3, 0.3, 0.5, 0.5)),
                Detection("chair", 0.90, BoundingBox(0.6, 0.6, 0.8, 0.8)),
            )

        def warmup(self, *, iterations=1):
            return None

    detector = TiledDetectionFusion(
        _Detector(),
        TiledDetectionConfig(
            columns=2,
            rows=1,
            overlap_fraction=0.0,
            tile_confidence_threshold=0.40,
            tile_labels=frozenset({" PERSON ", "car"}),
        ),
    )

    results = detector.detect(np.zeros((100, 200, 3), dtype=np.uint8))

    assert [item.label for item in results].count("chair") == 1
    assert [item.label for item in results].count("person") == 0
    assert [item.label for item in results].count("car") == 2
    assert detector.config.tile_labels == frozenset({"person", "car"})


def test_tiled_detection_applies_class_specific_confidence_override() -> None:
    class _Detector:
        class_names = ("airplane", "person")

        def __init__(self) -> None:
            self.call = 0

        def detect(self, _image):
            self.call += 1
            if self.call == 1:
                return ()
            return (
                Detection("airplane", 0.46, BoundingBox(0.1, 0.1, 0.2, 0.2)),
                Detection("person", 0.46, BoundingBox(0.3, 0.1, 0.4, 0.2)),
            )

        def warmup(self, *, iterations=1):
            return None

    detector = TiledDetectionFusion(
        _Detector(),
        TiledDetectionConfig(
            columns=2,
            rows=1,
            overlap_fraction=0.0,
            tile_confidence_threshold=0.40,
            tile_confidence_by_label={" AIRPLANE ": 0.50},
            tile_labels=frozenset({"airplane", "person"}),
        ),
    )

    result = detector.detect(np.zeros((100, 200, 3), dtype=np.uint8))

    assert [detection.label for detection in result] == ["person", "person"]
    assert dict(detector.config.tile_confidence_by_label) == {"airplane": 0.50}


def test_label_remap_merges_source_classes_into_runtime_family() -> None:
    class _Detector:
        class_names = ("pedestrian", "people", "van")
        provider_names = ("CPUExecutionProvider",)

        def detect(self, _image):
            return (
                Detection("pedestrian", 0.8, BoundingBox(0.1, 0.1, 0.3, 0.4)),
                Detection("people", 0.7, BoundingBox(0.11, 0.11, 0.31, 0.41)),
                Detection("van", 0.9, BoundingBox(0.6, 0.5, 0.9, 0.8)),
            )

        def warmup(self, *, iterations=1):
            self.iterations = iterations

    base = _Detector()
    detector = LabelRemapDetector(
        base,
        {"pedestrian": "person", "people": "person", "van": "car"},
    )

    results = detector.detect(None)
    detector.warmup(iterations=2)

    assert [(item.label, item.confidence) for item in results] == [
        ("car", 0.9),
        ("person", 0.8),
    ]
    assert results[1].metadata["source_label"] == "pedestrian"
    assert detector.class_names == ("person", "car")
    assert detector.provider_names == ("CPUExecutionProvider",)
    assert detector.covers_labels(("person", "car")) is True
    assert base.iterations == 2


def test_frame_cadenced_detector_staggers_inference_without_repeating_boxes() -> None:
    class _Detector:
        class_names = ("person",)
        provider_names = ("TensorrtExecutionProvider",)

        def __init__(self) -> None:
            self.calls = 0
            self.warmup_iterations = 0

        def detect(self, _image):
            self.calls += 1
            return (Detection("person", 0.8, BoundingBox(0.1, 0.1, 0.3, 0.5)),)

        def warmup(self, *, iterations: int = 1) -> None:
            self.warmup_iterations += iterations

        def covers_labels(self, required_labels) -> bool:
            return set(required_labels).issubset(self.class_names)

    first_base = _Detector()
    second_base = _Detector()
    first = FrameCadencedDetector(first_base, frame_stride=2, frame_phase=0)
    second = FrameCadencedDetector(second_base, frame_stride=2, frame_phase=1)

    first_results = [first.detect(None) for _ in range(6)]
    second_results = [second.detect(None) for _ in range(6)]

    assert [bool(item) for item in first_results] == [True, False, True, False, True, False]
    assert [bool(item) for item in second_results] == [False, True, False, True, False, True]
    assert first_base.calls == second_base.calls == 3
    assert first.inference_count == second.inference_count == 3
    assert first.skipped_count == second.skipped_count == 3
    assert first.provider_names == ("TensorrtExecutionProvider",)
    assert first.covers_labels(("person",)) is True
    first.warmup(iterations=2)
    assert first_base.warmup_iterations == 2


@pytest.mark.parametrize(
    ("stride", "phase"),
    ((0, 0), (2, -1), (2, 2), (True, 0), (2, True)),
)
def test_frame_cadenced_detector_rejects_invalid_schedule(stride, phase) -> None:
    with pytest.raises(ValueError):
        FrameCadencedDetector(object(), frame_stride=stride, frame_phase=phase)


def test_same_label_detection_fusion_suppresses_cross_model_duplicates() -> None:
    class _Ensemble:
        def detect(self, _image):
            return (
                Detection("car", 0.8, BoundingBox(0.1, 0.1, 0.3, 0.3)),
                Detection("car", 0.9, BoundingBox(0.11, 0.11, 0.31, 0.31)),
                Detection("person", 0.7, BoundingBox(0.11, 0.11, 0.31, 0.31)),
            )

        def covers_labels(self, labels):
            return set(labels).issubset({"car", "person"})

    detector = SameLabelDetectionFusion(_Ensemble(), iou_threshold=0.45)

    results = detector.detect(None)

    assert [(item.label, item.confidence) for item in results] == [
        ("car", 0.9),
        ("person", 0.7),
    ]
    assert detector.covers_labels(("car", "person")) is True


def test_letterbox_transform_restores_source_coordinates() -> None:
    transform = LetterboxTransform(
        source_width=1280,
        source_height=720,
        input_width=640,
        input_height=640,
        scale=0.5,
        pad_x=0,
        pad_y=140,
    )

    assert transform.map_input_xyxy_to_source((100, 190, 300, 390)) == (200, 100, 600, 500)


def test_ensemble_reports_person_safety_class_coverage() -> None:
    fire = detector(np.empty((0, 6), dtype=np.float32))
    safety = OnnxNx6Detector(
        OnnxNx6Config(
            model_path=Path("safety.onnx"),
            class_names=("person", "firefighter"),
            input_width=640,
            input_height=640,
        ),
        session=_Session(np.empty((0, 6), dtype=np.float32)),
    )
    ensemble = DetectorEnsemble((fire, safety))

    assert ensemble.covers_labels(("person", "firefighter")) is True
    assert ensemble.covers_labels(("person", "vehicle")) is False


def test_detector_ensemble_routes_lck_to_matching_class_models_and_filters_outputs() -> None:
    class _Detector:
        def __init__(self, class_names: tuple[str, ...], emitted: tuple[str, ...]) -> None:
            self.class_names = class_names
            self.emitted = emitted
            self.calls = 0

        def detect(self, _image):
            self.calls += 1
            return tuple(
                Detection(
                    label,
                    0.9,
                    BoundingBox(0.1 + index * 0.1, 0.1, 0.18 + index * 0.1, 0.3),
                )
                for index, label in enumerate(self.emitted)
            )

    common = _Detector(("person", "car", "chair"), ("person", "chair"))
    person_specialist = _Detector(("person",), ("person",))
    fire_specialist = _Detector(("flame", "smoke"), ("flame",))
    ensemble = DetectorEnsemble((common, person_specialist, fire_specialist))

    assert ensemble.set_active_labels(("person", "pedestrian")) == 2
    assert ensemble.active_labels == frozenset({"person", "pedestrian"})
    assert [item.label for item in ensemble.detect(None)] == ["person", "person"]
    assert (common.calls, person_specialist.calls, fire_specialist.calls) == (1, 1, 0)

    assert ensemble.set_active_labels(()) == 0
    assert ensemble.detect(None) == ()
    assert (common.calls, person_specialist.calls, fire_specialist.calls) == (1, 1, 0)

    assert ensemble.set_active_labels(None) == 3
    assert [item.label for item in ensemble.detect(None)] == [
        "person",
        "chair",
        "person",
        "flame",
    ]


def test_lck_route_overrides_detector_cadence_then_restores_configured_stride() -> None:
    class _Detector:
        class_names = ("person",)

        def __init__(self) -> None:
            self.calls = 0

        def detect(self, _image):
            self.calls += 1
            return (Detection("person", 0.9, BoundingBox(0.1, 0.1, 0.2, 0.3)),)

    base = _Detector()
    cadenced = FrameCadencedDetector(base, frame_stride=4, frame_phase=0)
    ensemble = DetectorEnsemble((cadenced,))

    ensemble.set_active_labels(("person",))
    assert cadenced.force_every_frame is True
    assert [bool(ensemble.detect(None)) for _ in range(3)] == [True, True, True]
    assert base.calls == 3

    ensemble.set_active_labels(None)
    assert cadenced.force_every_frame is False
    assert ensemble.detect(None) == ()
    assert base.calls == 3


def test_lck_route_can_keep_the_configured_detector_schedule() -> None:
    class _Detector:
        class_names = ("person",)

        def __init__(self) -> None:
            self.calls = 0

        def detect(self, _image):
            self.calls += 1
            return (Detection("person", 0.8, BoundingBox(0.1, 0.1, 0.2, 0.2)),)

    base = _Detector()
    cadenced = FrameCadencedDetector(base, frame_stride=3, frame_phase=0)
    ensemble = DetectorEnsemble((cadenced,), force_locked_cadence=False)

    ensemble.set_active_labels(("person",))
    assert cadenced.force_every_frame is False
    assert [bool(ensemble.detect(None)) for _ in range(4)] == [True, False, False, True]
    assert base.calls == 2


def test_capture_config_identifies_rtsp_and_validates_transport() -> None:
    assert CaptureConfig("rtsp://camera/live").is_rtsp is True
    assert CaptureConfig(0).is_rtsp is False
    assert CaptureConfig("synthetic://patrol").is_synthetic is True
    assert (
        CaptureConfig("synthetic://patrol").redacted_source_description
        == "deterministic synthetic patrol source"
    )
    assert CaptureConfig("rtsp://camera/live", backend="gstreamer").rtsp_codec == "h265"
    with pytest.raises(ValueError, match="rtsp_transport"):
        CaptureConfig(0, rtsp_transport="srt")
    with pytest.raises(ValueError, match="backend"):
        CaptureConfig(0, backend="v4l2")
    with pytest.raises(ValueError, match="requires an RTSP source"):
        CaptureConfig(0, backend="gstreamer")
    with pytest.raises(ValueError, match="synthetic capture source requires"):
        CaptureConfig("synthetic://patrol", backend="ffmpeg")
    with pytest.raises(ValueError, match="hardware decode requires"):
        CaptureConfig("rtsp://camera/live", gstreamer_hardware_decode=True)
    with pytest.raises(ValueError, match="rtsp_codec"):
        CaptureConfig("rtsp://camera/live", rtsp_codec="vp9")
    with pytest.raises(ValueError, match="gstreamer latency"):
        CaptureConfig("rtsp://camera/live", gstreamer_latency_ms=-1)
    with pytest.raises(ValueError, match="reconnect attempts"):
        CaptureConfig(0, reconnect_attempts=-1)
    with pytest.raises(ValueError, match="fps must be finite"):
        CaptureConfig(0, fps=float("nan"))
    with pytest.raises(ValueError, match="reconnect delay must be finite"):
        CaptureConfig(0, reconnect_delay_seconds=float("inf"))
    with pytest.raises(ValueError, match="source must be a camera index"):
        CaptureConfig(True)


def test_synthetic_frame_source_is_clock_paced_dynamic_and_camera_free() -> None:
    config = CaptureConfig("synthetic://patrol", width=160, height=120, fps=500.0)
    source = frame_source_from_config(config)

    assert isinstance(source, SyntheticFrameSource)
    with source:
        first = source.read()
        second = source.read()

    assert first.frame_id == "synthetic-000000001"
    assert second.frame_id == "synthetic-000000002"
    assert first.width == second.width == 160
    assert first.height == second.height == 120
    assert first.image_bgr.shape == (120, 160, 3)
    assert np.any(first.image_bgr != second.image_bgr)
    assert second.captured_at_s > first.captured_at_s
    assert source.reconnect_count == 0


def test_synthetic_frame_source_exercises_sparse_flow_after_warmup() -> None:
    source = SyntheticFrameSource(
        CaptureConfig("synthetic://patrol", width=640, height=480, fps=500.0)
    )
    avoidance = OpenCVSparseFlowAvoidance(MonocularAvoidanceConfig(analysis_width=320))
    assessments = []

    with source:
        for _ in range(8):
            frame = source.read()
            assessments.append(
                avoidance.update(
                    frame.image_bgr,
                    frame_id=frame.frame_id,
                    captured_at_s=frame.captured_at_s,
                    produced_at_s=time.monotonic(),
                )
            )

    assert assessments[0].state is CollisionRiskState.INVALID
    assert assessments[0].reason == "WARMUP"
    assert any(item.state is not CollisionRiskState.INVALID for item in assessments[1:])
    assert all(item.advisory_only for item in assessments)


class _Capture:
    def __init__(self, image) -> None:
        self.image = image
        self.released = False
        self.set_calls: list[tuple[object, object]] = []

    def isOpened(self) -> bool:
        return not self.released

    def set(self, key, value) -> bool:
        self.set_calls.append((key, value))
        return True

    def read(self):
        return (self.image is not None, self.image)

    def release(self) -> None:
        self.released = True


class _CV2:
    CAP_ANY = 0
    CAP_DSHOW = 1
    CAP_MSMF = 2
    CAP_FFMPEG = 3
    CAP_GSTREAMER = 8
    CAP_PROP_FRAME_WIDTH = 4
    CAP_PROP_FRAME_HEIGHT = 5
    CAP_PROP_FPS = 6
    CAP_PROP_BUFFERSIZE = 7

    def __init__(self, captures: list[_Capture]) -> None:
        self.captures = captures
        self.calls: list[tuple[object, int]] = []

    def VideoCapture(self, source, backend):
        self.calls.append((source, backend))
        return self.captures.pop(0)


def test_frame_source_reconnects_without_accumulating_stale_frames(monkeypatch) -> None:
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2 = _CV2([_Capture(None), _Capture(image)])
    monkeypatch.setattr(vision_module, "_require_cv2", lambda: cv2)
    source = OpenCVFrameSource(CaptureConfig(0, reconnect_attempts=1, reconnect_delay_seconds=0))

    frame = source.read()

    assert frame.width == 640
    assert frame.height == 480
    assert source.reconnect_count == 1
    source.close()


def test_rtsp_open_error_does_not_expose_credentials(monkeypatch) -> None:
    capture = _Capture(None)
    capture.released = True
    cv2 = _CV2([capture])
    monkeypatch.setattr(vision_module, "_require_cv2", lambda: cv2)
    source = OpenCVFrameSource(
        CaptureConfig("rtsp://SECRET_USER:SECRET_PASSWORD@camera.invalid/stream")
    )

    with pytest.raises(CameraReadError) as captured_error:
        source.open()

    message = str(captured_error.value)
    assert "RTSP source" in message
    assert "SECRET_USER" not in message
    assert "SECRET_PASSWORD" not in message


def test_rtsp_open_restores_process_ffmpeg_options(monkeypatch) -> None:
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2 = _CV2([_Capture(image)])
    monkeypatch.setattr(vision_module, "_require_cv2", lambda: cv2)
    monkeypatch.setenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", "existing;value")
    source = OpenCVFrameSource(CaptureConfig("rtsp://camera.invalid/stream"))

    source.open()

    assert vision_module.os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == "existing;value"
    source.close()


def test_rtsp_gstreamer_h265_hardware_pipeline_is_bounded(monkeypatch) -> None:
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2 = _CV2([_Capture(image)])
    monkeypatch.setattr(vision_module, "_require_cv2", lambda: cv2)
    source = OpenCVFrameSource(
        CaptureConfig(
            "rtsp://camera.invalid/stream=0",
            backend="gstreamer",
            rtsp_codec="h265",
            gstreamer_hardware_decode=True,
            gstreamer_latency_ms=80,
        )
    )

    frame = source.read()

    pipeline, backend = cv2.calls[0]
    assert backend == cv2.CAP_GSTREAMER
    assert "protocols=tcp" in pipeline
    assert "latency=80" in pipeline
    assert (
        "rtph265depay ! h265parse config-interval=-1 ! "
        "video/x-h265,stream-format=byte-stream,alignment=au ! nvv4l2decoder" in pipeline
    )
    assert "appsink drop=true max-buffers=1 sync=false" in pipeline
    assert source._capture is not None
    assert source._capture.set_calls == []
    assert frame.width == 1280
    assert frame.height == 720
    source.close()


def test_rtsp_gstreamer_software_pipeline_rejects_control_characters(monkeypatch) -> None:
    cv2 = _CV2([_Capture(None)])
    monkeypatch.setattr(vision_module, "_require_cv2", lambda: cv2)
    source = OpenCVFrameSource(
        CaptureConfig("rtsp://camera.invalid/stream\nattack", backend="gstreamer")
    )

    with pytest.raises(ValueError, match="control characters"):
        source.open()


def test_buffered_frame_source_preserves_fifo_order_and_reports_backpressure() -> None:
    class _FastSource:
        reconnect_count = 2

        def __init__(self) -> None:
            self.index = 0
            self.closed = False

        def open(self) -> None:
            pass

        def read(self):
            self.index += 1
            return vision_module.CapturedFrame(
                frame_id=f"frame-{self.index}",
                captured_at_s=time.monotonic(),
                image_bgr=None,
                width=640,
                height=480,
            )

        def close(self) -> None:
            self.closed = True

    inner = _FastSource()
    source = vision_module.BufferedFrameSource(inner, capacity=2)
    source.open()
    deadline = time.monotonic() + 1.0
    while source.backpressure_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    frames = [source.read(), source.read(), source.read()]
    source.close()

    assert [frame.frame_id for frame in frames] == ["frame-1", "frame-2", "frame-3"]
    assert source.reconnect_count == 2
    assert source.queue_high_watermark == 2
    assert source.backpressure_count >= 1
    assert source.captured_frame_count >= 3
    assert source.delivered_frame_count == 3
    assert inner.closed is True


def test_buffered_frame_source_propagates_worker_failure_without_secret_text() -> None:
    class _FailingSource:
        def open(self) -> None:
            pass

        def read(self):
            raise RuntimeError("SECRET camera address")

        def close(self) -> None:
            pass

    source = vision_module.BufferedFrameSource(_FailingSource(), capacity=2)

    with pytest.raises(CameraReadError) as captured_error:
        source.read()

    assert "RuntimeError" in str(captured_error.value)
    assert "SECRET" not in str(captured_error.value)
    source.close()
