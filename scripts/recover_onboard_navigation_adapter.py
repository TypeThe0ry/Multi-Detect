"""Fresh-process exact reload of a failed navigation pilot, never retraining.

The old failed report stays failed. The frozen loader's 'train' audit mode is
used only to admit its bound saved-adapter path; no training entry is called.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import random
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
from scripts import (  # noqa: E402
    run_onboard_navigation_pilot_streaming_no_warmup_importpath as runtime,
)

body, sup = runtime.body, runtime.sup
streaming = body.streaming
CONTRACT = "multidetect.onboard-navigation-adapter-recovery-v1"
FILES = {
    "plan",
    "runtime_manifest",
    "training_report",
    "parent_probes",
    "candidate_probes",
    "prepared_inputs",
    "training_events",
    "source_preflight",
    "source_completion",
}
CLAIMS = {
    **sup.CLAIMS,
    "optimizer_performed": False,
    "forward_backward_performed": False,
    "training_performed_by_this_check": False,
    "independent_holdout_available": False,
    "original_training_reload_gate_passed": False,
}


def verify_record(bound):
    if not isinstance(bound, dict) or set(bound) != {"path", "bytes", "sha256"}:
        raise ValueError("Recovery needs an exact canonical file binding")
    if sup.file_record(bound["path"]) != bound:
        raise ValueError("Recovery bound source identity differs")
    return bound


def bound_json(bound):
    verify_record(bound)
    path = Path(bound["path"])
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Recovery JSON exceeds its bounded input cap")
    return sup.strict_json(path.read_bytes())


@contextlib.contextmanager
def runtime_scope(arguments):
    with runtime.dispatch_scope(), body.dispatch_scope(arguments):
        yield


@contextlib.contextmanager
def restore_streaming_aliases(root_alias, torch_alias):
    """Restore aliases even when writing the final audit itself fails."""
    try:
        yield
    finally:
        if root_alias is not None:
            sys.modules["safetensors"].safe_open = root_alias
            torch_module = sys.modules.get("safetensors.torch")
            if torch_module is not None:
                torch_module.safe_open = torch_alias


def read_manifest(args):
    manifest_path = Path(args.recovery_manifest).resolve(strict=True)
    if manifest_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Recovery manifest exceeds cap")
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.recovery_manifest_sha256:
        raise ValueError("Recovery manifest SHA differs")
    manifest = sup.strict_json(raw)
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or manifest.get("contract") != CONTRACT
        or manifest.get("entrypoint_sha256") != sup.sha256_file(__file__)
        or not isinstance(manifest.get("files"), dict)
        or set(manifest["files"]) != FILES
        or not isinstance(manifest.get("source_files"), list)
        or not 1 <= len(manifest["source_files"]) <= 512
    ):
        raise ValueError("Recovery manifest entrypoint/schema/required bindings differ")
    records = {}
    for row in manifest["source_files"]:
        verify_record(row)
        if row["path"] in records:
            raise ValueError("Recovery source binding repeats")
        records[row["path"]] = row
    for source in (
        Path(__file__),
        Path(__file__).parents[1] / "tests/test_recover_onboard_navigation_adapter.py",
    ):
        if records.get(str(source.resolve(strict=True))) != sup.file_record(source):
            raise ValueError("Recovery must bind its source and CPU tests")
    documents = {
        key: bound_json(row) for key, row in manifest["files"].items() if key != "training_events"
    }
    for row in manifest["files"].values():
        verify_record(row)
        records[row["path"]] = row
    plan, root, plan_path = sup.read_plan(
        manifest["files"]["plan"]["path"], manifest["files"]["plan"]["sha256"]
    )
    if (
        not Path(manifest.get("artifact_root", "")).is_absolute()
        or Path(manifest["artifact_root"]).resolve(strict=True) != root
    ):
        raise ValueError("Recovery artifact_root differs from the frozen plan")
    original = documents["training_report"]
    trained = Path(manifest["files"]["training_report"]["path"]).parent
    for key, name in (
        ("parent_probes", "parent-probes.json"),
        ("candidate_probes", "candidate-probes.json"),
        ("prepared_inputs", "prepared-inputs.json"),
        ("training_events", "training-events.jsonl"),
    ):
        if Path(manifest["files"][key]["path"]) != trained / name:
            raise ValueError(
                "Recovery must bind probes/inputs/journal from this exact failed attempt"
            )
    old_args = SimpleNamespace(
        plan=plan_path,
        plan_sha256=manifest["files"]["plan"]["sha256"],
        mode="train",
        output_dir=trained,
        runtime_manifest=Path(manifest["files"]["runtime_manifest"]["path"]),
        runtime_manifest_sha256=manifest["files"]["runtime_manifest"]["sha256"],
    )
    with runtime_scope(old_args):
        old_runtime, _, _, old_records = streaming.read_runtime(old_args)
    if (
        original.get("contract") != "multidetect.onboard-vlm-navigation-pilot-training-v1"
        or type(original.get("native_exit_code")) is not int
        or original["native_exit_code"] != 1
        or original.get("failure_type") != "OutOfMemoryError"
        or original.get("training_plan_sha256") != old_args.plan_sha256
        or original.get("optimizer_update_uncertain") is not False
        or original.get("base_parameter_bytes_unchanged") is not True
        or original.get("base_parameter_sha256_before")
        != original.get("base_parameter_sha256_after")
        or original.get("adapter_parameter_sha256_before")
        == original.get("adapter_parameter_sha256_after")
        or original.get("model_id") != plan["base"].get("model_id")
        or original.get("revision") != plan["base"].get("revision")
        or original.get("declared_train_sample_ids") != plan["train_sample_ids"]
        or original.get("resubstitution_only") is not True
        or original.get("independent_holdout_available") is not False
        or "saved_adapter_reload" in original
        or any(type(original.get(key)) is not int or original[key] != 86 for key in sup.COUNTERS)
        or any(original.get(key) is not False for key in sup.CLAIMS)
    ):
        raise ValueError("Only the bound failed post-training fresh reload may be recovered")
    for key in (
        "base_parameter_sha256_before",
        "base_parameter_sha256_after",
        "adapter_parameter_sha256_before",
        "adapter_parameter_sha256_after",
    ):
        value = original.get(key)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("Original parameter digest must be canonical SHA-256")
    expected = list(plan["train_sample_ids"])
    random.Random(plan["optimization"]["seed"]).shuffle(expected)
    journal = sup.journal_counters(trained, expected_sample_ids=expected)
    if (
        journal.get("observed_counters") != dict.fromkeys(sup.COUNTERS, 86)
        or journal.get("binding") != manifest["files"]["training_events"]
    ):
        raise ValueError("Actual original 86-step journal differs")
    completion, preflight = documents["source_completion"], documents["source_preflight"]
    if (
        completion.get("source_bindings_unchanged") is not True
        or type(completion.get("supervisor_exit_code")) is not int
        or completion.get("supervisor_exit_code") != 1
        or type(completion.get("child_returncode_raw")) is not int
        or completion.get("child_returncode_raw") != 1
        or completion.get("child_completion_verified") is not False
        or completion.get("sources_before") != completion.get("sources_after")
        or preflight.get("sources_before") != completion.get("sources_before")
        or not completion.get("sources_before")
    ):
        raise ValueError("Original source pre/post evidence differs")
    original_records = [
        *completion["sources_before"],
        *original["runtime_files"],
        *original["adapter_files"],
        *original["output_artifact_files"],
    ]
    for row in original_records:
        if records.get(row["path"]) != row:
            raise ValueError("Recovery must explicitly bind every old runtime/saved source")
        verify_record(row)
    for row in [*old_records, *sup.capture_sources(plan, root, plan_path)]:
        if row["path"] not in records:
            # The new recovery entrypoint and its interpreter are separate sources.
            if row["path"] != str(Path(sys.executable).resolve(strict=True)):
                raise ValueError("Recovery omits an actual frozen planned/runtime source")
        else:
            if records[row["path"]] != row:
                raise ValueError("Old runtime source differs from recovery declaration")
        records[row["path"]] = row
    declarations = plan["diagnostic_probes"]
    for name, parent in (("parent_probes", True), ("candidate_probes", False)):
        probes = documents[name]
        if (
            not isinstance(probes, list)
            or len(probes) != 4
            or [
                {"sample_id": p.get("sample_id"), "action_class": p.get("action_class")}
                for p in probes
            ]
            != declarations
            or any(p.get("parent") is not parent for p in probes)
        ):
            raise ValueError("Recovery probes differ from the four frozen training probes")
    if len(documents["prepared_inputs"]) != 86 or set(documents["prepared_inputs"]) != set(
        plan["train_sample_ids"]
    ):
        raise ValueError("Recovery prepared inputs differ from the original 86 samples")
    weights = [
        r for r in original["adapter_files"] if Path(r["path"]).name == "adapter_model.safetensors"
    ]
    config = [r for r in original["adapter_files"] if Path(r["path"]).name == "adapter_config.json"]
    if (
        len(weights) != 1
        or len(config) != 1
        or Path(weights[0]["path"]) != trained / "adapter/adapter_model.safetensors"
    ):
        raise ValueError("Recovery requires the exact original saved default adapter")
    records[str(manifest_path)] = sup.file_record(manifest_path)
    return SimpleNamespace(
        manifest=manifest,
        documents=documents,
        original=original,
        plan=plan,
        root=root,
        old_args=old_args,
        old_runtime=old_runtime,
        journal=journal,
        records=[records[key] for key in sorted(records)],
        weights=weights[0],
        config=config[0],
    )


def protect_output(path, admitted, args):
    output = sup.resolve_fresh(path, admitted.root)
    sup.protect_sealed_directories(output, admitted.plan, admitted.root)
    protected = [Path(row["path"]).parent for row in admitted.records]
    protected.append(Path(args.recovery_manifest).resolve(strict=True).parent)
    if any(output.is_relative_to(parent) for parent in protected):
        raise ValueError("Recovery output would pollute a bound source directory")
    return output


def reload_and_probe(admitted, *, backend_factory=None):
    # Called only after safe_open aliases and warmup/import-path hooks are installed.
    from safetensors.torch import load_file

    from scripts import train_onboard_vlm_navigation_pilot as trainer

    plan = trainer.load_plan(admitted.old_args.plan, admitted.old_args.plan_sha256)
    budget = trainer.validate_plan_shape(plan)
    if trainer.asdict(budget) != admitted.original["optimization"]:
        raise ValueError("Recovery may not change the original training budget/model configuration")
    trainer.verify_plan_sources(plan)
    samples, targets = trainer.admit_dataset(plan)
    trainer.recovery.verify_config(bound_json(admitted.config), budget)
    state = load_file(admitted.weights["path"], device="cpu")
    digest = trainer.recovery.saved_parameter_digest(
        state, admitted.original["trainable_audit"], rank=budget.rank
    )
    if digest != admitted.original["adapter_parameter_sha256_after"]:
        raise ValueError("Saved FP32 tensors differ from the trained adapter identity")
    backend = (backend_factory or trainer.NavigationBackend)(Path(plan["base"]["snapshot"]), budget)
    if (
        backend.base_digest_initial != admitted.original["base_parameter_sha256_before"]
        or backend.trainable_audit != admitted.original["trainable_audit"]
    ):
        raise ValueError("Fresh BF16 base/FP32 default adapter audit differs")
    loaded = trainer.recovery.load_saved_exactly(
        backend.model, state, admitted.original["trainable_audit"], digest, rank=budget.rank
    )
    selected = {s.sample_id: s for s in samples}
    regenerated = []
    with trainer.navigation.NavigationSourceValidationSession(admitted.root) as session:
        session.prime(samples)
        for expected in admitted.documents["candidate_probes"]:
            sid = expected["sample_id"]
            encoded = backend.encode(selected[sid], admitted.root, session=session)
            actual = {k: v for k, v in encoded.items() if k not in {"prefix", "full", "labels"}}
            if (
                actual != admitted.documents["prepared_inputs"][sid]
                or encoded["prefix_tensors"] != expected["actual_prefix_tensors"]
            ):
                raise ValueError(
                    "Recovery source/RGB/processor tensors differ from original candidate input"
                )
            generated = backend.generate(encoded, parent=False)
            trainer.check_memory(backend, budget)
            if (
                generated["output_token_ids"] != expected["generated"]["output_token_ids"]
                or generated["raw_output"] != expected["generated"]["raw_output"]
            ):
                raise ValueError(
                    "Fresh exact adapter recovery changed candidate probe tokens/raw output"
                )
            diagnostic = trainer.diagnose(generated["raw_output"], targets[sid])
            if diagnostic != expected["diagnostic"]:
                raise ValueError("Recovery changed the original resubstitution diagnosis")
            regenerated.append(
                {
                    "sample_id": sid,
                    "generated": generated,
                    "diagnostic": diagnostic,
                    "actual_encoded": actual,
                }
            )
        for sample in samples:
            session.validate_sample(sample)
    if not session.verified:
        raise ValueError("Recovery RGB/source validation did not finish")
    after = trainer.core.parameter_digest(backend.model, trainable=False)
    adapter_after = trainer.core.parameter_digest(backend.model, trainable=True)
    if after != backend.base_digest_initial or adapter_after != digest:
        raise ValueError("Recovery changed frozen base or loaded adapter parameter bytes")
    trainer.verify_plan_sources(plan)
    return {
        "fresh_base_parameter_sha256": backend.base_digest_initial,
        "base_parameter_sha256_after": after,
        "base_parameter_bytes_unchanged": True,
        "loaded_adapter_parameter_sha256": adapter_after,
        "exact_adapter_load": loaded,
        "probe_count": len(regenerated),
        "probe_generation_token_ids_and_raw_equal": True,
        "source_rgb_and_processor_tensors_equal": True,
        "source_rgb_bytes_reverified": True,
        "resubstitution_only": True,
        "regenerated_probes": regenerated,
    }


def worker(args, *, check=reload_and_probe):
    admitted = read_manifest(args)
    output = protect_output(args.output_dir, admitted, args)
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        **CLAIMS,
        "recovery_manifest_sha256": args.recovery_manifest_sha256,
        "original_training_native_exit_code": 1,
        "optimizer_steps_performed_by_this_check": 0,
        "original_optimizer_journal": admitted.journal,
        "native_exit_code": 1,
        "reload_check": None,
        "legacy_loader_path_audit_mode_only": "train",
    }
    counts, openings, warmup, constructor = None, [], None, None
    restore_warmup, restore_constructor = None, None
    old_root_alias, old_torch_alias = None, None
    try:
        with runtime_scope(admitted.old_args):
            sup.emit(
                "streaming_runtime_verified",
                pid=os.getpid(),
                mode="train",
                plan_sha256=admitted.old_args.plan_sha256,
                runtime_manifest_sha256=admitted.old_args.runtime_manifest_sha256,
                **streaming.LOADER_CLAIMS,
            )
            import safetensors

            old_root_alias = safetensors.safe_open
            old_torch_module = sys.modules.get("safetensors.torch")
            old_torch_alias = getattr(old_torch_module, "safe_open", old_root_alias)
            allowed = streaming.checkpoint_paths(admitted.old_args, admitted.plan, admitted.root)
            counts = streaming.install_streaming_loader(allowed, openings)
            warmup, restore_warmup = body.install_warmup_skip(admitted.old_runtime)
            constructor, restore_constructor = body.install_constructor_traceback(
                admitted.old_runtime
            )
            report["reload_check"] = check(admitted)
            if read_manifest(args).records != admitted.records:
                raise ValueError("Recovery sources changed after checking saved adapter")
            report["native_exit_code"] = 0
    except Exception as exc:
        traceback.print_exc()
        report.update(failure_type=type(exc).__name__, failure=str(exc))
    finally:
        with (
            restore_streaming_aliases(old_root_alias, old_torch_alias),
            runtime_scope(admitted.old_args),
        ):
            for restore in (restore_constructor, restore_warmup):
                if restore is not None:
                    try:
                        restore()
                    except Exception:
                        traceback.print_exc()
                        report["native_exit_code"] = 1
            audits = []
            for handle, opening in openings:
                handle.close()
                audits.append({**opening, "final_diagnostics": handle.diagnostics()})
            loader_report = {
                "contract": streaming.CONTRACT,
                **sup.CLAIMS,
                **streaming.LOADER_CLAIMS,
                "mode": "train",
                "pid": os.getpid(),
                "plan_sha256": admitted.old_args.plan_sha256,
                "runtime_manifest_sha256": admitted.old_args.runtime_manifest_sha256,
                "effective_loader_backend": "stdlib_tensor_streaming",
                "package_upgrade_only_claim": False,
                "change_scope": "this_child_only",
                "worker_returncode": report["native_exit_code"],
                "observed_calls": counts,
                "reader_openings": audits,
            }
            sup.emit("explicit_streaming_worker_finished", **loader_report)
            sup.create_json(output / "streaming-loader-report.json", loader_report)
            warmup_report = {
                "contract": body.CONTRACT,
                **sup.CLAIMS,
                **body.DECLARATIONS,
                "mode": "train",
                "plan_sha256": admitted.old_args.plan_sha256,
                "runtime_manifest_sha256": admitted.old_args.runtime_manifest_sha256,
                "worker_returncode": report["native_exit_code"],
                "warmup_observation": warmup,
                "constructor_observation": constructor,
            }
            sup.emit("no_warmup_worker_finished", **warmup_report)
            sup.create_json(output / "allocator-warmup-report.json", warmup_report)
        sup.create_json(output / "report.json", report)
        sup.emit("navigation_adapter_recovery_finished", **report)
    return report["native_exit_code"]


def verify_success(output, log, args, admitted, *, expected_pid=None):
    report = bound_json(sup.file_record(output / "report.json"))
    result = report.get("reload_check")
    if (
        report.get("contract") != CONTRACT
        or report.get("recovery_manifest_sha256") != args.recovery_manifest_sha256
        or type(report.get("native_exit_code")) is not int
        or report["native_exit_code"] != 0
        or report.get("original_training_native_exit_code") != 1
        or type(report.get("optimizer_steps_performed_by_this_check")) is not int
        or report["optimizer_steps_performed_by_this_check"] != 0
        or any(report.get(key) is not value for key, value in CLAIMS.items())
        or not isinstance(result, dict)
        or result.get("probe_count") != 4
        or any(
            result.get(key) is not True
            for key in (
                "base_parameter_bytes_unchanged",
                "probe_generation_token_ids_and_raw_equal",
                "source_rgb_and_processor_tensors_equal",
                "source_rgb_bytes_reverified",
                "resubstitution_only",
            )
        )
        or result.get("fresh_base_parameter_sha256")
        != admitted.original["base_parameter_sha256_before"]
        or result.get("base_parameter_sha256_after")
        != admitted.original["base_parameter_sha256_before"]
        or result.get("loaded_adapter_parameter_sha256")
        != admitted.original["adapter_parameter_sha256_after"]
        or result.get("exact_adapter_load", {}).get("exact_tensor_equality") is not True
    ):
        raise ValueError("Recovery result identity/authority/exact reload check differs")
    regenerated = result.get("regenerated_probes")
    if not isinstance(regenerated, list) or len(regenerated) != 4:
        raise ValueError("Recovery needs four actual regenerated probe records")
    for actual, expected in zip(regenerated, admitted.documents["candidate_probes"], strict=True):
        sid = expected["sample_id"]
        if (
            actual.get("sample_id") != sid
            or actual.get("diagnostic") != expected["diagnostic"]
            or actual.get("actual_encoded") != admitted.documents["prepared_inputs"][sid]
            or actual.get("generated", {}).get("output_token_ids")
            != expected["generated"]["output_token_ids"]
            or actual.get("generated", {}).get("raw_output") != expected["generated"]["raw_output"]
        ):
            raise ValueError(
                "Actual recovery probe/input identity differs from the bound candidate"
            )
    loaded = result["exact_adapter_load"]
    digest = admitted.original["adapter_parameter_sha256_after"]
    if (
        loaded.get("saved_parameter_sha256") != digest
        or loaded.get("loaded_parameter_sha256") != digest
        or loaded.get("dtype_conversion_performed") is not False
        or loaded.get("tensor_count")
        != len(admitted.original["trainable_audit"]["trainable_parameters"])
    ):
        raise ValueError("Recovery exact tensor identity differs from original trainable audit")
    with runtime_scope(admitted.old_args):
        loader = bound_json(sup.file_record(output / "streaming-loader-report.json"))
        if expected_pid is not None and loader.get("pid") != expected_pid:
            raise ValueError("Recovery loader PID differs from the actual child process")
        streaming.verify_loader_events(log, loader, admitted.old_args)
        body.verify_warmup(
            output / "allocator-warmup-report.json", log, admitted.old_args, admitted.old_runtime
        )
    rows = [
        sup.strict_json(line)
        for line in log.read_bytes().splitlines()
        if line.lstrip().startswith(b"{")
    ]
    finished = [
        r
        for r in rows
        if isinstance(r, dict) and r.get("event") == "navigation_adapter_recovery_finished"
    ]
    if len(finished) != 1 or {k: v for k, v in finished[0].items() if k != "event"} != report:
        raise ValueError("Recovery result has no matching actual completion event")
    return report


def supervise(args, *, popen_factory=sup.subprocess.Popen):
    admitted = read_manifest(args)
    output, supervisor = (
        protect_output(args.output_dir, admitted, args),
        protect_output(args.supervisor_dir, admitted, args),
    )
    if (
        output == supervisor
        or output.is_relative_to(supervisor)
        or supervisor.is_relative_to(output)
    ):
        raise ValueError("Recovery output and external supervisor directories must be separate")
    supervisor.mkdir(parents=True, exist_ok=False)
    completion = {
        "contract": CONTRACT,
        **CLAIMS,
        "supervisor_exit_code": 1,
        "sources_before": admitted.records,
        "sources_unchanged": False,
        "child_returncode_raw": None,
        "worker_result_verified": False,
    }
    command = [
        str(Path(sys.executable).resolve(strict=True)),
        "-B",
        "-u",
        str(Path(__file__).resolve()),
        "--worker",
        "--recovery-manifest",
        str(args.recovery_manifest),
        "--recovery-manifest-sha256",
        args.recovery_manifest_sha256,
        "--output-dir",
        str(output),
    ]
    sup.create_json(supervisor / "preflight.json", {**completion, "argv": command})
    try:
        with (
            (supervisor / "stdout.bin").open("xb", buffering=0) as stdout,
            (supervisor / "stderr.bin").open("xb", buffering=0) as stderr,
        ):
            child = popen_factory(
                command,
                shell=False,
                cwd=str(WORKSPACE),
                stdin=sup.subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
            )
            completion["child_pid"] = child.pid
            completion["child_returncode_raw"] = child.wait()
        after = read_manifest(args)
        completion["sources_after"] = after.records
        completion["sources_unchanged"] = after.records == admitted.records
        result = verify_success(
            output, supervisor / "stdout.bin", args, admitted, expected_pid=completion["child_pid"]
        )
        completion["worker_result_verified"], completion["worker_report"] = True, result
        if completion["child_returncode_raw"] == 0 and completion["sources_unchanged"]:
            completion["supervisor_exit_code"] = 0
    except Exception as exc:
        completion.update(failure_type=type(exc).__name__, failure=str(exc))
    finally:
        sup.create_json(supervisor / "completion.json", completion)
        sup.emit("navigation_adapter_recovery_supervisor_finished", **completion)
    return completion["supervisor_exit_code"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recovery-manifest", type=Path, required=True)
    parser.add_argument("--recovery-manifest-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--supervisor-dir", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.worker:
            return worker(args)
        if args.supervisor_dir is None:
            raise ValueError("Recovery parent requires a fresh external supervisor directory")
        return supervise(args)
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
