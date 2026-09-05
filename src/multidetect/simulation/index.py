"""Scenario channel index shared by runtime and evaluator loaders."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..autonomy.serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    require_int,
    require_schema_version,
    require_sha256,
    require_text,
    strict_object,
)


@dataclass(frozen=True, slots=True)
class ScenarioChannelReference:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        normalized = require_text(self.path, "path").replace("\\", "/")
        candidate = PurePosixPath(normalized)
        if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() in {".", ""}:
            raise AutonomyContractError("scenario channel path must be relative and contained")
        object.__setattr__(self, "path", candidate.as_posix())
        object.__setattr__(self, "sha256", require_sha256(self.sha256, "sha256"))

    @classmethod
    def from_mapping(cls, raw: object) -> ScenarioChannelReference:
        value = strict_object(
            raw,
            name="ScenarioChannelReference",
            required=frozenset({"path", "sha256"}),
        )
        return cls(
            path=require_text(value["path"], "path"),
            sha256=require_sha256(value["sha256"], "sha256"),
        )

    def to_mapping(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class ScenarioManifestIndex:
    scenario_id: str
    scenario_version: int
    runtime_input: ScenarioChannelReference
    truth: ScenarioChannelReference
    expectations: ScenarioChannelReference
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(
            self, "scenario_id", require_text(self.scenario_id, "scenario_id")
        )
        object.__setattr__(
            self,
            "scenario_version",
            require_int(self.scenario_version, "scenario_version", minimum=1),
        )
        for name in ("runtime_input", "truth", "expectations"):
            if not isinstance(getattr(self, name), ScenarioChannelReference):
                raise AutonomyContractError(
                    f"{name} must be a ScenarioChannelReference"
                )
        paths = (self.runtime_input.path, self.truth.path, self.expectations.path)
        if len(paths) != len(set(paths)):
            raise AutonomyContractError("scenario channels must use distinct files")

    @classmethod
    def from_mapping(cls, raw: object) -> ScenarioManifestIndex:
        value = strict_object(
            raw,
            name="ScenarioManifestIndex",
            required=frozenset(
                {
                    "schema_version",
                    "scenario_id",
                    "scenario_version",
                    "channels",
                }
            ),
        )
        channels = strict_object(
            value["channels"],
            name="ScenarioChannels",
            required=frozenset({"runtime_input", "truth", "expectations"}),
        )
        return cls(
            scenario_id=require_text(value["scenario_id"], "scenario_id"),
            scenario_version=require_int(
                value["scenario_version"], "scenario_version", minimum=1
            ),
            runtime_input=ScenarioChannelReference.from_mapping(
                channels["runtime_input"]
            ),
            truth=ScenarioChannelReference.from_mapping(channels["truth"]),
            expectations=ScenarioChannelReference.from_mapping(
                channels["expectations"]
            ),
            schema_version=require_schema_version(value["schema_version"]),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "channels": {
                "runtime_input": self.runtime_input.to_mapping(),
                "truth": self.truth.to_mapping(),
                "expectations": self.expectations.to_mapping(),
            },
        }

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.to_mapping())

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def read_json_object(path: Path, *, name: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AutonomyContractError(f"unable to load {name} {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise AutonomyContractError(f"{name} must be a JSON object")
    return raw


def load_scenario_index(path: str | Path) -> tuple[Path, ScenarioManifestIndex]:
    source = Path(path).resolve()
    return source, ScenarioManifestIndex.from_mapping(
        read_json_object(source, name="scenario manifest")
    )


def load_verified_channel(
    manifest_path: Path,
    reference: ScenarioChannelReference,
    *,
    name: str,
) -> dict[str, Any]:
    base = manifest_path.parent.resolve()
    channel_path = (base / Path(*PurePosixPath(reference.path).parts)).resolve()
    try:
        channel_path.relative_to(base)
    except ValueError as exc:
        raise AutonomyContractError(f"{name} channel escapes scenario directory") from exc
    raw = read_json_object(channel_path, name=f"scenario {name} channel")
    observed_hash = canonical_sha256(raw)
    if observed_hash != reference.sha256:
        raise AutonomyContractError(
            f"scenario {name} channel hash mismatch: expected {reference.sha256}, "
            f"observed {observed_hash}"
        )
    return raw


__all__ = [
    "ScenarioChannelReference",
    "ScenarioManifestIndex",
    "load_scenario_index",
    "load_verified_channel",
    "read_json_object",
]
