"""Runtime-only scenario loader; it never reads truth or expectation files."""

from __future__ import annotations

from pathlib import Path

from ..autonomy.serialization import AutonomyContractError
from ..runtime import RuntimeMode
from .index import load_scenario_index, load_verified_channel
from .runtime_input import ScenarioRuntimeInput


def load_runtime_scenario(path: str | Path, *, mode: RuntimeMode) -> ScenarioRuntimeInput:
    if not isinstance(mode, RuntimeMode):
        raise AutonomyContractError("mode must be a RuntimeMode")
    if mode is RuntimeMode.REAL:
        raise AutonomyContractError("REAL runtime cannot load a scenario or injected faults")
    source, index = load_scenario_index(path)
    runtime_raw = load_verified_channel(source, index.runtime_input, name="runtime_input")
    runtime_input = ScenarioRuntimeInput.from_mapping(
        runtime_raw,
        scenario_manifest_hash=index.canonical_hash,
    )
    if (
        runtime_input.scenario_id != index.scenario_id
        or runtime_input.scenario_version != index.scenario_version
    ):
        raise AutonomyContractError("runtime channel scenario identity does not match manifest")
    return runtime_input


__all__ = ["load_runtime_scenario"]
