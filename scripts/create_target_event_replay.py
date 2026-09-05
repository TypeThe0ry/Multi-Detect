"""Create a hash-bound target-event replay bundle from a capture manifest."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.autonomy.serialization import (
    AutonomyContractError,
    optional_real,
    optional_text,
    require_array,
    require_int,
    require_real,
    require_text,
    strict_object,
)
from multidetect.target_event_replay import (
    TargetEventInput,
    TargetEventReplayBuilder,
    TargetEventReplayBundle,
    TargetGpsEvidence,
    TargetMediaInput,
    TargetRangeEvidence,
)


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AutonomyContractError(f"capture manifest is not valid UTF-8 JSON: {exc}") from exc
    return strict_object(
        raw,
        name="TargetEventCaptureManifest",
        required=frozenset({"session_id", "events"}),
        optional=frozenset({"bundle_id", "started_at_utc", "camera_calibration"}),
    )


def _media_from_mapping(
    raw: object,
    *,
    kind: str,
    base_dir: Path,
) -> TargetMediaInput | None:
    if raw is None:
        return None
    value = strict_object(
        raw,
        name=f"{kind} capture",
        required=frozenset({"path", "encoding"}),
        optional=frozenset({"width", "height"}),
    )
    source_path = Path(require_text(value["path"], f"{kind}.path"))
    if not source_path.is_absolute():
        source_path = base_dir / source_path
    source = source_path.resolve(strict=True)
    if not source.is_file():
        raise AutonomyContractError(f"{kind}.path must be a regular file")
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise AutonomyContractError(f"unable to read {kind}.path: {exc}") from exc
    return TargetMediaInput(
        kind=kind,
        payload=payload,
        encoding=require_text(value["encoding"], f"{kind}.encoding"),
        width=(
            require_int(value["width"], f"{kind}.width", minimum=1, maximum=32_768)
            if value.get("width") is not None
            else None
        ),
        height=(
            require_int(value["height"], f"{kind}.height", minimum=1, maximum=32_768)
            if value.get("height") is not None
            else None
        ),
    )


def _event_from_mapping(raw: object, *, base_dir: Path) -> TargetEventInput:
    value = strict_object(
        raw,
        name="TargetEventCapture",
        required=frozenset(
            {
                "event_id",
                "frame_id",
                "target_id",
                "target_label",
                "captured_at_s",
                "gps",
                "ranging",
            }
        ),
        optional=frozenset(
            {
                "video",
                "depth",
                "source_frame_id",
                "vehicle_altitude_agl_m",
                "vehicle_heading_deg",
            }
        ),
    )
    return TargetEventInput(
        event_id=require_text(value["event_id"], "event_id"),
        frame_id=require_text(value["frame_id"], "frame_id"),
        target_id=require_text(value["target_id"], "target_id"),
        target_label=require_text(value["target_label"], "target_label"),
        captured_at_s=require_real(value["captured_at_s"], "captured_at_s", minimum=0.0),
        gps=TargetGpsEvidence.from_mapping(value["gps"]),
        ranging=TargetRangeEvidence.from_mapping(value["ranging"]),
        video=_media_from_mapping(value.get("video"), kind="video_frame", base_dir=base_dir),
        depth=_media_from_mapping(value.get("depth"), kind="depth_map", base_dir=base_dir),
        source_frame_id=optional_text(value.get("source_frame_id"), "source_frame_id"),
        vehicle_altitude_agl_m=optional_real(
            value.get("vehicle_altitude_agl_m"), "vehicle_altitude_agl_m", minimum=0.0
        ),
        vehicle_heading_deg=optional_real(
            value.get("vehicle_heading_deg"), "vehicle_heading_deg", minimum=0.0, maximum=360.0
        ),
    )


def _resolve_input_file(value: object, *, name: str, base_dir: Path) -> Path:
    requested = value if isinstance(value, Path) else Path(require_text(value, name))
    if not requested.is_absolute():
        requested = base_dir / requested
    resolved = requested.resolve(strict=True)
    if not resolved.is_file():
        raise AutonomyContractError(f"{name} must be a regular file")
    return resolved


def create_bundle(
    capture_manifest: Path,
    output_root: Path,
    *,
    camera_calibration: Path | None = None,
) -> TargetEventReplayBundle:
    document = _read_json(capture_manifest)
    base_dir = capture_manifest.resolve(strict=True).parent
    events_raw = require_array(document["events"], "events")
    if not isinstance(events_raw, Sequence) or isinstance(events_raw, (str, bytes, bytearray)):
        raise AutonomyContractError("events must be an array")
    kwargs: dict[str, object] = {
        "root": output_root,
        "session_id": require_text(document["session_id"], "session_id"),
    }
    calibration_input: object | None = camera_calibration
    if calibration_input is None:
        calibration_input = document.get("camera_calibration")
    if calibration_input is not None:
        kwargs["camera_calibration_path"] = _resolve_input_file(
            calibration_input,
            name="camera_calibration",
            base_dir=base_dir,
        )
    if document.get("bundle_id") is not None:
        kwargs["bundle_id"] = require_text(document["bundle_id"], "bundle_id")
    if document.get("started_at_utc") is not None:
        kwargs["started_at_utc"] = require_text(document["started_at_utc"], "started_at_utc")
    builder = TargetEventReplayBuilder(**kwargs)  # type: ignore[arg-type]
    for raw_event in events_raw:
        builder.append(_event_from_mapping(raw_event, base_dir=base_dir))
    return builder.finalize()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Copy capture media into a verified Multi-Detect target-event replay bundle. "
            "The input manifest contains paths to local video/depth files."
        )
    )
    parser.add_argument("capture_manifest", type=Path, help="JSON capture manifest")
    parser.add_argument("output_root", type=Path, help="new empty output bundle directory")
    parser.add_argument(
        "--camera-calibration",
        type=Path,
        help=(
            "strict camera calibration JSON to copy and hash-bind into the bundle; "
            "overrides camera_calibration in the capture manifest"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bundle = create_bundle(
            args.capture_manifest,
            args.output_root,
            camera_calibration=args.camera_calibration,
        )
        print(json.dumps(bundle.summary(), ensure_ascii=False, allow_nan=False, indent=2))
        return 0
    except (AutonomyContractError, OSError, ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
