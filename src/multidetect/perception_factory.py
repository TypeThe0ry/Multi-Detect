"""Reusable construction boundary for the production perception graph.

The live camera and integrity-bound raw replay launchers deliberately share this
factory.  It verifies every configured model manifest before loading any ONNX or
TensorRT graph, then returns the complete detector/wrapper graph together with
the independent RGB verifier, semantic-context side channel, and provenance
metadata needed by startup evidence.

The ``args`` object is the parsed ``live-camera`` argument namespace (or another
object exposing the same perception attributes).  Keeping this narrow adapter
lets a replay launcher reuse the exact production parameters without binding the
factory to a frame source, telemetry provider, or world backend.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import MissionConfig
from .engine_provenance import verify_engine_provenance
from .model_manifest import VerifiedModelArtifact, sha256_file, verify_model_manifest
from .operator_tracking import FIRE_CANDIDATE_TRACK_LABELS
from .rgb_fire_corroboration import (
    IndependentRgbFireCorroborationConfig,
    IndependentRgbFireCorroborator,
)
from .semantic_environment import (
    CITYSEMSEGFORMER_LABELS,
    AsyncSemanticContextRunner,
    OnnxCategoricalSemanticContext,
    OnnxSemanticContextConfig,
)
from .tensorrt_session import TensorRtSemanticSession
from .vision import (
    BrightNeutralLightVetoFilter,
    ClassConfidenceFilter,
    DetectorEnsemble,
    FrameCadencedDetector,
    LabelAllowListFilter,
    LabelRemapDetector,
    MultiSourceConfidenceFilter,
    OnnxNx6Config,
    OnnxNx6Detector,
    OnnxRawYoloConfig,
    OnnxRawYoloDetector,
    PersonOverlapVetoFilter,
    SameLabelDetectionFusion,
    TemporalDetectionFilter,
    TiledDetectionConfig,
    TiledDetectionFusion,
    VehicleFurnitureOverlapVetoFilter,
)

COCO80_CLASS_NAMES = tuple(
    "person,bicycle,car,motorcycle,airplane,bus,train,truck,boat,traffic light,fire hydrant,"
    "stop sign,parking meter,bench,bird,cat,dog,horse,sheep,cow,elephant,bear,zebra,giraffe,"
    "backpack,umbrella,handbag,tie,suitcase,frisbee,skis,snowboard,sports ball,kite,"
    "baseball bat,baseball glove,skateboard,surfboard,tennis racket,bottle,wine glass,cup,"
    "fork,knife,spoon,bowl,banana,apple,sandwich,orange,broccoli,carrot,hot dog,pizza,donut,"
    "cake,chair,couch,potted plant,bed,dining table,toilet,tv,laptop,mouse,remote,keyboard,"
    "cell phone,microwave,oven,toaster,sink,refrigerator,book,clock,vase,scissors,teddy bear,"
    "hair drier,toothbrush".split(",")
)
PRIORITY_DETECTION_CLASS_NAMES = (
    "person",
    "firefighter",
    "airplane",
    "bicycle",
    "car",
    "motorcycle",
    "bus",
    "train",
    "truck",
    "boat",
)
AIRCRAFT_DETECTION_CLASS_NAMES = frozenset({"airplane"})
VEHICLE_DETECTION_CLASS_NAMES = frozenset(
    label
    for label in PRIORITY_DETECTION_CLASS_NAMES
    if label not in {"person", "firefighter", *AIRCRAFT_DETECTION_CLASS_NAMES}
)
VEHICLE_TEMPORAL_LABEL_ALIASES = {
    label: "vehicle"
    for label in (
        *VEHICLE_DETECTION_CLASS_NAMES,
        "vehicle",
        "van",
        "motorbike",
        "motor",
        "tricycle",
        "awning-tricycle",
        "awning_tricycle",
    )
}
ENVIRONMENT_RISK_CLASS_NAMES = ("power_line", "flammable_tank")
NON_SELECTABLE_AUTOMATIC_LABELS = frozenset(
    {
        "chair",
        "couch",
        "bed",
        "dining table",
        "toilet",
        "tv",
        "laptop",
        "mouse",
        "remote",
        "keyboard",
        "cell phone",
        "potted plant",
        "book",
        "clock",
        "vase",
    }
)
VISDRONE_PRIORITY_CLASS_NAMES = (
    "pedestrian",
    "people",
    "bicycle",
    "car",
    "van",
    "truck",
    "tricycle",
    "awning-tricycle",
    "bus",
    "motor",
)
VISDRONE_PRIORITY_LABEL_MAP = (
    "pedestrian=person,people=person,van=car,tricycle=motorcycle,"
    "awning-tricycle=motorcycle,motor=motorcycle"
)


@dataclass(frozen=True, slots=True)
class VerifiedPerceptionArtifact:
    """One role-bound executable artifact and the manifest that admitted it."""

    component: str
    model_path: Path
    manifest_path: Path
    manifest_sha256: str
    artifact: VerifiedModelArtifact


@dataclass(slots=True)
class ProductionPerceptionBundle:
    """Complete production perception graph plus immutable admission evidence."""

    detector: Any
    model_detectors: tuple[Any, ...]
    primary_detector: Any
    rgb_fire_verifier: Any | None
    rgb_fire_corroborator: IndependentRgbFireCorroborator | None
    semantic_context_model: OnnxCategoricalSemanticContext | None
    semantic_context_runner: AsyncSemanticContextRunner | None
    verified_fire_model: VerifiedModelArtifact | None
    verified_rgb_fire_verifier: VerifiedModelArtifact | None
    verified_safety_model: VerifiedModelArtifact | None
    verified_priority_model: VerifiedModelArtifact | None
    verified_environment_model: VerifiedModelArtifact | None
    verified_semantic_context_model: VerifiedModelArtifact | None
    verified_artifacts: tuple[VerifiedPerceptionArtifact, ...]
    configured_model_components: tuple[str, ...]
    model_set_sha256: str | None
    rgb_fire_verifier_evidence_qualified: bool
    person_safety_model_coverage: bool
    person_safety_evidence_qualified: bool
    environment_model_coverage: bool
    environment_evidence_qualified: bool
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def provider_names(self) -> tuple[str, ...]:
        """Flattened provider list in the same order as the live startup event."""

        return tuple(
            provider
            for detector in self.model_detectors
            for provider in getattr(detector, "provider_names", ())
        )

    @property
    def all_configured_artifacts_verified(self) -> bool:
        return len(self.verified_artifacts) == len(self.configured_model_components)

    @property
    def all_configured_artifacts_production_approved(self) -> bool:
        return self.all_configured_artifacts_verified and all(
            item.artifact.production_approved for item in self.verified_artifacts
        )

    @property
    def synthetic_hil_components(self) -> tuple[str, ...]:
        return tuple(
            item.component for item in self.verified_artifacts if item.artifact.synthetic_hil_only
        )

    def verified_artifact(self, component: str) -> VerifiedPerceptionArtifact | None:
        normalized = component.strip().lower()
        return next(
            (item for item in self.verified_artifacts if item.component == normalized),
            None,
        )

    def close(self) -> bool:
        """Release owned semantic/TensorRT resources; safe to call repeatedly."""

        if self._closed:
            return True
        self._closed = True
        clean = True
        if self.semantic_context_runner is not None:
            clean = bool(self.semantic_context_runner.close())
        seen: set[int] = set()
        for root in (*self.model_detectors, self.rgb_fire_verifier):
            if root is not None:
                _close_detector_resources(root, seen)
        return clean


def build_production_perception(
    args: Any,
    mission_config: MissionConfig,
) -> ProductionPerceptionBundle:
    """Verify and build the exact live production perception graph.

    ``args`` must expose the perception options produced by the ``live-camera``
    parser.  The function intentionally has no camera, telemetry, clock, world,
    or mission-runner dependency, so live and raw replay can consume the same
    returned graph.
    """

    _validate_perception_arguments(args)
    providers = tuple(args.provider)
    class_names = _parse_class_names(args.class_names)
    verified_bindings: list[VerifiedPerceptionArtifact] = []
    configured_components = ["primary"]

    verified_fire_model, binding = _verify_model_binding(
        component="primary",
        manifest_path=args.model_manifest,
        model_path=args.onnx_model,
        class_names=class_names,
        output_coordinates=args.output_coordinates,
        require_production_approved=args.require_production_approved_models,
        expected_model_role="fire_candidate",
    )
    if binding is not None:
        verified_bindings.append(binding)

    verified_rgb_fire_verifier: VerifiedModelArtifact | None = None
    rgb_fire_verifier_class_names: tuple[str, ...] | None = None
    if args.rgb_fire_verifier_model is not None:
        configured_components.append("rgb_fire_verifier")
        rgb_fire_verifier_class_names = _parse_class_names(args.rgb_fire_verifier_class_names)
        verified_rgb_fire_verifier, binding = _verify_model_binding(
            component="rgb_fire_verifier",
            manifest_path=args.rgb_fire_verifier_model_manifest,
            model_path=args.rgb_fire_verifier_model,
            class_names=rgb_fire_verifier_class_names,
            output_coordinates=(
                args.rgb_fire_verifier_output_coordinates or args.output_coordinates
            ),
            require_production_approved=args.require_production_approved_models,
            expected_model_role="fire_verifier",
        )
        if binding is not None:
            verified_bindings.append(binding)
        if verified_fire_model is None or verified_rgb_fire_verifier is None:
            raise RuntimeError("independent RGB fire model manifests were not verified")
        if verified_fire_model.artifact_sha256 == verified_rgb_fire_verifier.artifact_sha256:
            raise ValueError(
                "primary fire detector and independent RGB verifier must use different artifacts"
            )
        verifier_labels = {
            "flame" if label.strip().lower() == "fire" else label.strip().lower()
            for label in rgb_fire_verifier_class_names
        }
        if mission_config.require_independent_rgb_corroboration and not (
            verifier_labels.intersection(mission_config.target_classes)
        ):
            raise ValueError(
                "independent RGB verifier does not cover any configured fire target class"
            )

    verified_safety_model: VerifiedModelArtifact | None = None
    safety_class_names: tuple[str, ...] | None = None
    if args.safety_onnx_model is not None:
        configured_components.append("safety")
        safety_class_names = (
            COCO80_CLASS_NAMES
            if args.safety_model_coco80
            else _parse_class_names(args.safety_class_names)
        )
        verified_safety_model, binding = _verify_model_binding(
            component="safety",
            manifest_path=args.safety_model_manifest,
            model_path=args.safety_onnx_model,
            class_names=safety_class_names,
            output_coordinates=(args.safety_output_coordinates or args.output_coordinates),
            require_production_approved=args.require_production_approved_models,
            expected_model_role="safety_object_evidence",
            expected_native_output_format=(
                "ultralytics_raw_xywh_class_scores"
                if args.safety_model_format == "ultralytics_raw"
                else "post_nms_N_x_6"
            ),
        )
        if binding is not None:
            verified_bindings.append(binding)

    verified_priority_model: VerifiedModelArtifact | None = None
    priority_class_names: tuple[str, ...] | None = None
    priority_label_map: dict[str, str] = {}
    priority_label_confidence_overrides: dict[str, float] = {}
    if args.priority_onnx_model is not None:
        configured_components.append("priority")
        priority_class_names = _parse_class_names(args.priority_class_names)
        priority_label_map = _parse_label_map(args.priority_label_map)
        unknown_sources = set(priority_label_map).difference(
            label.strip().lower() for label in priority_class_names
        )
        if unknown_sources:
            raise ValueError(
                "priority label map contains unknown source classes: "
                + ", ".join(sorted(unknown_sources))
            )
        priority_label_confidence_overrides = _parse_label_confidence_thresholds(
            args.priority_label_confidence_thresholds
        )
        unknown_override_labels = set(priority_label_confidence_overrides).difference(
            label.strip().lower() for label in priority_class_names
        )
        if unknown_override_labels:
            raise ValueError(
                "priority label confidence overrides contain unknown source classes: "
                + ", ".join(sorted(unknown_override_labels))
            )
        verified_priority_model, binding = _verify_model_binding(
            component="priority",
            manifest_path=args.priority_model_manifest,
            model_path=args.priority_onnx_model,
            class_names=priority_class_names,
            output_coordinates="letterbox_xyxy_px",
            require_production_approved=args.require_production_approved_models,
            expected_model_role="safety_object_evidence",
            expected_native_output_format="ultralytics_raw_xywh_class_scores",
        )
        if binding is not None:
            verified_bindings.append(binding)

    verified_environment_model: VerifiedModelArtifact | None = None
    environment_class_names: tuple[str, ...] | None = None
    if args.environment_onnx_model is not None:
        configured_components.append("environment")
        environment_class_names = _parse_class_names(args.environment_class_names)
        reserved_environment_labels = {
            "fire",
            "flame",
            "smoke",
            "hotspot",
            "person",
            "firefighter",
            "car",
            "bus",
            "truck",
            "vehicle",
        }
        conflicting = reserved_environment_labels.intersection(environment_class_names)
        if conflicting:
            raise ValueError(
                "environment model labels overlap protected fire/person/vehicle domains: "
                + ", ".join(sorted(conflicting))
            )
        verified_environment_model, binding = _verify_model_binding(
            component="environment",
            manifest_path=args.environment_model_manifest,
            model_path=args.environment_onnx_model,
            class_names=environment_class_names,
            output_coordinates=(args.environment_output_coordinates or args.output_coordinates),
            require_production_approved=args.require_production_approved_models,
            expected_model_role="environment_risk_evidence",
        )
        if binding is not None:
            verified_bindings.append(binding)

    verified_semantic_context_model: VerifiedModelArtifact | None = None
    if args.semantic_context_onnx_model is not None:
        configured_components.append("semantic_context")
        verified_semantic_context_model, binding = _verify_model_binding(
            component="semantic_context",
            manifest_path=args.semantic_context_model_manifest,
            model_path=args.semantic_context_onnx_model,
            class_names=CITYSEMSEGFORMER_LABELS,
            output_coordinates=None,
            require_production_approved=args.require_production_approved_models,
            expected_model_role="semantic_scene_context",
            expected_output_format="categorical_H_W_1",
        )
        if binding is not None:
            verified_bindings.append(binding)
        if args.semantic_context_engine is not None:
            verify_engine_provenance(
                provenance=args.semantic_context_engine_provenance,
                engine=args.semantic_context_engine,
                source_model=args.semantic_context_onnx_model,
                trtexec=args.semantic_context_trtexec,
            )

    synthetic_models = tuple(
        item.artifact for item in verified_bindings if item.artifact.synthetic_hil_only
    )
    if synthetic_models and not args.allow_synthetic_hil_model:
        raise ValueError(
            "synthetic HIL model requires the explicit --allow-synthetic-hil-model flag"
        )
    if args.allow_synthetic_hil_model and not synthetic_models:
        raise ValueError(
            "--allow-synthetic-hil-model requires a manifest marked synthetic_hil_only"
        )

    # Executable graph loading starts only after every configured manifest,
    # role, hash, coordinate contract, production gate and synthetic gate passes.
    primary_detector = FrameCadencedDetector(
        OnnxNx6Detector(
            OnnxNx6Config(
                model_path=args.onnx_model,
                class_names=class_names,
                input_width=args.input_width,
                input_height=args.input_height,
                confidence_threshold=args.confidence_threshold,
                providers=providers,
                trt_engine_cache_path=args.trt_engine_cache,
                output_coordinates=args.output_coordinates,
                model_version=(
                    verified_fire_model.model_version if verified_fire_model is not None else None
                ),
            )
        ),
        frame_stride=args.primary_model_frame_stride,
        frame_phase=args.primary_model_frame_phase,
    )
    detectors: list[Any] = [primary_detector]
    rgb_fire_verifier = None
    rgb_fire_corroborator = None
    rgb_fire_verifier_evidence_qualified = False
    semantic_context_model = None
    semantic_context_runner = None
    try:
        if args.rgb_fire_verifier_model is not None:
            if (
                rgb_fire_verifier_class_names is None
                or verified_fire_model is None
                or verified_rgb_fire_verifier is None
            ):
                raise RuntimeError("independent RGB fire verifier was not initialized")
            rgb_fire_verifier = OnnxNx6Detector(
                OnnxNx6Config(
                    model_path=args.rgb_fire_verifier_model,
                    class_names=rgb_fire_verifier_class_names,
                    input_width=args.input_width,
                    input_height=args.input_height,
                    confidence_threshold=args.rgb_fire_verifier_confidence_threshold,
                    providers=providers,
                    trt_engine_cache_path=args.trt_engine_cache,
                    output_coordinates=(
                        args.rgb_fire_verifier_output_coordinates or args.output_coordinates
                    ),
                    model_version=verified_rgb_fire_verifier.model_version,
                )
            )
            rgb_fire_verifier.warmup(iterations=args.warmup_iterations)
            rgb_fire_verifier_evidence_qualified = bool(
                (
                    verified_fire_model.production_approved
                    and verified_rgb_fire_verifier.production_approved
                )
                or (
                    args.allow_synthetic_hil_model
                    and verified_fire_model.synthetic_hil_only
                    and verified_rgb_fire_verifier.synthetic_hil_only
                )
            )
            rgb_fire_corroborator = IndependentRgbFireCorroborator(
                IndependentRgbFireCorroborationConfig(
                    minimum_iou=args.rgb_fire_verifier_minimum_iou,
                    minimum_verifier_confidence=args.rgb_fire_verifier_confidence_threshold,
                    evidence_qualified=rgb_fire_verifier_evidence_qualified,
                    primary_artifact_sha256=verified_fire_model.artifact_sha256,
                    verifier_artifact_sha256=verified_rgb_fire_verifier.artifact_sha256,
                )
            )

        if args.safety_onnx_model is not None:
            if safety_class_names is None:
                raise RuntimeError("safety model class names were not initialized")
            priority_detection_labels = frozenset(
                label.strip().lower() for label in _parse_class_names(args.safety_tile_labels)
            )
            safety_candidate_confidence = min(
                args.safety_confidence_threshold,
                args.safety_priority_confidence_threshold,
                args.safety_fallback_confidence_threshold,
                args.safety_tile_confidence_threshold,
            )
            if args.safety_model_format == "ultralytics_raw":
                safety_detector: Any = OnnxRawYoloDetector(
                    OnnxRawYoloConfig(
                        model_path=args.safety_onnx_model,
                        class_names=safety_class_names,
                        input_width=args.input_width,
                        input_height=args.input_height,
                        confidence_threshold=safety_candidate_confidence,
                        iou_threshold=args.safety_model_iou_threshold,
                        maximum_detections=args.safety_model_maximum_detections,
                        providers=providers,
                        trt_engine_cache_path=args.trt_engine_cache,
                        model_version=(
                            verified_safety_model.model_version
                            if verified_safety_model is not None
                            else None
                        ),
                    )
                )
            else:
                safety_detector = OnnxNx6Detector(
                    OnnxNx6Config(
                        model_path=args.safety_onnx_model,
                        class_names=safety_class_names,
                        input_width=args.input_width,
                        input_height=args.input_height,
                        confidence_threshold=safety_candidate_confidence,
                        providers=providers,
                        trt_engine_cache_path=args.trt_engine_cache,
                        output_coordinates=(
                            args.safety_output_coordinates or args.output_coordinates
                        ),
                        model_version=(
                            verified_safety_model.model_version
                            if verified_safety_model is not None
                            else None
                        ),
                    )
                )
            if args.safety_tile_columns * args.safety_tile_rows > 1:
                safety_detector = TiledDetectionFusion(
                    safety_detector,
                    TiledDetectionConfig(
                        columns=args.safety_tile_columns,
                        rows=args.safety_tile_rows,
                        overlap_fraction=args.safety_tile_overlap,
                        scan_interval_frames=args.safety_tile_scan_interval_frames,
                        fusion_iou_threshold=args.safety_tile_fusion_iou_threshold,
                        tile_confidence_threshold=args.safety_tile_confidence_threshold,
                        tile_confidence_by_label=_parse_label_confidence_thresholds(
                            args.safety_tile_label_confidence_thresholds
                        ),
                        tile_labels=priority_detection_labels,
                        maximum_tile_box_area=args.safety_tile_maximum_box_area,
                        maximum_detections=args.safety_model_maximum_detections,
                    ),
                )
            safety_detector = TemporalDetectionFilter(
                safety_detector,
                labels=VEHICLE_DETECTION_CLASS_NAMES,
                minimum_consecutive_frames=args.priority_vehicle_stability_frames,
                iou_threshold=0.25,
                maximum_missed_frames=1,
                label_aliases=VEHICLE_TEMPORAL_LABEL_ALIASES,
            )
            safety_detector = FrameCadencedDetector(
                safety_detector,
                frame_stride=args.safety_model_frame_stride,
                frame_phase=args.safety_model_frame_phase,
            )
            detectors.append(safety_detector)

        if args.priority_onnx_model is not None:
            if priority_class_names is None:
                raise RuntimeError("priority model class names were not initialized")
            priority_source_thresholds = {
                "pedestrian": args.priority_person_confidence_threshold,
                "people": args.priority_person_confidence_threshold,
                "bicycle": args.priority_vehicle_confidence_threshold,
                "car": args.priority_vehicle_confidence_threshold,
                "van": args.priority_vehicle_confidence_threshold,
                "truck": args.priority_vehicle_confidence_threshold,
                "tricycle": args.priority_vehicle_confidence_threshold,
                "awning-tricycle": args.priority_vehicle_confidence_threshold,
                "bus": args.priority_vehicle_confidence_threshold,
                "motor": args.priority_vehicle_confidence_threshold,
            }
            priority_source_thresholds.update(priority_label_confidence_overrides)
            priority_candidate_confidence = min(
                args.priority_confidence_threshold,
                args.priority_person_confidence_threshold,
                args.priority_vehicle_confidence_threshold,
                *priority_label_confidence_overrides.values(),
            )
            priority_detector: Any = LabelRemapDetector(
                ClassConfidenceFilter(
                    OnnxRawYoloDetector(
                        OnnxRawYoloConfig(
                            model_path=args.priority_onnx_model,
                            class_names=priority_class_names,
                            input_width=args.priority_input_width,
                            input_height=args.priority_input_height,
                            confidence_threshold=priority_candidate_confidence,
                            iou_threshold=args.priority_model_iou_threshold,
                            maximum_detections=args.priority_model_maximum_detections,
                            providers=providers,
                            trt_engine_cache_path=args.trt_engine_cache,
                            model_version=(
                                verified_priority_model.model_version
                                if verified_priority_model is not None
                                else None
                            ),
                        )
                    ),
                    priority_source_thresholds,
                    default_threshold=args.priority_confidence_threshold,
                ),
                priority_label_map,
                fusion_iou_threshold=args.priority_model_iou_threshold,
            )
            priority_detector = TemporalDetectionFilter(
                priority_detector,
                labels=frozenset({"bicycle", "car", "motorcycle", "bus", "truck"}),
                minimum_consecutive_frames=args.priority_vehicle_stability_frames,
                iou_threshold=0.25,
                maximum_missed_frames=1,
                label_aliases=VEHICLE_TEMPORAL_LABEL_ALIASES,
            )
            priority_detector = FrameCadencedDetector(
                priority_detector,
                frame_stride=args.priority_model_frame_stride,
                frame_phase=args.priority_model_frame_phase,
            )
            detectors.append(priority_detector)

        if args.environment_onnx_model is not None:
            if environment_class_names is None:
                raise RuntimeError("environment model class names were not initialized")
            detectors.append(
                OnnxNx6Detector(
                    OnnxNx6Config(
                        model_path=args.environment_onnx_model,
                        class_names=environment_class_names,
                        input_width=args.input_width,
                        input_height=args.input_height,
                        confidence_threshold=args.environment_confidence_threshold,
                        providers=providers,
                        trt_engine_cache_path=args.trt_engine_cache,
                        output_coordinates=(
                            args.environment_output_coordinates or args.output_coordinates
                        ),
                        model_version=(
                            verified_environment_model.model_version
                            if verified_environment_model is not None
                            else None
                        ),
                    )
                )
            )

        for model_detector in detectors:
            model_detector.warmup(iterations=args.warmup_iterations)

        detector = _wrap_detector_graph(
            args,
            mission_config,
            detectors=detectors,
            safety_class_names=safety_class_names,
            priority_class_names=priority_class_names,
            priority_label_map=priority_label_map,
            environment_class_names=environment_class_names,
        )
        person_safety_model_coverage = detector.covers_labels(mission_config.person_labels)
        person_safety_evidence_qualified = (
            person_safety_model_coverage and verified_safety_model is not None
        )
        environment_model_coverage = detector.covers_labels(ENVIRONMENT_RISK_CLASS_NAMES)
        environment_evidence_qualified = (
            environment_model_coverage and verified_environment_model is not None
        )

        if args.semantic_context_onnx_model is not None:
            semantic_context_session = (
                TensorRtSemanticSession(args.semantic_context_engine)
                if args.semantic_context_engine is not None
                else None
            )
            try:
                semantic_context_model = OnnxCategoricalSemanticContext(
                    OnnxSemanticContextConfig(
                        args.semantic_context_onnx_model,
                        providers=providers,
                    ),
                    session=semantic_context_session,
                )
                semantic_context_model.warmup()
            except BaseException:
                if semantic_context_session is not None:
                    semantic_context_session.close()
                raise
            semantic_context_runner = AsyncSemanticContextRunner(
                semantic_context_model,
                minimum_interval_s=args.semantic_context_minimum_interval_seconds,
            )

        bindings = tuple(verified_bindings)
        components = tuple(configured_components)
        return ProductionPerceptionBundle(
            detector=detector,
            model_detectors=tuple(detectors),
            primary_detector=primary_detector,
            rgb_fire_verifier=rgb_fire_verifier,
            rgb_fire_corroborator=rgb_fire_corroborator,
            semantic_context_model=semantic_context_model,
            semantic_context_runner=semantic_context_runner,
            verified_fire_model=verified_fire_model,
            verified_rgb_fire_verifier=verified_rgb_fire_verifier,
            verified_safety_model=verified_safety_model,
            verified_priority_model=verified_priority_model,
            verified_environment_model=verified_environment_model,
            verified_semantic_context_model=verified_semantic_context_model,
            verified_artifacts=bindings,
            configured_model_components=components,
            model_set_sha256=_model_set_sha256(bindings, components),
            rgb_fire_verifier_evidence_qualified=rgb_fire_verifier_evidence_qualified,
            person_safety_model_coverage=person_safety_model_coverage,
            person_safety_evidence_qualified=person_safety_evidence_qualified,
            environment_model_coverage=environment_model_coverage,
            environment_evidence_qualified=environment_evidence_qualified,
        )
    except BaseException:
        if semantic_context_runner is not None:
            semantic_context_runner.close()
        elif semantic_context_model is not None:
            semantic_context_model.close()
        seen: set[int] = set()
        for root in (*detectors, rgb_fire_verifier):
            if root is not None:
                _close_detector_resources(root, seen)
        raise


def _wrap_detector_graph(
    args: Any,
    mission_config: MissionConfig,
    *,
    detectors: list[Any],
    safety_class_names: tuple[str, ...] | None,
    priority_class_names: tuple[str, ...] | None,
    priority_label_map: dict[str, str],
    environment_class_names: tuple[str, ...] | None,
) -> Any:
    class_thresholds = {
        "fire": args.flame_confidence_threshold,
        "flame": args.flame_confidence_threshold,
        "smoke": args.smoke_confidence_threshold,
    }
    if safety_class_names is not None:
        priority_detection_labels = frozenset(
            label.strip().lower() for label in _parse_class_names(args.safety_tile_labels)
        )
        for label in safety_class_names:
            normalized_label = label.strip().lower()
            threshold = (
                max(
                    args.safety_priority_confidence_threshold,
                    args.priority_vehicle_confidence_threshold,
                )
                if normalized_label in VEHICLE_DETECTION_CLASS_NAMES
                else args.safety_priority_confidence_threshold
                if normalized_label in priority_detection_labels
                else args.safety_fallback_confidence_threshold
            )
            class_thresholds.setdefault(normalized_label, threshold)
    if environment_class_names is not None:
        class_thresholds.update(
            {
                label.strip().lower(): args.environment_confidence_threshold
                for label in environment_class_names
            }
        )
    if priority_class_names is not None:
        for label in priority_class_names:
            source = label.strip().lower()
            destination = priority_label_map.get(source, source)
            class_thresholds.setdefault(destination, args.priority_confidence_threshold)
    detector_ensemble: Any = DetectorEnsemble(
        detectors,
        force_locked_cadence=args.lock_model_force_every_frame,
    )
    if args.safety_onnx_model is not None and args.priority_onnx_model is not None:
        detector_ensemble = MultiSourceConfidenceFilter(
            detector_ensemble,
            labels=frozenset({"car"}),
            iou_threshold=args.priority_model_iou_threshold,
            single_source_confidence=args.car_single_source_confidence_threshold,
        )
    fused_ensemble = SameLabelDetectionFusion(
        detector_ensemble,
        iou_threshold=args.priority_model_iou_threshold,
        maximum_detections=max(
            args.safety_model_maximum_detections,
            args.priority_model_maximum_detections,
        ),
    )
    detector: Any = ClassConfidenceFilter(
        fused_ensemble,
        class_thresholds,
        default_threshold=None,
    )
    detector = VehicleFurnitureOverlapVetoFilter(detector)
    automatic_candidate_labels = (
        frozenset(
            label.strip().lower()
            for label in (
                *FIRE_CANDIDATE_TRACK_LABELS,
                *PRIORITY_DETECTION_CLASS_NAMES,
                *ENVIRONMENT_RISK_CLASS_NAMES,
                *mission_config.target_classes,
                *mission_config.person_labels,
            )
            if label.strip()
        )
        - NON_SELECTABLE_AUTOMATIC_LABELS
    )
    detector = LabelAllowListFilter(detector, labels=automatic_candidate_labels)
    detector = BrightNeutralLightVetoFilter(
        detector,
        minimum_bright_warm_fraction=args.fire_minimum_bright_warm_fraction,
    )
    if args.safety_onnx_model is not None:
        detector = PersonOverlapVetoFilter(
            detector,
            minimum_fire_coverage=args.person_veto_fire_coverage,
        )
    return TemporalDetectionFilter(
        detector,
        labels=FIRE_CANDIDATE_TRACK_LABELS,
        minimum_consecutive_frames=args.candidate_stability_frames,
        label_aliases={"fire": "flame"},
        maximum_center_distance=0.10,
        minimum_area_ratio=0.12,
        maximum_missed_frames=max(1, args.primary_model_frame_stride),
    )


def _validate_perception_arguments(args: Any) -> None:
    if not 0 <= args.warmup_iterations <= 100:
        raise ValueError("--warmup-iterations must be between 0 and 100")
    if args.onnx_model.suffix.lower() in {".engine", ".plan"} and args.model_manifest is None:
        raise ValueError("TensorRT fire model requires a hash-bound --model-manifest")
    if args.rgb_fire_verifier_model is not None:
        if args.rgb_fire_verifier_model_manifest is None:
            raise ValueError(
                "--rgb-fire-verifier-model requires a hash-bound --rgb-fire-verifier-model-manifest"
            )
        if args.model_manifest is None:
            raise ValueError(
                "independent RGB fire corroboration requires a hash-bound primary --model-manifest"
            )
    if args.rgb_fire_verifier_model_manifest is not None and args.rgb_fire_verifier_model is None:
        raise ValueError("--rgb-fire-verifier-model-manifest requires --rgb-fire-verifier-model")
    if not math.isfinite(args.rgb_fire_verifier_confidence_threshold) or not (
        0.0 <= args.rgb_fire_verifier_confidence_threshold <= 1.0
    ):
        raise ValueError("--rgb-fire-verifier-confidence-threshold must be in [0, 1]")
    if not math.isfinite(args.rgb_fire_verifier_minimum_iou) or not (
        0.0 < args.rgb_fire_verifier_minimum_iou <= 1.0
    ):
        raise ValueError("--rgb-fire-verifier-minimum-iou must be in (0, 1]")
    if (
        args.safety_onnx_model is not None
        and args.safety_onnx_model.suffix.lower() in {".engine", ".plan"}
        and args.safety_model_manifest is None
    ):
        raise ValueError("TensorRT safety model requires a hash-bound --safety-model-manifest")
    if (
        args.priority_onnx_model is not None
        and args.priority_onnx_model.suffix.lower() in {".engine", ".plan"}
        and args.priority_model_manifest is None
    ):
        raise ValueError("TensorRT priority model requires a hash-bound --priority-model-manifest")
    if (
        args.environment_onnx_model is not None
        and args.environment_onnx_model.suffix.lower() in {".engine", ".plan"}
        and args.environment_model_manifest is None
    ):
        raise ValueError(
            "TensorRT environment model requires a hash-bound --environment-model-manifest"
        )
    if args.safety_model_manifest is not None and args.safety_onnx_model is None:
        raise ValueError("--safety-model-manifest requires --safety-onnx-model")
    if args.priority_model_manifest is not None and args.priority_onnx_model is None:
        raise ValueError("--priority-model-manifest requires --priority-onnx-model")
    if args.environment_model_manifest is not None and args.environment_onnx_model is None:
        raise ValueError("--environment-model-manifest requires --environment-onnx-model")
    if (
        args.semantic_context_onnx_model is None
        and args.semantic_context_model_manifest is not None
    ):
        raise ValueError("--semantic-context-model-manifest requires --semantic-context-onnx-model")
    if (
        args.semantic_context_onnx_model is not None
        and args.semantic_context_model_manifest is None
    ):
        raise ValueError(
            "--semantic-context-onnx-model requires a hash-bound --semantic-context-model-manifest"
        )
    if (
        args.semantic_context_onnx_model is not None
        and args.semantic_context_onnx_model.suffix.lower() != ".onnx"
    ):
        raise ValueError("semantic context currently requires an ONNX artifact")
    if (args.semantic_context_engine is None) != (args.semantic_context_engine_provenance is None):
        raise ValueError(
            "--semantic-context-engine and --semantic-context-engine-provenance "
            "must be supplied together"
        )
    if args.semantic_context_engine is not None and args.semantic_context_onnx_model is None:
        raise ValueError("--semantic-context-engine requires --semantic-context-onnx-model")
    for name, value in (
        (
            "--semantic-context-minimum-interval-seconds",
            args.semantic_context_minimum_interval_seconds,
        ),
        (
            "--semantic-context-maximum-age-seconds",
            args.semantic_context_maximum_age_seconds,
        ),
    ):
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"{name} must be finite and positive")
    if args.safety_model_coco80 and args.safety_onnx_model is None:
        raise ValueError("--safety-model-coco80 requires --safety-onnx-model")


def _verify_model_binding(
    *,
    component: str,
    manifest_path: Path | None,
    model_path: Path,
    class_names: tuple[str, ...],
    output_coordinates: str | None,
    require_production_approved: bool,
    expected_model_role: str,
    expected_output_format: str = "N_x_6",
    expected_native_output_format: str | None = None,
) -> tuple[VerifiedModelArtifact | None, VerifiedPerceptionArtifact | None]:
    if manifest_path is None:
        if require_production_approved:
            raise ValueError("a model manifest is required by the production-approval gate")
        return None, None
    verified = verify_model_manifest(
        manifest_path,
        model_path,
        expected_class_names=class_names,
        expected_output_coordinates=output_coordinates,
        expected_model_role=expected_model_role,
        expected_output_format=expected_output_format,
        expected_native_output_format=expected_native_output_format,
        require_production_approved=require_production_approved,
    )
    normalized_component = component.strip().lower()
    return verified, VerifiedPerceptionArtifact(
        component=normalized_component,
        model_path=Path(model_path),
        manifest_path=Path(manifest_path),
        manifest_sha256=sha256_file(manifest_path),
        artifact=verified,
    )


def _model_set_sha256(
    bindings: tuple[VerifiedPerceptionArtifact, ...],
    configured_components: tuple[str, ...],
) -> str | None:
    if len(bindings) != len(configured_components):
        return None
    payload = {
        "schema_version": 1,
        "components": [
            {
                "component": item.component,
                "manifest_sha256": item.manifest_sha256,
                "artifact_sha256": item.artifact.artifact_sha256,
                "model_id": item.artifact.model_id,
                "model_version": item.artifact.model_version,
                "model_role": item.artifact.model_role,
                "status": item.artifact.status,
                "production_approved": item.artifact.production_approved,
                "synthetic_hil_only": item.artifact.synthetic_hil_only,
                "class_names": list(item.artifact.class_names),
                "output_coordinates": item.artifact.output_coordinates,
                "output_format": item.artifact.output_format,
            }
            for item in bindings
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _close_detector_resources(component: Any, seen: set[int]) -> None:
    identity = id(component)
    if identity in seen:
        return
    seen.add(identity)
    child = getattr(component, "detector", None)
    if child is not None:
        _close_detector_resources(child, seen)
    for nested in getattr(component, "detectors", ()):
        _close_detector_resources(nested, seen)
    session = getattr(component, "_session", None)
    close = getattr(session, "close", None)
    if callable(close):
        close()


def _parse_class_names(raw: str) -> tuple[str, ...]:
    labels = tuple(label.strip() for label in raw.split(",") if label.strip())
    if not labels:
        raise ValueError("class names must contain at least one comma-separated label")
    return labels


def _parse_label_map(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        if item.count("=") != 1:
            raise ValueError("label map entries must use source=destination")
        source, destination = (value.strip().lower() for value in item.split("=", 1))
        if not source or not destination:
            raise ValueError("label map entries cannot be empty")
        mapping[source] = destination
    return mapping


def _parse_label_confidence_thresholds(raw: str) -> dict[str, float]:
    thresholds: dict[str, float] = {}
    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        if item.count("=") != 1:
            raise ValueError("tile confidence entries must use class=confidence")
        raw_label, raw_value = (value.strip() for value in item.split("=", 1))
        label = raw_label.lower()
        if not label:
            raise ValueError("tile confidence labels cannot be empty")
        try:
            threshold = float(raw_value)
        except ValueError as exc:
            raise ValueError("tile confidence values must be numeric") from exc
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            raise ValueError("tile confidence values must be in [0, 1]")
        thresholds[label] = threshold
    return thresholds


__all__ = [
    "AIRCRAFT_DETECTION_CLASS_NAMES",
    "COCO80_CLASS_NAMES",
    "ENVIRONMENT_RISK_CLASS_NAMES",
    "NON_SELECTABLE_AUTOMATIC_LABELS",
    "PRIORITY_DETECTION_CLASS_NAMES",
    "ProductionPerceptionBundle",
    "VEHICLE_DETECTION_CLASS_NAMES",
    "VEHICLE_TEMPORAL_LABEL_ALIASES",
    "VISDRONE_PRIORITY_CLASS_NAMES",
    "VISDRONE_PRIORITY_LABEL_MAP",
    "VerifiedPerceptionArtifact",
    "build_production_perception",
]
