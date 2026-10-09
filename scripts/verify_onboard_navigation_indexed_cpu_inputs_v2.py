"""CPU122 re-admission with process-local optional Arrow import isolation.

The sealed r1 failure remains failed. This NEW worker must reproduce every
saved prefix/full metadata row, with the original budgets and CPU threads=1.
No model, weights, network, optimizer, dataset edits, or flight authority.
Only this scope's finder is removed; legitimate dependency finders survive.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.abc
import importlib.metadata
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.onboard-navigation-indexed-cpu122-inputs-v2"
NEW_FILES = (
    "scripts/verify_onboard_navigation_indexed_cpu_inputs_v2.py",
    "tests/test_verify_onboard_navigation_indexed_cpu_inputs_v2.py",
)
R1_PINS = {
    "scripts/verify_onboard_navigation_indexed_cpu_inputs.py": (
        37413,
        "566e576a52546b34380526c17bb406de47a4efca2762c299cd28275521ac0373",
    ),
    "tests/test_verify_onboard_navigation_indexed_cpu_inputs.py": (
        25612,
        "719ed0f38b5bd2c073475ba34185cbe64d57cb50a310c0d06b850203c4ed7c3c",
    ),
}
FAILURE = {
    "path": "docs/evidence/onboard-navigation-indexed-cpu122-failure-20261008-r1.json",
    "bytes": 5356,
    "sha256": "fbd2aeaefdaf8ba41203ff193a487e66d463471e480e883df28ae1ad6f7b4373",
}
PRIOR_PATHS = {
    "plan": "configs/training/onboard-navigation-indexed-cpu122-20261008-r1.json",
    "report": "artifacts/diagnostics/onboard-navigation-indexed-cpu122-20261008-r1/report.json",
    "prefix_reference": (
        "artifacts/diagnostics/onboard-navigation-indexed-cpu122-20261008-r1/prefix-reference.json"
    ),
    "prepared_inputs": (
        "artifacts/diagnostics/onboard-navigation-indexed-cpu122-20261008-r1/prepared-inputs.json"
    ),
}
ISOLATION_POLICY = {
    "mechanism": "process_local_MetaPathFinder_ImportError_for_pyarrow_and_children",
    "install_before_heavy_imports": True,
    "deny_preloaded_pyarrow_without_module_deletion": True,
    "datasets_forbidden_before_and_after": True,
    "pyarrow_absent_before_and_after": True,
    "remove_only_own_finder_by_identity": True,
    "preserve_prior_finder_order_and_identity": True,
    "retain_and_record_legitimate_dependency_finders": True,
    "site_packages_modified": False,
    "claim_exhaustive_native_file_io_audit": False,
}
OPTIONAL_FILES = {
    "transformers": (
        "transformers/models/auto/auto_factory.py",
        "transformers/generation/utils.py",
        "transformers/generation/candidate_generator.py",
    ),
    "scikit-learn": (
        "sklearn/__init__.py",
        "sklearn/metrics/_ranking.py",
        "sklearn/utils/validation.py",
        "sklearn/utils/fixes.py",
    ),
    "pandas": (
        "pandas/__init__.py",
        "pandas/compat/__init__.py",
        "pandas/compat/pyarrow.py",
    ),
}


def sealed_r1():
    for name, (size, digest) in R1_PINS.items():
        path = ROOT / name
        if path.stat().st_size != size or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Sealed r1 source/test identity differs")
    spec = importlib.util.spec_from_file_location(
        "_sealed_indexed_cpu122_r1", ROOT / next(iter(R1_PINS))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = sealed_r1()


def module_roots(names):
    return sorted(name for name in sys.modules if name.split(".")[0] in names)


def finder_description(finder):
    value = finder if isinstance(finder, type) else type(finder)
    return {"module": value.__module__, "class": value.__qualname__}


class OptionalArrowIsolation(importlib.abc.MetaPathFinder):
    """Deny optional Arrow through the standard ImportError fallback only."""

    def __init__(self, observation):
        self.observation = observation
        self.before = ()

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "pyarrow" or fullname.startswith("pyarrow."):
            self.observation["blocked_imports"].append(fullname)
            raise ImportError(
                f"Optional Arrow isolated by explicit CPU122 plan: {fullname}", name=fullname
            )
        return None

    def __enter__(self):
        self.observation.update(
            installed=False,
            own_finder_removed=False,
            prior_finders_preserved=False,
            blocked_imports=[],
            pyarrow_modules_before=module_roots({"pyarrow"}),
            datasets_modules_before=module_roots({"datasets"}),
        )
        if (
            self.observation["pyarrow_modules_before"]
            or self.observation["datasets_modules_before"]
        ):
            raise ValueError("Fresh worker required; forbidden optional modules are preloaded")
        self.before = tuple(sys.meta_path)
        self.observation["prior_finders"] = [finder_description(value) for value in self.before]
        sys.meta_path.insert(0, self)
        self.observation["installed"] = True
        return self

    def __exit__(self, error_type, error, traceback):
        count = sum(value is self for value in sys.meta_path)
        # Never clear other finders or sys.modules to make a gate appear successful.
        sys.meta_path[:] = [value for value in sys.meta_path if value is not self]
        prior_ids = {id(value) for value in self.before}
        retained = tuple(value for value in sys.meta_path if id(value) in prior_ids)
        preserved = len(retained) == len(self.before) and all(
            left is right for left, right in zip(retained, self.before, strict=True)
        )
        self.observation.update(
            own_finder_occurrences_at_exit=count,
            own_finder_removed=not any(value is self for value in sys.meta_path),
            prior_finders_preserved=preserved,
            dependency_finders_added=[
                finder_description(value) for value in sys.meta_path if id(value) not in prior_ids
            ],
            pyarrow_modules_after=module_roots({"pyarrow"}),
            datasets_modules_after=module_roots({"datasets"}),
        )
        if count != 1 or not preserved or module_roots({"pyarrow", "datasets"}):
            issue = ValueError("Optional Arrow scope cleanup/import identity gate failed")
            if error is not None:
                error.add_note(str(issue))
            else:
                raise issue
        return False


def optional_runtime(controls):
    """Narrow primary import-chain sources; not all runtime/transitive code."""
    files, versions, modules = [], {}, {}
    for name, entries in OPTIONAL_FILES.items():
        dist = importlib.metadata.distribution(name)
        versions[name] = dist.version
        available = {str(value).replace("\\", "/") for value in dist.files or ()}
        root = Path(dist.locate_file("")).resolve()
        for relative in entries:
            path = Path(dist.locate_file(relative)).resolve()
            if relative not in available or not path.is_relative_to(root):
                raise ValueError("Optional import-chain origin is not the installed distribution")
            files.append(base.runtime_record(path, controls))
            if relative in {"sklearn/__init__.py", "pandas/__init__.py"}:
                modules[relative.split("/")[0]] = str(path)
    return {
        "package_versions": versions,
        "source_files": files,
        "module_origins": modules,
        "scope": "narrow_primary_optional_import_chain_not_exhaustive_native_or_transitive_audit",
    }


def validate_optional_origins(runtime):
    actual = {}
    for name, path in runtime["module_origins"].items():
        module = sys.modules.get(name)
        if module is not None:
            if Path(module.__file__).resolve() != Path(path).resolve():
                raise ValueError("Actual optional Python module origin differs")
            package = "scikit-learn" if name == "sklearn" else name
            if module.__version__ != runtime["package_versions"][package]:
                raise ValueError("Actual optional Python module version differs")
            actual[name] = str(Path(module.__file__).resolve())
    return actual


def read_prior(controls):
    receipt = controls.parse_json(controls.read_bound(FAILURE))
    execution, observations = receipt["actual_execution"], receipt["observations"]
    if (
        receipt["contract"] != "multidetect.onboard-navigation-indexed-cpu122-failure-evidence-v1"
        or type(execution["actual_native_exit_code"]) is not int
        or execution["actual_native_exit_code"] != 1
        or execution["processes_terminated"] is not True
        or type(execution["optimizer_attempts"]) is not int
        or execution["optimizer_attempts"] != 0
        or execution["model_weights_or_GPU_used"] is not False
        or observations["training_dataset_cpu_admitted"] is not False
        or any(value is not False for value in receipt["claims"].values())
    ):
        raise ValueError("Bound closed failed CPU122 attempt is required, not a prior admission")
    declared = {row["path"]: row for row in controls.merge_bindings(receipt["bindings"])}
    for row in declared.values():
        controls.read_bound(row)
    bindings = {name: declared[path] for name, path in PRIOR_PATHS.items()}
    for name, (size, digest) in R1_PINS.items():
        if declared.get(name) != {"path": name, "bytes": size, "sha256": digest}:
            raise ValueError("Failure receipt must bind exact r1 source/test")
    values = {name: controls.parse_json(controls.read_bound(row)) for name, row in bindings.items()}
    report, plan = values["report"], values["plan"]
    if (
        report["contract"] != base.CONTRACT
        or type(report["native_exit_code"]) is not int
        or report["native_exit_code"] != 1
        or report["success"] is not False
        or report["training_dataset_cpu_admitted"] is not False
        or report["partial_failures"]
        != [
            {
                "type": "ValueError",
                "message": "Forbidden module imported in this phase",
                "admitted_count": 122,
            }
        ]
        or any(
            type(report[key]) is not int or report[key] != 122
            for key in ("sample_count", "source_validation_sessions_verified")
        )
        or report["admitted_sample_ids"] != plan["sample_ids"]
        or report["source_bindings_unchanged"] is not True
        or base.canonical(report["source_bindings_before"]) != base.canonical(plan["sources"])
        or base.canonical(report["source_bindings_after"]) != base.canonical(plan["sources"])
        or report["CUDA_uninitialized_before"] is not True
        or report["CUDA_uninitialized_after"] is not True
        or report["official_processor_used"] is not True
        or any(report[key] is not False for key in base.FALSE_CLAIMS)
        or controls.normalize(report["plan"]) != bindings["plan"]
        or controls.normalize(report["prefix_reference"]) != bindings["prefix_reference"]
        or controls.normalize(report["prepared_inputs"]) != bindings["prepared_inputs"]
        or values["prefix_reference"]["reference_kind"] != plan["reference_kind"]
    ):
        raise ValueError("Only the sealed all122 final-import failure may supply the reference")
    return {"receipt": FAILURE, **bindings}, values, list(declared.values())


def build_plan(output_dir):
    # Reuse frozen lower controls; never replace r1 globals or its run function.
    plan = base.build_plan(output_dir)
    controls = base.bootstrap()
    prior, values, declared = read_prior(controls)
    if base.canonical(plan) != base.canonical({**values["plan"], "output_dir": plan["output_dir"]}):
        raise ValueError("Original CPU122 dataset/budget/runtime plan changed")
    api = base.dependencies(controls)
    samples = base.admit_rows(
        [controls.parse_json(line) for line in controls.read_bound(plan["dataset"]).splitlines()],
        controls.parse_json(controls.read_bound(plan["source_report"])),
        api,
    )
    base.validate_table(
        values["prefix_reference"]["samples"],
        values["prepared_inputs"],
        samples,
        controls.parse_json(controls.read_bound(plan["source_report"])),
        api,
    )
    plan.update(
        contract=CONTRACT + "-plan",
        prior_failed_attempt=prior,
        optional_arrow_isolation=ISOLATION_POLICY,
        optional_import_runtime=optional_runtime(controls),
        reference_kind="saved_cpu122_metadata_reproduced_under_optional_arrow_isolation",
    )
    plan["sources"] = controls.merge_bindings(
        [
            *plan["sources"],
            *declared,
            FAILURE,
            *(controls.bind_file(name) for name in NEW_FILES),
        ]
    )
    for row in plan["sources"]:
        controls.read_bound(row)
    base.assert_no_forbidden_modules(stdlib_phase=True)
    return plan


def recheck(plan, controls, *, include_images):
    base.recheck(plan, controls, include_images=include_images)
    if optional_runtime(controls) != plan["optional_import_runtime"]:
        raise ValueError("Optional primary import-chain runtime drift")


def run(plan_path, plan_sha):
    base.require_sha(plan_sha)
    controls = base.bootstrap()
    binding = controls.bind_file(plan_path)
    if binding["sha256"] != plan_sha:
        raise ValueError("Explicit v2 CPU122 plan SHA differs")
    plan = controls.parse_json(controls.read_bound(binding))
    if base.canonical(plan) != base.canonical(build_plan(plan["output_dir"])):
        raise ValueError("v2 CPU122 admission differs before processor import")
    recheck(plan, controls, include_images=True)
    output = base.fresh_output(plan["output_dir"], controls)
    output.mkdir(parents=True, exist_ok=False)
    prior = plan["prior_failed_attempt"]
    prior_references = controls.parse_json(controls.read_bound(prior["prefix_reference"]))[
        "samples"
    ]
    prior_prepared = controls.parse_json(controls.read_bound(prior["prepared_inputs"]))
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        "pid": os.getpid(),
        "plan": binding,
        "success": False,
        "native_exit_code": 1,
        "training_dataset_cpu_admitted": False,
        "reference_kind": plan["reference_kind"],
        "historical_cpu86_metadata_reference_exists": False,
        "prior_failed_attempt": prior,
        "saved_cpu122_metadata_exact": False,
        "sample_count": 0,
        "sample_ids": plan["sample_ids"],
        "admitted_sample_ids": [],
        "source_validation_sessions_verified": 0,
        "source_bindings_before": plan["sources"],
        "source_bindings_after": None,
        "source_bindings_unchanged": False,
        "processor_files_before": plan["processor_files"],
        "processor_files_after": None,
        "runtime_before": plan["runtime"],
        "runtime_after": None,
        "optional_import_runtime_before": plan["optional_import_runtime"],
        "optional_import_runtime_after": None,
        "limits": base.LIMITS,
        "optional_arrow_isolation": {"policy": plan["optional_arrow_isolation"]},
        "observations": [],
        "partial_failures": [],
        **dict.fromkeys(base.FALSE_CLAIMS, False),
    }
    references, prepared = [], {}
    started, torch = time.perf_counter(), None

    def observe(stage, **fields):
        row = {"stage": stage, "elapsed_since_start_s": time.perf_counter() - started, **fields}
        report["observations"].append(row)
        print(json.dumps({"event": "indexed_cpu122_v2_progress", **row}), flush=True)

    try:
        with OptionalArrowIsolation(report["optional_arrow_isolation"]):
            api = base.dependencies(controls)
            source_report = controls.parse_json(controls.read_bound(plan["source_report"]))
            samples = base.admit_rows(
                [
                    controls.parse_json(line)
                    for line in controls.read_bound(plan["dataset"]).splitlines()
                ],
                source_report,
                api,
            )
            actors = {
                row["sample_id"]: row for row in source_report["sample_actor_target_bindings"]
            }
            if [sample.sample_id for sample in samples] != plan["sample_ids"]:
                raise ValueError("Original all122 source order changed")
            import torch as torch_module

            torch = torch_module
            if torch.cuda.is_initialized():
                raise ValueError("CPU122 v2 must not initialize CUDA")
            report["CUDA_uninitialized_before"] = True
            torch.set_num_threads(base.LIMITS["cpu_threads"])
            if torch.get_num_threads() != base.LIMITS["cpu_threads"]:
                raise ValueError("Explicit one-thread CPU plan was not applied")
            observe("processor_load_started")
            processor = api.checker.official_processor(
                Path(plan["processor_snapshot"]), SimpleNamespace(**base.LIMITS)
            )
            report["actual_runtime_origins"] = base.validate_runtime_origins(
                processor, torch, plan["runtime"]
            )
            report["optional_python_origins_before"] = validate_optional_origins(
                plan["optional_import_runtime"]
            )
            base.assert_no_forbidden_modules()
            report["official_processor_used"] = True
            observe("processor_load_finished")
            for sample, retained in zip(samples, prior_references, strict=True):
                observe("sample_started", sample_id=sample.sample_id, admitted_count=len(prepared))
                reference, row = base.encode_first_reference(
                    sample,
                    ROOT,
                    processor=processor,
                    torch=torch,
                    budget=SimpleNamespace(**base.LIMITS),
                    source_actor=actors[sample.sample_id],
                    api=api,
                )
                if base.canonical(reference) != base.canonical(retained) or base.canonical(
                    row
                ) != base.canonical(prior_prepared[sample.sample_id]):
                    raise ValueError(
                        "Isolated CPU122 prefix/full metadata differs from saved r1 reference"
                    )
                if torch.cuda.is_initialized():
                    raise ValueError("CPU sample encoding initialized CUDA")
                references.append(reference)
                prepared[sample.sample_id] = row
                report["admitted_sample_ids"].append(sample.sample_id)
                report["sample_count"] = report["source_validation_sessions_verified"] = len(
                    prepared
                )
                observe(
                    "sample_admitted",
                    sample_id=sample.sample_id,
                    admitted_count=len(prepared),
                    source_session_normally_closed=True,
                    saved_cpu122_metadata_exact=True,
                )
            base.validate_table(references, prepared, samples, source_report, api)
            observe("source_recheck_started")
            recheck(plan, controls, include_images=True)
            controls.read_bound(binding)
            if (
                base.runtime_inventory(
                    controls.parse_json(controls.read_bound(base.RUNTIME)), controls
                )
                != plan["runtime"]
            ):
                raise ValueError("CPU tensor/processor runtime changed before seal")
            if (
                base.validate_runtime_origins(processor, torch, plan["runtime"])
                != report["actual_runtime_origins"]
            ):
                raise ValueError("Actual CPU imported origins changed")
            if (
                validate_optional_origins(plan["optional_import_runtime"])
                != report["optional_python_origins_before"]
            ):
                raise ValueError("Actual optional Python origins changed")
            base.assert_no_forbidden_modules()
            if torch.cuda.is_initialized():
                raise ValueError("CPU preparation initialized CUDA")
            report.update(
                source_bindings_after=plan["sources"],
                source_bindings_unchanged=True,
                processor_files_after=plan["processor_files"],
                runtime_after=plan["runtime"],
                optional_import_runtime_after=plan["optional_import_runtime"],
                CUDA_uninitialized_after=True,
                saved_cpu122_metadata_exact=True,
            )
            observe("source_recheck_finished")
        # No provisional success before the owned isolation finder has been removed.
        base.assert_no_forbidden_modules()
        report.update(success=True, native_exit_code=0, training_dataset_cpu_admitted=True)
    except BaseException as error:
        report["partial_failures"].append(
            {"type": type(error).__name__, "message": str(error), "admitted_count": len(prepared)}
        )
        if torch is not None:
            report["CUDA_uninitialized_after"] = not torch.cuda.is_initialized()
    report["prefix_reference"] = controls.write_json(
        output / "prefix-reference.json",
        {
            "reference_kind": plan["reference_kind"],
            "samples": references,
        },
    )
    report["prepared_inputs"] = controls.write_json(output / "prepared-inputs.json", prepared)
    report["duration_s"] = time.perf_counter() - started
    controls.write_json(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-plan")
    parser.add_argument("--output-dir")
    parser.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    args = parser.parse_args()
    try:
        if args.emit_plan and args.output_dir and not args.plan and not args.plan_sha256:
            controls = base.bootstrap()
            plan = build_plan(args.output_dir)
            target = controls.repo_path(args.emit_plan)
            if (
                target.parent != (ROOT / "configs/training").resolve()
                or not target.name.startswith("onboard-navigation-indexed-cpu122-")
                or target.suffix != ".json"
            ):
                raise ValueError("Require NEW indexed-cpu122 plan in configs/training")
            print(json.dumps(controls.write_json(target, plan), sort_keys=True))
            return 0
        if args.plan and args.plan_sha256 and not args.emit_plan and not args.output_dir:
            report = run(args.plan, args.plan_sha256)
            print(
                json.dumps(
                    {
                        key: report[key]
                        for key in (
                            "native_exit_code",
                            "sample_count",
                            "training_dataset_cpu_admitted",
                            "saved_cpu122_metadata_exact",
                            "training_ready_for_candidate",
                        )
                    },
                    sort_keys=True,
                )
            )
            return report["native_exit_code"]
        raise ValueError("Use exactly emit-plan/output-dir or plan/plan-sha256")
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
