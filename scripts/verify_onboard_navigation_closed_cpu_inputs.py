"""All86 CPU metadata acceptance with one verified closed session per encoding.

The inherited semantic admission still uses the frozen old all86 source
session. Only the actual encoding phase has the new one-sample lifecycle.
No model, CUDA context, optimizer, transport, or training recovery is created.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))
CONTRACT = "multidetect.onboard-navigation-closed-cpu86-acceptance-v1"
PROFILE_PLAN = "configs/training/onboard-navigation-closed-sample-cpu-profile-20261008-r1.json"
PROFILE_PLAN_SHA = "d6d5e9473a6bbe8d64490c9bf6602a476f9a1c3c857bc13a2454b2ca6eb72c52"
PROFILE_REPORT = (
    "artifacts/diagnostics/onboard-navigation-closed-sample-cpu-profile-20261008-r1/report.json"
)
PROFILE_REPORT_SHA = "986543a7245a825d472796e6b36da530d931aedcb994bbae4554df03978ad5fe"
PROFILE_SOURCE = "scripts/profile_onboard_navigation_closed_samples.py"
PROFILE_SOURCE_SHA = "ebd6fb5a276462d688fdc8483a78ddd7ec0b717fc98e2a6b73fda6dcc9ec6467"
PREPARED_PATH = (
    "artifacts/models/onboard-navigation-balanced-streamed-20261008-r1/"
    "cpu-encode/prepared-inputs.json"
)
PREPARED_SHA = "49f32a217a22482e8261e19a4c6c216fc8aec934613351164fb1b65d5e0810eb"
NEW_FILES = (
    "scripts/verify_onboard_navigation_closed_cpu_inputs.py",
    "tests/test_verify_onboard_navigation_closed_cpu_inputs.py",
)
FALSE_CLAIMS = (
    "model_loaded",
    "GPU_used",
    "optimizer_created",
    "forward_backward_performed",
    "training_budget_changed",
    "prior_memory_error_cause_proven",
    "memory_benefit_proven",
    "quality_claim",
    "candidate_promoted",
    "execution_authority",
    "transport_created",
    "mission_created",
    "physical_performance_claimed",
    "hil_real_evidence",
    "outdoor_gnss_evidence",
    "sim_real_parity_verified",
    "final_release_allowed",
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def read_small(path, digest):
    path = Path(path)
    path = (path if path.is_absolute() else ROOT / path).resolve(strict=True)
    if not path.is_relative_to(ROOT) or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("CPU admission document escaped root or exceeded its cap")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("CPU admission SHA changed")
    return raw


def admitted_dependencies():
    read_small(PROFILE_SOURCE, PROFILE_SOURCE_SHA)
    from scripts import profile_onboard_navigation_closed_samples as profile

    profile.bootstrap(ROOT / PROFILE_PLAN, PROFILE_PLAN_SHA)
    return profile, profile.legacy_api()


def build_plan():
    profile, legacy = admitted_dependencies()
    retained = legacy.strict_json(read_small(PROFILE_PLAN, PROFILE_PLAN_SHA))
    prior = legacy.strict_json(read_small(PROFILE_REPORT, PROFILE_REPORT_SHA))
    if (
        prior["success"] is not True
        or prior["official_processor_used"] is not True
        or prior["contract"] != profile.CONTRACT
        or prior["plan_sha256"] != PROFILE_PLAN_SHA
        or prior["source_bindings_unchanged"] is not True
        or canonical(prior["source_bindings_before"]) != canonical(prior["source_bindings_after"])
        or len(prior["rows"]) != 10
        or any(prior[key] is not False for key in profile.FALSE_CLAIMS)
    ):
        raise ValueError("The sealed five-sample CPU profile must have actually succeeded")
    old = legacy.read_bound(retained["legacy_plan"])
    ids = old["diagnostics"]["sample_ids"]
    if len(ids) != 86 or len(set(ids)) != 86:
        raise ValueError("The original 86 unique identities are required")
    return {
        "schema_version": 1,
        "contract": CONTRACT + "-plan",
        "artifact_root": str(ROOT),
        "profile_plan": legacy.record(ROOT / PROFILE_PLAN),
        "profile_report": legacy.record(ROOT / PROFILE_REPORT),
        "prepared_reference": retained["prepared_inputs"],
        "sample_ids": ids,
        "source_files": [legacy.record(ROOT / name) for name in NEW_FILES],
        "claims": dict.fromkeys(FALSE_CLAIMS, False),
        "semantic_admission_scope": "inherited_verified_old_all86_source_session_once",
        "encoding_residency": "one_closed_verified_sample_without_tensor_cache",
    }


def admit_plan(path, digest):
    raw = read_small(path, digest)  # No repo dependency code runs before this SHA gate.
    bootstrap_plan = json.loads(raw)
    for name in NEW_FILES:
        declared = next(
            row
            for row in bootstrap_plan["source_files"]
            if Path(row["path"]).resolve() == (ROOT / name).resolve()
        )
        if (
            type(declared["bytes"]) is not int
            or len(read_small(declared["path"], declared["sha256"])) != declared["bytes"]
        ):
            raise ValueError("NEW CPU source bootstrap size changed")
    profile, legacy = admitted_dependencies()
    plan = legacy.strict_json(raw)
    if canonical(plan) != canonical(build_plan()):
        raise ValueError("CPU plan differs from the exact all86 experiment")
    retained, old, budget, before = profile.admit_plan(ROOT / PROFILE_PLAN, PROFILE_PLAN_SHA)
    inventory = {row["path"]: row for row in before}
    for row in [plan["profile_report"], *plan["source_files"], legacy.record(path)]:
        key = str(legacy.binding_path(row, allow_external=True))
        normalized = {**row, "path": key}
        if key in inventory and inventory[key] != normalized:
            raise ValueError("Conflicting CPU/retained source binding")
        inventory[key] = normalized
    return plan, retained, old, budget, [inventory[key] for key in sorted(inventory)]


def validate_success_report(
    report, prepared, legacy_plan, *, expected_metadata=None, expected_plan_sha256=None
):
    """Pure-stdlib consumer gate AFTER caller SHA-admits report, plan and tables.

    The caller must additionally recheck these recorded source bindings against
    its admitted inventory/current files. A report digest is not authentication
    or training/flight authority. No CPU processing is performed by this gate.
    """
    if expected_metadata is None:
        expected_metadata = json.loads(read_small(PREPARED_PATH, PREPARED_SHA))
    plan_binding = report["plan"]
    if set(plan_binding) != {"path", "bytes", "sha256"} or type(plan_binding["bytes"]) is not int:
        raise ValueError("An exact bound NEW CPU plan is required")
    if len(read_small(plan_binding["path"], plan_binding["sha256"])) != plan_binding["bytes"]:
        raise ValueError("NEW CPU plan size changed")
    if expected_plan_sha256 is None:
        expected_plan_sha256 = plan_binding["sha256"]
    if plan_binding["sha256"] != expected_plan_sha256:
        raise ValueError("NEW CPU plan SHA differs from the consumer binding")
    ids = legacy_plan["diagnostics"]["sample_ids"]
    if (
        type(report["schema_version"]) is not int
        or report["schema_version"] != 1
        or report["contract"] != CONTRACT
        or report["plan_sha256"] != expected_plan_sha256
        or report["success"] is not True
        or report["official_processor_used"] is not True
        or report["all86_metadata_equal"] is not True
        or report["source_bindings_unchanged"] is not True
        or report["CUDA_uninitialized_before"] is not True
        or report["CUDA_uninitialized_after"] is not True
        or type(report["sample_count"]) is not int
        or report["sample_count"] != 86
        or type(report["source_validation_sessions_verified"]) is not int
        or report["source_validation_sessions_verified"] != 86
        or len(ids) != 86
        or len(set(ids)) != 86
        or canonical(report["sample_ids"]) != canonical(ids)
        or canonical(report["admitted_sample_ids"]) != canonical(ids)
        or type(prepared) is not dict
        or set(prepared) != set(ids)
        or canonical(prepared) != canonical(expected_metadata)
        or not report["source_bindings_before"]
        or canonical(report["source_bindings_before"]) != canonical(report["source_bindings_after"])
        or any(report[key] is not False for key in FALSE_CLAIMS)
    ):
        raise ValueError("Closed CPU86 success/identity/source/metadata gate failed")
    binding = report["prepared_inputs"]
    payload = canonical(prepared) + b"\n"
    if (
        set(binding) != {"path", "bytes", "sha256"}
        or type(binding["bytes"]) is not int
        or binding["bytes"] != len(payload)
        or binding["sha256"] != hashlib.sha256(payload).hexdigest()
    ):
        raise ValueError("Closed CPU prepared artifact identity differs")
    return report


def encode_metadata(sample, root, *, processor, torch, budget, reference, expected_metadata):
    from scripts import onboard_navigation_closed_sample_encoding as closed

    sessions = []

    def factory(artifact_root):
        session = closed.ClosedNavigationSourceValidationSession(artifact_root)
        sessions.append(session)
        return session

    value = closed.encode_closed_navigation_sample(
        sample,
        root,
        processor=processor,
        torch_module=torch,
        budget=budget,
        prefix_reference=reference,
        expected_metadata=expected_metadata,
        session_factory=factory,
    )
    try:
        if len(sessions) != 1 or sessions[0].verified is not True or sessions[0]._active:
            raise ValueError(
                "A single normally closed verified session is required before admission"
            )
        metadata = {
            key: item for key, item in value.items() if key not in {"prefix", "full", "labels"}
        }
        if canonical(metadata) != canonical(expected_metadata):
            raise ValueError("Complete CPU metadata differs from the retained prepared row")
        return json.loads(canonical(metadata))
    finally:
        value = None
        sessions.clear()


def write_json(path, value):
    with Path(path).open("xb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def run(args):
    output = Path(args.output_dir).absolute().resolve(strict=False)
    if output.parent != (ROOT / "artifacts/diagnostics").resolve(strict=True):
        raise ValueError("Use a fresh direct artifacts/diagnostics directory")
    output.mkdir()
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        "pid": os.getpid(),
        "plan_sha256": args.plan_sha256,
        "success": False,
        "sample_count": 0,
        "admitted_sample_ids": [],
        "source_validation_sessions_verified": 0,
        "all86_metadata_equal": False,
        "observations": [],
        "source_bindings_before": None,
        "source_bindings_after": None,
        "source_bindings_unchanged": False,
        **dict.fromkeys(FALSE_CLAIMS, False),
    }
    prepared = {}
    try:
        plan, retained, old_plan, budget, before = admit_plan(args.plan, args.plan_sha256)
        profile, legacy = admitted_dependencies()
        from scripts import onboard_navigation_host_memory as memory

        report["plan"] = legacy.record(args.plan)
        report.update(
            sample_ids=plan["sample_ids"],
            source_bindings_before=before,
            semantic_admission_scope=plan["semantic_admission_scope"],
            encoding_residency=plan["encoding_residency"],
        )

        def observe(event, **fields):
            row = {"stage": event, "host_memory": memory.snapshot(), **fields}
            report["observations"].append(row)
            legacy.emit("closed_cpu86_progress", **row)

        with legacy.repository_imports():
            observe("semantic_admission_started")
            old, samples, _targets, _schedule, _pilot = legacy.semantic_admission(old_plan)
            expected = legacy.validate_prepared_inputs(
                legacy.read_bound(plan["prepared_reference"]), old_plan, budget
            )
            if [sample.sample_id for sample in samples] != plan["sample_ids"]:
                raise ValueError("Semantic samples must cover all86 in exact source order")
            references = {
                row["sample_id"]: row
                for row in legacy.read_bound(old_plan["cpu_prefix_report"])["samples"]
            }
            observe("semantic_admission_finished")
            torch = profile.import_cpu_torch()
            report["CUDA_uninitialized_before"] = True
            from scripts.verify_onboard_navigation_actor_inputs import official_processor

            observe("official_processor_load_started")
            processor = official_processor(Path(old["base"]["snapshot"]), budget)
            if torch.cuda.is_initialized():
                raise ValueError("CPU processor loading initialized CUDA")
            report["official_processor_used"] = True
            observe("official_processor_load_finished")
            for sample in samples:
                sample_id = sample.sample_id
                observe("sample_started", sample_id=sample_id, admitted_count=len(prepared))
                start = time.perf_counter()
                metadata = encode_metadata(
                    sample,
                    ROOT,
                    processor=processor,
                    torch=torch,
                    budget=budget,
                    reference=references[sample_id],
                    expected_metadata=expected[sample_id],
                )
                if torch.cuda.is_initialized():
                    raise ValueError("CPU sample encoding initialized CUDA")
                prepared[sample_id] = metadata
                report["admitted_sample_ids"].append(sample_id)
                report["sample_count"] = report["source_validation_sessions_verified"] = len(
                    prepared
                )
                observe(
                    "sample_admitted",
                    sample_id=sample_id,
                    admitted_count=len(prepared),
                    elapsed_s=time.perf_counter() - start,
                )
            legacy.validate_prepared_inputs(prepared, old_plan, budget)
            if canonical(prepared) != canonical(expected):
                raise ValueError("Complete all86 metadata differs from the retained CPU table")
            report["all86_metadata_equal"] = True
            report["CUDA_uninitialized_after"] = not torch.cuda.is_initialized()
            if not report["CUDA_uninitialized_after"]:
                raise ValueError("CPU acceptance initialized CUDA")
        observe("source_recheck_started")
        after = [legacy.record(row["path"]) for row in before]
        report["source_bindings_after"] = after
        if canonical(before) != canonical(after):
            raise ValueError("Sources changed during CPU acceptance")
        report["source_bindings_unchanged"] = True
        observe("source_recheck_finished")
        write_json(output / "prepared-inputs.json", prepared)
        report["prepared_inputs"] = legacy.record(output / "prepared-inputs.json")
        report["success"] = True
        validate_success_report(
            report,
            prepared,
            old_plan,
            expected_metadata=expected,
            expected_plan_sha256=args.plan_sha256,
        )
    except Exception as error:
        traceback.print_exc()
        report["success"] = False
        report["failure"] = {"type": type(error).__name__, "message": str(error)}
        write_json(output / "failure.json", report["failure"])
    write_json(output / "report.json", report)
    print(
        json.dumps(
            {
                "event": "closed_cpu86_finished",
                "native_exit_code": 0 if report["success"] else 1,
                "report_path": str(output / "report.json"),
            }
        ),
        flush=True,
    )
    return 0 if report["success"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-plan", action="store_true")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if args.emit_plan:
        if any(value is not None for value in (args.plan, args.plan_sha256, args.output_dir)):
            parser.error("--emit-plan cannot run CPU acceptance")
        sys.stdout.buffer.write(canonical(build_plan()) + b"\n")
        return 0
    if any(value is None for value in (args.plan, args.plan_sha256, args.output_dir)):
        parser.error("--plan, --plan-sha256 and --output-dir are required")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
