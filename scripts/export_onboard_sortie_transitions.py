"""Export a reviewable transition proposal from one real SIM sortie.

This adapter joins the existing, truth-free trajectory staging rows with the
OnboardAI bridge records emitted by an external acceptance run.  It does not
label a transition, create a reviewer receipt, or promote a dataset.  A frame
is ``bound`` only when a bridge observation and a complete mission revision
for that exact frame are both present.  Everything else remains ``pending``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.onboard-sortie-transition-proposal-v1"
SCHEMA = "multidetect.onboard-sortie-transition-proposal"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_MARKERS = ("truth.json", "red_car", "red_green", "gazebo_red_")


class SortieTransitionExportError(ValueError):
    """The supplied acceptance/staging pair cannot be exported safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repo_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise SortieTransitionExportError(f"{label} must not be a symbolic link")
    resolved = candidate.resolve(strict=True)
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SortieTransitionExportError(f"{label} is not readable JSON: {resolved}") from exc
    if not isinstance(value, dict):
        raise SortieTransitionExportError(f"{label} must be one JSON object")
    return value


def _reject_fixture_markers(value: Any, *, path: str = "") -> None:
    """Reject fixture identity without rejecting the normal truth_free flags."""

    if isinstance(value, str):
        lowered = value.casefold()
        if any(marker in lowered for marker in _FORBIDDEN_MARKERS):
            raise SortieTransitionExportError(
                f"simulator truth or red/green fixture marker at {path or '<root>'}"
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_fixture_markers(item, path=f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_fixture_markers(item, path=f"{path}[{index}]")


def _require_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value.casefold()) is None:
        raise SortieTransitionExportError(f"{label} must be a lowercase SHA-256")
    return value.casefold()


def _validate_staging(document: Mapping[str, Any]) -> None:
    if document.get("schema") != "multidetect.onboard-trajectory-staging":
        raise SortieTransitionExportError("trajectory input is not staging schema v1")
    if document.get("contract") != "multidetect.onboard-trajectory-staging-v1":
        raise SortieTransitionExportError("trajectory staging contract mismatch")
    if document.get("training_ready") is not False:
        raise SortieTransitionExportError("trajectory staging must remain training_ready=false")
    if document.get("truth_free") is not True or document.get("truth_injected") is not False:
        raise SortieTransitionExportError("trajectory staging must be truth-free")
    if document.get("policy_applied_to_actuator") is not False:
        raise SortieTransitionExportError("trajectory staging actuator boundary is invalid")
    episodes = document.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        raise SortieTransitionExportError("trajectory staging episodes are missing")
    if any(
        not isinstance(episode, Mapping)
        or not isinstance(episode.get("transitions"), list)
        or not episode.get("transitions")
        for episode in episodes
    ):
        raise SortieTransitionExportError("trajectory staging transitions are missing")


def _bridge(acceptance: Mapping[str, Any]) -> Mapping[str, Any]:
    probe = acceptance.get("probe")
    if not isinstance(probe, Mapping):
        raise SortieTransitionExportError("acceptance.probe is missing")
    onboard = probe.get("onboard_model")
    if not isinstance(onboard, Mapping):
        raise SortieTransitionExportError("acceptance.probe.onboard_model is missing")
    bridge = onboard.get("bridge")
    if not isinstance(bridge, Mapping):
        raise SortieTransitionExportError("acceptance.probe.onboard_model.bridge is missing")
    if bridge.get("truth_free") is not True:
        raise SortieTransitionExportError("OnboardAI bridge is not truth-free")
    if bridge.get("policy_applied_to_actuator") is not False:
        raise SortieTransitionExportError("OnboardAI bridge actuator boundary is invalid")
    observations = bridge.get("observations")
    revisions = bridge.get("revisions")
    if not isinstance(observations, list) or not isinstance(revisions, list):
        raise SortieTransitionExportError("OnboardAI bridge observations/revisions are missing")
    return bridge


def _frame_key(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _frame_aliases(value: Any) -> set[str]:
    """Return only transport-safe aliases for one captured frame identity.

    The external RGB bridge prefixes an observation/event id with
    ``gazebo-rgb-`` while the recording manifest uses the canonical
    ``gazebo-sim-...`` id.  The prefix is transport metadata, not a different
    frame.  We keep the original value and add the deterministic normalized
    form; no timestamp or nearest-frame matching is allowed here.
    """

    frame_id = _frame_key(value)
    if frame_id is None:
        return set()
    aliases = {frame_id}
    if frame_id.startswith("gazebo-rgb-"):
        aliases.add(frame_id.removeprefix("gazebo-rgb-"))
    if frame_id.endswith(":target-observed"):
        aliases.add(frame_id.removesuffix(":target-observed"))
    if frame_id.startswith("gazebo-rgb-") and frame_id.endswith(":target-observed"):
        aliases.add(frame_id.removeprefix("gazebo-rgb-").removesuffix(":target-observed"))
    return aliases


def _observation_index(observations: Sequence[Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for item in observations:
        if not isinstance(item, Mapping):
            continue
        if item.get("truth_free") is not True:
            raise SortieTransitionExportError("bridge observation is not truth-free")
        keys = _frame_aliases(item.get("observation_id"))
        keys.update(_frame_aliases(item.get("source_frame_sha256")))
        if not keys:
            continue
        for frame_id in keys:
            if frame_id in result and result[frame_id] is not item:
                raise SortieTransitionExportError(
                    f"duplicate bridge observation frame_id: {frame_id}"
                )
            result[frame_id] = item
    return result


def _revision_index(revisions: Sequence[Any]) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = {}
    for item in revisions:
        if not isinstance(item, Mapping):
            continue
        if item.get("truth_free") is not True:
            raise SortieTransitionExportError("bridge revision is not truth-free")
        keys: set[str] = set()
        for candidate in (
            item.get("frame_id"),
            item.get("event", {}).get("event_id")
            if isinstance(item.get("event"), Mapping)
            else None,
            item.get("task_receipt", {}).get("event_id")
            if isinstance(item.get("task_receipt"), Mapping)
            else None,
        ):
            keys.update(_frame_aliases(candidate))
        for frame_id in keys:
            result.setdefault(frame_id, []).append(item)
    return result


def _hash_bindings(revision: Mapping[str, Any]) -> dict[str, str] | None:
    decision = revision.get("decision")
    binding = decision.get("policy_runtime_binding") if isinstance(decision, Mapping) else None
    if not isinstance(decision, Mapping) or not isinstance(binding, Mapping):
        return None
    if (
        decision.get("contract") != "multidetect.onboard-decision"
        or decision.get("planner_revalidated") is not True
        or decision.get("mission_bound") is not True
        or decision.get("policy_applied_to_actuator") is not False
        or decision.get("truth_free") is not True
        or binding.get("action") != "select"
        or binding.get("planner_revalidated") is not True
        or binding.get("mission_bound") is not True
        or binding.get("policy_applied_to_actuator") is not False
        or binding.get("truth_free") is not True
    ):
        return None
    fields = {
        "context_sha256": binding.get("context_sha256"),
        "plan_sha256": binding.get("plan_sha256"),
        "wire_hash": binding.get("wire_hash"),
        "executable_mission_sha256": binding.get("executable_mission_sha256"),
        "policy_artifact_sha256": binding.get("policy_artifact_sha256"),
        "ranker_sha256": binding.get("ranker_sha256"),
        "airframe_profile_hash": binding.get("airframe_profile_hash"),
    }
    try:
        return {
            key: _require_sha256(value, label=f"decision {key}")
            for key, value in fields.items()
        }
    except SortieTransitionExportError:
        return None


def _transition_rows(staging: Mapping[str, Any], bridge: Mapping[str, Any]) -> list[dict[str, Any]]:
    observations = _observation_index(bridge["observations"])
    revisions = _revision_index(bridge["revisions"])
    rows: list[dict[str, Any]] = []
    for episode in staging["episodes"]:
        if not isinstance(episode, Mapping):
            raise SortieTransitionExportError("trajectory staging episode must be an object")
        transitions = episode.get("transitions")
        if not isinstance(transitions, list):
            raise SortieTransitionExportError("trajectory staging transitions are missing")
        for transition in transitions:
            if not isinstance(transition, Mapping):
                raise SortieTransitionExportError("trajectory transition must be an object")
            frame_id = _frame_key(transition.get("frame_id"))
            if frame_id is None:
                raise SortieTransitionExportError("trajectory transition frame_id is required")
            transition_source = transition.get("source")
            lookup_keys = _frame_aliases(frame_id)
            if isinstance(transition_source, Mapping):
                lookup_keys.update(_frame_aliases(transition_source.get("rgb_payload_sha256")))
            matched_observation = next(
                (observations[key] for key in lookup_keys if key in observations), None
            )
            matched_revision_map: dict[int, Mapping[str, Any]] = {}
            for key in lookup_keys:
                for revision in revisions.get(key, []):
                    matched_revision_map[id(revision)] = revision
            matched_revisions = list(matched_revision_map.values())
            complete = None
            complete_hashes: dict[str, str] | None = None
            if matched_observation is not None:
                for revision in matched_revisions:
                    hashes = _hash_bindings(revision)
                    if hashes is not None:
                        if complete is not None:
                            raise SortieTransitionExportError(
                                f"ambiguous complete OnboardAI decisions for frame_id: {frame_id}"
                            )
                        complete = revision
                        complete_hashes = hashes
            status = "bound" if complete is not None and complete_hashes is not None else "pending"
            binding: dict[str, Any] = {
                "status": status,
                "frame_id": frame_id,
                "observation_present": matched_observation is not None,
                "decision_present": bool(matched_revisions),
                "decision_complete": status == "bound",
            }
            if matched_observation is not None:
                binding["observation_id"] = matched_observation.get("observation_id")
                binding["observation_contract"] = matched_observation.get("contract")
            if complete is not None and complete_hashes is not None:
                binding.update(
                    {
                        "revision_sequence": complete.get("sequence"),
                        "revision_trigger": complete.get("trigger"),
                        "revision_mission_sha256": complete.get("mission_sha256"),
                        "decision": {
                            "response": complete.get("decision", {}).get("response"),
                            "selected_task_id": complete.get("decision", {}).get(
                                "selected_task_id"
                            ),
                            "decision_source": complete.get("decision", {}).get("decision_source"),
                        },
                        "hashes": complete_hashes,
                    }
                )
            rows.append(
                {
                    "episode_id": episode.get("episode_id"),
                    "transition_id": transition.get("transition_id"),
                    "frame_id": frame_id,
                    "observed_at_s": transition.get("observed_at_s"),
                    "status": status,
                    "staging_transition": dict(transition),
                    "acceptance_binding": binding,
                    "truth_free": True,
                    "training_ready": False,
                    "review_required": True,
                }
            )
    return rows


def build_proposal(acceptance: Mapping[str, Any], staging: Mapping[str, Any], *,
                   acceptance_path: Path, staging_path: Path) -> dict[str, Any]:
    _reject_fixture_markers(acceptance, path="acceptance")
    _reject_fixture_markers(staging, path="trajectory_staging")
    _validate_staging(staging)
    bridge = _bridge(acceptance)
    rows = _transition_rows(staging, bridge)
    bound = sum(row["status"] == "bound" for row in rows)
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "contract": CONTRACT,
        "status": "proposal_pending_review",
        "source": {
            "acceptance": {
                "path": _repo_relative(acceptance_path),
                "sha256": _sha256(acceptance_path),
            },
            "trajectory_staging": {
                "path": _repo_relative(staging_path), "sha256": _sha256(staging_path)
            },
            "bridge_contract": bridge.get("contract"),
        },
        "training_ready": False,
        "review_required": True,
        "truth_free": True,
        "truth_injected": False,
        "policy_applied_to_actuator": False,
        "candidate_promoted": False,
        "final_release_allowed": False,
        "rows": rows,
        "summary": {
            "staging_transition_count": len(rows),
            "bound_transition_count": bound,
            "pending_transition_count": len(rows) - bound,
            "all_bound_decisions_complete": bool(rows) and bound == len(rows),
            "synthetic_labels_created": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acceptance", type=Path, required=True)
    parser.add_argument("--trajectory-staging", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        acceptance_path = args.acceptance.expanduser().resolve(strict=True)
        staging_path = args.trajectory_staging.expanduser().resolve(strict=True)
        result = build_proposal(
            _read_object(acceptance_path, label="acceptance"),
            _read_object(staging_path, label="trajectory staging"),
            acceptance_path=acceptance_path,
            staging_path=staging_path,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            json.dumps(
                {"schema": SCHEMA, "status": result["status"], "summary": result["summary"]},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (OSError, SortieTransitionExportError, TypeError, ValueError) as exc:
        print(json.dumps({"schema": SCHEMA, "status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
