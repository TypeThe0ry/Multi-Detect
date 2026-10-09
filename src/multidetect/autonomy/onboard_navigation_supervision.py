"""Source-only external planned-navigation supervision, never flight authority.

Explicit pointers reopen original JSON/JSONL/JSON sequences; steps are not inferred from
filenames. Metric labels need independently supplied unit/alignment evidence.
Their hashes bind integrity, not expert review, physical calibration or execution.
Only verbatim source Prompt and current/most-recent-past RGB reach the actor.
"""

from __future__ import annotations

import hashlib
import json
import warnings
from dataclasses import dataclass, fields
from io import BytesIO, TextIOWrapper
from math import pi
from pathlib import Path, PureWindowsPath

from .serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)

SUPERVISION_CONTRACT = "multidetect.onboard-navigation-supervision-v1"
TARGET_CONTRACT = "multidetect.onboard-planned-navigation-target-v1"
LABEL_PROVENANCE = "external_planned_navigation_supervision"


class NavigationSupervisionError(AutonomyContractError):
    """Malformed, unsafe or unbound external supervision is rejected."""


def _text(value, name):
    if require_text(value, name) != value or len(value) > 1024:
        raise NavigationSupervisionError(f"{name} must be bounded exact nonblank text")
    return value


def _sha(value, name):
    if require_sha256(value, name) != value:
        raise NavigationSupervisionError(f"{name} must be canonical lowercase SHA-256")


def _object(value, cls, *, optional=()):
    names = frozenset(item.name for item in fields(cls))
    return strict_object(
        value, name=cls.__name__, required=names - frozenset(optional), optional=frozenset(optional)
    )


def _triple(value, name):
    if type(value) is not tuple or len(value) != 3:
        raise NavigationSupervisionError(f"{name} must be an immutable 3-tuple")
    for item in value:
        require_real(item, name)


@dataclass(frozen=True, slots=True)
class NavigationSupervisionBudget:
    max_image_bytes: int = 8 * 1024 * 1024
    max_total_image_bytes: int = 16 * 1024 * 1024
    max_image_pixels: int = 4 * 1024 * 1024
    max_total_pixels: int = 8 * 1024 * 1024
    max_image_side: int = 4096
    max_source_file_bytes: int = 450 * 1024 * 1024
    max_prompt_bytes: int = 16 * 1024
    max_source_record_bytes: int = 2 * 1024 * 1024
    max_cached_pointer_bytes: int = 16 * 1024 * 1024
    max_source_files: int = 4096

    def __post_init__(self):
        for item in fields(self):
            require_int(getattr(self, item.name), item.name, minimum=1)


_DEFAULT_BUDGET = NavigationSupervisionBudget()


@dataclass(frozen=True, slots=True)
class NavigationSourceFile:
    path: str
    bytes: int
    sha256: str

    def __post_init__(self):
        _text(self.path, "source path")
        if (
            ".." in Path(self.path).parts
            or ".." in PureWindowsPath(self.path).parts
            or "\x00" in self.path
        ):
            raise NavigationSupervisionError("source path cannot traverse or contain NUL")
        require_int(self.bytes, "source bytes", minimum=1)
        _sha(self.sha256, "source sha256")

    @classmethod
    def from_mapping(cls, value):
        return cls(**_object(value, cls))

    def to_mapping(self):
        return {"path": self.path, "bytes": self.bytes, "sha256": self.sha256}

    def resolve(self, artifact_root: Path, *, maximum_bytes: int) -> Path:
        require_int(maximum_bytes, "maximum_bytes", minimum=1)
        if self.bytes > maximum_bytes:
            raise NavigationSupervisionError("declared source exceeds byte budget")
        root = Path(artifact_root).resolve(strict=True)
        path = Path(self.path)
        if PureWindowsPath(self.path).drive and not path.is_absolute():
            raise NavigationSupervisionError("source drive-relative path is not allowed")
        resolved = (path if path.is_absolute() else root / path).resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise NavigationSupervisionError("source must be a file inside artifact_root")
        if resolved.stat().st_size != self.bytes:
            raise NavigationSupervisionError("actual source byte length differs")
        return resolved

    def verify(self, artifact_root: Path, *, maximum_bytes: int) -> Path:
        """Hash the whole bounded source without retaining its bytes in memory."""
        resolved = self.resolve(artifact_root, maximum_bytes=maximum_bytes)
        digest, size = hashlib.sha256(), 0
        with resolved.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > maximum_bytes:
                    raise NavigationSupervisionError("actual source exceeds byte budget")
                digest.update(chunk)
        if size != self.bytes or digest.hexdigest() != self.sha256:
            raise NavigationSupervisionError("actual source bytes/SHA-256 differ")
        return resolved

    def read(self, artifact_root: Path, *, maximum_bytes: int) -> bytes:
        resolved = self.resolve(artifact_root, maximum_bytes=maximum_bytes)
        with resolved.open("rb") as stream:
            payload = stream.read(maximum_bytes + 1)
        if len(payload) != self.bytes or hashlib.sha256(payload).hexdigest() != self.sha256:
            raise NavigationSupervisionError("actual source bytes/SHA-256 differ")
        return payload


@dataclass(frozen=True, slots=True)
class NavigationRecordPointer:
    format: str
    record_index: int | None
    json_pointer: str

    def __post_init__(self):
        if self.format not in {"json", "jsonl", "json_sequence"}:
            raise NavigationSupervisionError(
                "source record format must be json, jsonl or json_sequence"
            )
        if self.format in {"jsonl", "json_sequence"}:
            require_int(self.record_index, "sequence record_index", minimum=0)
        elif self.record_index is not None:
            raise NavigationSupervisionError("JSON document pointer cannot have record_index")
        if (
            not isinstance(self.json_pointer, str)
            or (self.json_pointer and not self.json_pointer.startswith("/"))
            or len(self.json_pointer) > 1024
        ):
            raise NavigationSupervisionError("source field requires an explicit JSON pointer")
        _pointer_keys(self.json_pointer)

    @classmethod
    def from_mapping(cls, value):
        return cls(**_object(value, cls))

    def to_mapping(self):
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def extract(self, payload: bytes, *, budget=_DEFAULT_BUDGET):
        if not isinstance(budget, NavigationSupervisionBudget):
            raise TypeError("budget must be NavigationSupervisionBudget")
        if not isinstance(payload, bytes):
            raise TypeError("source payload must be bytes")
        if len(payload) > budget.max_source_file_bytes:
            raise NavigationSupervisionError("source payload exceeds byte budget")
        return _extract_pointers(BytesIO(payload), (self,), budget)[self]


def _pointer_keys(pointer):
    if not pointer:
        return ()
    result = []
    for escaped in pointer[1:].split("/"):
        if any(
            escaped[i] == "~" and (i + 1 == len(escaped) or escaped[i + 1] not in "01")
            for i in range(len(escaped))
        ):
            raise NavigationSupervisionError("invalid JSON pointer escape")
        result.append(escaped.replace("~1", "/").replace("~0", "~"))
    return tuple(result)


def _select_pointer(value, keys):
    for key in keys:
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif (
            isinstance(value, list)
            and key.isascii()
            and key.isdecimal()
            and (key == "0" or not key.startswith("0"))
            and int(key) < len(value)
        ):
            value = value[int(key)]
        else:
            raise NavigationSupervisionError("explicit source JSON pointer does not exist")
    return value


class _JsonStream:
    """At most one source object plus a fixed read chunk is retained."""

    def __init__(self, stream, maximum_record_bytes):
        self.stream = TextIOWrapper(stream, encoding="utf-8-sig", newline="")
        self.buffer = ""
        self.eof = False
        self.maximum = maximum_record_bytes

        def reject_duplicates(pairs):
            value = {}
            for key, nested in pairs:
                if key in value:
                    raise NavigationSupervisionError(f"duplicate source JSON key {key!r}")
                value[key] = nested
            return value

        def reject_constant(value):
            raise NavigationSupervisionError(f"non-finite source JSON number {value}")

        self.decoder = json.JSONDecoder(
            object_pairs_hook=reject_duplicates, parse_constant=reject_constant
        )

    def fill(self):
        if self.eof:
            return
        try:
            chunk = self.stream.read(64 * 1024)
        except UnicodeDecodeError as exc:
            raise NavigationSupervisionError("original source is not valid UTF-8") from exc
        self.buffer += chunk
        self.eof = not chunk

    def head(self):
        while True:
            self.buffer = self.buffer.lstrip(" \t\r\n")
            if self.buffer or self.eof:
                return self.buffer[:1]
            self.fill()

    def object_record(self):
        if self.head() != "{":
            raise NavigationSupervisionError("streamed source records must be JSON objects")
        while True:
            try:
                value, consumed = self.decoder.raw_decode(self.buffer)
            except json.JSONDecodeError as exc:
                if self.eof:
                    raise NavigationSupervisionError("incomplete/malformed source record") from exc
                if len(self.buffer) > self.maximum:
                    raise NavigationSupervisionError(
                        "source record exceeds bounded parser budget"
                    ) from exc
                self.fill()
                continue
            if len(self.buffer[:consumed].encode("utf-8")) > self.maximum:
                raise NavigationSupervisionError("source record exceeds byte budget")
            self.buffer = self.buffer[consumed:]
            canonical_json(value)  # Also reject finite-looking exponent overflow.
            return value

    def detach(self):
        self.stream.detach()  # Caller owns the original byte stream.


def _extract_pointers(stream, pointers, budget):
    """Parse the complete source, retaining only explicit requested fields."""
    formats = {pointer.format for pointer in pointers}
    if len(formats) != 1:
        raise NavigationSupervisionError("one original source cannot mix record formats")
    record_format = next(iter(formats))
    result, cached_bytes = {}, 0

    def retain(pointer, value, keys):
        nonlocal cached_bytes
        extracted = _select_pointer(value, keys)
        encoded = canonical_json(extracted).encode("utf-8")
        cached_bytes += len(encoded)
        if cached_bytes > budget.max_cached_pointer_bytes:
            raise NavigationSupervisionError("requested source fields exceed cache byte budget")
        result[pointer] = extracted

    if record_format == "jsonl":
        requested = {}
        for pointer in pointers:
            requested.setdefault(pointer.record_index, []).append(pointer)
        index = 0
        while line := stream.readline(budget.max_source_record_bytes + 1):
            if len(line) > budget.max_source_record_bytes:
                raise NavigationSupervisionError("source JSONL record exceeds byte budget")
            value = strict_json_loads(line, name="original source JSONL record")
            canonical_json(value)
            for pointer in requested.get(index, ()):
                retain(pointer, value, _pointer_keys(pointer.json_pointer))
            index += 1
    else:
        reader = _JsonStream(stream, budget.max_source_record_bytes)
        try:
            head = reader.head()
            if record_format == "json" and head != "[":
                # Small ordinary JSON documents retain their existing pointer semantics.
                while not reader.eof:
                    if len(reader.buffer) > budget.max_source_record_bytes:
                        raise NavigationSupervisionError("source document exceeds record budget")
                    reader.fill()
                if len(reader.buffer.encode("utf-8")) > budget.max_source_record_bytes:
                    raise NavigationSupervisionError("source document exceeds record byte budget")
                value = strict_json_loads(reader.buffer, name="original source JSON")
                canonical_json(value)
                for pointer in pointers:
                    retain(pointer, value, _pointer_keys(pointer.json_pointer))
            else:
                requested = {}
                for pointer in pointers:
                    keys = _pointer_keys(pointer.json_pointer)
                    index = pointer.record_index
                    if record_format == "json":
                        if (
                            not keys
                            or not keys[0].isascii()
                            or not keys[0].isdecimal()
                            or (keys[0] != "0" and keys[0].startswith("0"))
                        ):
                            raise NavigationSupervisionError(
                                "streamed JSON array requires explicit canonical record index"
                            )
                        index, keys = int(keys[0]), keys[1:]
                    requested.setdefault(index, []).append((pointer, keys))
                array = record_format == "json"
                if array:
                    reader.buffer = reader.buffer[1:]
                index = 0
                while True:
                    head = reader.head()
                    if array and head == "]":
                        reader.buffer = reader.buffer[1:]
                        if reader.head():
                            raise NavigationSupervisionError("trailing source JSON bytes")
                        break
                    if not head:
                        if array:
                            raise NavigationSupervisionError("unterminated source JSON array")
                        break
                    value = reader.object_record()
                    for pointer, keys in requested.get(index, ()):
                        retain(pointer, value, keys)
                    index += 1
                    if array:
                        head = reader.head()
                        if head == ",":
                            reader.buffer = reader.buffer[1:]
                            if reader.head() == "]":
                                raise NavigationSupervisionError("trailing source array comma")
                        elif head != "]":
                            raise NavigationSupervisionError("source array requires comma/end")
                    else:
                        if not reader.buffer and not reader.eof:
                            reader.fill()
                        if reader.buffer and reader.buffer[0] not in " \t\r\n":
                            raise NavigationSupervisionError(
                                "JSON sequence records require whitespace separators"
                            )
        finally:
            reader.detach()
    if len(result) != len(set(pointers)):
        raise NavigationSupervisionError("explicit source record index does not exist")
    return result


@dataclass(frozen=True, slots=True)
class NavigationSupervisionSource:
    repository_id: str
    revision: str
    original_split: str
    annotation_record_id: str
    annotation: NavigationSourceFile
    pose: NavigationSourceFile
    conversion: NavigationSourceFile
    license: NavigationSourceFile
    review_receipt: NavigationSourceFile | None
    unit_verification: NavigationSourceFile | None
    alignment_verification: NavigationSourceFile | None

    def __post_init__(self):
        for name in ("repository_id", "revision", "original_split", "annotation_record_id"):
            _text(getattr(self, name), name)
        for name in ("annotation", "pose", "conversion", "license"):
            if not isinstance(getattr(self, name), NavigationSourceFile):
                raise TypeError(f"{name} must be NavigationSourceFile")
        for name in ("review_receipt", "unit_verification", "alignment_verification"):
            if getattr(self, name) is not None and not isinstance(
                getattr(self, name), NavigationSourceFile
            ):
                raise TypeError(f"{name} must be NavigationSourceFile or unknown")

    @classmethod
    def from_mapping(cls, value):
        value = dict(_object(value, cls))
        for name in (
            "annotation",
            "pose",
            "conversion",
            "license",
            "review_receipt",
            "unit_verification",
            "alignment_verification",
        ):
            if value[name] is not None:
                value[name] = NavigationSourceFile.from_mapping(value[name])
        return cls(**value)

    def to_mapping(self):
        return {
            item.name: value.to_mapping()
            if isinstance(value := getattr(self, item.name), NavigationSourceFile)
            else value
            for item in fields(self)
        }


@dataclass(frozen=True, slots=True)
class NavigationPrompt:
    text_original: str
    utf8_sha256: str
    source_record_sha256: str
    source_pointer: NavigationRecordPointer | None = None

    def __post_init__(self):
        if not isinstance(self.text_original, str) or not self.text_original.strip():
            raise NavigationSupervisionError("original Prompt must be exact nonblank text")
        _sha(self.utf8_sha256, "prompt utf8_sha256")
        _sha(self.source_record_sha256, "prompt source_record_sha256")
        if hashlib.sha256(self.text_original.encode("utf-8")).hexdigest() != self.utf8_sha256:
            raise NavigationSupervisionError("original Prompt exact UTF-8 SHA differs")
        if self.source_pointer is not None and not isinstance(
            self.source_pointer, NavigationRecordPointer
        ):
            raise TypeError("source_pointer must be NavigationRecordPointer")

    @classmethod
    def from_mapping(cls, value):
        value = dict(_object(value, cls, optional=("source_pointer",)))
        if value.get("source_pointer") is not None:
            value["source_pointer"] = NavigationRecordPointer.from_mapping(value["source_pointer"])
        return cls(**value)

    def to_mapping(self):
        return {
            "text_original": self.text_original,
            "utf8_sha256": self.utf8_sha256,
            "source_record_sha256": self.source_record_sha256,
            "source_pointer": None
            if self.source_pointer is None
            else self.source_pointer.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class NavigationFrame:
    frame_id: str
    episode_id: str
    scene_id: str
    step_index: int
    path: str
    bytes: int
    sha256: str
    width: int
    height: int
    encoding: str
    source_clock_id: str | None
    source_stamp: int | float | None
    stamp_unit: str | None

    def __post_init__(self):
        for name in ("frame_id", "episode_id", "scene_id"):
            _text(getattr(self, name), name)
        require_int(self.step_index, "frame step_index", minimum=0)
        NavigationSourceFile(self.path, self.bytes, self.sha256)
        require_int(self.width, "frame width", minimum=1)
        require_int(self.height, "frame height", minimum=1)
        if self.encoding not in {"png", "jpeg"}:
            raise NavigationSupervisionError("navigation source RGB must be original PNG/JPEG")
        known = (
            self.source_clock_id is not None,
            self.source_stamp is not None,
            self.stamp_unit is not None,
        )
        if any(known) and not all(known):
            raise NavigationSupervisionError(
                "source clock/stamp/unit must be known together or all unknown"
            )
        if all(known):
            _text(self.source_clock_id, "source_clock_id")
            if self.stamp_unit in {"ns", "ms"}:
                require_int(self.source_stamp, "source_stamp", minimum=0)
            elif self.stamp_unit == "s":
                require_real(self.source_stamp, "source_stamp", minimum=0)
            else:
                raise NavigationSupervisionError("source stamp unit must be s/ms/ns or unknown")

    @classmethod
    def from_mapping(cls, value):
        return cls(**_object(value, cls))

    def to_mapping(self):
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def decode(self, payload: bytes, budget: NavigationSupervisionBudget):
        if (
            self.width > budget.max_image_side
            or self.height > budget.max_image_side
            or self.width * self.height > budget.max_image_pixels
        ):
            raise NavigationSupervisionError("declared RGB dimensions exceed pixel budget")
        try:
            from PIL import Image

            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(payload)) as image:
                    if (
                        image.format != {"png": "PNG", "jpeg": "JPEG"}[self.encoding]
                        or image.size != (self.width, self.height)
                        or image.mode != "RGB"
                        or getattr(image, "n_frames", 1) != 1
                    ):
                        raise NavigationSupervisionError(
                            "actual source PNG/JPEG RGB metadata differs"
                        )
                    image.verify()
                with Image.open(BytesIO(payload)) as image:
                    image.load()
                    return image.copy()
        except ImportError as exc:
            raise NavigationSupervisionError(
                "source image decoding requires optional Pillow"
            ) from exc
        except Exception as exc:
            raise NavigationSupervisionError(f"invalid source RGB: {exc}") from exc


@dataclass(frozen=True, slots=True)
class NavigationPose:
    position_xyz: tuple[float, float, float] | None
    yaw: float | None
    coordinate_frame: str | None
    length_unit: str | None
    yaw_unit: str | None
    origin: str
    source_record_sha256: str

    def __post_init__(self):
        if self.position_xyz is not None:
            _triple(self.position_xyz, "source position_xyz")
        if self.yaw is not None:
            require_real(self.yaw, "source yaw")
        if self.coordinate_frame is not None:
            _text(self.coordinate_frame, "source coordinate_frame")
        if self.length_unit not in {None, "m", "cm", "mm"} or self.yaw_unit not in {
            None,
            "rad",
            "deg",
        }:
            raise NavigationSupervisionError(
                "source pose units must be explicitly supported or unknown"
            )
        if self.origin not in {"planned_pose", "simulated_observation", "measured_observation"}:
            raise NavigationSupervisionError(
                "pose origin must not imply executed or expert evidence"
            )
        _sha(self.source_record_sha256, "pose source_record_sha256")

    @classmethod
    def from_mapping(cls, value):
        value = dict(_object(value, cls))
        if value["position_xyz"] is not None:
            if not isinstance(value["position_xyz"], list):
                raise NavigationSupervisionError("source position_xyz mapping requires an array")
            value["position_xyz"] = tuple(value["position_xyz"])
        return cls(**value)

    def to_mapping(self):
        return {
            item.name: list(value) if type(value := getattr(self, item.name)) is tuple else value
            for item in fields(self)
        }


@dataclass(frozen=True, slots=True)
class NavigationPlannedAction:
    raw_json: str
    raw_record_sha256: str
    kind: str
    translation_m: tuple[float, float, float] | None
    yaw_delta_rad: float | None
    unit_verification_sha256: str | None
    raw_source_pointer: NavigationRecordPointer | None = None

    def __post_init__(self):
        if (
            not isinstance(self.raw_json, str)
            or canonical_json(strict_json_loads(self.raw_json)) != self.raw_json
        ):
            raise NavigationSupervisionError(
                "raw action must retain canonical finite JSON without mutable aliases"
            )
        _sha(self.raw_record_sha256, "raw_record_sha256")
        if self.kind not in {"translate", "turn_yaw", "stop"}:
            raise NavigationSupervisionError(
                "only external translate/turn_yaw/stop labels are supported; LAND is not STOP"
            )
        if self.translation_m is not None:
            _triple(self.translation_m, "translation_m")
            if (
                self.kind != "translate"
                or not any(self.translation_m)
                or any(abs(item) > 500 for item in self.translation_m)
            ):
                raise NavigationSupervisionError(
                    "metric translation must be a nonzero body-FRD translation within bounds"
                )
        if self.yaw_delta_rad is not None:
            yaw = require_real(self.yaw_delta_rad, "yaw_delta_rad", minimum=-pi, maximum=pi)
            if self.kind != "turn_yaw" or yaw == 0:
                raise NavigationSupervisionError(
                    "metric yaw must be a nonzero clockwise-right-positive turn"
                )
        metric = self.translation_m is not None or self.yaw_delta_rad is not None
        if metric != (self.unit_verification_sha256 is not None):
            raise NavigationSupervisionError(
                "metric labels require an explicit unit-verification binding, not invented defaults"
            )
        if self.unit_verification_sha256 is not None:
            _sha(self.unit_verification_sha256, "unit_verification_sha256")
        if self.raw_source_pointer is not None and not isinstance(
            self.raw_source_pointer, NavigationRecordPointer
        ):
            raise TypeError("raw_source_pointer must be NavigationRecordPointer")

    @classmethod
    def from_mapping(cls, value):
        value = dict(
            strict_object(
                value,
                name=cls.__name__,
                required=frozenset(
                    {
                        "raw",
                        "raw_record_sha256",
                        "kind",
                        "translation_m",
                        "yaw_delta_rad",
                        "unit_verification_sha256",
                    }
                ),
                optional=frozenset({"raw_source_pointer"}),
            )
        )
        value["raw_json"] = canonical_json(value.pop("raw"))
        if value["translation_m"] is not None:
            if not isinstance(value["translation_m"], list):
                raise NavigationSupervisionError("translation_m mapping requires an array")
            value["translation_m"] = tuple(value["translation_m"])
        if value.get("raw_source_pointer") is not None:
            value["raw_source_pointer"] = NavigationRecordPointer.from_mapping(
                value["raw_source_pointer"]
            )
        return cls(**value)

    def to_mapping(self):
        return {
            "raw": strict_json_loads(self.raw_json),
            "raw_record_sha256": self.raw_record_sha256,
            "kind": self.kind,
            "translation_m": None if self.translation_m is None else list(self.translation_m),
            "yaw_delta_rad": self.yaw_delta_rad,
            "unit_verification_sha256": self.unit_verification_sha256,
            "raw_source_pointer": None
            if self.raw_source_pointer is None
            else self.raw_source_pointer.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class NavigationAlignment:
    observation_step: int
    action_step: int
    next_step: int | None
    evidence_sha256: str | None
    dt_s: float | None

    def __post_init__(self):
        require_int(self.observation_step, "observation_step", minimum=0)
        require_int(self.action_step, "action_step", minimum=0)
        if self.next_step is not None:
            require_int(self.next_step, "next_step", minimum=self.observation_step + 1)
        if self.evidence_sha256 is not None:
            _sha(self.evidence_sha256, "alignment evidence_sha256")
        if self.dt_s is not None:
            if (
                require_real(self.dt_s, "source dt_s", minimum=0) == 0
                or self.evidence_sha256 is None
            ):
                raise NavigationSupervisionError(
                    "known dt must be positive and source-evidence-bound"
                )

    @classmethod
    def from_mapping(cls, value):
        return cls(**_object(value, cls))

    def to_mapping(self):
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass(frozen=True, slots=True)
class NavigationSupervisionSample:
    sample_id: str
    episode_id: str
    scene_id: str
    step_index: int
    source: NavigationSupervisionSource
    prompt: NavigationPrompt
    frames: tuple[NavigationFrame, ...]
    pose: NavigationPose
    planned_action: NavigationPlannedAction
    alignment: NavigationAlignment
    partition: str
    label_provenance: str = LABEL_PROVENANCE

    def __post_init__(self):
        for name in ("sample_id", "episode_id", "scene_id"):
            _text(getattr(self, name), name)
        require_int(self.step_index, "sample step_index", minimum=0)
        for name, cls in (
            ("source", NavigationSupervisionSource),
            ("prompt", NavigationPrompt),
            ("pose", NavigationPose),
            ("planned_action", NavigationPlannedAction),
            ("alignment", NavigationAlignment),
        ):
            if not isinstance(getattr(self, name), cls):
                raise TypeError(f"{name} must be {cls.__name__}")
        if (
            type(self.frames) is not tuple
            or not 1 <= len(self.frames) <= 2
            or any(not isinstance(frame, NavigationFrame) for frame in self.frames)
        ):
            raise NavigationSupervisionError(
                "sample frames must be 1-2 immutable typed current/past RGB frames"
            )
        if len({frame.frame_id for frame in self.frames}) != len(self.frames):
            raise NavigationSupervisionError("duplicate frame IDs are not history")
        if any(
            frame.episode_id != self.episode_id or frame.scene_id != self.scene_id
            for frame in self.frames
        ):
            raise NavigationSupervisionError("RGB history cannot cross episode/scene")
        if self.frames[-1].step_index != self.step_index or any(
            frame.step_index > self.step_index for frame in self.frames
        ):
            raise NavigationSupervisionError(
                "actor input must end at current step and cannot include future RGB"
            )
        if len(self.frames) == 2:
            past, current = self.frames
            if past.step_index != self.step_index - 1:
                raise NavigationSupervisionError(
                    "history must be the latest original past step, not a guessed keyframe"
                )
            if (past.source_clock_id, past.stamp_unit) != (
                current.source_clock_id,
                current.stamp_unit,
            ):
                raise NavigationSupervisionError("history cannot cross source clock domains")
            if past.source_stamp is not None and past.source_stamp >= current.source_stamp:
                raise NavigationSupervisionError("known source timestamps must strictly increase")
        if (
            self.partition not in {"train", "dev-validation", "dev-test"}
            or self.label_provenance != LABEL_PROVENANCE
        ):
            raise NavigationSupervisionError(
                "unsupported partition or non-planned/expert label provenance"
            )
        if self.partition in {"train", "dev-validation"} and self.source.original_split != "train":
            raise NavigationSupervisionError(
                "original holdout data cannot enter optimizer/checkpoint-selection partitions"
            )
        if (
            self.prompt.source_record_sha256 != self.source.annotation.sha256
            or self.pose.source_record_sha256 != self.source.pose.sha256
        ):
            raise NavigationSupervisionError("Prompt/pose full original file binding differs")
        if self.planned_action.raw_record_sha256 != self.source.pose.sha256:
            raise NavigationSupervisionError(
                "planned primitive must bind original pose/action file, "
                "not aggregated annotation action"
            )
        if (
            self.alignment.observation_step != self.step_index
            or self.alignment.action_step != self.step_index
        ):
            raise NavigationSupervisionError(
                "observation/action alignment cannot infer a different input step"
            )
        if (self.planned_action.kind == "stop") != (self.alignment.next_step is None):
            raise NavigationSupervisionError("only original terminal STOP may omit the next step")
        if self.alignment.next_step is not None and self.alignment.next_step != self.step_index + 1:
            raise NavigationSupervisionError("one-step navigation requires the immediate next pose")
        if (
            self.planned_action.raw_source_pointer is not None
            and self.planned_action.raw_source_pointer.record_index is not None
            and self.planned_action.raw_source_pointer.record_index != self.step_index
        ):
            raise NavigationSupervisionError("raw action record index differs from source step")
        expected_alignment = (
            None
            if self.source.alignment_verification is None
            else self.source.alignment_verification.sha256
        )
        if self.alignment.evidence_sha256 != expected_alignment:
            raise NavigationSupervisionError("alignment evidence binding differs")
        if self.planned_action.unit_verification_sha256 is not None:
            if (
                self.source.unit_verification is None
                or self.planned_action.unit_verification_sha256
                != self.source.unit_verification.sha256
                or self.pose.coordinate_frame is None
                or self.pose.length_unit is None
                or self.pose.yaw_unit is None
            ):
                raise NavigationSupervisionError(
                    "metric supervision cannot have unknown or unbound source units/frame"
                )

    @classmethod
    def from_mapping(cls, value):
        required = frozenset(item.name for item in fields(cls)) | {"schema_version", "contract"}
        value = dict(strict_object(value, name=cls.__name__, required=required))
        require_schema_version(value.pop("schema_version"))
        if value.pop("contract") != SUPERVISION_CONTRACT:
            raise NavigationSupervisionError("unsupported navigation supervision contract")
        for name, typed in (
            ("source", NavigationSupervisionSource),
            ("prompt", NavigationPrompt),
            ("pose", NavigationPose),
            ("planned_action", NavigationPlannedAction),
            ("alignment", NavigationAlignment),
        ):
            value[name] = typed.from_mapping(value[name])
        if not isinstance(value["frames"], list):
            raise NavigationSupervisionError("frames mapping must be an array")
        value["frames"] = tuple(NavigationFrame.from_mapping(frame) for frame in value["frames"])
        return cls(**value)

    @classmethod
    def from_json(cls, payload):
        value = strict_json_loads(payload, name="navigation supervision sample")
        canonical_json(value)
        return cls.from_mapping(value)

    def to_mapping(self):
        return {
            "schema_version": 1,
            "contract": SUPERVISION_CONTRACT,
            **{
                item.name: value.to_mapping()
                if hasattr(value := getattr(self, item.name), "to_mapping")
                else [frame.to_mapping() for frame in value]
                if item.name == "frames"
                else value
                for item in fields(self)
            },
        }

    @property
    def canonical_hash(self):
        return canonical_sha256(self.to_mapping())

    @property
    def episode_key(self):
        return canonical_sha256(
            {
                "repository_id": self.source.repository_id,
                "revision": self.source.revision,
                "episode_id": self.episode_id,
            }
        )

    @property
    def training_ready(self):
        return False  # Integrity/source structure never grants a training review.

    def validate_sources(self, artifact_root: Path, *, budget=_DEFAULT_BUDGET, session=None):
        """Default fully verifies; a shared session defers final SHA to context exit."""
        if session is None:
            with NavigationSourceValidationSession(artifact_root, budget=budget) as owned:
                return owned.validate_sample(self)
        if not isinstance(session, NavigationSourceValidationSession):
            raise TypeError("session must be NavigationSourceValidationSession")
        if Path(artifact_root).resolve(strict=True) != session.artifact_root:
            raise NavigationSupervisionError("validation session artifact_root differs")
        return session.validate_sample(self)


class NavigationSourceValidationSession:
    """Reusable bounded source validation, not semantic review or flight authority.

    Use as a context manager and prime all samples together. Original source files
    get one complete SHA pass before use and another before successful exit;
    source array/sequence parsing retains only requested fields. Returned images
    and labels are provisional until normal exit sets ``verified``. No optimizer
    or publication should consume provisional results. Stat checks detect ordinary
    intervening edits; final full hashes, not stats, establish byte integrity.
    """

    def __init__(self, artifact_root: Path, *, budget=_DEFAULT_BUDGET):
        if not isinstance(budget, NavigationSupervisionBudget):
            raise TypeError("budget must be NavigationSupervisionBudget")
        self.artifact_root = Path(artifact_root).resolve(strict=True)
        if not self.artifact_root.is_dir():
            raise NavigationSupervisionError("artifact_root must be a directory")
        self.budget = budget
        self._files = {}
        self._pointers = {}
        self._pointer_bytes = 0
        self._active = False
        self._used = False
        self.verified = False

    def __enter__(self):
        if self._used:
            raise NavigationSupervisionError("validation session cannot be reused after exit")
        self._used, self._active = True, True
        return self

    def __exit__(self, error_type, error, traceback):
        try:
            if error_type is None:
                for bound, maximum, fingerprint in self._files.values():
                    resolved = bound.verify(self.artifact_root, maximum_bytes=maximum)
                    if self._fingerprint(resolved) != fingerprint:
                        raise NavigationSupervisionError("source changed during validation session")
                self.verified = True
        finally:
            self._active = False
            self._pointers.clear()
        return False

    @staticmethod
    def _fingerprint(path):
        stat = path.stat()
        return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino)

    def _require_active(self):
        if not self._active:
            raise NavigationSupervisionError("validation session requires an active context")

    def _register(self, bound, maximum):
        path = bound.resolve(self.artifact_root, maximum_bytes=maximum)
        if path in self._files:
            original, _, fingerprint = self._files[path]
            if (original.bytes, original.sha256) != (bound.bytes, bound.sha256):
                raise NavigationSupervisionError("same source path has conflicting byte binding")
            if self._fingerprint(path) != fingerprint:
                raise NavigationSupervisionError("source changed during validation session")
        else:
            if len(self._files) >= self.budget.max_source_files:
                raise NavigationSupervisionError("validation source count exceeds budget")
            bound.verify(self.artifact_root, maximum_bytes=maximum)
            self._files[path] = (bound, maximum, self._fingerprint(path))
        return path

    def prime(self, samples):
        """Collect all explicit pointers before one streaming parse per original file."""
        self._require_active()
        if type(samples) is not tuple or not samples:
            raise NavigationSupervisionError("prime requires a nonempty typed sample tuple")
        pending = {}
        for sample in samples:
            if not isinstance(sample, NavigationSupervisionSample):
                raise TypeError("prime samples must be NavigationSupervisionSample")
            if len(sample.prompt.text_original.encode("utf-8")) > self.budget.max_prompt_bytes:
                raise NavigationSupervisionError("original Prompt exceeds byte budget")
            for name in (
                "annotation",
                "pose",
                "conversion",
                "license",
                "review_receipt",
                "unit_verification",
                "alignment_verification",
            ):
                bound = getattr(sample.source, name)
                if bound is not None:
                    self._register(bound, self.budget.max_source_file_bytes)
            for bound, pointer in (
                (sample.source.annotation, sample.prompt.source_pointer),
                (sample.source.pose, sample.planned_action.raw_source_pointer),
            ):
                if pointer is None:
                    raise NavigationSupervisionError(
                        "original Prompt/action require explicit source record pointers"
                    )
                path = self._register(bound, self.budget.max_source_file_bytes)
                if (path, pointer) not in self._pointers:
                    pending.setdefault(path, set()).add(pointer)
        for path, pointers in pending.items():
            with path.open("rb") as stream:
                values = _extract_pointers(stream, tuple(pointers), self.budget)
            for pointer, value in values.items():
                encoded = canonical_json(value)
                self._pointer_bytes += len(encoded.encode("utf-8"))
                if self._pointer_bytes > self.budget.max_cached_pointer_bytes:
                    raise NavigationSupervisionError("session source pointer cache exceeds budget")
                self._pointers[(path, pointer)] = encoded

    def validate_sample(self, sample):
        self._require_active()
        self.prime((sample,))  # Already primed pointers incur no full reparse/hash.
        annotation_path = self._register(
            sample.source.annotation, self.budget.max_source_file_bytes
        )
        original_prompt = strict_json_loads(
            self._pointers[(annotation_path, sample.prompt.source_pointer)]
        )
        if original_prompt != sample.prompt.text_original:
            raise NavigationSupervisionError(
                "Prompt differs from original source field (even if rehashed)"
            )
        pose_path = self._register(sample.source.pose, self.budget.max_source_file_bytes)
        if (
            self._pointers[(pose_path, sample.planned_action.raw_source_pointer)]
            != sample.planned_action.raw_json
        ):
            raise NavigationSupervisionError(
                "raw planned action differs from original source record"
            )
        raw = strict_json_loads(sample.planned_action.raw_json)
        if isinstance(raw, dict) and "imageid" in raw:
            if (
                require_int(raw["imageid"], "original action imageid", minimum=0)
                != sample.step_index
            ):
                raise NavigationSupervisionError("original source imageid differs from source step")
            if "pos" in raw and raw["pos"] != (
                None if sample.pose.position_xyz is None else list(sample.pose.position_xyz)
            ):
                raise NavigationSupervisionError(
                    "source pose differs from original action position"
                )
            if "yaw" in raw and raw["yaw"] != sample.pose.yaw:
                raise NavigationSupervisionError("source pose differs from original action yaw")
        if sample.source.alignment_verification is None:
            raise NavigationSupervisionError(
                "unproven observation/action alignment cannot enter model supervision"
            )
        if (
            sum(frame.bytes for frame in sample.frames) > self.budget.max_total_image_bytes
            or sum(frame.width * frame.height for frame in sample.frames)
            > self.budget.max_total_pixels
        ):
            raise NavigationSupervisionError("RGB input exceeds total byte/pixel budget")
        images = []
        for frame in sample.frames:
            bound = NavigationSourceFile(frame.path, frame.bytes, frame.sha256)
            self._register(bound, self.budget.max_image_bytes)
            images.append(
                frame.decode(
                    bound.read(self.artifact_root, maximum_bytes=self.budget.max_image_bytes),
                    self.budget,
                )
            )
        return tuple(images)


def validate_navigation_episode_samples(samples):
    """Validate complete original episodes, not fragments or flight completion.

    Preserve supplied source order; never sort by filenames or create labels.
    Every episode begins at original step 0 and ends at its external STOP.
    """
    if (
        type(samples) is not tuple
        or not samples
        or any(not isinstance(sample, NavigationSupervisionSample) for sample in samples)
    ):
        raise NavigationSupervisionError("episode samples must be a nonempty typed tuple")
    previous, sample_ids = {}, set()
    scene_partitions = {}
    for sample in samples:
        if sample.sample_id in sample_ids:
            raise NavigationSupervisionError("duplicate source sample ID")
        sample_ids.add(sample.sample_id)
        scene_key = (sample.source.repository_id, sample.source.revision, sample.scene_id)
        if scene_key in scene_partitions and scene_partitions[scene_key] != sample.partition:
            raise NavigationSupervisionError(
                "same source scene cannot cross development partitions"
            )
        scene_partitions[scene_key] = sample.partition
        if sample.episode_key in previous:
            prior = previous[sample.episode_key]
            if prior.planned_action.kind == "stop":
                raise NavigationSupervisionError("original STOP cannot have subsequent steps")
            if sample.source != prior.source:
                raise NavigationSupervisionError(
                    "original full-source identity cannot change within an episode"
                )
            if (
                sample.step_index != prior.step_index + 1
                or sample.scene_id != prior.scene_id
                or sample.partition != prior.partition
                or sample.source.original_split != prior.source.original_split
                or sample.prompt != prior.prompt
            ):
                raise NavigationSupervisionError(
                    "episode steps must preserve exact identity/Prompt/partition "
                    "and consecutive source order"
                )
        elif sample.step_index != 0:
            raise NavigationSupervisionError("complete episode must begin at original step 0")
        previous[sample.episode_key] = sample
    if any(sample.planned_action.kind != "stop" for sample in previous.values()):
        raise NavigationSupervisionError("complete episode must end at original terminal STOP")
    return samples


_SYSTEM = (
    "Predict only the next externally planned navigation primitive from the original instruction "
    "and actual RGB observations. Unknown metric quantities stay null. Return JSON with "
    f"schema_version=1 and contract='{TARGET_CONTRACT}', kind translate/turn_yaw/stop, "
    "translation_frame='body_frd', yaw_convention='clockwise_right_positive', "
    "translation_m=[forward,right,down] in metres or null, yaw_delta_rad in radians or null. "
    "STOP means only external navigation sequence stop, not landing, arrival or task completion. "
    "Do not emit confidence, speed, duration, telemetry, phase, transport or flight commands."
)


def serialize_navigation_supervision_messages(
    sample, artifact_root: Path, *, budget=_DEFAULT_BUDGET, session=None
):
    """Only verbatim Prompt + ordered past/current RGB; no source/pose/label IDs."""
    if not isinstance(sample, NavigationSupervisionSample):
        raise TypeError("sample must be NavigationSupervisionSample")
    images = sample.validate_sources(artifact_root, budget=budget, session=session)
    content = [{"type": "text", "text": sample.prompt.text_original}]
    for index, image in enumerate(images):
        content.append(
            {
                "type": "text",
                "text": "Current RGB observation"
                if index == len(images) - 1
                else "Most recent past RGB observation",
            }
        )
        content.append({"type": "image", "image": image})
    return [
        {"role": "system", "content": [{"type": "text", "text": _SYSTEM}]},
        {"role": "user", "content": content},
    ]


def serialize_navigation_supervision_target(
    sample, artifact_root: Path, *, budget=_DEFAULT_BUDGET, session=None
):
    """Separate planned target; never a v2 flight intent or an executed-action label."""
    if not isinstance(sample, NavigationSupervisionSample):
        raise TypeError("sample must be NavigationSupervisionSample")
    sample.validate_sources(artifact_root, budget=budget, session=session)
    action = sample.planned_action
    return canonical_json(
        {
            "schema_version": 1,
            "contract": TARGET_CONTRACT,
            "kind": action.kind,
            "translation_frame": "body_frd",
            "yaw_convention": "clockwise_right_positive",
            "translation_m": None if action.translation_m is None else list(action.translation_m),
            "yaw_delta_rad": action.yaw_delta_rad,
        }
    )


__all__ = [
    "SUPERVISION_CONTRACT",
    "TARGET_CONTRACT",
    "NavigationSupervisionError",
    "NavigationSupervisionBudget",
    "NavigationSourceFile",
    "NavigationRecordPointer",
    "NavigationSupervisionSource",
    "NavigationPrompt",
    "NavigationFrame",
    "NavigationPose",
    "NavigationPlannedAction",
    "NavigationAlignment",
    "NavigationSupervisionSample",
    "NavigationSourceValidationSession",
    "validate_navigation_episode_samples",
    "serialize_navigation_supervision_messages",
    "serialize_navigation_supervision_target",
]
