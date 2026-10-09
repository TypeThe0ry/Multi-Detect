"""V2 NEW267 warm-start recovery after the preserved zero-update r1 failure.

The old failed13 ledger is authenticated, not restored. CPU86 acceptance is a
separate required artifact. No supervisor, download, retry, or budget reset is
provided. Model dependencies are lazy and only train/reload phases load a model.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import json
import math
import os
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

CLASSES = ("forward", "left", "right", "stop")
CONTRACT = "multidetect.onboard-navigation-recovery267-v2"
PLAN_CONTRACT = CONTRACT + "-plan"
JOURNAL_CONTRACT = CONTRACT + "-optimizer-journal"
LEGACY_PLAN = "configs/training/onboard-navigation-streamed-continuation-20261008-r1.json"
LEGACY_SHA = "eac75d2def168f5daa44b3bba713456c71f80d00a1b91933b8075a1dd32c20cc"
OLD_RUN = "artifacts/models/onboard-navigation-balanced-streamed-20261008-r1"
HISTORY = {
    "failure_evidence": (
        "docs/evidence/onboard-navigation-streamed-training-failure-20261008-r1.json",
        "0d7575dc7cf39ae363087e28ed06f09e4dd879432f26e1e18d49f7bf6de2bb69",
    ),
    "failure_report": (
        f"{OLD_RUN}/train/report.json",
        "a6085b69948341e61987bdbd6ad80c8ab7ad9dfd0c4e95fdb9b3b9df70f68e47",
    ),
    "failure_journal": (
        f"{OLD_RUN}/train/training-events.jsonl",
        "2f247580e4c010fa544bf9c607bb61288a57f727406572eaae2064fea0c09d68",
    ),
    "completion": (
        f"{OLD_RUN}/completion.json",
        "10d468b52dc945f7dc16e2f212dd95ad409554a2bb2648139f1cfd79b4866565",
    ),
    "prepared_inputs": (
        f"{OLD_RUN}/cpu-encode/prepared-inputs.json",
        "49f32a217a22482e8261e19a4c6c216fc8aec934613351164fb1b65d5e0810eb",
    ),
    "baseline_evidence": (
        "docs/evidence/onboard-navigation-streamed-baseline-phase-20261008-r1.json",
        "63a9f8470f24fa403fd262e6fa5112d44f2bacd673f1271ccd0cb145c68a99af",
    ),
}
NEW_FILES = (
    "scripts/train_onboard_navigation_recovery.py",
    "tests/test_train_onboard_navigation_recovery.py",
    "scripts/train_onboard_navigation_recovery_v2.py",
    "tests/test_train_onboard_navigation_recovery_v2.py",
    "scripts/onboard_navigation_recovery_budget.py",
    "tests/test_onboard_navigation_recovery_budget.py",
    "scripts/onboard_navigation_closed_sample_encoding.py",
    "tests/test_onboard_navigation_closed_sample_encoding.py",
    "scripts/onboard_navigation_host_memory.py",
    "tests/test_onboard_navigation_host_memory.py",
    "scripts/verify_onboard_navigation_closed_cpu_inputs.py",
    "tests/test_verify_onboard_navigation_closed_cpu_inputs.py",
)

ZERO_ATTEMPT_RUN = "artifacts/models/onboard-navigation-recovery-20261008-r1"
V2_OUTPUT = "artifacts/models/onboard-navigation-recovery-20261008-r2"
ZERO_ATTEMPT_HISTORY = {
    "evidence": (
        "docs/evidence/onboard-navigation-recovery-loader-failure-20261008-r1.json",
        "85e655d01dd4083bf7ed920486812a45b388ba1dc0efe70b6c1c67201ab05261",
    ),
    "plan": (
        "configs/training/onboard-navigation-recovery-20261008-r1.json",
        "74d918841a782d1c9942439c2faeaed516c8e0137ac8301c3d06f79818d70e51",
    ),
    "report": (
        f"{ZERO_ATTEMPT_RUN}/train/report.json",
        "4b0b8ff091b82f1d4cd59e12db21b34bba304517ae2e899f577a901aac71207f",
    ),
    "source": (
        "scripts/train_onboard_navigation_recovery.py",
        "7b03450a6b6f958475c5aa83afcf176dcc56b02e092f14cc40b2f3acec6931a8",
    ),
    "tests": (
        "tests/test_train_onboard_navigation_recovery.py",
        "8b5e6f9face0fe4f92ba6f56912f97b5b5212b1b76d21846efc35ddd59c23339",
    ),
}
CONSTRUCTOR_SOURCE_SHA = "2eedf41c1b25abfcc686dc79617b15686c2802b2930884e44dd2fe0ab02ef16f"

FALSE_CLAIMS = (
    "quality_claim",
    "candidate_promoted",
    "physical_performance_claimed",
    "hil_real_evidence",
    "outdoor_gnss_evidence",
    "final_release_allowed",
    "execution_authority",
    "transport_created",
    "mission_created",
    "independent_holdout_available",
    "sim_real_parity_verified",
    "exact_resume",
    "optimizer_state_restored",
    "scheduler_state_restored",
    "rng_state_restored",
    "prior_journal_copied",
    "budget_reset",
    "default_model_changed",
)
STAGES = ("attempted", "returned", "synchronized", "completed")
STAGE_MEASUREMENTS = {
    "attempted": {"loss", "gradient_norm"},
    "returned": set(),
    "synchronized": set(),
    "completed": {"elapsed_s", "peak_allocated_bytes", "peak_reserved_bytes"},
}


def budget_api():
    from scripts import onboard_navigation_recovery_budget

    return onboard_navigation_recovery_budget


def authenticate_code(rows):
    for row in rows:
        path = bound_path(row)
        if not path.is_relative_to(ROOT) or row["bytes"] > 2 * 1024 * 1024:
            raise ValueError("Dependency code must be a bounded repository source")
        if record(path) != {**row, "path": str(path)}:
            raise ValueError("Dependency source bytes changed before import")


@dataclass(frozen=True)
class RecoveryModelBudget:
    """Only backend input/LoRA attributes; NEW accounting remains separate."""

    epochs: int = 1
    max_optimizer_steps: int = 267
    max_pixels_per_image: int = 50176
    max_input_tokens: int = 4096
    max_new_tokens: int = 192
    rank: int = 4
    alpha: int = 8
    learning_rate: float = 2e-5
    seed: int = 20261008
    gradient_checkpointing: bool = True
    gradient_clip_norm: float = 1.0
    max_cuda_reserved_bytes: int = 8053063680

    def validate(self):
        expected = asdict(RecoveryModelBudget())
        if any(
            type(v) is not type(expected[k]) or v != expected[k] for k, v in asdict(self).items()
        ):
            raise ValueError("Only the explicit NEW267 model/input budget is allowed")


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def strict_json(raw):
    def pairs(values):
        result = {}
        for name, value in values:
            if name in result:
                raise ValueError("Duplicate JSON key")
            result[name] = value
        return result

    def reject(value):
        raise ValueError("Nonfinite JSON constant: " + value)

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)
    canonical(value)  # Reject numeric overflow to infinity as well.
    return value


def require_sha(value):
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("An explicit canonical SHA-256 is required")


def record(path):
    path = Path(path).resolve(strict=True)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def bound_path(row):
    if type(row) is not dict or set(row) != {"path", "bytes", "sha256"}:
        raise ValueError("Exact file binding fields are required")
    if type(row["path"]) is not str or type(row["bytes"]) is not int or row["bytes"] <= 0:
        raise ValueError("Invalid source path or byte count")
    require_sha(row["sha256"])
    return (ROOT / row["path"]).resolve(strict=True)


def read_bound(row):
    path = bound_path(row)
    if not path.is_relative_to(ROOT) or row["bytes"] > 2 * 1024 * 1024:
        raise ValueError("Expected a bounded repository JSON artifact")
    if record(path) != {**row, "path": str(path)}:
        raise ValueError("Bound small input bytes changed")
    return strict_json(path.read_bytes())


def create_json(path, value):
    with Path(path).open("xb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def history():
    legacy_binding = record(ROOT / LEGACY_PLAN)
    if legacy_binding["sha256"] != LEGACY_SHA:
        raise ValueError("Frozen legacy plan changed")
    legacy = read_bound(legacy_binding)
    # Authenticate low-level code before importing it, not an altered old trainer.
    declarations = {str(bound_path(row)): row for row in legacy["source_files"]}
    for name in (
        "scripts/train_onboard_navigation_streamed.py",
        "scripts/onboard_navigation_balanced_journal.py",
    ):
        row = declarations[str(ROOT / name)]
        if record(bound_path(row)) != {**row, "path": str(bound_path(row))}:
            raise ValueError("Frozen admission implementation changed")
    from scripts import onboard_navigation_balanced_journal as old_journal
    from scripts import train_onboard_navigation_streamed as old_api

    bindings = {name: record(ROOT / path) for name, (path, _) in HISTORY.items()}
    if any(bindings[name]["sha256"] != digest for name, (_, digest) in HISTORY.items()):
        raise ValueError("Closed historical failure or baseline evidence changed")
    schedule = read_bound(legacy["schedule"])
    observed = old_journal.verify_journal(
        bound_path(bindings["failure_journal"]), schedule["draws"]
    )
    failure, completion = read_bound(bindings["failure_report"]), read_bound(bindings["completion"])
    if (
        any(observed[f"optimizer_steps_{stage}"] != 13 for stage in STAGES)
        or observed["optimizer_update_uncertain"] is not False
        or observed["complete"] is not False
        or canonical(observed) != canonical(failure["journal"])
        or failure["native_exit_code"] != 1
        or completion["native_exit_code"] != 1
        or failure["plan_sha256"] != LEGACY_SHA
        or completion["plan_sha256"] != LEGACY_SHA
    ):
        raise ValueError("The failed13 spent ledger is not the exact closed attempt")
    original = read_bound(legacy["original_plan"])
    prepared = read_bound(bindings["prepared_inputs"])
    old_api.validate_prepared_inputs(prepared, legacy, old_api.BalancedTrainingBudget())
    baseline = read_bound(bindings["baseline_evidence"])
    for name, suffix in (
        ("baseline_accepted", "baseline-accepted.json"),
        ("baseline_report", "baseline/report.json"),
        ("base_round", "baseline/base-all86.json"),
        ("warm_round", "baseline/warm_start-all86.json"),
    ):
        row = next(row for row in baseline["bindings"] if row["path"] == f"{OLD_RUN}/{suffix}")
        read_bound(row)
        bindings[name] = {**row, "path": str(bound_path(row))}
    return legacy_binding, legacy, original, prepared, bindings


def targets_from_rows(rows, ids, prepared):
    if len(rows) != 86 or [row["sample_id"] for row in rows] != ids or len(set(ids)) != 86:
        raise ValueError("Exactly the ordered original86 source identities are required")
    targets, pools = {}, {name: [] for name in CLASSES}
    for row in rows:
        if row["partition"] != "train" or row["source"]["original_split"] != "train":
            raise ValueError("Recovery cannot introduce holdout or new samples")
        action = row["planned_action"]
        target = {
            "schema_version": 1,
            "contract": "multidetect.onboard-planned-navigation-target-v1",
            "kind": action["kind"],
            "translation_frame": "body_frd",
            "yaw_convention": "clockwise_right_positive",
            "translation_m": action["translation_m"],
            "yaw_delta_rad": action["yaw_delta_rad"],
        }
        sample_id = row["sample_id"]
        if hashlib.sha256(canonical(target)).hexdigest() != prepared[sample_id]["target_sha256"]:
            raise ValueError("Semantic planned target differs from accepted full-chat metadata")
        targets[sample_id] = target
        pools[classify(target)].append(sample_id)
    return targets, pools


def classify(target):
    if target["kind"] == "stop":
        return "stop"
    if target["kind"] == "turn_yaw" and type(target["yaw_delta_rad"]) in (int, float):
        if target["yaw_delta_rad"] < 0:
            return "left"
        if target["yaw_delta_rad"] > 0:
            return "right"
    translation = target["translation_m"]
    if target["kind"] == "translate" and type(translation) is list and len(translation) == 3:
        if translation[0] > 0 and translation[1:] == [0, 0]:
            return "forward"
    raise ValueError("No undeclared semantic action class can enter recovery")


def validate_zero_attempt(prior_plan, report, *, plan_sha):
    """The closed r1 load/schema failure spent zero; it does not erase failed13."""
    if (
        prior_plan.get("contract") != "multidetect.onboard-navigation-recovery267-v1-plan"
        or prior_plan.get("output_dir") != str(ROOT / ZERO_ATTEMPT_RUN)
        or type(
            prior_plan.get("schedule", {}).get("optimization", {}).get("spent_optimizer_attempts")
        )
        is not int
        or prior_plan["schedule"]["optimization"]["spent_optimizer_attempts"] != 13
        or prior_plan["schedule"]["optimization"].get("max_optimizer_steps") != 267
        or prior_plan["schedule"]["optimization"].get("campaign_attempt_cap") != 280
        or report.get("contract") != "multidetect.onboard-navigation-recovery267-v1"
        or report.get("phase") != "train"
        or type(report.get("native_exit_code")) is not int
        or report["native_exit_code"] != 1
        or report.get("plan_sha256") != plan_sha
        or report.get("failure_type") != "KeyError"
        or report.get("failure") != "'pid'"
        or report.get("model_loaded") is not True
        or report.get("training_completed") is not False
        or report.get("optimizer_performed") is not False
        or report.get("optimizer_update_uncertain") is not False
        or type(report.get("old_spent_optimizer_attempts")) is not int
        or report["old_spent_optimizer_attempts"] != 13
        or any(
            type(report.get(f"optimizer_steps_{stage}")) is not int
            or report[f"optimizer_steps_{stage}"] != 0
            for stage in STAGES
        )
        or report.get("backward_preflights", []) != []
        or report.get("journal") is not None
        or report.get("saved_adapter_weights") is not None
        or report.get("candidate_all86") is not None
        or report.get("sources_after") is not None
        or report.get("source_bindings_unchanged") is not False
        or report.get("base_parameter_sha256_before")
        != prior_plan.get("expected_base_parameter_sha256")
        or report.get("adapter_parameter_sha256_before")
        != prior_plan.get("expected_warm_start_parameter_sha256")
        or any(report.get(key) is not False for key in FALSE_CLAIMS)
    ):
        raise ValueError("Only the exact preserved zero-update r1 load/schema failure is admitted")


def zero_attempt_history(legacy):
    bindings = {name: record(ROOT / path) for name, (path, _) in ZERO_ATTEMPT_HISTORY.items()}
    if any(
        bindings[name]["sha256"] != digest for name, (_, digest) in ZERO_ATTEMPT_HISTORY.items()
    ):
        raise ValueError("Frozen zero-update r1 plan/report/source/tests changed")
    prior_plan, report = read_bound(bindings["plan"]), read_bound(bindings["report"])
    validate_zero_attempt(prior_plan, report, plan_sha=bindings["plan"]["sha256"])
    evidence = read_bound(bindings["evidence"])
    execution, accounting = evidence["actual_execution"], evidence["accounting"]
    if (
        evidence.get("contract") != "multidetect.onboard-navigation-recovery-loader-failure-v1"
        or execution.get("train_native_exit_code") != 1
        or execution.get("worker_pid") != report["pid"]
        or execution.get("both_processes_absent") is not True
        or execution.get("backward_preflight_started") is not False
        or execution.get("optimizer_created") is not False
        or execution.get("optimizer_journal_created") is not False
        or accounting.get("historical_failed_streamed_attempts_spent") != 13
        or accounting.get("this_failed_loader_attempts_spent") != 0
        or accounting.get("remaining_max_NEW_updates") != 267
        or accounting.get("cumulative_campaign_max_attempts") != 280
    ):
        raise ValueError("Sealed zero-update failure receipt differs from the closed attempt")
    for key in ("expected_base_parameter_sha256", "expected_warm_start_parameter_sha256"):
        if prior_plan[key] != legacy[key]:
            raise ValueError("Zero-update r1 attempt used a different base/warm checkpoint")
    if any(
        (ROOT / ZERO_ATTEMPT_RUN / "train" / name).exists()
        for name in (
            "training-events.jsonl",
            "draw-events.jsonl",
            "adapter",
            "candidate-all86.json",
        )
    ):
        raise ValueError("Zero-update r1 attempt unexpectedly contains optimizer/candidate outputs")
    return bindings


def build_plan(cpu_report_path, cpu_report_sha, output_dir):
    legacy_binding, legacy, original, prepared, bindings = history()
    zero_bindings = zero_attempt_history(legacy)
    cpu_binding = record(cpu_report_path)
    require_sha(cpu_report_sha)
    if cpu_binding["sha256"] != cpu_report_sha:
        raise ValueError("CPU report SHA changed")
    cpu = read_bound(cpu_binding)
    cpu_plan = read_bound(cpu["plan"])
    cpu_code_paths = {str(bound_path(row)) for row in cpu_plan["source_files"]}
    if not {str(ROOT / name) for name in NEW_FILES[-2:]} <= cpu_code_paths:
        raise ValueError("CPU86 implementation and tests must be explicitly authenticated")
    authenticate_code(cpu_plan["source_files"])
    authenticate_code(
        [
            {
                "path": "scripts/onboard_navigation_recovery_budget.py",
                "bytes": 8223,
                "sha256": "56580ea056279027867c82ff70e2e2da0162424fd225fc4f58e1e1b22d9bac71",
            },
            {
                "path": "scripts/onboard_navigation_closed_sample_encoding.py",
                "bytes": 5165,
                "sha256": "5f3bc0e7284c5a92bca1a4781898546eda2ec30c58626b1f321a023a179455b9",
            },
        ]
    )
    from scripts import verify_onboard_navigation_closed_cpu_inputs as cpu_api

    cpu_prepared = read_bound(cpu["prepared_inputs"])
    cpu_api.validate_success_report(
        cpu,
        cpu_prepared,
        legacy,
        expected_metadata=prepared,
        expected_plan_sha256=cpu["plan"]["sha256"],
    )
    if canonical(cpu_prepared) != canonical(prepared):
        raise ValueError("NEW full86 closed metadata differs from original prepared records")
    data_path = bound_path(original["dataset_samples"])
    if record(data_path) != {**original["dataset_samples"], "path": str(data_path)}:
        raise ValueError("Original sample bytes changed")
    rows = [strict_json(line) for line in data_path.read_bytes().splitlines() if line.strip()]
    targets, pools = targets_from_rows(rows, original["train_sample_ids"], prepared)
    sources = {
        str(bound_path(row)): {**row, "path": str(bound_path(row))}
        for row in legacy["source_files"]
    }
    added_sources = [*cpu["source_bindings_before"], *[record(ROOT / name) for name in NEW_FILES]]
    for row in added_sources:
        row = {**row, "path": str(bound_path(row))}
        if row["path"] in sources and sources[row["path"]] != row:
            raise ValueError("Conflicting NEW source declaration")
        sources[row["path"]] = row
    output = Path(output_dir).resolve(strict=False)
    if output != ROOT / V2_OUTPUT:
        raise ValueError("V2 recovery must use its explicit NEW r2 output directory")
    return {
        "schema_version": 1,
        "contract": PLAN_CONTRACT,
        "artifact_root": str(ROOT),
        "legacy_plan": legacy_binding,
        **bindings,
        "prior_failed_zero_plan": zero_bindings["plan"],
        "prior_failed_zero_report": zero_bindings["report"],
        "prior_failed_zero_evidence": zero_bindings["evidence"],
        "prior_failed_zero_optimizer_attempts": 0,
        "cumulative_spent_optimizer_attempts_before_v2": 13,
        "cpu_report": cpu_binding,
        "cpu_plan": cpu["plan"],
        "closed_prepared_inputs": cpu["prepared_inputs"],
        "warm_start": legacy["warm_start"],
        "expected_base_parameter_sha256": legacy["expected_base_parameter_sha256"],
        "expected_warm_start_parameter_sha256": legacy["expected_warm_start_parameter_sha256"],
        "source_files": [sources[key] for key in sorted(sources)],
        "schedule": budget_api().build_recovery_draws(pools, targets=targets, classify=classify),
        "model_budget": asdict(RecoveryModelBudget()),
        "optimizer": legacy["optimizer"],
        "checkpoint_interval_completed_updates": 8,
        "output_dir": str(output),
        "claims": dict.fromkeys(FALSE_CLAIMS, False),
        "continuation_kind": "original_warm_adapter_fresh_optimizer_not_exact_resume",
        "inherited_semantic_admission": (
            "frozen_legacy280_source_admission_once_not_closed_per_draw_or_NEW267_authorization"
        ),
        "baseline_reused_not_rerun": True,
    }


def admit_plan(path, digest):
    binding = record(path)
    require_sha(digest)
    if binding["sha256"] != digest:
        raise ValueError("Recovery plan SHA changed")
    plan = read_bound(binding)
    authenticate_code(
        [
            row
            for row in plan["source_files"]
            if str(bound_path(row)) in {str(ROOT / name) for name in NEW_FILES}
        ]
    )
    expected = build_plan(
        bound_path(plan["cpu_report"]), plan["cpu_report"]["sha256"], plan["output_dir"]
    )
    if canonical(plan) != canonical(expected):
        raise ValueError("Recovery plan differs from exact source/budget/CPU86 admission")
    budget = RecoveryModelBudget(**plan["model_budget"])
    budget.validate()
    return plan, budget


def capture_sources(plan, plan_path, *, expected_plan_sha):
    plan_binding = record(plan_path)
    if plan_binding["sha256"] != expected_plan_sha:
        raise ValueError("Recovery plan changed before source capture")
    rows = plan["source_files"] + [
        plan["legacy_plan"],
        *[plan[k] for k in HISTORY],
        *[plan[k] for k in ("baseline_accepted", "baseline_report", "base_round", "warm_round")],
        plan["cpu_report"],
        plan["cpu_plan"],
        plan["closed_prepared_inputs"],
        plan["prior_failed_zero_plan"],
        plan["prior_failed_zero_report"],
        plan["prior_failed_zero_evidence"],
        *plan["warm_start"].values(),
        plan_binding,
    ]
    inventory = {}
    for row in rows:
        path = bound_path(row)
        normalized = {**row, "path": str(path)}
        if str(path) in inventory and inventory[str(path)] != normalized:
            raise ValueError("Conflicting execution source binding")
        if record(path) != normalized:
            raise ValueError("Execution source bytes changed")
        inventory[str(path)] = normalized
    return [inventory[key] for key in sorted(inventory)]


def append_event(stream, event):
    stream.write(canonical(event).decode("utf-8") + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def append_optimizer_event(stream, draw, stage, **measurements):
    if stage not in STAGES:
        raise ValueError("Unknown optimizer stage")
    if set(measurements) != STAGE_MEASUREMENTS[stage]:
        raise ValueError("Required stage-specific measurements differ")
    for name, value in measurements.items():
        if name not in {
            "loss",
            "gradient_norm",
            "elapsed_s",
            "peak_allocated_bytes",
            "peak_reserved_bytes",
        }:
            raise ValueError("Unbounded optimizer measurement")
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("Invalid bounded optimizer measurement")
        if name.endswith("_bytes") and type(value) is not int:
            raise ValueError("Byte measurements must be exact integers")
    append_event(stream, {"contract": JOURNAL_CONTRACT, **draw, "stage": stage, **measurements})


def verify_journal(path, schedule, *, require_complete=False):
    nominal_targets = {
        sample_id: name for name, values in schedule["sources"].items() for sample_id in values
    }
    budget_api().validate_recovery_schedule(
        schedule, schedule["sources"], targets=nominal_targets, classify=lambda value: value
    )
    draws = schedule["draws"]
    counts = dict.fromkeys(STAGES, 0)
    data = Path(path).read_bytes()
    if data and not data.endswith(b"\n"):
        raise ValueError("Truncated optimizer journal")
    lines = data.splitlines()
    for index, line in enumerate(lines):
        row = strict_json(line)
        if index >= 4 * len(draws):
            raise ValueError("Extra optimizer attempts")
        draw, stage = draws[index // 4], STAGES[index % 4]
        allowed = {
            "contract",
            *draw,
            "stage",
            "loss",
            "gradient_norm",
            "elapsed_s",
            "peak_allocated_bytes",
            "peak_reserved_bytes",
        }
        if (
            type(row) is not dict
            or not {"contract", *draw, "stage"} <= set(row)
            or set(row) - allowed
        ):
            raise ValueError("Unknown optimizer event fields")
        if set(row) != {"contract", *draw, "stage", *STAGE_MEASUREMENTS[stage]}:
            raise ValueError("Required actual optimizer stage measurements are missing")
        if row["contract"] != JOURNAL_CONTRACT or row["stage"] != stage:
            raise ValueError("Optimizer stage order changed")
        if canonical({name: row[name] for name in draw}) != canonical(draw):
            raise ValueError("Optimizer event does not match the bound NEW local draw")
        for name in set(row) - {"contract", *draw, "stage"}:
            value = row[name]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid optimizer measurements")
            if name.endswith("_bytes") and type(value) is not int:
                raise ValueError("Invalid byte measurement type")
        counts[stage] += 1
    attempted = counts["attempted"]
    cumulative = budget_api().RecoveryTrainingBudget().validate_attempts(attempted)
    complete = counts == dict.fromkeys(STAGES, 267)
    if require_complete and not complete:
        raise ValueError("All267 four-stage completed updates are required")
    return {
        **{f"optimizer_steps_{k}": v for k, v in counts.items()},
        "event_count": len(lines),
        "last_phase": STAGES[(len(lines) - 1) % 4] if lines else None,
        "optimizer_update_uncertain": attempted > counts["completed"],
        "complete": complete,
        "cumulative_campaign_attempts": cumulative,
        "old_spent_attempts": 13,
    }


class ClosedInputs:
    def __init__(self, samples, prepared, references, budget, backend, *, encoder=None):
        from scripts.onboard_navigation_closed_sample_encoding import (
            encode_closed_navigation_sample,
        )

        self.samples = {sample.sample_id: sample for sample in samples}
        self.prepared, self.references, self.budget, self.backend = (
            prepared,
            references,
            budget,
            backend,
        )
        self.encoder = encode_closed_navigation_sample if encoder is None else encoder
        self.accesses = 0

    def get(self, sample_id):
        result = self.encoder(
            self.samples[sample_id],
            ROOT,
            processor=self.backend.processor,
            torch_module=self.backend.torch,
            budget=self.budget,
            prefix_reference=self.references[sample_id],
            expected_metadata=self.prepared[sample_id],
        )
        self.accesses += 1
        return result


def save_checkpoint(backend, pilot, budget, directory, completed, journal_path, schedule):
    if directory.exists():
        raise FileExistsError("Checkpoint is create-only")
    directory.mkdir()
    expected = pilot.core.parameter_digest(backend.model, trainable=True)
    backend.model.save_pretrained(str(directory), safe_serialization=True)
    from safetensors.torch import load_file

    weights, config = directory / "adapter_model.safetensors", directory / "adapter_config.json"
    pilot.recovery.verify_config(strict_json(config.read_bytes()), budget)
    state = load_file(str(weights), device="cpu")
    if (
        pilot.recovery.saved_parameter_digest(state, backend.trainable_audit, rank=budget.rank)
        != expected
    ):
        raise ValueError("Saved checkpoint tensor digest differs from actual adapter")
    del state
    observed = verify_journal(journal_path, schedule)
    if observed["optimizer_steps_completed"] != completed or observed["optimizer_update_uncertain"]:
        raise ValueError("Checkpoint does not coincide with a synchronized completed update")
    prefix_path = directory / "training-events-prefix.jsonl"
    with prefix_path.open("xb") as stream:
        stream.write(journal_path.read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    receipt = {
        "completed_new_updates": completed,
        "cumulative_campaign_attempts": 13 + completed,
        "adapter_parameter_sha256": expected,
        "weights": record(weights),
        "config": record(config),
        "journal": record(prefix_path),
        "optimizer_state_saved": False,
        "scheduler_rng_state_saved": False,
        "exact_resume": False,
        "candidate_promoted": False,
    }
    create_json(directory / "checkpoint.json", receipt)
    return receipt


def train_draws(
    backend, inputs, schedule, budget, output, report, pilot, *, saver=save_checkpoint, observe=None
):
    torch = backend.torch
    nominal_targets = {
        sample_id: name for name, values in schedule["sources"].items() for sample_id in values
    }
    budget_api().validate_recovery_schedule(
        schedule, schedule["sources"], targets=nominal_targets, classify=lambda value: value
    )
    actual_audit = pilot.core.audit_trainable_parameters(backend.model)
    if (
        canonical(actual_audit) != canonical(backend.trainable_audit)
        or actual_audit["trainable_dtypes"] != ["torch.float32"]
        or actual_audit["frozen_base_dtypes"] != ["torch.bfloat16"]
        or actual_audit["all_base_and_vision_parameters_frozen"] is not True
        or len(actual_audit["trainable_parameters"]) != 112
    ):
        raise ValueError("Exactly the audited112 FP32 q/v parameters may reach AdamW")
    parameters = [p for p in backend.model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(
        parameters,
        lr=budget.learning_rate,
        weight_decay=0.0,
        betas=(0.9, 0.999),
        eps=1e-8,
        amsgrad=False,
        foreach=False,
        fused=False,
    )
    if optimizer.state:
        raise ValueError("NEW fresh optimizer must have no restored state")
    report["fresh_optimizer_initial_state_empty"] = True
    report["checkpoints"] = []
    report["optimizer_api_calls_started"] = 0
    report["optimizer_attempt_count_scope"] = (
        "conservative_reservations_before_durable_step_intent_not_only_returned_calls"
    )
    journal_path = output / "training-events.jsonl"
    counts = dict.fromkeys(STAGES, 0)
    with (
        journal_path.open("x", encoding="utf-8", newline="\n") as stream,
        (output / "draw-events.jsonl").open("x", encoding="utf-8", newline="\n") as draw_stream,
    ):
        for draw in schedule["draws"]:
            started = time.perf_counter()
            append_event(draw_stream, {"event": "draw_start", **draw})
            current = loss = norm = None
            try:
                if observe:
                    observe("draw_start", draw["local_draw_index"])
                current = inputs.get(draw["sample_id"])
                optimizer.zero_grad(set_to_none=True)
                torch.cuda.reset_peak_memory_stats()
                loss = backend.loss(current)
                if not torch.isfinite(loss).item():
                    raise ValueError("Nonfinite response-only loss")
                loss.backward()
                pilot.finite_gradients(backend)
                norm = torch.nn.utils.clip_grad_norm_(parameters, budget.gradient_clip_norm)
                if not torch.isfinite(norm).item():
                    raise ValueError("Nonfinite clipped gradient norm")
                torch.cuda.synchronize()
                pilot.check_memory(backend, budget)
                loss_value, norm_value = float(loss.detach().item()), float(norm.item())
                budget_api().RecoveryTrainingBudget().validate_attempts(counts["attempted"] + 1)
                counts["attempted"] += 1  # Conservative reservation precedes durable step intent.
                append_optimizer_event(
                    stream, draw, "attempted", loss=loss_value, gradient_norm=norm_value
                )
                report["optimizer_api_calls_started"] += 1
                optimizer.step()
                counts["returned"] += 1
                append_optimizer_event(stream, draw, "returned")
                torch.cuda.synchronize()
                counts["synchronized"] += 1
                append_optimizer_event(stream, draw, "synchronized")
                memory = pilot.check_memory(backend, budget)
                append_optimizer_event(
                    stream,
                    draw,
                    "completed",
                    elapsed_s=time.perf_counter() - started,
                    peak_allocated_bytes=memory["peak_allocated_bytes"],
                    peak_reserved_bytes=memory["peak_reserved_bytes"],
                )
                counts["completed"] += 1
                optimizer.zero_grad(set_to_none=True)
                del current, loss, norm
                current = loss = norm = None
                append_event(
                    draw_stream,
                    {"event": "draw_end", **draw, "completed_new_updates": counts["completed"]},
                )
                if observe:
                    observe("draw_end", draw["local_draw_index"])
                print(
                    json.dumps(
                        {
                            "event": "recovery_draw_completed",
                            "completed": counts["completed"],
                            "total": 267,
                        }
                    ),
                    flush=True,
                )
                if counts["completed"] % 8 == 0:
                    if observe:
                        observe("checkpoint_before", draw["local_draw_index"])
                    directory = output / f"checkpoint-{counts['completed']:03d}"
                    report["checkpoints"].append(
                        saver(
                            backend,
                            pilot,
                            budget,
                            directory,
                            counts["completed"],
                            journal_path,
                            schedule,
                        )
                    )
                    if observe:
                        observe("checkpoint_after", draw["local_draw_index"])
            except BaseException as error:
                try:
                    append_event(
                        draw_stream,
                        {
                            "event": "draw_error",
                            **draw,
                            "error_type": type(error).__name__,
                            "error": str(error)[:2048],
                        },
                    )
                except BaseException:
                    report["draw_error_logging_failed"] = True
                raise
            finally:
                del current, loss, norm
                report.update({f"optimizer_steps_{name}": value for name, value in counts.items()})
                report["optimizer_update_uncertain"] = counts["attempted"] > counts["completed"]
                report["cumulative_campaign_attempts"] = 13 + counts["attempted"]
                report["optimizer_performed"] = (
                    True if counts["returned"] else None if counts["attempted"] else False
                )
    report["journal"] = verify_journal(journal_path, schedule, require_complete=True)


def validate_loader_hooks(report, legacy):
    """Constructor observation has no PID; warmup binds the actual worker PID."""
    constructor, warmup = report.get("constructor"), report.get("warmup")
    expected_fields = {
        "source",
        "constructor_source_sha256",
        "calls_attempted",
        "exceptions_observed",
        "installed",
        "restored",
        "scope",
    }
    if (
        type(constructor) is not dict
        or set(constructor) != expected_fields
        or type(warmup) is not dict
        or type(report.get("pid")) is not int
        or report["pid"] <= 0
        or type(warmup.get("pid")) is not int
        or warmup["pid"] != report["pid"]
        or type(constructor["calls_attempted"]) is not int
        or constructor["calls_attempted"] != 1
        or type(constructor["exceptions_observed"]) is not int
        or constructor["exceptions_observed"] != 0
        or constructor["installed"] is not True
        or constructor["restored"] is not True
        or constructor["scope"] != "this_child_only_original_init_passthrough_traceback"
        or constructor["constructor_source_sha256"] != CONSTRUCTOR_SOURCE_SHA
        or type(warmup.get("warmup_calls_attempted")) is not int
        or warmup["warmup_calls_attempted"] != 1
        or type(warmup.get("warmup_calls_skipped")) is not int
        or warmup["warmup_calls_skipped"] != 1
        or warmup.get("warmup_override_installed") is not True
        or warmup.get("warmup_override_restored") is not True
        or warmup.get("training_budget_changed") is not True
        or any(
            warmup.get(key) is not False
            for key in (
                "backend_initialization_changed",
                "device_map_changed",
                "model_weights_loading_skipped",
                "quantization_changed",
            )
        )
    ):
        raise ValueError("One original constructor and restored child-only hooks are required")
    sources = [
        row
        for row in legacy["source_files"]
        if str(bound_path(row)) == str(ROOT / "scripts/train_onboard_vlm_navigation_pilot.py")
    ]
    if len(sources) != 1 or constructor["source"] != {
        **sources[0],
        "path": str(bound_path(sources[0])),
    }:
        raise ValueError("Constructor observation source differs from frozen loader source")
    runtime = read_bound(legacy["runtime_reference"])
    for key in (
        "allocator_warmup_source",
        "allocator_warmup_function_sha256",
        "allocator_warmup_parameter_names",
        "warmup_policy",
        "warmup_scope",
    ):
        if warmup.get(key) != runtime[key]:
            raise ValueError("Warmup observation differs from the pinned runtime")


def check_loader_cleanup(report, legacy, old, run):
    if (
        report.get("loader_aliases_restored") is not True
        or report.get("outer_hook_fallbacks_restored") is not True
    ):
        raise ValueError("Loader aliases or constructor/warmup hooks were not restored")
    readers = report.get("reader_openings", [])
    if not readers or any(row["final_diagnostics"]["closed"] is not True for row in readers):
        raise ValueError("Every initial model/adapter checkpoint reader must close")
    validate_loader_hooks(report, legacy)
    from scripts import run_onboard_navigation_pilot_streaming as streaming

    allowed = {
        str(bound_path(old["base"]["safetensors"])): ("base", 625, 4255064064, "BF16", "cuda:0"),
        str(bound_path(legacy["warm_start"]["weights"])): ("adapter", 112, 3211264, "F32", "cpu"),
    }
    if report["phase"] == "reload":
        allowed[str((run / "train/adapter/adapter_model.safetensors").resolve())] = (
            "adapter",
            112,
            3211264,
            "F32",
            "cpu",
        )
    completed_kinds = []
    for row in readers:
        if row["path"] not in allowed:
            raise ValueError("Unbound checkpoint reader path")
        kind, count, size, dtype, device = allowed[row["path"]]
        header, entries = streaming.header_binding(row["path"], with_entries=True)
        diag = row["final_diagnostics"]
        reads = diag["tensor_reads"]
        if (
            row["kind"] != kind
            or row["header"] != header
            or diag["filename"] != row["path"]
            or any(diag[k] != value for k, value in header.items())
            or diag["tensor_calls_attempted"] != diag["tensor_calls_completed"]
        ):
            raise ValueError("Checkpoint header or read completion differs")
        if reads:
            if (
                len(reads) != count
                or {r["name"] for r in reads} != set(entries)
                or diag["tensor_calls_completed"] != count
                or diag["tensor_payload_bytes_read"] != size
                or diag["normalized_device"] != device
            ):
                raise ValueError("Initial BF16/F32 tensor payload coverage differs")
            for read in reads:
                entry = entries[read["name"]]
                if (
                    read["completed"] is not True
                    or read["dtype"] != dtype
                    or read["shape"] != entry["shape"]
                    or read["declared_bytes"] != entry["data_offsets"][1] - entry["data_offsets"][0]
                    or read["actual_bytes_read"] != read["declared_bytes"]
                ):
                    raise ValueError("Actual tensor span/dtype differs")
            completed_kinds.append(kind)
    if sorted(completed_kinds) != ["adapter", "base"]:
        raise ValueError("Exactly one complete base and saved adapter load are required")


def evaluate(backend, inputs, samples, targets, pilot, budget):
    rows = []
    for sample in samples:
        encoded = inputs.get(sample.sample_id)
        generated = backend.generate(encoded, parent=False)
        rows.append(
            {
                "sample_id": sample.sample_id,
                "gold_action_class": pilot.action_class(targets[sample.sample_id]),
                "parent": False,
                "generated": generated,
                "diagnostic": pilot.diagnose(generated["raw_output"], targets[sample.sample_id]),
                "actor_identity_sha256": encoded["actor_identity_sha256"],
                "actual_prefix_tensors": encoded["prefix_tensors"],
                "resubstitution_only": True,
            }
        )
        pilot.check_memory(backend, budget)
        del encoded
    return rows


def require_phase_prerequisites(run, phase, plan, plan_sha, *, expected_sources):
    accepted = strict_json((run / "admit-cpu/report.json").read_bytes())

    def checked(value, expected_phase):
        if (
            value.get("contract") != CONTRACT
            or value.get("phase") != expected_phase
            or type(value.get("native_exit_code")) is not int
            or value["native_exit_code"] != 0
            or value.get("plan_sha256") != plan_sha
            or value.get("source_bindings_unchanged") is not True
            or not value.get("sources_before")
            or canonical(value["sources_before"]) != canonical(value.get("sources_after"))
            or canonical(value["sources_before"]) != canonical(expected_sources)
            or any(value.get(key) is not False for key in FALSE_CLAIMS)
        ):
            raise ValueError("Completed prior phase/source/claim gate differs")

    checked(accepted, "admit-cpu")
    if (
        accepted.get("closed_CPU86_accepted") is not True
        or canonical(accepted.get("CPU_acceptance_report")) != canonical(plan["cpu_report"])
        or accepted.get("model_loaded") is not False
        or accepted.get("optimizer_performed") is not False
        or any(
            type(accepted.get(f"optimizer_steps_{name}")) is not int
            or accepted[f"optimizer_steps_{name}"] != 0
            for name in STAGES
        )
    ):
        raise ValueError("CPU-only full86 phase admission differs")
    if phase == "reload":
        trained = strict_json((run / "train/report.json").read_bytes())
        checked(trained, "train")
        journal = verify_journal(
            run / "train/training-events.jsonl", plan["schedule"], require_complete=True
        )
        if (
            canonical(journal) != canonical(trained.get("journal"))
            or trained.get("optimizer_update_uncertain") is not False
            or trained.get("training_completed") is not True
            or trained.get("optimizer_performed") is not True
            or any(
                type(trained.get(f"optimizer_steps_{name}")) is not int
                or trained[f"optimizer_steps_{name}"] != 267
                for name in STAGES
            )
            or trained.get("cumulative_campaign_attempts") != 280
            or trained.get("base_parameter_sha256_after") != plan["expected_base_parameter_sha256"]
        ):
            raise ValueError("Fresh reload requires actual all267 closed training evidence")
        for name, relative in (
            ("saved_adapter_weights", "adapter/adapter_model.safetensors"),
            ("saved_adapter_config", "adapter/adapter_config.json"),
            ("candidate_all86", "candidate-all86.json"),
        ):
            if record(run / "train" / relative) != trained.get(name):
                raise ValueError("Completed candidate artifact bytes changed")
        require_sha(trained["adapter_parameter_sha256_after"])
        if (
            trained["adapter_parameter_sha256_after"]
            == plan["expected_warm_start_parameter_sha256"]
        ):
            raise ValueError("Trained candidate did not change original warm parameter bytes")
        legacy = read_bound(plan["legacy_plan"])
        original = read_bound(legacy["original_plan"])
        prepared = read_bound(plan["closed_prepared_inputs"])
        data = bound_path(original["dataset_samples"]).read_bytes()
        source_rows = [strict_json(line) for line in data.splitlines() if line.strip()]
        targets, _ = targets_from_rows(source_rows, original["train_sample_ids"], prepared)
        from scripts import train_onboard_navigation_streamed as old_api

        with old_api.repository_imports():
            from scripts import train_onboard_vlm_navigation_pilot as pilot

            actual_metrics = old_api.diagnose_rows(
                read_bound(trained["candidate_all86"]),
                original["train_sample_ids"],
                prepared,
                targets,
                pilot,
                parent=False,
            )
        if canonical(actual_metrics) != canonical(
            trained["train_resubstitution_metrics_not_holdout"]["new_candidate"]
        ):
            raise ValueError("Candidate86 raw diagnostics differ from accepted train report")
    return accepted


def run_phase(args):
    plan, budget = admit_plan(args.plan, args.plan_sha256)
    run = Path(plan["output_dir"])
    if args.output_dir is not None and Path(args.output_dir).resolve() != run:
        raise ValueError(
            "Output must be the one explicitly bound run; no reset into a fresh folder"
        )
    run.mkdir(exist_ok=True)
    output = run / args.phase
    output.mkdir(exist_ok=False)
    lock = run / "active-phase.lock"
    lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(lock_fd)
    report = {
        "contract": CONTRACT,
        "phase": args.phase,
        "pid": os.getpid(),
        "plan_sha256": args.plan_sha256,
        "python_executable": str(Path(sys.executable).resolve()),
        "native_exit_code": 1,
        "started_at_utc": datetime.now(UTC).isoformat(),
        **plan["claims"],
        "model_loaded": False,
        "optimizer_performed": False,
        "training_completed": False,
        "source_bindings_unchanged": False,
        "sources_before": None,
        "sources_after": None,
        "host_memory_observations": [],
        "old_spent_optimizer_attempts": 13,
        "prior_failed_zero_optimizer_attempts": plan["prior_failed_zero_optimizer_attempts"],
        "prior_failed_zero_plan": plan["prior_failed_zero_plan"],
        "prior_failed_zero_report": plan["prior_failed_zero_report"],
        "prior_failed_zero_evidence": plan["prior_failed_zero_evidence"],
        "optimizer_update_uncertain": False,
        **dict.fromkeys((f"optimizer_steps_{stage}" for stage in STAGES), 0),
    }

    def observe(stage, draw=None):
        from scripts.onboard_navigation_host_memory import snapshot

        report["host_memory_observations"].append(
            {"stage": stage, "local_draw_index": draw, "snapshot": snapshot()}
        )

    backend = None
    try:
        report["sources_before"] = capture_sources(
            plan, args.plan, expected_plan_sha=args.plan_sha256
        )
        legacy = read_bound(plan["legacy_plan"])
        from scripts import train_onboard_navigation_streamed as old_api

        runtime = read_bound(legacy["runtime_reference"])
        if Path(sys.executable).resolve() != Path(runtime["python_executable"]).resolve():
            raise ValueError("The pinned navigation venv is required")
        for name, version in runtime["package_versions"].items():
            if importlib.metadata.version(name) != version:
                raise ValueError("Pinned runtime distribution changed")
        if args.phase == "admit-cpu":
            report["closed_CPU86_accepted"] = True
            report["CPU_acceptance_report"] = plan["cpu_report"]
        else:
            require_phase_prerequisites(
                run, args.phase, plan, args.plan_sha256, expected_sources=report["sources_before"]
            )
            with old_api.repository_imports():
                observe("semantic_admission_before")
                old, samples, targets, _, pilot = old_api.semantic_admission(legacy)
                report["inherited_semantic_admission"] = plan["inherited_semantic_admission"]
                observe("semantic_admission_after")
                prepared = read_bound(plan["closed_prepared_inputs"])
                pools = {name: [] for name in CLASSES}
                for sample in samples:
                    pools[pilot.action_class(targets[sample.sample_id])].append(sample.sample_id)
                budget_api().validate_recovery_schedule(
                    plan["schedule"], pools, targets=targets, classify=pilot.action_class
                )
                references = {
                    row["sample_id"]: row
                    for row in read_bound(legacy["cpu_prefix_report"])["samples"]
                }
                observe("load_before")
                with old_api.loading_scope(legacy, old, output, report):
                    backend = old_api.load_backend(
                        legacy, budget, old, pilot, args.phase, output, report
                    )
                check_loader_cleanup(report, legacy, old, run)
                observe("load_after")
                inputs = ClosedInputs(samples, prepared, references, budget, backend)
                if args.phase == "train":
                    report["backward_preflights"] = []
                    for probe in legacy["diagnostics"]["smoke_probes"]:
                        observe("preflight_before")
                        current = inputs.get(probe["sample_id"])
                        report["backward_preflights"].append(
                            {"sample_id": probe["sample_id"], **backend.backward_preflight(current)}
                        )
                        del current
                        pilot.check_memory(backend, budget)
                        observe("preflight_after")
                    if (
                        pilot.core.parameter_digest(backend.model, trainable=True)
                        != plan["expected_warm_start_parameter_sha256"]
                    ):
                        raise ValueError("Backward-only preflight changed original warm adapter")
                    if (
                        pilot.core.parameter_digest(backend.model, trainable=False)
                        != plan["expected_base_parameter_sha256"]
                    ):
                        raise ValueError("Backward-only preflight changed frozen base")
                    observe("train_before")
                    train_draws(
                        backend,
                        inputs,
                        plan["schedule"],
                        budget,
                        output,
                        report,
                        pilot,
                        observe=observe,
                    )
                    observe("train_after")
                    observe("candidate_save_before")
                    checkpoint = save_checkpoint(
                        backend,
                        pilot,
                        budget,
                        output / "adapter",
                        267,
                        output / "training-events.jsonl",
                        plan["schedule"],
                    )
                    report["saved_adapter_weights"], report["saved_adapter_config"] = (
                        checkpoint["weights"],
                        checkpoint["config"],
                    )
                    report["adapter_parameter_sha256_after"] = checkpoint[
                        "adapter_parameter_sha256"
                    ]
                    observe("candidate_save_after")
                    observe("candidate_all86_before")
                    rows = evaluate(backend, inputs, samples, targets, pilot, budget)
                    create_json(output / "candidate-all86.json", rows)
                    report["candidate_all86"] = record(output / "candidate-all86.json")
                    observe("candidate_all86_after")
                    old_base, old_warm = (
                        read_bound(plan["base_round"]),
                        read_bound(plan["warm_round"]),
                    )
                    ids = legacy["diagnostics"]["sample_ids"]
                    report["train_resubstitution_metrics_not_holdout"] = {
                        "old_base": old_api.diagnose_rows(
                            old_base, ids, prepared, targets, pilot, parent=True
                        ),
                        "old_warm": old_api.diagnose_rows(
                            old_warm, ids, prepared, targets, pilot, parent=False
                        ),
                        "new_candidate": old_api.diagnose_rows(
                            rows, ids, prepared, targets, pilot, parent=False
                        ),
                    }
                    report["training_completed"] = True
                else:
                    previous = strict_json((run / "train/candidate-all86.json").read_bytes())
                    trained = strict_json((run / "train/report.json").read_bytes())
                    if record(run / "train/candidate-all86.json") != trained["candidate_all86"]:
                        raise ValueError("Completed train's candidate rows changed")
                    rows = evaluate(backend, inputs, samples, targets, pilot, budget)
                    for actual, prior in zip(rows, previous, strict=True):
                        for key in (
                            "sample_id",
                            "diagnostic",
                            "actor_identity_sha256",
                            "actual_prefix_tensors",
                        ):
                            if canonical(actual[key]) != canonical(prior[key]):
                                raise ValueError("Fresh saved reload input/diagnosis changed")
                        for key in ("output_token_ids", "raw_output"):
                            if actual["generated"][key] != prior["generated"][key]:
                                raise ValueError("Fresh saved reload generation changed")
                    create_json(output / "reloaded-all86.json", rows)
                    report["fresh_saved_adapter_reload_verified"] = True
                    report["reload_resubstitution_metrics_not_holdout"] = old_api.diagnose_rows(
                        rows,
                        legacy["diagnostics"]["sample_ids"],
                        prepared,
                        targets,
                        pilot,
                        parent=False,
                    )
                if (
                    pilot.core.parameter_digest(backend.model, trainable=False)
                    != plan["expected_base_parameter_sha256"]
                ):
                    raise ValueError("Frozen base changed during recovery")
                report["base_parameter_sha256_after"] = plan["expected_base_parameter_sha256"]
                after_adapter = pilot.core.parameter_digest(backend.model, trainable=True)
                if (
                    args.phase == "reload"
                    and after_adapter != report["adapter_parameter_sha256_before"]
                ):
                    raise ValueError("Reload inference changed the saved adapter")
                report["adapter_parameter_sha256_after"] = after_adapter
                report["closed_sample_accesses"] = inputs.accesses
                del inputs, backend
                backend = None
                gc.collect()
        report["sources_after"] = capture_sources(
            plan, args.plan, expected_plan_sha=args.plan_sha256
        )
        if report["sources_after"] != report["sources_before"]:
            raise ValueError("Execution sources changed")
        report["source_bindings_unchanged"] = True
        report["native_exit_code"] = 0
    except BaseException as error:
        report.update(failure_type=type(error).__name__, failure=str(error)[:4096])
        traceback.print_exc()
        if args.phase == "train" and (output / "training-events.jsonl").is_file():
            try:
                actual = verify_journal(output / "training-events.jsonl", plan["schedule"])
                report["journal"] = actual
                # A failed durable write can reserve more than the readable WAL.
                report["optimizer_update_uncertain"] = (
                    report["optimizer_update_uncertain"] or actual["optimizer_update_uncertain"]
                )
            except Exception as journal_error:
                report["journal_validation_error"] = str(journal_error)[:2048]
                report["optimizer_update_uncertain"] = True
    finally:
        backend = None
        report["ended_at_utc"] = datetime.now(UTC).isoformat()
        create_json(output / "report.json", report)
        lock.unlink()
    print(
        json.dumps(
            {
                "event": "recovery_phase_finished",
                "phase": args.phase,
                "pid": os.getpid(),
                "native_exit_code": report["native_exit_code"],
                "report": record(output / "report.json"),
            }
        ),
        flush=True,
    )
    return report["native_exit_code"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-plan", action="store_true")
    parser.add_argument("--cpu-report")
    parser.add_argument("--cpu-report-sha256")
    parser.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--output-dir")
    parser.add_argument("--phase", choices=("admit-cpu", "train", "reload"))
    args = parser.parse_args(argv)
    if args.emit_plan:
        if not all((args.cpu_report, args.cpu_report_sha256, args.output_dir)):
            parser.error("--emit-plan needs exact CPU86 report/SHA and the single output directory")
        print(
            canonical(build_plan(args.cpu_report, args.cpu_report_sha256, args.output_dir)).decode(
                "utf-8"
            )
        )
        return 0
    if not all((args.plan, args.plan_sha256, args.phase)):
        parser.error("Explicit bound plan/SHA and phase are required")
    return run_phase(args)


if __name__ == "__main__":
    raise SystemExit(main())
