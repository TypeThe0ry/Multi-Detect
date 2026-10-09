"""Fit the existing optional task ranker on explicit synthetic budget cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy import ContextSnapshot, ResolvedTaskSpec  # noqa: E402
from multidetect.autonomy.policy_artifact import PolicyArtifact  # noqa: E402
from multidetect.autonomy.serialization import (  # noqa: E402
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)
from multidetect.autonomy.task_ranker import TaskRanker, load_task_ranker  # noqa: E402
from multidetect.autonomy.task_selection import TaskCandidate  # noqa: E402
from multidetect.simulation.task_training import (  # noqa: E402
    TaskTrainingCase,
    evaluate_task_ranker,
    fit_task_ranker,
)


def _contract_hash(contract: str) -> str:
    return hashlib.sha256(contract.encode("utf-8")).hexdigest()


def _artifact_bytes(artifact: PolicyArtifact) -> bytes:
    payload = json.dumps(artifact.to_mapping(), ensure_ascii=False, indent=2, sort_keys=True)
    return (payload + "\n").encode("utf-8")


def load_dataset(path: Path, *, raw: bytes | None = None):
    if raw is None:
        with path.open("rb") as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
    else:
        raw = bytes(raw)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("dataset exceeds 4 MiB")
    data = strict_object(
        strict_json_loads(raw.decode("utf-8")),
        name="dataset",
        required=frozenset({"schema_version", "provenance", "cases", "coefficient_candidates"}),
    )
    require_schema_version(data["schema_version"])
    require_text(data["provenance"], "provenance")
    if not isinstance(data["cases"], list) or not 3 <= len(data["cases"]) <= 512:
        raise ValueError("dataset requires 3..512 explicit cases")
    groups = {name: [] for name in ("train", "validation", "test")}
    families, ids, identities = {}, set(), set()
    for row in data["cases"]:
        row = strict_object(
            row,
            name="case",
            required=frozenset(
                {
                    "case_id",
                    "family",
                    "split",
                    "context",
                    "vehicle_class",
                    "tasks",
                    "airframe_profile_hash",
                }
            ),
        )
        split = row["split"]
        if split not in groups:
            raise ValueError("invalid split")
        family = require_text(row["family"], "family")
        if family in families and families[family] != split:
            raise ValueError("scenario family crosses splits")
        families[family] = split
        if not isinstance(row["tasks"], list) or not 1 <= len(row["tasks"]) <= 32:
            raise ValueError("case requires 1..32 tasks")
        candidates, runtimes = [], {}
        for item in row["tasks"]:
            item = strict_object(
                item, name="task entry", required=frozenset({"task", "priority", "runtime"})
            )
            task = ResolvedTaskSpec.from_mapping(item["task"])
            if task.task_id in runtimes:
                raise ValueError("duplicate task ID")
            candidates.append(TaskCandidate(task, item["priority"]))
            runtimes[task.task_id] = item["runtime"]
        airframe_profile_hash = require_sha256(
            row["airframe_profile_hash"], "airframe_profile_hash"
        )
        context = ContextSnapshot.from_mapping(row["context"])
        if context.airframe_profile_hash is None:
            context = replace(context, airframe_profile_hash=airframe_profile_hash)
        case = TaskTrainingCase(
            row["case_id"],
            context,
            row["vehicle_class"],
            tuple(candidates),
            runtimes,
            airframe_profile_hash,
        )
        if case.case_id in ids or case.input_hash in identities:
            raise ValueError("duplicate case ID or inputs")
        ids.add(case.case_id)
        identities.add(case.input_hash)
        groups[split].append(case)
    if any(not cases for cases in groups.values()):
        raise ValueError("all three splits must be nonempty")
    grid = data["coefficient_candidates"]
    if not isinstance(grid, list) or not 1 <= len(grid) <= 256:
        raise ValueError("1..256 coefficient candidates required")
    coefficients = [TaskRanker(tuple(values), "grid validation").coefficients for values in grid]
    return (
        groups,
        coefficients,
        {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "provenance": data["provenance"],
            "families": families,
        },
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--parent-sha256", required=True)
    parser.add_argument(
        "--airframe-profile-sha256",
        help=(
            "explicit runtime airframe identity to bind into policy artifacts; "
            "defaults to the single training-dataset identity"
        ),
    )
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output_directory.exists():
        raise ValueError("output directory must be new")
    groups, coefficients, identity = load_dataset(args.dataset)
    parent = load_task_ranker(args.parent, expected_sha256=args.parent_sha256)
    dataset_sha256 = identity["sha256"]
    airframe_hashes = {case.airframe_profile_hash for case in groups["train"]}
    if len(airframe_hashes) != 1:
        raise ValueError("training cases must share one airframe profile for a policy artifact")
    dataset_airframe_profile_hash = next(iter(airframe_hashes))
    airframe_profile_hash = (
        require_sha256(args.airframe_profile_sha256, "airframe_profile_sha256")
        if args.airframe_profile_sha256 is not None
        else dataset_airframe_profile_hash
    )

    def artifact_factory(model: TaskRanker, *, airframe_hash: str) -> PolicyArtifact:
        return PolicyArtifact.for_ranker(
            model,
            policy_id="task-ranker-budget-v1",
            policy_version="0.3.0-pre.8",
            airframe_profile_hash=airframe_hash,
            mission_contract_hash=_contract_hash("multidetect.mission-compile-runtime-v1"),
            safety_policy_hash=_contract_hash("multidetect.safety-policy-v1"),
            parent_model_sha256=parent.canonical_hash,
            training_dataset_sha256=dataset_sha256,
        )

    candidate, search = fit_task_ranker(
        groups["train"],
        parent,
        coefficients,
        policy_artifact_factory=lambda model: artifact_factory(
            model, airframe_hash=dataset_airframe_profile_hash
        ),
    )
    # Freeze the training-selected candidate before consulting either held-out split.
    evaluations = {}
    for split, cases in groups.items():
        forbidden = search["training_input_hashes"] if split != "train" else ()
        evaluations[split] = {
            "parent": evaluate_task_ranker(
                cases,
                parent,
                forbidden_input_hashes=forbidden,
                policy_artifact=artifact_factory(
                    parent, airframe_hash=dataset_airframe_profile_hash
                ),
            ),
            "candidate": evaluate_task_ranker(
                cases,
                candidate,
                forbidden_input_hashes=forbidden,
                policy_artifact=artifact_factory(
                    candidate, airframe_hash=dataset_airframe_profile_hash
                ),
            ),
        }
    raw_candidate = (json.dumps(candidate.to_mapping(), indent=2) + "\n").encode("utf-8")
    parent_artifact = artifact_factory(parent, airframe_hash=airframe_profile_hash)
    candidate_artifact = artifact_factory(candidate, airframe_hash=airframe_profile_hash)
    raw_parent_artifact = _artifact_bytes(parent_artifact)
    raw_candidate_artifact = _artifact_bytes(candidate_artifact)
    report = {
        "schema_version": 1,
        "method": "synthetic_budget_parameter_search",
        "dataset": {"path": str(args.dataset), **identity},
        "airframe_profile_hash": airframe_profile_hash,
        "airframe_profile_hash_source": (
            "explicit_cli" if args.airframe_profile_sha256 is not None else "training_dataset"
        ),
        "parent_file_sha256": args.parent_sha256,
        "candidate_file_sha256": hashlib.sha256(raw_candidate).hexdigest(),
        "candidate_bytes": len(raw_candidate),
        "parent_policy_artifact_sha256": parent_artifact.canonical_hash,
        "candidate_policy_artifact_sha256": candidate_artifact.canonical_hash,
        "parent_policy_artifact_file_sha256": hashlib.sha256(raw_parent_artifact).hexdigest(),
        "candidate_policy_artifact_file_sha256": hashlib.sha256(raw_candidate_artifact).hexdigest(),
        "policy_artifact_files": {
            "parent": "parent-policy-artifact.json",
            "candidate": "candidate-policy-artifact.json",
        },
        "search": search,
        "evaluations": evaluations,
        "candidate_promoted": False,
        "physical_performance_claimed": False,
        "holdout_used_for_selection": False,
    }
    args.output_directory.mkdir(parents=True, exist_ok=False)
    with (args.output_directory / "candidate.json").open("xb") as stream:
        stream.write(raw_candidate)
    with (args.output_directory / "parent-policy-artifact.json").open("xb") as stream:
        stream.write(raw_parent_artifact)
    with (args.output_directory / "candidate-policy-artifact.json").open("xb") as stream:
        stream.write(raw_candidate_artifact)
    with (args.output_directory / "report.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            {
                "candidate_sha256": candidate.canonical_hash,
                "selected_trial": search["selected_trial"],
                "rewards": {
                    s: {k: v["reward"] for k, v in e.items()} for s, e in evaluations.items()
                },
                "candidate_promoted": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
