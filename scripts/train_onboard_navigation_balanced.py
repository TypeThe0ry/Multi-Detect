"""Warm-start the existing navigation LoRA; never a flight or promotion path.

The stdlib supervisor admits one explicit experiment and runs CPU encoding,
baseline, training, and exact reload in separate, sequential child processes.
No previous source, checkpoint, dataset, plan, or result is overwritten.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.onboard-navigation-balanced-continuation-v1"
PLAN_CONTRACT = "multidetect.onboard-navigation-balanced-continuation-plan-v1"
ORIGINAL_SHA = "f4d76902c220812201f2ca4e961b0f427e4378ba6ee5449bf96e86be23999c53"
PREFIX_SHA = "53aaaf7feeb878a35d0d3573115cb816c8290f008e5ca9b0f30a1cf520382c36"
SCHEDULE_SHA = "23999ab5a032c69a467de5789644067d4e0a7b83f3213786e7ddec5025450d23"
WARM_SHA = "e50249e3e9c87e0cdef6e7207ab75c2930fae6d17c10d05ff68aec652d0638ca"
BASE_SHA = "c04c7a37e3a717329d2f6b06c9fd7109f431e938e4d201c319e819662113aac6"
NEW_FILES = (
    "scripts/train_onboard_navigation_balanced.py",
    "tests/test_train_onboard_navigation_balanced.py",
    "scripts/onboard_navigation_balanced_encoding.py",
    "tests/test_onboard_navigation_balanced_encoding.py",
    "scripts/onboard_navigation_balanced_journal.py",
    "tests/test_onboard_navigation_balanced_journal.py",
)
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
)
OPTIMIZER = {
    "name": "AdamW",
    "weight_decay": 0.0,
    "betas": [0.9, 0.999],
    "eps": 1e-8,
    "amsgrad": False,
    "foreach": False,
    "fused": False,
    "optimizer_state_restored": False,
    "scheduler_state_restored": False,
    "rng_state_restored": False,
}


@dataclass(frozen=True)
class BalancedTrainingBudget:
    epochs: int = 1  # One bound schedule pass, not one unique-source epoch.
    max_optimizer_steps: int = 280
    max_pixels_per_image: int = 50176
    max_input_tokens: int = 4096
    max_new_tokens: int = 192
    rank: int = 4
    alpha: int = 8
    learning_rate: float = 2e-5
    seed: int = 20261007
    gradient_checkpointing: bool = True
    gradient_clip_norm: float = 1.0
    max_cuda_reserved_bytes: int = 8053063680

    def validate(self):
        defaults = asdict(BalancedTrainingBudget())
        if any(
            type(v) is not type(defaults[k]) or v != defaults[k] for k, v in asdict(self).items()
        ):
            raise ValueError("Only the explicitly declared balanced experiment is admitted")


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def reject(value):
        raise ValueError(f"Non-finite JSON constant: {value}")

    value = json.loads(raw, object_pairs_hook=unique, parse_constant=reject)

    def finite(item):
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Non-finite JSON number")
        if isinstance(item, dict):
            for child in item.values():
                finite(child)
        elif isinstance(item, list):
            for child in item:
                finite(child)

    finite(value)
    return value


def require_sha(value):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("Expected an explicit lowercase SHA-256")


def record(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("Source must be an existing file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def binding_path(row, *, allow_external=False):
    if (
        not isinstance(row, dict)
        or set(row) != {"path", "bytes", "sha256"}
        or not isinstance(row["path"], str)
        or not row["path"]
        or type(row["bytes"]) is not int
        or row["bytes"] <= 0
    ):
        raise ValueError("Exact positive source binding is required")
    require_sha(row["sha256"])
    path = Path(row["path"])
    path = (path if path.is_absolute() else ROOT / path).resolve(strict=True)
    if not allow_external and not path.is_relative_to(ROOT):
        raise ValueError("Project source escaped the authoritative repository")
    return path


def read_bound(row, maximum=2 * 1024 * 1024):
    path = binding_path(row)
    if row["bytes"] > maximum or record(path) != {**row, "path": str(path)}:
        raise ValueError("Bound document changed or exceeds its cap")
    return strict_json(path.read_bytes())


def create_json(path, value):
    with Path(path).open("xb") as stream:
        stream.write(
            json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
            + b"\n"
        )
        stream.flush()
        os.fsync(stream.fileno())


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, allow_nan=False), flush=True)


@contextlib.contextmanager
def repository_imports():
    original = sys.path[:]
    try:
        sys.path[:0] = [str(ROOT), str(ROOT / "src")]
        yield
    finally:
        sys.path[:] = original


def plan_bindings(plan):
    return (
        [
            plan[k]
            for k in (
                "original_plan",
                "schedule",
                "cpu_prefix_report",
                "exact_reload_evidence",
                "original_training_report",
                "runtime_reference",
            )
        ]
        + list(plan["warm_start"].values())
        + plan["source_files"]
    )


def capture_sources(plan, plan_path):
    declarations = {}
    for row in plan_bindings(plan):
        path = binding_path(row, allow_external=True)
        canonical = {**row, "path": str(path)}
        if str(path) in declarations and declarations[str(path)] != canonical:
            raise ValueError("Conflicting source bindings")
        declarations[str(path)] = canonical
    result = []
    for path in sorted(declarations):
        actual = record(path)
        if actual != declarations[path]:
            raise ValueError(f"Source bytes changed: {path}")
        result.append(actual)
    result.append(record(plan_path))
    return result


def required_source_inventory(original, runtime, prefix):
    bindings = list(runtime["source_files"]) + list(prefix["source_bindings_before"])
    bindings += original["source_files"]
    bindings += [original[k] for k in ("dataset_samples", "dataset_report", "review_receipt")]
    bindings += [original["base"]["safetensors"], *original["base"]["processor_files"]]
    bindings += [record(ROOT / name) for name in NEW_FILES]
    torch_spec = importlib.util.find_spec("torch")
    if torch_spec is None or torch_spec.origin is None:
        raise ValueError("Actual Torch optimizer source is unavailable")
    for name in ("adamw.py", "adam.py", "optimizer.py", "_functional.py"):
        bindings.append(record(Path(torch_spec.origin).parent / "optim" / name))
    records = {}
    for row in bindings:
        path = binding_path(row, allow_external=True)
        declared = {**row, "path": str(path)}
        if str(path) in records and records[str(path)] != declared:
            raise ValueError("Source inventories disagree before plan construction")
        records[str(path)] = declared
    return records


def require_source_inventory(declared, required):
    actual = {str(binding_path(row, allow_external=True)): row for row in declared}
    if len(actual) != len(declared):
        raise ValueError("Duplicate source inventory record")
    for path, row in required.items():
        if path not in actual or {**actual[path], "path": path} != row:
            raise ValueError(f"Mandatory original/runtime/optimizer source is unbound: {path}")


def build_plan():
    """Emit only a source-bound proposal; caller uses apply_patch to save it."""
    original_path = ROOT / "configs/training/onboard-vlm-navigation-pilot-plan-20261007-r1.json"
    runtime_path = ROOT / "configs/training/onboard-navigation-importpath-runtime-20261007-r2.json"
    prefix_path = (
        ROOT / "artifacts/evaluation/onboard-navigation-actor-inputs-20261007-r1/report.json"
    )
    schedule_path = (
        ROOT / "artifacts/training/onboard-navigation-balanced-schedule-20261007-r2/schedule.json"
    )
    trained = ROOT / "artifacts/models/onboard-vlm-navigation-pilot-importpath-20261007-r2"
    original = strict_json(original_path.read_bytes())
    runtime = strict_json(runtime_path.read_bytes())
    prefix = strict_json(prefix_path.read_bytes())
    records = required_source_inventory(original, runtime, prefix)
    for path, row in records.items():
        if record(path) != row:
            raise ValueError("An existing bound source drifted before plan construction")
    return {
        "schema_version": 1,
        "contract": PLAN_CONTRACT,
        "artifact_root": str(ROOT),
        "original_plan": record(original_path),
        "schedule": record(schedule_path),
        "cpu_prefix_report": record(prefix_path),
        "exact_reload_evidence": record(
            ROOT / "docs/evidence/onboard-navigation-exact-reload-20261007-r1.json"
        ),
        "original_training_report": record(trained / "report.json"),
        "warm_start": {
            "weights": record(trained / "adapter/adapter_model.safetensors"),
            "config": record(trained / "adapter/adapter_config.json"),
        },
        "runtime_reference": record(runtime_path),
        "source_files": [records[key] for key in sorted(records)],
        "optimization": asdict(BalancedTrainingBudget()),
        "optimizer": OPTIMIZER.copy(),
        "expected_base_parameter_sha256": BASE_SHA,
        "expected_warm_start_parameter_sha256": WARM_SHA,
        "training_budget_changed": True,
        "continuation_kind": "warm_start_adapter_weights_with_fresh_optimizer",
        "diagnostics": {
            "sample_ids": original["train_sample_ids"],
            "smoke_probes": original["diagnostic_probes"],
            "scope": "all86_train_resubstitution_not_independent_holdout",
        },
        "claims": dict.fromkeys(FALSE_CLAIMS, False),
    }


def admit_plan(path, expected_sha):
    require_sha(expected_sha)
    path = Path(path).resolve(strict=True)
    if not path.is_relative_to(ROOT) or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Plan must be a bounded document in this repository")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("Plan SHA changed")
    plan = strict_json(raw)
    expected = {
        "schema_version",
        "contract",
        "artifact_root",
        "original_plan",
        "schedule",
        "cpu_prefix_report",
        "exact_reload_evidence",
        "original_training_report",
        "warm_start",
        "runtime_reference",
        "source_files",
        "optimization",
        "optimizer",
        "claims",
        "expected_base_parameter_sha256",
        "expected_warm_start_parameter_sha256",
        "training_budget_changed",
        "continuation_kind",
        "diagnostics",
    }
    if not isinstance(plan, dict) or set(plan) != expected:
        raise ValueError("Unknown or missing continuation plan fields")
    if (
        type(plan["schema_version"]) is not int
        or plan["schema_version"] != 1
        or plan["contract"] != PLAN_CONTRACT
        or Path(plan["artifact_root"]).resolve(strict=True) != ROOT
    ):
        raise ValueError("Plan identity/root differs")
    if not isinstance(plan["optimization"], dict):
        raise ValueError("Explicit optimization budget is required")
    budget = BalancedTrainingBudget(**plan["optimization"])
    budget.validate()
    if (
        plan["optimizer"] != OPTIMIZER
        or any(type(plan["optimizer"][k]) is not type(v) for k, v in OPTIMIZER.items())
        or plan["claims"] != dict.fromkeys(FALSE_CLAIMS, False)
        or any(plan["claims"][k] is not False for k in FALSE_CLAIMS)
        or plan["training_budget_changed"] is not True
        or plan["continuation_kind"] != "warm_start_adapter_weights_with_fresh_optimizer"
        or plan["expected_base_parameter_sha256"] != BASE_SHA
        or plan["expected_warm_start_parameter_sha256"] != WARM_SHA
    ):
        raise ValueError("Explicit warm-start, optimizer or no-flight declarations differ")
    if not isinstance(plan["warm_start"], dict) or set(plan["warm_start"]) != {"weights", "config"}:
        raise ValueError("Exact warm-start adapter/config are required")
    if (
        plan["original_plan"]["sha256"] != ORIGINAL_SHA
        or plan["cpu_prefix_report"]["sha256"] != PREFIX_SHA
        or plan["schedule"]["sha256"] != SCHEDULE_SHA
    ):
        raise ValueError("Original data, new CPU prefixes and schedule must remain frozen")
    if not isinstance(plan["source_files"], list) or not 1 <= len(plan["source_files"]) <= 1024:
        raise ValueError("A bounded explicit runtime/source inventory is required")
    paths = [str(binding_path(row, allow_external=True)) for row in plan["source_files"]]
    if len(paths) != len(set(paths)) or any(str(ROOT / name) not in paths for name in NEW_FILES):
        raise ValueError("All new implementation and test sources must be uniquely bound")
    before = capture_sources(plan, path)
    original = read_bound(plan["original_plan"])
    if plan["diagnostics"] != {
        "sample_ids": original["train_sample_ids"],
        "smoke_probes": original["diagnostic_probes"],
        "scope": "all86_train_resubstitution_not_independent_holdout",
    }:
        raise ValueError("All86 evaluation and four predefined smoke probes are required")
    runtime = read_bound(plan["runtime_reference"])
    if Path(sys.executable).resolve(strict=True) != Path(runtime["python_executable"]).resolve():
        raise ValueError("Only the previously verified navigation venv is admitted")
    for name, version in runtime["package_versions"].items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f"Runtime distribution changed: {name}")
    prefix = read_bound(plan["cpu_prefix_report"])
    require_source_inventory(
        plan["source_files"], required_source_inventory(original, runtime, prefix)
    )
    if (
        prefix["sample_count"] != 86
        or prefix["official_processor_used"] is not True
        or prefix["all_86_prefix_pairs_equal"] is not True
        or prefix["source_validation_session_verified"] is not True
        or prefix["source_plan_sha256"] != ORIGINAL_SHA
        or prefix["source_bindings_before"] != prefix["source_bindings_after"]
        or [r["sample_id"] for r in prefix["samples"]] != original["train_sample_ids"]
        or any(prefix[k] is not False for k in FALSE_CLAIMS)
    ):
        raise ValueError("Official retained CPU86 admission evidence differs")
    prior = read_bound(plan["original_training_report"])
    if (
        prior["adapter_parameter_sha256_after"] != WARM_SHA
        or prior["base_parameter_sha256_after"] != BASE_SHA
        or prior["optimizer_steps_completed"] != 86
    ):
        raise ValueError("Warm-start must be the actually trained, saved adapter")
    exact = read_bound(plan["exact_reload_evidence"])
    if exact["fresh_saved_adapter_reload_verified"] is not True:
        raise ValueError("Saved adapter must have passed exact fresh reload")
    return plan, budget, before


def semantic_admission(plan):
    from scripts import build_onboard_navigation_balanced_schedule as scheduler
    from scripts import onboard_navigation_balanced_journal as journal
    from scripts import train_onboard_vlm_navigation_pilot as pilot

    old = pilot.load_plan(binding_path(plan["original_plan"]), ORIGINAL_SHA)
    pilot.verify_plan_sources(old)
    samples, targets = pilot.admit_dataset(old)
    schedule = read_bound(plan["schedule"])
    journal.validate_balanced_schedule(schedule)
    journal.validate_balanced_budget(plan["optimization"], schedule["draws"])
    draws, exposures = scheduler.build_draws(
        samples, targets, old["train_sample_ids"], seed=20261007, classify=pilot.action_class
    )
    if schedule["draws"] != draws or schedule["per_sample_exposure"] != exposures:
        raise ValueError("Schedule differs from independently rederived semantic draw order")
    return old, samples, targets, schedule, pilot


def fresh_run_directory(path):
    path = Path(path).absolute()
    if path.exists() or path.is_symlink():
        raise FileExistsError("Do not overwrite any previous run")
    resolved = path.resolve(strict=False)
    if resolved.parent != (ROOT / "artifacts/models").resolve(
        strict=True
    ) or not resolved.name.startswith("onboard-navigation-balanced-"):
        raise ValueError(
            "Use a fresh direct onboard-navigation-balanced-* model artifact directory"
        )
    return resolved


def metadata(encoded):
    return {k: v for k, v in encoded.items() if k not in {"prefix", "full", "labels"}}


def encode_all(plan, budget, old, samples, pilot, processor, torch):
    from scripts import onboard_navigation_balanced_encoding as encoding

    reference = read_bound(plan["cpu_prefix_report"])
    by_id = {row["sample_id"]: row for row in reference["samples"]}
    result = {}
    with pilot.navigation.NavigationSourceValidationSession(ROOT) as session:
        session.prime(samples)
        for index, sample in enumerate(samples, 1):
            result[sample.sample_id] = encoding.encode_navigation_sample(
                sample,
                ROOT,
                processor=processor,
                torch_module=torch,
                budget=budget,
                prefix_reference=by_id[sample.sample_id],
                session=session,
            )
            if index % 10 == 0 or index == 86:
                emit("balanced_full_chat_encoded", completed=index, total=86)
    if not session.verified:
        raise ValueError("Source validation did not finish")
    return result


@contextlib.contextmanager
def loading_scope(plan, old, output, report):
    from scripts import run_onboard_navigation_pilot_streaming as streaming
    from scripts import run_onboard_navigation_pilot_streaming_no_warmup as body

    runtime = read_bound(plan["runtime_reference"])
    runtime = {**runtime, "source_files": plan["source_files"]}
    paths = [
        (binding_path(old["base"]["safetensors"]), "base"),
        (binding_path(plan["warm_start"]["weights"]), "adapter"),
    ]
    allowed = {
        str(path.resolve(strict=True)): {
            "kind": kind,
            "header": streaming.header_binding(path),
        }
        for path, kind in paths
    }
    if report["phase"] in {"train", "reload"}:
        candidate = output.parent / "train/adapter/adapter_model.safetensors"
        allowed[str(candidate.resolve(strict=False))] = {
            "kind": "adapter",
            "header": streaming.header_binding(candidate) if candidate.is_file() else None,
        }
    openings = []
    restores = []
    root_alias = torch_alias = None
    warmup_module = original_warmup = original_constructor = None
    old_declarations = body.DECLARATIONS
    try:
        import safetensors

        root_alias = safetensors.safe_open
        torch_alias = getattr(sys.modules.get("safetensors.torch"), "safe_open", root_alias)
        report["loader_counts"] = streaming.install_streaming_loader(allowed, openings)
        body.DECLARATIONS = {**old_declarations, "training_budget_changed": True}
        # A helper can raise after installing its hook, before returning its restore.
        import transformers.modeling_utils as warmup_module

        from scripts import train_onboard_vlm_navigation_pilot as original_trainer

        original_warmup = warmup_module.caching_allocator_warmup
        original_constructor = original_trainer.NavigationBackend.__init__
        state, restore = body.install_warmup_skip(runtime)
        report["warmup"] = state
        restores.append(restore)
        state, restore = body.install_constructor_traceback(runtime)
        report["constructor"] = state
        restores.append(restore)
        yield
    finally:
        cleanup_errors = []
        for restore in reversed(restores):
            try:
                restore()
            except Exception as exc:
                cleanup_errors.append(f"hook restoration: {type(exc).__name__}: {exc}")
        if original_warmup is not None:
            warmup_module.caching_allocator_warmup = original_warmup
        if original_constructor is not None:
            original_trainer.NavigationBackend.__init__ = original_constructor
        report["outer_hook_fallbacks_restored"] = (
            original_warmup is not None
            and warmup_module.caching_allocator_warmup is original_warmup
            and original_trainer.NavigationBackend.__init__ is original_constructor
        )
        body.DECLARATIONS = old_declarations
        for handle, _row in openings:
            try:
                handle.close()
            except Exception as exc:
                cleanup_errors.append(f"reader close: {type(exc).__name__}: {exc}")
        report["reader_openings"] = []
        try:
            for handle, row in openings:
                try:
                    report["reader_openings"].append(
                        {**row, "final_diagnostics": handle.diagnostics()}
                    )
                except Exception as exc:
                    cleanup_errors.append(f"reader diagnostics: {type(exc).__name__}: {exc}")
        finally:
            if root_alias is not None:
                sys.modules["safetensors"].safe_open = root_alias
                if "safetensors.torch" in sys.modules:
                    sys.modules["safetensors.torch"].safe_open = torch_alias
            report["loader_aliases_restored"] = root_alias is not None
        if cleanup_errors:
            report["cleanup_errors"] = cleanup_errors
            raise ValueError(
                "Child-only loader cleanup failed after all restorations were attempted"
            )


def load_backend(plan, budget, old, pilot, phase, output, report):
    from safetensors.torch import load_file

    prior = read_bound(plan["original_training_report"])
    weights = binding_path(plan["warm_start"]["weights"])
    config = read_bound(plan["warm_start"]["config"])
    expected = WARM_SHA
    if phase == "reload":
        trained = strict_json((output.parent / "train/report.json").read_bytes())
        if trained["native_exit_code"] != 0 or trained["phase"] != "train":
            raise ValueError("No accepted train worker output exists for reload")
        weights = output.parent / "train/adapter/adapter_model.safetensors"
        config = strict_json((weights.parent / "adapter_config.json").read_bytes())
        expected = trained["adapter_parameter_sha256_after"]
        require_sha(expected)
        if (
            record(weights) != trained["saved_adapter_weights"]
            or record(weights.parent / "adapter_config.json") != trained["saved_adapter_config"]
        ):
            raise ValueError("New adapter file differs from the completed train worker")
    pilot.recovery.verify_config(config, budget)
    state = load_file(str(weights), device="cpu")
    audit = prior["trainable_audit"]
    if pilot.recovery.saved_parameter_digest(state, audit, rank=budget.rank) != expected:
        raise ValueError("Saved FP32 parameter digest changed")
    backend = pilot.NavigationBackend(Path(old["base"]["snapshot"]), budget)
    if backend.base_digest_initial != BASE_SHA or backend.trainable_audit != audit:
        raise ValueError("Actual BF16 base or default FP32 adapter audit changed")
    report["exact_adapter_load"] = pilot.recovery.load_saved_exactly(
        backend.model, state, audit, expected, rank=budget.rank
    )
    report["adapter_parameter_sha256_before"] = expected
    report["base_parameter_sha256_before"] = backend.base_digest_initial
    report["model_loaded"] = True
    del state
    return backend


def evaluate_all(backend, samples, encoded, targets, pilot, budget, *, parent):
    rows = []
    for index, sample in enumerate(samples, 1):
        generated = backend.generate(encoded[sample.sample_id], parent=parent)
        memory = pilot.check_memory(backend, budget)
        rows.append(
            {
                "sample_id": sample.sample_id,
                "gold_action_class": pilot.action_class(targets[sample.sample_id]),
                "generated": generated,
                "diagnostic": pilot.diagnose(generated["raw_output"], targets[sample.sample_id]),
                "actor_identity_sha256": encoded[sample.sample_id]["actor_identity_sha256"],
                "actual_prefix_tensors": encoded[sample.sample_id]["prefix_tensors"],
                "parent": parent,
                **memory,
            }
        )
        if index % 10 == 0 or index == 86:
            emit("balanced_all86_generated", completed=index, parent=parent)
    return rows


def train_draws(backend, encoded, draws, plan, plan_path, plan_sha, pilot, budget, output, report):
    from scripts import onboard_navigation_balanced_journal as journal

    if capture_sources(plan, plan_path) != report["sources_before"]:
        raise ValueError("Source drift before optimizer creation")
    admit_plan(plan_path, plan_sha)
    torch = backend.torch
    parameters = [p for p in backend.model.parameters() if p.requires_grad]
    if len(parameters) != 112:
        raise ValueError("Fresh AdamW must own exactly the audited FP32 LoRA parameters")
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
        raise ValueError("Fresh optimizer must start without restored state")
    report["fresh_optimizer_initial_state_empty"] = True
    with (output / "training-events.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
        for draw in draws:
            started = time.perf_counter()
            optimizer.zero_grad(set_to_none=True)
            torch.cuda.reset_peak_memory_stats()
            loss = backend.loss(encoded[draw["sample_id"]])
            if not torch.isfinite(loss).item():
                raise ValueError("Non-finite actual training loss")
            report["backward_attempted"] = True
            loss.backward()
            report["forward_backward_performed"] = True
            pilot.finite_gradients(backend)
            norm = torch.nn.utils.clip_grad_norm_(
                parameters, budget.gradient_clip_norm, error_if_nonfinite=True
            )
            torch.cuda.synchronize()
            pilot.check_memory(backend, budget)
            journal.append_journal_event(
                stream, draw, "attempted", loss=loss.detach().item(), gradient_norm=norm.item()
            )
            optimizer.step()
            report["optimizer_performed"] = True
            journal.append_journal_event(stream, draw, "returned")
            torch.cuda.synchronize()
            journal.append_journal_event(stream, draw, "synchronized")
            memory = pilot.check_memory(backend, budget)
            journal.append_journal_event(
                stream,
                draw,
                "completed",
                elapsed_s=time.perf_counter() - started,
                peak_allocated_bytes=memory["peak_allocated_bytes"],
                peak_reserved_bytes=memory["peak_reserved_bytes"],
            )
            emit("balanced_optimizer_draw_completed", completed=draw["draw_index"], total=280)
    backend.model.zero_grad(set_to_none=True)
    record_optimizer_observation(
        report,
        journal.verify_journal(output / "training-events.jsonl", draws, require_complete=True),
    )


def record_optimizer_observation(report, observed):
    report["journal"] = observed
    report["optimizer_update_uncertain"] = observed["optimizer_update_uncertain"]
    report["optimizer_performed"] = (
        True
        if observed["optimizer_steps_returned"] > 0
        else None
        if observed["optimizer_steps_attempted"] > 0
        else False
    )
    report["training_completed"] = observed["complete"]
    for key, value in observed.items():
        if key.startswith("optimizer_steps_"):
            report[key] = value


def verify_load_observations(report, plan, run):
    if report["phase"] == "cpu-encode":
        return
    warmup, ctor = report.get("warmup"), report.get("constructor")
    if (
        not warmup
        or not ctor
        or warmup["pid"] != report["pid"]
        or warmup["warmup_override_restored"] is not True
        or warmup["training_budget_changed"] is not True
        or warmup["warmup_calls_attempted"] != warmup["warmup_calls_skipped"]
        or warmup["warmup_calls_skipped"] != 1
        or ctor["restored"] is not True
        or ctor["calls_attempted"] != 1
        or ctor["exceptions_observed"] != 0
        or report.get("loader_aliases_restored") is not True
        or report.get("outer_hook_fallbacks_restored") is not True
    ):
        raise ValueError("One original constructor and restored child-only hooks are required")
    readers = report["reader_openings"]
    if not readers or not all(r["final_diagnostics"]["closed"] for r in readers):
        raise ValueError("Every actual checkpoint reader must close")
    with repository_imports():
        from scripts import run_onboard_navigation_pilot_streaming as streaming

        old = read_bound(plan["original_plan"])
        allowed = {
            str(binding_path(old["base"]["safetensors"])): "base",
            str(binding_path(plan["warm_start"]["weights"])): "adapter",
        }
        if report["phase"] in {"train", "reload"}:
            allowed[str((run / "train/adapter/adapter_model.safetensors").resolve())] = "adapter"
        for row in readers:
            if row["path"] not in allowed or row["kind"] != allowed[row["path"]]:
                raise ValueError("Reader filename is not an admitted checkpoint")
            header, entries = streaming.header_binding(row["path"], with_entries=True)
            diag = row["final_diagnostics"]
            if (
                row["header"] != header
                or diag["filename"] != row["path"]
                or any(diag[k] != v for k, v in header.items())
            ):
                raise ValueError("Reader's actual header identity differs")
            reads = diag["tensor_reads"]
            if reads and {r["name"] for r in reads} != set(entries):
                raise ValueError("Reader did not cover exactly the checkpoint tensor names")
            for read in reads:
                entry = entries[read["name"]]
                if (
                    read["dtype"] != entry["dtype"]
                    or read["shape"] != entry["shape"]
                    or read["declared_bytes"] != entry["data_offsets"][1] - entry["data_offsets"][0]
                ):
                    raise ValueError(
                        "Read shape/dtype/byte span differs from actual checkpoint header"
                    )
    base = [
        r["final_diagnostics"]
        for r in readers
        if r["kind"] == "base" and r["final_diagnostics"]["tensor_calls_completed"]
    ]
    if (
        len(base) != 1
        or base[0]["tensor_calls_completed"] != 625
        or base[0]["tensor_payload_bytes_read"] != 4255064064
        or base[0]["normalized_device"] != "cuda:0"
    ):
        raise ValueError("The complete original BF16 base was not actually read")
    adapters = []
    for row in readers:
        diag = row["final_diagnostics"]
        if diag["tensor_calls_attempted"] != diag["tensor_calls_completed"]:
            raise ValueError("Checkpoint reader contains an incomplete tensor read")
        reads = diag["tensor_reads"]
        if len(reads) != diag["tensor_calls_completed"] or any(
            r["completed"] is not True or r["actual_bytes_read"] != r["declared_bytes"]
            for r in reads
        ):
            raise ValueError("Actual checkpoint tensor spans differ")
        if len({r["name"] for r in reads}) != len(reads):
            raise ValueError("Checkpoint tensor reads repeated a tensor name")
        if row["kind"] == "base" and reads:
            if any(r["dtype"] != "BF16" for r in reads):
                raise ValueError("Base payload dtype changed")
        elif row["kind"] == "adapter" and reads:
            if (
                diag["normalized_device"] != "cpu"
                or len(reads) != 112
                or diag["tensor_payload_bytes_read"] != 3211264
                or any(r["dtype"] != "F32" for r in reads)
            ):
                raise ValueError("Saved adapter was not read exactly as FP32 on CPU")
            adapters.append(diag)
    if len(adapters) != (2 if report["phase"] == "train" else 1):
        raise ValueError("Expected one exact adapter load and a train-only saved-byte reread")


def windows_argv(command_line):
    import ctypes
    from ctypes import wintypes

    count = ctypes.c_int()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    pointer = shell32.CommandLineToArgvW(command_line, ctypes.byref(count))
    if not pointer:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [pointer[i] for i in range(count.value)]
    finally:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(pointer)


def verify_process_observation(
    observation,
    launcher_pid,
    argv,
    interpreter_paths,
    *,
    supervisor_pid=None,
    parse_argv=windows_argv,
):
    if (
        not isinstance(observation, dict)
        or set(observation) != {"observed_at", "processes"}
        or type(launcher_pid) is not int
        or launcher_pid <= 0
    ):
        raise ValueError("A bounded live process snapshot is required")
    observed = datetime.fromisoformat(observation["observed_at"])
    if observed.tzinfo is None or not isinstance(observation["processes"], list):
        raise ValueError("Live process timestamps must include timezone")
    rows = observation["processes"]
    if not 1 <= len(rows) <= 2:
        raise ValueError("Expected only the launcher and at most its one worker")
    by_id = {}
    allowed = {str(Path(p).resolve()).casefold() for p in interpreter_paths}
    for row in rows:
        if (
            set(row) != {"pid", "parent_pid", "executable", "command_line", "created_at"}
            or type(row["pid"]) is not int
            or row["pid"] <= 0
            or type(row["parent_pid"]) is not int
            or row["pid"] in by_id
            or str(Path(row["executable"]).resolve()).casefold() not in allowed
        ):
            raise ValueError("Process identity/executable differs")
        actual_argv = parse_argv(row["command_line"])
        if (
            not actual_argv
            or actual_argv[1:] != argv[1:]
            or str(Path(actual_argv[0]).resolve()).casefold() not in allowed
        ):
            raise ValueError("Live process command differs from the exact invocation")
        created = datetime.fromisoformat(row["created_at"])
        if created.tzinfo is None or created > observed:
            raise ValueError("Process was not observed after creation")
        by_id[row["pid"]] = row
    if launcher_pid not in by_id:
        raise ValueError("The Popen launcher was not observed live")
    launcher = by_id[launcher_pid]
    if supervisor_pid is not None and launcher["parent_pid"] != supervisor_pid:
        raise ValueError("Launcher does not belong to this supervisor")
    workers = [row for row in rows if row["pid"] != launcher_pid]
    actual = launcher
    if workers:
        actual = workers[0]
        if actual["parent_pid"] != launcher_pid or datetime.fromisoformat(
            actual["created_at"]
        ) < datetime.fromisoformat(launcher["created_at"]):
            raise ValueError("Captured venv launcher/worker ancestry differs")
    return {
        "launcher_pid": launcher_pid,
        "actual_worker_pid": actual["pid"],
        "identity_verified": True,
        "observation": observation,
    }


def capture_process_observation(launcher_pid):
    if os.name != "nt":
        raise ValueError("This bound Windows runtime requires native live CIM identity")
    if type(launcher_pid) is not int or launcher_pid <= 0:
        raise ValueError("Invalid owned launcher PID")
    # The only interpolated value is a validated native PID, not a shell/path string.
    script = (
        "$ErrorActionPreference='Stop'; "
        f"$balancedRows=@(Get-CimInstance Win32_Process -Filter "
        f"'ProcessId = {launcher_pid} OR ParentProcessId = {launcher_pid}' | "
        "ForEach-Object { [ordered]@{ pid=[int]$_.ProcessId; parent_pid=[int]$_.ParentProcessId; "
        "executable=$_.ExecutablePath; command_line=$_.CommandLine; "
        "created_at=$_.CreationDate.ToUniversalTime().ToString('o') } }); "
        "[ordered]@{ observed_at=[datetime]::UtcNow.ToString('o'); "
        "processes=$balancedRows } | ConvertTo-Json -Depth 6 -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
        timeout=20,
        encoding="utf-8",
    )
    if len(result.stdout) > 1024 * 1024:
        raise ValueError("Process observation exceeds its cap")
    return strict_json(result.stdout)


def verify_finished_events(path, phase, worker_pid, report_binding):
    path = Path(path)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Worker stdout exceeds its bounded evidence cap")
    admitted = finished = False
    with path.open("rb") as stream:
        for raw in stream:
            if len(raw) > 1024 * 1024 or not raw.endswith(b"\n"):
                raise ValueError("Worker stdout line is oversized or truncated")
            if not raw.strip().startswith(b"{"):
                continue
            row = strict_json(raw)
            event = row.get("event") if isinstance(row, dict) else None
            if event == "balanced_worker_admitted":
                if (
                    admitted
                    or finished
                    or row != {"event": event, "phase": phase, "pid": worker_pid}
                ):
                    raise ValueError("Worker admission event differs from live invocation")
                admitted = True
            elif event == "balanced_worker_finished":
                if (
                    not admitted
                    or finished
                    or row
                    != {
                        "event": event,
                        "phase": phase,
                        "native_exit_code": 0,
                        "report": report_binding,
                    }
                ):
                    raise ValueError("Worker finished event differs from actual sealed report")
                finished = True
    if not admitted or not finished:
        raise ValueError("Actual worker stdout does not prove unique admission and completion")
    return {"admitted": True, "finished": True, "report": report_binding}


def validate_prepared_inputs(prepared, plan, budget):
    """Reconcile persisted CPU records against the frozen official prefix report."""
    reference = read_bound(plan["cpu_prefix_report"])
    ids = plan["diagnostics"]["sample_ids"]
    if not isinstance(prepared, dict) or set(prepared) != set(ids):
        raise ValueError("Prepared inputs must cover exactly the 86 admitted identities")
    fields = {"input_ids", "attention_mask", "pixel_values", "image_grid_thw"}
    for row in reference["samples"]:
        value = prepared[row["sample_id"]]
        prefix = row["training_prefix"]
        if (
            value["actor_identity_sha256"] != row["actor_identity_sha256"]
            or value["prefix_tensors"] != prefix["tensors"]
            or value["prefix_tokens"] != prefix["prefix_tokens"]
            or value["prefix_reference_verified"] is not True
            or value["prompt_label_mask_verified"] is not True
        ):
            raise ValueError("Prepared actor/prefix differs from the retained official CPU input")
        for key in ("response_labels_sha256", "target_sha256"):
            require_sha(value[key])
        for key in ("prefix_tokens", "total_tokens", "response_tokens", "supervised_tokens"):
            if type(value[key]) is not int or value[key] <= 0:
                raise ValueError("Exact positive token counts are required")
        if (
            value["total_tokens"] != value["prefix_tokens"] + value["response_tokens"]
            or value["supervised_tokens"] != value["response_tokens"]
            or value["total_tokens"] > budget.max_input_tokens
            or value["response_tokens"] > budget.max_new_tokens
        ):
            raise ValueError("Full response-only token budget differs")
        full = value["input_tensors"]
        if not isinstance(full, dict) or set(full) != fields:
            raise ValueError("Four exact full-chat tensor records are required")
        for name, tensor in full.items():
            if set(tensor) != {"dtype", "shape", "sha256"}:
                raise ValueError("Unknown tensor record fields")
            require_sha(tensor["sha256"])
            if name in {"input_ids", "attention_mask"}:
                if tensor["dtype"] != "torch.int64" or tensor["shape"] != [
                    1,
                    value["total_tokens"],
                ]:
                    raise ValueError("Full chat token shape/dtype differs")
            elif tensor != prefix["tensors"][name]:
                raise ValueError("Assistant gold changed the actual image tensor bytes")
    return prepared


def diagnose_rows(rows, ids, prepared, targets, pilot, *, parent):
    """Independently recompute diagnostics from actual raw output, without repair."""
    if not isinstance(rows, list) or [r["sample_id"] for r in rows] != list(ids):
        raise ValueError("Diagnostic result identities/order differ")
    classes = ("forward", "left", "right", "stop")
    confusion = {gold: dict.fromkeys((*classes, "invalid", "other"), 0) for gold in classes}
    summary = {
        "sample_count": len(rows),
        "strict_contract_valid": 0,
        "json_syntax_valid": 0,
        "action_class_correct": 0,
        "metric_correct": 0,
        "unknown_translation": 0,
        "unknown_yaw": 0,
        "token_cap_reached": 0,
        "elapsed_at_or_above_30s_generation_bound": 0,
        "translation_l2_errors_m": [],
        "yaw_absolute_errors_rad": [],
        "confusion": confusion,
        "resubstitution_only": True,
        "quality_claim": False,
    }
    for row in rows:
        sample_id = row["sample_id"]
        raw = row["generated"]["raw_output"]
        tokens = row["generated"]["output_token_ids"]
        generation = row["generated"]
        elapsed = generation.get("elapsed_s")
        if (
            not isinstance(raw, str)
            or len(raw.encode("utf-8")) > 65536
            or not isinstance(tokens, list)
            or not 1 <= len(tokens) <= 192
            or any(type(t) is not int or t < 0 for t in tokens)
            or type(generation.get("generated_tokens")) is not int
            or generation["generated_tokens"] != len(tokens)
            or generation.get("token_cap_reached") is not (len(tokens) >= 192)
            or type(elapsed) not in (int, float)
            or not math.isfinite(elapsed)
            or elapsed < 0
            or row["parent"] is not parent
            or row["actor_identity_sha256"] != prepared[sample_id]["actor_identity_sha256"]
            or row["actual_prefix_tensors"] != prepared[sample_id]["prefix_tensors"]
            or row["gold_action_class"] != pilot.action_class(targets[sample_id])
        ):
            raise ValueError("Actual generated tokens, actor inputs or target class differ")
        summary["token_cap_reached"] += int(generation["token_cap_reached"])
        summary["elapsed_at_or_above_30s_generation_bound"] += int(elapsed >= 30)
        diagnostic = pilot.diagnose(raw, targets[sample_id])
        if row["diagnostic"] != diagnostic:
            raise ValueError("Stored diagnostic differs from independently diagnosed raw output")
        for field in ("strict_json_valid", "action_class_correct", "metric_correct"):
            summary[{"strict_json_valid": "strict_contract_valid"}.get(field, field)] += int(
                diagnostic[field]
            )
        predicted = diagnostic.get("predicted_action_class", "invalid")
        confusion[row["gold_action_class"]][
            predicted if predicted in classes else "invalid" if predicted == "invalid" else "other"
        ] += 1
        try:
            value = strict_json(raw)
            summary["json_syntax_valid"] += 1
            if diagnostic["strict_json_valid"]:
                summary["unknown_translation"] += int(
                    value["kind"] == "translate" and value["translation_m"] is None
                )
                summary["unknown_yaw"] += int(
                    value["kind"] == "turn_yaw" and value["yaw_delta_rad"] is None
                )
        except (ValueError, TypeError):
            pass
        for source, dest in (
            ("translation_l2_error_m", "translation_l2_errors_m"),
            ("yaw_absolute_error_rad", "yaw_absolute_errors_rad"),
        ):
            if source in diagnostic:
                summary[dest].append(diagnostic[source])
    return summary


def validate_phase_outputs(run, phase, plan, budget, report):
    """Parent-side acceptance: never accept self-reported success alone."""
    if (
        report["contract"] != CONTRACT
        or report["phase"] != phase
        or report["native_exit_code"] != 0
        or report["source_bindings_unchanged"] is not True
        or report["all86_full_chat_encoding_verified"] is not True
        or any(report[k] is not False for k in FALSE_CLAIMS)
        or report["optimizer_state_restored"] is not False
        or report["scheduler_state_restored"] is not False
        or report["rng_state_restored"] is not False
        or report["actual_live_camera_parity_verified"] is not False
        or report["optimizer_performed"] is not (phase == "train")
        or report["model_loaded"] is not (phase != "cpu-encode")
        or report["inference_performed"] is not (phase != "cpu-encode")
    ):
        raise ValueError("Completed phase scope differs")
    output = run / phase
    prepared_path = output / "prepared-inputs.json"
    prepared = validate_prepared_inputs(strict_json(prepared_path.read_bytes()), plan, budget)
    if phase != "cpu-encode":
        cpu = strict_json((run / "cpu-encode/prepared-inputs.json").read_bytes())
        if prepared != cpu:
            raise ValueError("GPU phase full inputs differ from this invocation's CPU admission")
        verify_load_observations(report, plan, run)
        if (
            report["base_parameter_sha256_before"] != BASE_SHA
            or report["base_parameter_sha256_after"] != BASE_SHA
        ):
            raise ValueError("Frozen base changed across actual phase")
    verified = {"prepared_inputs": record(prepared_path)}
    with repository_imports():
        _old, _samples, targets, schedule, pilot = semantic_admission(plan)
        evaluations = ()
        if phase == "baseline":
            if (
                report["adapter_parameter_sha256_before"] != WARM_SHA
                or report["adapter_parameter_sha256_after"] != WARM_SHA
            ):
                raise ValueError("Baseline modified the original adapter")
            evaluations = (("base-all86.json", True), ("warm_start-all86.json", False))
        elif phase == "train":
            from scripts import onboard_navigation_balanced_journal as journal

            actual = journal.verify_journal(
                output / "training-events.jsonl", schedule["draws"], require_complete=True
            )
            if actual != report["journal"] or actual["optimizer_steps_completed"] != 280:
                raise ValueError("Actual four-stage draw journal differs from train report")
            if (
                report["fresh_optimizer_initial_state_empty"] is not True
                or report["forward_backward_performed"] is not True
            ):
                raise ValueError("Fresh optimizer and actual backward evidence are required")
            require_sha(report["adapter_parameter_sha256_after"])
            if (
                report["adapter_parameter_sha256_before"] != WARM_SHA
                or report["adapter_parameter_sha256_after"] == WARM_SHA
            ):
                raise ValueError("Balanced optimizer did not change the warmed adapter")
            for field, name in (
                ("saved_adapter_weights", "adapter_model.safetensors"),
                ("saved_adapter_config", "adapter_config.json"),
            ):
                if report[field] != record(output / "adapter" / name):
                    raise ValueError("Saved adapter artifact differs from actual file")
                verified[field] = report[field]
            verified["journal"] = record(output / "training-events.jsonl")
            evaluations = (("candidate-all86.json", False),)
        elif phase == "reload":
            trained = strict_json((run / "train/report.json").read_bytes())
            if (
                report["fresh_saved_adapter_reload_verified"] is not True
                or report["adapter_parameter_sha256_before"]
                != trained["adapter_parameter_sha256_after"]
                or report["adapter_parameter_sha256_after"]
                != trained["adapter_parameter_sha256_after"]
            ):
                raise ValueError("Fresh reload did not retain exact candidate parameters")
            actual = strict_json((output / "regenerated-probes.json").read_bytes())
            ids = [p["sample_id"] for p in plan["diagnostics"]["smoke_probes"]]
            verified["probe_summary"] = diagnose_rows(
                actual, ids, prepared, targets, pilot, parent=False
            )
            previous = {
                r["sample_id"]: r
                for r in strict_json((run / "train/candidate-all86.json").read_bytes())
            }
            for row in actual:
                expected = previous[row["sample_id"]]
                if any(
                    row[k] != expected[k]
                    for k in ("diagnostic", "actual_prefix_tensors", "actor_identity_sha256")
                ) or any(
                    row["generated"][k] != expected["generated"][k]
                    for k in ("raw_output", "output_token_ids")
                ):
                    raise ValueError("Fresh probe tokens/input differ from saved candidate")
            verified["regenerated_probes"] = record(output / "regenerated-probes.json")
        else:
            if report["cpu_only"] is not True or report["forward_backward_performed"] is not False:
                raise ValueError("CPU-only phase performed model work")
        for name, parent in evaluations:
            path = output / name
            rows = strict_json(path.read_bytes())
            verified[name] = {
                "file": record(path),
                "summary": diagnose_rows(
                    rows, plan["diagnostics"]["sample_ids"], prepared, targets, pilot, parent=parent
                ),
            }
    return verified


def require_phase_prerequisites(run, phase, plan, budget):
    prior = {
        "cpu-encode": (),
        "baseline": ("cpu-encode",),
        "train": ("cpu-encode", "baseline"),
        "reload": ("cpu-encode", "train"),
    }[phase]
    for name in prior:
        admission = strict_json((run / f"{name}-accepted.json").read_bytes())
        path = run / name / "report.json"
        if admission["native_exit_code"] != 0 or admission["report"] != record(path):
            raise ValueError("Prerequisite phase was not accepted by this supervisor")
        if admission["verified_outputs"] != validate_phase_outputs(
            run, name, plan, budget, strict_json(path.read_bytes())
        ):
            raise ValueError("Accepted prerequisite outputs drifted")


def worker(args):
    plan, budget, before = admit_plan(args.plan, args.plan_sha256)
    run = Path(args.output_dir).resolve(strict=True)
    if run.parent != (ROOT / "artifacts/models").resolve() or not run.name.startswith(
        "onboard-navigation-balanced-"
    ):
        raise ValueError("Worker output must belong to its newly created run root")
    invocation = strict_json((run / "invocation.json").read_bytes())
    request = strict_json((run / f"{args.phase}-request.json").read_bytes())
    if (
        (run / "completion.json").exists()
        or invocation.get("contract") != CONTRACT
        or invocation.get("invocation_id") != args.invocation_id
        or invocation.get("plan_sha256") != args.plan_sha256
        or invocation.get("entrypoint_sha256") != record(__file__)["sha256"]
        or request
        != {
            "phase": args.phase,
            "invocation_id": args.invocation_id,
            "plan_sha256": args.plan_sha256,
        }
    ):
        raise ValueError("Worker requires this supervisor's live, explicit phase request")
    output = run / args.phase
    if output.exists() or output.is_symlink():
        raise FileExistsError("Phase output must be new")
    output.mkdir(exist_ok=False)
    report = {
        "contract": CONTRACT,
        "phase": args.phase,
        "pid": os.getpid(),
        "python_executable": str(Path(sys.executable).resolve()),
        "native_exit_code": 1,
        "plan_sha256": args.plan_sha256,
        "sources_before": before,
        "sources_after": None,
        "source_bindings_unchanged": False,
        "optimization": asdict(budget),
        "training_budget_changed": True,
        "continuation_kind": plan["continuation_kind"],
        **plan["claims"],
        "model_loaded": False,
        "inference_performed": False,
        "optimizer_performed": False,
        "forward_backward_performed": False,
        "optimizer_update_uncertain": False,
        "training_completed": False,
        "optimizer_state_restored": False,
        "scheduler_state_restored": False,
        "rng_state_restored": False,
        "prior_optimizer_steps_observed": 86,
        "resubstitution_only": True,
        "actual_live_camera_parity_verified": False,
    }
    try:
        emit("balanced_worker_admitted", phase=args.phase, pid=os.getpid())
        with repository_imports():
            old, samples, targets, schedule, pilot = semantic_admission(plan)
            require_phase_prerequisites(run, args.phase, plan, budget)
            if args.phase == "cpu-encode":
                import torch

                from scripts.verify_onboard_navigation_actor_inputs import official_processor

                if torch.cuda.is_initialized():
                    raise ValueError("CPU encoding cannot initialize CUDA")
                encoded = encode_all(
                    plan,
                    budget,
                    old,
                    samples,
                    pilot,
                    official_processor(Path(old["base"]["snapshot"]), budget),
                    torch,
                )
                if torch.cuda.is_initialized():
                    raise ValueError("CPU encoding initialized CUDA")
                report["cpu_only"] = True
            else:
                with loading_scope(plan, old, output, report):
                    backend = load_backend(plan, budget, old, pilot, args.phase, output, report)
                    encoded = encode_all(
                        plan, budget, old, samples, pilot, backend.processor, backend.torch
                    )
                    if args.phase == "baseline":
                        for name, parent in (("base", True), ("warm_start", False)):
                            values = evaluate_all(
                                backend, samples, encoded, targets, pilot, budget, parent=parent
                            )
                            create_json(output / f"{name}-all86.json", values)
                        report["inference_performed"] = True
                    elif args.phase == "train":
                        report["backward_preflights"] = []
                        for probe in plan["diagnostics"]["smoke_probes"]:
                            report["backward_attempted"] = True
                            preflight = backend.backward_preflight(encoded[probe["sample_id"]])
                            pilot.check_memory(backend, budget)
                            report["forward_backward_performed"] = True
                            report["backward_preflights"].append(
                                {"sample_id": probe["sample_id"], **preflight}
                            )
                        if (
                            pilot.core.parameter_digest(backend.model, trainable=True) != WARM_SHA
                            or pilot.core.parameter_digest(backend.model, trainable=False)
                            != BASE_SHA
                        ):
                            raise ValueError("Backward-only preflight changed parameter bytes")
                        train_draws(
                            backend,
                            encoded,
                            schedule["draws"],
                            plan,
                            Path(args.plan),
                            args.plan_sha256,
                            pilot,
                            budget,
                            output,
                            report,
                        )
                        report["adapter_parameter_sha256_after"] = pilot.core.parameter_digest(
                            backend.model, trainable=True
                        )
                        adapter_dir = output / "adapter"
                        backend.model.save_pretrained(str(adapter_dir), safe_serialization=True)
                        weights = adapter_dir / "adapter_model.safetensors"
                        from safetensors.torch import load_file

                        state = load_file(str(weights), device="cpu")
                        if (
                            pilot.recovery.saved_parameter_digest(
                                state, backend.trainable_audit, rank=budget.rank
                            )
                            != report["adapter_parameter_sha256_after"]
                        ):
                            raise ValueError("Saved adapter differs from actual trained bytes")
                        del state
                        report["saved_adapter_weights"] = record(weights)
                        report["saved_adapter_config"] = record(adapter_dir / "adapter_config.json")
                        values = evaluate_all(
                            backend, samples, encoded, targets, pilot, budget, parent=False
                        )
                        create_json(output / "candidate-all86.json", values)
                        report["inference_performed"] = True
                    else:
                        previous = strict_json((run / "train/candidate-all86.json").read_bytes())
                        expected = {row["sample_id"]: row for row in previous}
                        selected = {s.sample_id: s for s in samples}
                        probes = [
                            selected[p["sample_id"]] for p in plan["diagnostics"]["smoke_probes"]
                        ]
                        values = evaluate_all(
                            backend, probes, encoded, targets, pilot, budget, parent=False
                        )
                        for actual in values:
                            prior = expected[actual["sample_id"]]
                            for key in ("raw_output", "output_token_ids"):
                                if actual["generated"][key] != prior["generated"][key]:
                                    raise ValueError("Fresh reload changed actual generated tokens")
                            for key in (
                                "diagnostic",
                                "actual_prefix_tensors",
                                "actor_identity_sha256",
                            ):
                                if actual[key] != prior[key]:
                                    raise ValueError("Fresh reload input/diagnosis differs")
                        create_json(output / "regenerated-probes.json", values)
                        report["fresh_saved_adapter_reload_verified"] = True
                        report["inference_performed"] = True
                    base_after = pilot.core.parameter_digest(backend.model, trainable=False)
                    if base_after != BASE_SHA:
                        raise ValueError("Frozen base bytes changed")
                    report["base_parameter_sha256_after"] = base_after
                    adapter_after = pilot.core.parameter_digest(backend.model, trainable=True)
                    if (
                        args.phase != "train"
                        and adapter_after != report["adapter_parameter_sha256_before"]
                    ):
                        raise ValueError("Inference/reload changed adapter bytes")
                    report["adapter_parameter_sha256_after"] = adapter_after
                verify_load_observations(report, plan, run)
            create_json(
                output / "prepared-inputs.json", {k: metadata(v) for k, v in encoded.items()}
            )
        after = capture_sources(plan, args.plan)
        if after != before:
            raise ValueError("Sources changed during worker execution")
        report["sources_after"] = after
        report["source_bindings_unchanged"] = True
        report["sample_count"] = 86
        report["all86_full_chat_encoding_verified"] = True
        report["native_exit_code"] = 0
    except Exception as exc:
        traceback.print_exc()
        report.update(failure_type=type(exc).__name__, failure=str(exc))
        if args.phase == "train" and (output / "training-events.jsonl").is_file():
            try:
                with repository_imports():
                    from scripts import onboard_navigation_balanced_journal as journal

                    record_optimizer_observation(
                        report,
                        journal.verify_journal(
                            output / "training-events.jsonl", read_bound(plan["schedule"])["draws"]
                        ),
                    )
            except Exception as journal_error:
                report["journal_error"] = str(journal_error)
                report["optimizer_update_uncertain"] = True
                if report["optimizer_performed"] is not True:
                    report["optimizer_performed"] = None
    create_json(output / "report.json", report)
    emit(
        "balanced_worker_finished",
        phase=args.phase,
        native_exit_code=report["native_exit_code"],
        report=record(output / "report.json"),
    )
    return report["native_exit_code"]


def supervise(args):
    plan, budget, before = admit_plan(args.plan, args.plan_sha256)
    output = fresh_run_directory(args.output_dir)
    output.mkdir(exist_ok=False)
    completion = {
        "contract": CONTRACT,
        "plan_sha256": args.plan_sha256,
        "started_at": datetime.now(UTC).isoformat(),
        "native_exit_code": 1,
        "sources_before": before,
        "phases": [],
        **plan["claims"],
        "automatic_retry_performed": False,
    }
    invocation_id = uuid.uuid4().hex
    create_json(
        output / "invocation.json",
        {
            "contract": CONTRACT,
            "invocation_id": invocation_id,
            "plan_sha256": args.plan_sha256,
            "entrypoint_sha256": record(__file__)["sha256"],
            "supervisor_pid": os.getpid(),
            "started_at": completion["started_at"],
        },
    )
    try:
        phases = (
            ("cpu-encode",)
            if args.cpu_encode_only
            else ("cpu-encode", "baseline", "train", "reload")
        )
        for phase in phases:
            create_json(
                output / f"{phase}-request.json",
                {
                    "phase": phase,
                    "invocation_id": invocation_id,
                    "plan_sha256": args.plan_sha256,
                },
            )
            command = [
                sys.executable,
                "-I",
                "-B",
                "-X",
                "utf8",
                str(Path(__file__).resolve()),
                "--worker",
                "--phase",
                phase,
                "--plan",
                str(Path(args.plan).resolve()),
                "--plan-sha256",
                args.plan_sha256,
                "--output-dir",
                str(output),
                "--invocation-id",
                invocation_id,
            ]
            stage = {"phase": phase, "argv": command, "started_at": datetime.now(UTC).isoformat()}
            completion["phases"].append(stage)
            emit("balanced_phase_starting", phase=phase)
            with (
                (output / f"{phase}-stdout.bin").open("xb", buffering=0) as stdout,
                (output / f"{phase}-stderr.bin").open("xb", buffering=0) as stderr,
            ):
                child = subprocess.Popen(
                    command,
                    cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                )
                stage["launcher_pid"] = child.pid
                try:
                    observation = capture_process_observation(child.pid)
                    interpreters = [
                        row["path"]
                        for row in plan["source_files"]
                        if Path(row["path"]).name.lower() == "python.exe"
                    ]
                    stage["process_identity"] = verify_process_observation(
                        observation, child.pid, command, interpreters, supervisor_pid=os.getpid()
                    )
                    create_json(output / f"{phase}-process-observation.json", observation)
                except Exception as exc:
                    stage["process_identity_failure"] = f"{type(exc).__name__}: {exc}"
                # Native status is authoritative; do not infer terminal from an observation timeout.
                stage["native_exit_code"] = child.wait()
            stage["ended_at"] = datetime.now(UTC).isoformat()
            report_path = output / phase / "report.json"
            if stage["native_exit_code"] != 0 or not report_path.is_file():
                raise ValueError("Phase failed; preserve native status/logs and do not retry")
            report = strict_json(report_path.read_bytes())
            if (
                report["native_exit_code"] != 0
                or report["phase"] != phase
                or report["plan_sha256"] != args.plan_sha256
                or report["sources_before"] != before
                or report["sources_after"] != before
                or report["sample_count"] != 86
                or report["all86_full_chat_encoding_verified"] is not True
                or any(report[k] is not False for k in FALSE_CLAIMS)
            ):
                raise ValueError("Actual completed phase admission differs")
            if (
                "process_identity_failure" in stage
                or report["pid"] != stage["process_identity"]["actual_worker_pid"]
            ):
                raise ValueError("Worker PID does not match the live exact process invocation")
            stage["worker_pid"] = report["pid"]
            stage["report"] = record(report_path)
            stage["finished_events"] = verify_finished_events(
                output / f"{phase}-stdout.bin", phase, report["pid"], stage["report"]
            )
            stage["verified_outputs"] = validate_phase_outputs(output, phase, plan, budget, report)
            create_json(output / f"{phase}-accepted.json", stage)
            emit("balanced_phase_completed", phase=phase, native_exit_code=0)
        after = capture_sources(plan, args.plan)
        if after != before:
            raise ValueError("Supervisor source bindings changed")
        completion["sources_after"] = after
        completion["native_exit_code"] = 0
    except Exception as exc:
        traceback.print_exc()
        completion.update(failure_type=type(exc).__name__, failure=str(exc))
    completion["ended_at"] = datetime.now(UTC).isoformat()
    create_json(output / "completion.json", completion)
    return completion["native_exit_code"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-plan", action="store_true")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--cpu-encode-only", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--phase", choices=("cpu-encode", "baseline", "train", "reload"), help=argparse.SUPPRESS
    )
    parser.add_argument("--invocation-id", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.emit_plan:
            if any(
                (
                    args.plan,
                    args.plan_sha256,
                    args.output_dir,
                    args.worker,
                    args.phase,
                    args.invocation_id,
                    args.cpu_encode_only,
                )
            ):
                raise ValueError("Plan emission is not a run or a file write")
            print(json.dumps(build_plan(), ensure_ascii=False, sort_keys=True, allow_nan=False))
            return 0
        if args.plan is None or args.plan_sha256 is None or args.output_dir is None:
            raise ValueError("Run requires explicit plan, SHA and new output directory")
        if (
            args.worker != (args.phase is not None)
            or args.worker
            and args.cpu_encode_only
            or args.worker != (args.invocation_id is not None)
        ):
            raise ValueError("Internal phase and worker flags must agree")
        return worker(args) if args.worker else supervise(args)
    except Exception as exc:
        emit("balanced_invocation_rejected", error_type=type(exc).__name__, error=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
