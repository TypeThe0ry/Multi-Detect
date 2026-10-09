"""Paired, bounded official-processor CPU lifecycle profiling; never training.

Stages are inclusive wall-time observations with boundary host snapshots, not
exclusive timings or continuous memory sampling. This experiment cannot prove
the prior MemoryError's cause, a memory benefit, or all86 CPU acceptance.
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

CONTRACT = "multidetect.onboard-navigation-closed-samples-profile-v1"
FROZEN_SHA = "eac75d2def168f5daa44b3bba713456c71f80d00a1b91933b8075a1dd32c20cc"
RUN = "artifacts/models/onboard-navigation-balanced-streamed-20261008-r1"
BOUND = {
    "legacy_plan": (
        "configs/training/onboard-navigation-streamed-continuation-20261008-r1.json",
        FROZEN_SHA,
    ),
    "failure_evidence": (
        "docs/evidence/onboard-navigation-streamed-training-failure-20261008-r1.json",
        "0d7575dc7cf39ae363087e28ed06f09e4dd879432f26e1e18d49f7bf6de2bb69",
    ),
    "failure_report": (
        f"{RUN}/train/report.json",
        "a6085b69948341e61987bdbd6ad80c8ab7ad9dfd0c4e95fdb9b3b9df70f68e47",
    ),
    "failure_journal": (
        f"{RUN}/train/training-events.jsonl",
        "2f247580e4c010fa544bf9c607bb61288a57f727406572eaae2064fea0c09d68",
    ),
    "completion": (
        f"{RUN}/completion.json",
        "10d468b52dc945f7dc16e2f212dd95ad409554a2bb2648139f1cfd79b4866565",
    ),
    "prepared_inputs": (
        f"{RUN}/cpu-encode/prepared-inputs.json",
        "49f32a217a22482e8261e19a4c6c216fc8aec934613351164fb1b65d5e0810eb",
    ),
}
NEW_FILES = (
    "scripts/onboard_navigation_closed_sample_encoding.py",
    "tests/test_onboard_navigation_closed_sample_encoding.py",
    "scripts/onboard_navigation_host_memory.py",
    "tests/test_onboard_navigation_host_memory.py",
    "scripts/profile_onboard_navigation_closed_samples.py",
    "tests/test_profile_onboard_navigation_closed_samples.py",
)
FALSE_CLAIMS = (
    "model_loaded",
    "GPU_used",
    "optimizer_created",
    "forward_backward_performed",
    "all86_reencoded",
    "prior_memory_error_cause_proven",
    "memory_benefit_proven",
    "quality_claim",
    "candidate_promoted",
    "execution_authority",
    "final_release_allowed",
    "training_budget_changed",
    "transport_created",
)


def legacy_api():
    from scripts import train_onboard_navigation_streamed

    return train_onboard_navigation_streamed


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def bootstrap(path=None, expected_sha=None):
    """Admit small dependency bytes before importing their Python code."""

    def read(path, digest):
        path = Path(path)
        path = (path if path.is_absolute() else ROOT / path).resolve(strict=True)
        if not path.is_relative_to(ROOT) or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Bootstrap input escaped root or exceeded its small-file cap")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError(f"Bootstrap SHA changed: {path}")
        return raw

    old = json.loads(read(*BOUND["legacy_plan"]))
    names = {
        "scripts/train_onboard_navigation_streamed.py",
        "scripts/onboard_navigation_balanced_journal.py",
    }
    declarations = {str(Path(row["path"]).resolve()): row for row in old["source_files"]}
    if path is not None:
        plan = json.loads(read(path, expected_sha))
        for row in plan["source_files"]:
            source = Path(row["path"])
            key = str((source if source.is_absolute() else ROOT / source).resolve())
            if key in declarations and declarations[key] != row:
                raise ValueError("Conflicting bootstrap dependency binding")
            declarations[key] = row
        names |= {
            "scripts/onboard_navigation_host_memory.py",
            "scripts/onboard_navigation_closed_sample_encoding.py",
        }
    for name in names:
        row = declarations[str((ROOT / name).resolve())]
        if type(row["bytes"]) is not int or len(read(row["path"], row["sha256"])) != row["bytes"]:
            raise ValueError("Bootstrap dependency size changed")


def select_samples(legacy_plan, schedule, failure, completion, journal):
    counts = [
        journal[f"optimizer_steps_{stage}"]
        for stage in ("attempted", "returned", "synchronized", "completed")
    ]
    if (
        counts != [13] * 4
        or any(type(value) is not int for value in counts)
        or journal["event_count"] != 52
        or journal["complete"] is not False
        or journal["optimizer_update_uncertain"] is not False
        or canonical(failure["journal"]) != canonical(journal)
        or failure["failure_type"] != "MemoryError"
        or failure["phase"] != "train"
        or failure["plan_sha256"] != FROZEN_SHA
        or completion["plan_sha256"] != FROZEN_SHA
        or type(completion["native_exit_code"]) is not int
        or completion["native_exit_code"] != 1
        or [(row["phase"], row["native_exit_code"]) for row in completion["phases"]]
        != [("cpu-encode", 0), ("baseline", 0), ("train", 1)]
    ):
        raise ValueError("Frozen failure must prove exactly 13 completed draws before draw14")
    draw = schedule["draws"][13]
    if type(draw["draw_index"]) is not int or draw["draw_index"] != 14:
        raise ValueError("The next scheduled draw is not draw14")
    probes = legacy_plan["diagnostics"]["smoke_probes"]
    if len(probes) != 4:
        raise ValueError("Exactly the four original smoke probes are required")
    result = [{"sample_id": draw["sample_id"], "roles": ["failed_draw14"]}]
    for probe in probes:
        role = "original_probe_" + probe["action_class"]
        existing = next((row for row in result if row["sample_id"] == probe["sample_id"]), None)
        if existing is None:
            result.append({"sample_id": probe["sample_id"], "roles": [role]})
        else:
            existing["roles"].append(role)
    return result


def build_plan():
    bootstrap()
    legacy = legacy_api()
    bindings = {name: legacy.record(ROOT / path) for name, (path, _) in BOUND.items()}
    if any(bindings[name]["sha256"] != digest for name, (_, digest) in BOUND.items()):
        raise ValueError("Frozen historical profile inputs changed")
    old = legacy.read_bound(bindings["legacy_plan"])
    schedule = legacy.read_bound(old["schedule"])
    from scripts import onboard_navigation_balanced_journal as journal

    observed = journal.verify_journal(
        legacy.binding_path(bindings["failure_journal"]), schedule["draws"]
    )
    selected = select_samples(
        old,
        schedule,
        legacy.read_bound(bindings["failure_report"]),
        legacy.read_bound(bindings["completion"]),
        observed,
    )
    return {
        "schema_version": 1,
        "contract": CONTRACT + "-plan",
        "artifact_root": str(ROOT),
        **bindings,
        "source_files": [legacy.record(ROOT / name) for name in NEW_FILES],
        "selection": selected,
        "paired_order": ["legacy_owned", "closed_sample"],
        "claims": dict.fromkeys(FALSE_CLAIMS, False),
        "timing_scope": "inclusive_nested_wall_time_not_exclusive_or_additive",
        "memory_scope": "boundary_snapshots_and_process_lifetime_OS_peaks_not_continuous",
    }


def admit_plan(path, expected_sha):
    legacy = legacy_api()
    legacy.require_sha(expected_sha)
    path = Path(path).resolve(strict=True)
    if not path.is_relative_to(ROOT) or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Profile plan must be bounded and within this repository")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("Profile plan SHA changed")
    plan = legacy.strict_json(raw)
    if canonical(plan) != canonical(build_plan()):
        raise ValueError("Profile plan differs from exact frozen experiment")
    old_plan, budget, before = legacy.admit_plan(
        legacy.binding_path(plan["legacy_plan"]), FROZEN_SHA
    )
    # Conflicting declarations are errors; never overwrite a frozen old binding.
    inventory = {row["path"]: row for row in before}
    for row in [*[plan[name] for name in BOUND], *plan["source_files"], legacy.record(path)]:
        normalized = {**row, "path": str(legacy.binding_path(row, allow_external=True))}
        if normalized["path"] in inventory and inventory[normalized["path"]] != normalized:
            raise ValueError("Conflicting historical/profile source declaration")
        inventory[normalized["path"]] = normalized
    return plan, old_plan, budget, [inventory[key] for key in sorted(inventory)]


def timed_session_factory(observe, clock=time.perf_counter):
    from scripts.onboard_navigation_closed_sample_encoding import (
        ClosedNavigationSourceValidationSession,
    )

    class TimedClosedSession(ClosedNavigationSourceValidationSession):
        def timed(self, name, operation, *, cleanup=False, body_error=None):
            observation_error = None
            try:
                observe(name, "started")
            except BaseException as error:
                if not cleanup:
                    raise
                observation_error = error
            start = clock()
            try:
                result = operation()
            except BaseException:
                try:
                    observe(name, "finished", elapsed_s=clock() - start, verified=self.verified)
                except BaseException:
                    pass
                raise
            if observation_error is not None:
                if body_error is None:
                    raise observation_error
                body_error.add_note(f"Exit start observation also failed: {observation_error!r}")
            try:
                observe(name, "finished", elapsed_s=clock() - start, verified=self.verified)
            except BaseException as error:
                if body_error is None:
                    raise
                body_error.add_note(f"Exit finish observation also failed: {error!r}")
            return result

        def prime(self, samples):
            method = super().prime
            return self.timed("session_prime", lambda: method(samples))

        def validate_sample(self, sample):
            method = super().validate_sample
            return self.timed("session_validate_sample", lambda: method(sample))

        def __exit__(self, error_type, error, traceback):
            method = super().__exit__
            return self.timed(
                "session_exit",
                lambda: method(error_type, error, traceback),
                cleanup=True,
                body_error=error,
            )

    return TimedClosedSession


def import_cpu_torch():
    import torch

    if torch.cuda.is_initialized():
        raise ValueError("CPU profiling requires CUDA to remain uninitialized")
    return torch


def paired_encodings(
    selection,
    samples,
    root,
    processor,
    torch,
    budget,
    references,
    prepared,
    stage,
    observe,
    *,
    rows=None,
):
    from scripts import onboard_navigation_balanced_encoding as old_encoder
    from scripts import onboard_navigation_closed_sample_encoding as closed

    legacy = legacy_api()
    by_id = {sample.sample_id: sample for sample in samples}
    rows = [] if rows is None else rows
    for selected in selection:
        sample_id = selected["sample_id"]
        sample = by_id[sample_id]
        common = dict(
            processor=processor,
            torch_module=torch,
            budget=budget,
            prefix_reference=references[sample_id],
        )
        for mode in ("legacy_owned", "closed_sample"):
            label = {"sample_id": sample_id, "mode": mode}

            def encode(mode=mode, sample=sample, common=common, sample_id=sample_id, label=label):
                if mode == "legacy_owned":
                    value = old_encoder.encode_navigation_sample(
                        sample, root, session=None, **common
                    )
                else:
                    value = closed.encode_closed_navigation_sample(
                        sample,
                        root,
                        expected_metadata=prepared[sample_id],
                        session_factory=timed_session_factory(
                            lambda *a, **k: observe(*a, **k, **label)
                        ),
                        **common,
                    )
                try:
                    metadata = legacy.metadata(value)
                    if canonical(metadata) != canonical(prepared[sample_id]):
                        raise ValueError("Full prepared metadata parity failed")
                    return metadata
                finally:
                    del value  # Never overlap a previous result with the next encoding.

            metadata = stage("sample_encoding", encode, **label)
            rows.append(
                {
                    **selected,
                    "mode": mode,
                    "metadata": metadata,
                    "full_metadata_equal": True,
                    "closed_session_verified_before_admission": mode == "closed_sample",
                }
            )
    return rows


def run_profile(args):
    legacy = None
    output = Path(args.output_dir).absolute().resolve(strict=False)
    if output.parent != (ROOT / "artifacts/diagnostics").resolve(strict=True):
        raise ValueError("Use a fresh direct artifacts/diagnostics output directory")
    output.mkdir()  # Create-only: an existing run is never modified.
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        "pid": os.getpid(),
        "plan_sha256": args.plan_sha256,
        "success": False,
        "rows": [],
        "observations": [],
        "source_bindings_before": None,
        "source_bindings_after": None,
        "source_bindings_unchanged": False,
        **dict.fromkeys(FALSE_CLAIMS, False),
    }
    state = {"torch": None}

    def observe(stage, boundary, **fields):
        initialized = None if state["torch"] is None else state["torch"].cuda.is_initialized()
        row = {
            "stage": stage,
            "boundary": boundary,
            "CUDA_initialized": initialized,
            "host_memory": memory.snapshot(),
            **fields,
        }
        report["observations"].append(row)
        legacy.emit("closed_sample_profile_progress", **row)
        if initialized is True:
            raise ValueError("CPU profiling initialized CUDA")

    def stage(name, operation, **fields):
        observe(name, "started", **fields)
        start = time.perf_counter()
        try:
            result = operation()
        except BaseException:
            try:
                observe(name, "finished", elapsed_s=time.perf_counter() - start, **fields)
            except BaseException:
                pass
            raise
        else:
            observe(name, "finished", elapsed_s=time.perf_counter() - start, **fields)
        return result

    try:
        bootstrap(args.plan, args.plan_sha256)
        legacy = legacy_api()
        from scripts import onboard_navigation_host_memory as memory

        plan, old_plan, budget, before = stage(
            "plan_admission", lambda: admit_plan(args.plan, args.plan_sha256)
        )
        report["source_bindings_before"] = before
        report.update(
            selection=plan["selection"],
            timing_scope=plan["timing_scope"],
            memory_scope=plan["memory_scope"],
        )
        with legacy.repository_imports():
            old, samples, _targets, _schedule, _pilot = stage(
                "semantic_admission", lambda: legacy.semantic_admission(old_plan)
            )
            prepared = stage(
                "prepared_admission",
                lambda: legacy.validate_prepared_inputs(
                    legacy.read_bound(plan["prepared_inputs"]), old_plan, budget
                ),
            )
            references = {
                row["sample_id"]: row
                for row in legacy.read_bound(old_plan["cpu_prefix_report"])["samples"]
            }

            def torch_import():
                state["torch"] = import_cpu_torch()

            stage("torch_import", torch_import)
            report["CUDA_uninitialized_before_processor"] = True
            from scripts.verify_onboard_navigation_actor_inputs import official_processor

            processor = stage(
                "official_processor_load",
                lambda: official_processor(Path(old["base"]["snapshot"]), budget),
            )
            report["official_processor_used"] = True
            paired_encodings(
                plan["selection"],
                samples,
                ROOT,
                processor,
                state["torch"],
                budget,
                references,
                prepared,
                stage,
                observe,
                rows=report["rows"],
            )
            observe("paired_encoding_complete", "finished")
            report["CUDA_uninitialized_after_encoding"] = True
        after = stage("source_recheck", lambda: [legacy.record(row["path"]) for row in before])
        report["source_bindings_after"] = after
        if canonical(after) != canonical(before):
            raise ValueError("Source bytes changed during CPU profiling")
        report["source_bindings_unchanged"] = report["success"] = True
    except Exception as error:
        traceback.print_exc()
        report["failure"] = {"type": type(error).__name__, "message": str(error)}
        with (output / "failure.json").open("xb") as stream:
            stream.write(canonical(report["failure"]) + b"\n")
    with (output / "report.json").open("xb") as stream:
        stream.write(canonical(report) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(
        json.dumps(
            {
                "event": "closed_sample_profile_finished",
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
            parser.error("--emit-plan cannot run a profile")
        sys.stdout.buffer.write(canonical(build_plan()) + b"\n")
        return 0
    if any(value is None for value in (args.plan, args.plan_sha256, args.output_dir)):
        parser.error("--plan, --plan-sha256 and --output-dir are required")
    return run_profile(args)


if __name__ == "__main__":
    raise SystemExit(main())
