"""Bounded candidate-only offline readout experiment; never a flight/default path.

Only execute loads the exact frozen Qwen3-VL+267 LoRA. The four CPU FP32 heads
each have a precommitted final200 rule. LOEO holds out head labels, not backbone
exposure; same-episode permutation is only an input diagnostic. Original59 is
read by the model once after the final86 head is saved, reloaded and locked.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import importlib.metadata
import json
import os
import random
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.onboard-navigation-action-readout-experiment-v1"
NEW_FILES = (
    "scripts/experiment_onboard_navigation_action_readout.py",
    "tests/test_experiment_onboard_navigation_action_readout.py",
)
BOUND = {
    "terminal": (
        "docs/evidence/onboard-navigation-recovery267-terminal-20261008-r2.json",
        "4a40567318bffbd547f68f025e5ce5f60012056df5ed0af10615f6eccf553203",
    ),
    "actual59": (
        "docs/evidence/onboard-navigation-original59-actual-evaluation-20261008-r1.json",
        "f096470524722f482f8e248d4e3bd115105ae7b39d8538bd16676cdb1bfeb2ef",
    ),
    "evaluation_plan": (
        "configs/training/onboard-navigation-original59-evaluation-20261008-r1.json",
        "298ae322ef320a590d6853cc4b997f3f9691e8d18f8d1c81c02f18845197fb6c",
    ),
}
PINS = {
    "scripts/evaluate_onboard_navigation_original82.py": (
        "6d3f502df0dbad6cc511634a06b70a97b84598451a4d83f1e1f15b6f53ff2b69"
    ),
    "scripts/onboard_navigation_action_readout.py": (
        "b4191c30b7e927fad7845347932f699e32c928ed45bca291d2f526b5e8860367"
    ),
    "scripts/onboard_navigation_closed_sample_encoding.py": (
        "5f3bc0e7284c5a92bca1a4781898546eda2ec30c58626b1f321a023a179455b9"
    ),
    "tests/test_onboard_navigation_action_readout.py": (
        "e1b45d46fdd4e81ee573ec9efe8878d4f93802c6502ac5d716c5e81d4b603eb4"
    ),
}
RULE = {
    "feature_source": "exact_recovery267_candidate_only",
    "feature_dim": 2048,
    "head_parameters": 12294,
    "seed": 20261008,
    "updates_per_head": 200,
    "maximum_optimizer_attempts": 800,
    "optimizer": "CPU_FP32_SGD_full_batch",
    "learning_rate": 0.01,
    "feature_normalization": "none",
    "checkpoint_rule": "final_update_only_no_best_no_retry",
    "feature_forward": "Qwen3VLModel.last_hidden_state_last_nonzero_prefix_mask",
    "permutation": "seeded_nonzero_cyclic_offset_within_heldout_episode_no_training",
    "missing_scale": "None_masked_not_zero",
    "training_scale_scope": "retained_labels_only_not_arbitrary_scale_skill",
}
FALSE_CLAIMS = (
    "base_or_adapter_optimizer_performed",
    "text_generation_performed",
    "quality_claim",
    "candidate_promoted",
    "default_model_changed",
    "execution_authority",
    "flight_performed",
    "final_release_allowed",
    "official_holdout_verified",
    "independent_backbone_holdout",
    "pristine_base_or_warm_comparison",
    "generation_only_cause_proven",
    "end_to_end_realtime_latency_verified",
    "training_budget_reset",
)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def safe_path(value, *, fresh=False):
    path = ROOT / value
    if not path.resolve().is_relative_to(ROOT.resolve()) or ".." in path.parts:
        raise ValueError("Path escapes repository")
    cursor = ROOT
    for part in path.relative_to(ROOT).parts:
        cursor /= part
        if cursor.is_symlink() or (
            cursor.exists()
            and getattr(cursor.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("Reparse/symlink source or output denied")
    if fresh and path.exists():
        raise FileExistsError("Create-only output exists")
    return path


def small_record(path):
    path = safe_path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= 2 * 1024 * 1024:
        raise ValueError("Small source cap")
    with path.open("rb") as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if not 0 < len(raw) <= 2 * 1024 * 1024:
        raise ValueError("Small source cap")
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def evaluation_api():
    # This stdlib-only authentication happens before importing repository code.
    for name, digest in PINS.items():
        if small_record(name)["sha256"] != digest:
            raise ValueError("Frozen dependency SHA changed")
    original = sys.path[:]
    try:
        sys.path[:0] = [str(ROOT), str(ROOT / "src")]
        return importlib.import_module("scripts.evaluate_onboard_navigation_original82")
    finally:
        sys.path[:] = original


def runtime_readout_sources():
    # File lookup via installed metadata does not import Torch/Transformers.
    entries = {
        "transformers": ("transformers/models/qwen3_vl/modeling_qwen3_vl.py",),
        "safetensors": ("safetensors/torch.py",),
        "torch": ("torch/optim/sgd.py",),
        "peft": ("peft/__init__.py",),
    }
    result = []
    for package, names in entries.items():
        dist = importlib.metadata.distribution(package)
        for name in names:
            path = Path(dist.locate_file(name)).resolve(strict=True)
            if not 0 < path.stat().st_size <= 2 * 1024 * 1024:
                raise ValueError("Runtime entrypoint cap")
            with path.open("rb") as stream:
                raw = stream.read(2 * 1024 * 1024 + 1)
            if not 0 < len(raw) <= 2 * 1024 * 1024:
                raise ValueError("Runtime entrypoint cap")
            result.append(
                {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            )
    return result


def admit_runtime(plan, api):
    runtime = api.read_bound(plan["evaluation_plan"]["runtime_reference"])
    if Path(sys.executable).resolve() != Path(runtime["python_executable"]).resolve():
        raise ValueError("Pinned actual navigation runtime executable required")
    versions = runtime["package_versions"]
    if set(versions) != {"torch", "transformers", "peft", "accelerate", "safetensors"}:
        raise ValueError("Exact inherited five runtime distributions required")
    for package, version in versions.items():
        if importlib.metadata.version(package) != version:
            raise ValueError("Actual navigation runtime distribution differs")
    return {"python_executable": str(Path(sys.executable).resolve()), "package_versions": versions}


def verify_runtime_modules(plan, api):
    observed = {}
    for name, suffix in (
        ("torch", "/torch/__init__.py"),
        ("peft", "/peft/__init__.py"),
        ("torch.optim.sgd", "/torch/optim/sgd.py"),
        ("safetensors.torch", "/safetensors/torch.py"),
    ):
        bindings = [
            row
            for row in plan["runtime_sources"]
            if row["path"].replace("\\", "/").endswith(suffix)
        ]
        if len(bindings) != 1:
            raise ValueError("Exact imported runtime entrypoint binding required")
        binding = bindings[0]
        # Authenticate bytes before import, then check the actual imported module origin.
        expected = api.verify(binding, runtime=True).resolve()
        module = importlib.import_module(name)
        if Path(module.__file__).resolve() != expected:
            raise ValueError("Actual imported runtime module path differs")
        api.verify(binding, runtime=True)
        observed[name] = binding
    return observed


def load_samples(binding, api):
    path = api.verify(binding)
    if binding["bytes"] > 2 * 1024 * 1024:
        raise ValueError("Typed metadata cap")
    navigation = api.apis().navigation
    samples = tuple(
        navigation.NavigationSupervisionSample.from_json(row)
        for row in path.read_bytes().splitlines()
        if row.strip()
    )
    navigation.validate_navigation_episode_samples(samples)
    return samples


def episode_folds(samples):
    ids = [s.sample_id for s in samples]
    episodes = sorted({s.episode_id for s in samples})
    if len(ids) != 86 or len(set(ids)) != 86 or len(episodes) != 3:
        raise ValueError("Exactly86 unique rows from three complete episodes required")
    result = []
    for episode in episodes:
        held = [s.sample_id for s in samples if s.episode_id == episode]
        train = [s.sample_id for s in samples if s.episode_id != episode]
        if len(held) < 2 or set(held) & set(train) or set(held + train) != set(ids):
            raise ValueError("Whole-episode disjoint fold required")
        result.append({"episode_id": episode, "train_ids": train, "heldout_ids": held})
    return result


def train_forward_reference(samples, api):
    targets = {
        canonical(api.gold_mapping(sample))
        for sample in samples
        if sample.planned_action.kind == "translate"
        and sample.planned_action.translation_m[0] > 0
        and tuple(sample.planned_action.translation_m[1:]) == (0.0, 0.0)
    }
    return api.strict_json(next(iter(targets))) if len(targets) == 1 else None


def build_plan(output_dir):
    output = safe_path(output_dir, fresh=True)
    if output.parent != ROOT / "artifacts/evaluation" or not output.name.startswith(
        "onboard-navigation-action-readout-"
    ):
        raise ValueError("One NEW action-readout experiment directory required")
    api = evaluation_api()
    refs = {key: api.explicit_binding(path, digest) for key, (path, digest) in BOUND.items()}
    inherited = api.read_bound(refs["evaluation_plan"])
    api.authenticate_code([r for r in inherited["source_files"] if Path(r["path"]).suffix == ".py"])
    env = api.apis()
    recovery = api.read_bound(inherited["recovery_plan"])
    terminal, train, _reload = api.verify_terminal(api.read_bound(refs["terminal"]), recovery, env)
    receipt = api.read_bound(refs["actual59"])
    execution = receipt["actual_execution"]
    if (
        receipt["contract"]
        != "multidetect.onboard-navigation-original59-actual-evaluation-evidence-v1"
        or type(execution["actual_native_exit_code"]) is not int
        or execution["actual_native_exit_code"] != 0
        or execution["processes_terminated"] is not True
    ):
        raise ValueError("Closed native original59 reference receipt required")
    for row in receipt["bindings"]:
        api.verify(row)
    receipt_bindings = {str(api.bound_path(row)): row for row in receipt["bindings"]}
    declared_plan = receipt_bindings.get(str(api.bound_path(refs["evaluation_plan"])))
    if declared_plan is None or declared_plan["sha256"] != refs["evaluation_plan"]["sha256"]:
        raise ValueError("Actual59 receipt must bind this exact evaluation plan")
    reference_report_binding = receipt_bindings[str(Path(inherited["output_dir"]) / "report.json")]
    reference_report = api.read_bound(reference_report_binding)
    if (
        type(reference_report.get("native_exit_code")) is not int
        or reference_report["native_exit_code"] != 0
        or reference_report.get("source_bindings_unchanged") is not True
        or reference_report.get("durable_prediction_count") != 59
    ):
        raise ValueError("Closed actual59 reference report required")
    bridge = api.read_bound(inherited["dataset_report"])
    _all82, dev, actors = api.admit_dataset(bridge, env)
    if [s.sample_id for s in dev] != inherited["sample_ids"]:
        raise ValueError("Bound17+42 dev roles differ")
    legacy = api.read_bound(inherited["legacy_plan"])
    old = api.read_bound(legacy["original_plan"])
    samples = load_samples(old["dataset_samples"], api)
    folds = episode_folds(samples)
    if [s.sample_id for s in samples] != old["train_sample_ids"]:
        raise ValueError("Original ordered86 differs")
    prepared = api.read_bound(recovery["prepared_inputs"])
    env.old.validate_prepared_inputs(prepared, legacy, env.old.BalancedTrainingBudget())
    additional = [
        *refs.values(),
        *receipt["bindings"],
        *terminal.values(),
        *(small_record(name) for name in (*NEW_FILES, *PINS)),
    ]
    repo = api.normalize(inherited["source_files"] + additional)
    runtime = api.normalize(inherited["runtime_sources"] + runtime_readout_sources(), runtime=True)
    return {
        "schema_version": 1,
        "contract": CONTRACT + "-plan",
        "rule": RULE.copy(),
        "output_dir": str(output),
        "references": refs,
        "evaluation_plan": inherited,
        "source_files": repo,
        "runtime_sources": runtime,
        "folds": folds,
        "old86_ids": [s.sample_id for s in samples],
        "train_derived_forward_reference_target": train_forward_reference(samples, api),
        "dev59_ids": [s.sample_id for s in dev],
        "dev_actor_identities": {
            s.sample_id: actors[s.sample_id]["actor_identity_sha256"] for s in dev
        },
        "prepared_inputs": recovery["prepared_inputs"],
        "legacy_plan": inherited["legacy_plan"],
        "base_weights": old["base"]["safetensors"],
        "adapter_weights": terminal["weights"],
        "generative_reference86": terminal["candidate86"],
        "generative_reference59": reference_report["predictions"],
        "expected_base_parameter_sha256": recovery["expected_base_parameter_sha256"],
        "expected_adapter_parameter_sha256": train["adapter_parameter_sha256_after"],
        "claims": dict.fromkeys(FALSE_CLAIMS, False),
        "split_scope": "head_LOEO_on_backbone_seen86_then_original_TRAIN_internal59_development",
        "unsupported_development_actions": {"up": 1, "down": 1},
        "source_inventory_scope": (
            "inherited_exact_eval_inventory_plus_readout_entries_not_all_transitive_code"
        ),
    }


def admit_plan(path, digest):
    api = evaluation_api()
    binding = api.explicit_binding(safe_path(path), digest)
    plan = api.read_bound(binding)
    api.authenticate_code([r for r in plan["source_files"] if Path(r["path"]).suffix == ".py"])
    if canonical(plan) != canonical(build_plan(plan["output_dir"])):
        raise ValueError("Experiment plan differs from the precommitted exact experiment")
    return plan, binding, api


def freeze_backend(backend, api):
    model = backend.model
    initial = parameter_identity(backend, api)
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    model.zero_grad(set_to_none=True)
    model.eval()
    if any(p.requires_grad or p.grad is not None for p in model.parameters()):
        raise ValueError("Every base/LoRA parameter must be frozen without gradients")
    return initial


def parameter_identity(backend, api):
    state = backend.peft.get_peft_model_state_dict(backend.model, adapter_name="default")
    return {
        "base": api.pilot.core.parameter_digest(backend.model, trainable=False),
        "adapter": api.pilot.recovery.saved_parameter_digest(
            state, backend.trainable_audit, rank=4
        ),
    }


def check_frozen(backend):
    from scripts.onboard_navigation_inference_backend import PreloadedNavigationInferenceBackend

    PreloadedNavigationInferenceBackend._check_adapter_state(backend.model)
    if backend.model.training or any(
        p.requires_grad or p.grad is not None for p in backend.model.parameters()
    ):
        raise ValueError("Frozen feature model mode/parameters drifted")


def closed_prefix(sample, backend, env, closed, *, reference=None, expected=None):
    if expected is not None:
        value = closed.encode_closed_navigation_sample(
            sample,
            ROOT,
            processor=backend.processor,
            torch_module=backend.torch,
            budget=env.old.BalancedTrainingBudget(),
            prefix_reference=reference,
            expected_metadata=expected,
        )
        prefix = value["prefix"]
        identity = value["actor_identity_sha256"]
        del value
        return prefix, identity
    session = closed.ClosedNavigationSourceValidationSession(ROOT)
    body_completed = False
    prefix = None
    try:
        with session:
            session.prime((sample,))
            actor = env.actor.NavigationActorInput.from_supervision_sample(
                sample, ROOT, session=session
            )
            prefix = env.prefix.process_actor(actor, backend.processor, env.actor)
            env.prefix.validate_prefix_inputs(
                prefix,
                processor=backend.processor,
                image_count=len(actor.frames),
                torch_module=backend.torch,
                tensor_record=env.pilot.tensor_record,
            )
            identity = actor.identity_sha256
            body_completed = True
        if not body_completed or session.verified is not True or session._active:
            raise ValueError("Normal verified single-sample source exit required")
        return prefix, identity
    except BaseException:
        prefix = None
        raise


def extract_feature(backend, prefix, head_api, *, expected_runtime_path=None):
    """One fused multimodal prefill, no labels/generation/cache/all-layer tuple."""
    check_frozen(backend)
    torch = backend.torch
    if set(prefix) != {"input_ids", "attention_mask", "pixel_values", "image_grid_thw"}:
        raise ValueError("Only the four admitted prefix tensors may enter the model")
    base = backend.model.get_base_model()
    trunk = base.model
    if type(trunk).__name__ != "Qwen3VLModel":
        raise ValueError("Exact installed Qwen3VLModel multimodal trunk required")
    if expected_runtime_path is not None:
        module = sys.modules[type(trunk).__module__]
        if Path(module.__file__).resolve() != Path(expected_runtime_path).resolve():
            raise ValueError("Multimodal feature implementation differs from bound runtime")
    device = next(trunk.parameters()).device
    moved = {key: value.to(device) for key, value in prefix.items()}
    started = time.perf_counter()
    if device.type == "cuda":
        torch.cuda.synchronize()
    with torch.no_grad():
        output = trunk(
            **moved,
            use_cache=False,
            past_key_values=None,
            return_dict=True,
            output_hidden_states=False,
        )
        hidden = output.last_hidden_state
        if tuple(hidden.shape) != (*prefix["input_ids"].shape, 2048):
            raise ValueError("Actual fused final hidden state shape differs")
        feature = (
            head_api.extract_last_prefix_feature(hidden, moved["attention_mask"]).cpu().clone()
        )
    if device.type == "cuda":
        torch.cuda.synchronize()
    elapsed = time.perf_counter() - started
    check_frozen(backend)
    return feature, elapsed


def labels_for(samples, env, head_api):
    result = []
    names = {
        "forward": "forward",
        "yaw_left": "turn_yaw_left",
        "yaw_right": "turn_yaw_right",
        "stop": "sequence_stop",
    }
    for sample in samples:
        target = env.inference.parse_navigation_target(canonical(env.eval.gold_mapping(sample)))
        category = env.eval.primitive_class(target)
        if category not in names:
            raise ValueError("Unsupported action must not enter old86 head training")
        result.append(
            head_api.ActionReadoutLabel(
                names[category],
                distance_m=(target.translation_m[0] if category == "forward" else None),
                angle_magnitude_rad=(
                    abs(target.yaw_delta_rad)
                    if category.startswith("yaw_") and target.yaw_delta_rad is not None
                    else None
                ),
            )
        )
    return result


def head_digest(head, torch):
    digest = hashlib.sha256()
    for name, value in sorted(head.state_dict().items()):
        digest.update(canonical([name, list(value.shape), str(value.dtype)]))
        digest.update(value.detach().cpu().contiguous().view(torch.uint8).numpy().tobytes())
    return digest.hexdigest()


def fit_head(features, labels, *, head_api, torch, seed, updates, stream, ledger, head_id):
    if type(updates) is not int or not 1 <= updates <= 200:
        raise ValueError("At most200 precommitted full-batch updates per head")
    if features.device.type != "cpu" or features.dtype != torch.float32 or features.requires_grad:
        raise ValueError("Detached CPU FP32 features required")
    torch.manual_seed(seed)
    head = head_api.NavigationActionReadout()
    if sum(p.numel() for p in head.parameters()) != 12294:
        raise ValueError("Exactly12294 head-only parameters required")
    initial = head_digest(head, torch)
    optimizer = torch.optim.SGD(tuple(head.parameters()), lr=RULE["learning_rate"])
    weights = head_api.class_balanced_weights(labels)
    for step in range(1, updates + 1):
        optimizer.zero_grad(set_to_none=True)
        loss = head_api.readout_loss(head(features), labels, class_weights=weights)["total"]
        loss.backward()
        if any(
            p.grad is None or not torch.isfinite(p.grad).all().item() for p in head.parameters()
        ):
            raise ValueError("Finite head-only gradients required")
        for stage in ("attempted", "returned", "synchronized", "completed"):
            if stage == "attempted" and ledger[stage] >= 800:
                raise ValueError("Global head optimizer attempt budget exhausted")
            ledger[stage] += 1
            env_record = {
                "head_id": head_id,
                "update": step,
                "stage": stage,
                "global_count": ledger[stage],
                "loss": float(loss.detach()),
            }
            stream.write(canonical(env_record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
            if stage == "attempted":
                optimizer.step()
    head.zero_grad(set_to_none=True)
    head.eval()
    return head, {
        "initial_parameter_sha256": initial,
        "final_parameter_sha256": head_digest(head, torch),
        "updates": updates,
        "training_class_weights": weights.tolist(),
    }


def save_reload_head(head, directory, config, head_api, torch, api):
    from safetensors.torch import load_file, save

    directory.mkdir(exist_ok=False)
    api.create_json(directory / "config.json", config)
    raw = save({key: value.detach().cpu().contiguous() for key, value in head.state_dict().items()})
    with (directory / "head.safetensors").open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    clone = head_api.NavigationActionReadout()
    clone.load_state_dict(load_file(str(directory / "head.safetensors"), device="cpu"), strict=True)
    clone.eval()
    if head_digest(clone, torch) != head_digest(head, torch):
        raise ValueError("Saved head exact reload differs")
    for parameter in clone.parameters():
        parameter.requires_grad_(False)
    return clone, {
        "weights": api.record(directory / "head.safetensors"),
        "config": api.record(directory / "config.json"),
        "reloaded_parameter_sha256": head_digest(clone, torch),
    }


def permuted_ids(ids, seed):
    if len(ids) < 2 or len(set(ids)) != len(ids):
        raise ValueError("Nontrivial within-episode permutation required")
    offset = random.Random(seed).randrange(1, len(ids))
    return ids[offset:] + ids[:offset]


def predict_head(head, sample_id, feature, observation, bindings, head_api):
    started = time.perf_counter()
    with head_api.torch.no_grad():
        outputs = head(feature)
    elapsed = time.perf_counter() - started
    provenance = head_api.ActionReadoutProvenance(
        prefix_sha256=hashlib.sha256(canonical(observation["prefix_tensors"])).hexdigest(),
        base_weights_sha256=bindings["base_weights"]["sha256"],
        adapter_weights_sha256=bindings["adapter_weights"]["sha256"],
        head_weights_sha256=bindings["weights"]["sha256"],
        head_config_sha256=bindings["config"]["sha256"],
    )
    try:
        raw = head_api.readout_result(outputs, provenance=provenance)
        rejected = False
    except head_api.ActionReadoutRejected as exc:
        raw, rejected = exc.observation, True
    return {
        "sample_id": sample_id,
        "feature_source_sample_id": observation["sample_id"],
        "raw": raw,
        "selected_scale_rejected": rejected,
        "feature_observation": observation,
        "head_forward_elapsed_s": elapsed,
        "timing_scope": "offline_cached_feature_CPU_head_not_end_to_end_live",
        "text_generation_performed": False,
    }


def summarize_scores(rows):
    classes = sorted({r["score"]["gold_class"] for r in rows})
    per_class = {}
    for name in classes:
        group = [r["score"] for r in rows if r["score"]["gold_class"] == name]
        correct = sum(r["action_class_correct"] for r in group)
        comparable = [r for r in group if r["metric_comparable"]]
        per_class[name] = {
            "support": len(group),
            "action_correct": correct,
            "action_recall": correct / len(group),
            "metric_comparable": len(comparable),
            "metric_correct": sum(r["metric_correct"] is True for r in comparable),
            "raw_selected_class_correct": sum(r["raw_selected_class_correct"] for r in group),
        }
    confusion = {}
    for row in rows:
        score = row["score"]
        counts = confusion.setdefault(score["gold_class"], Counter())
        counts[score["raw_selected_class"]] += 1
    return {
        "rows": len(rows),
        "action_correct": sum(r["score"]["action_class_correct"] for r in rows),
        "unsupported": sum(not r["score"]["head_label_supported"] for r in rows),
        "joint_correct": sum(
            r["score"]["action_class_correct"] and r["score"]["metric_correct"] is True
            for r in rows
        ),
        "per_class": per_class,
        "raw_selected_class_correct": sum(r["score"]["raw_selected_class_correct"] for r in rows),
        "raw_selected_class_accuracy": sum(r["score"]["raw_selected_class_correct"] for r in rows)
        / len(rows)
        if rows
        else None,
        "scale_rejected_count": sum(r["score"]["selected_scale_rejected"] for r in rows),
        "raw_class_confusion": {gold: dict(counts) for gold, counts in confusion.items()},
        "macro_action_recall_present_classes": sum(r["action_recall"] for r in per_class.values())
        / len(classes)
        if classes
        else None,
        "scope": "strict_accepted_target_action_joint_separate_from_raw_classifier_accuracy",
    }


def score_readout_row(row, sample, api, env):
    target = row["raw"].get("target")
    score = api.score_prediction(
        canonical(target).decode() if target else "", api.gold_mapping(sample), env.inference
    )
    selected = {
        "forward": "forward",
        "turn_yaw_left": "yaw_left",
        "turn_yaw_right": "yaw_right",
        "sequence_stop": "stop",
    }[row["raw"]["selected_class"]]
    score.update(
        head_label_supported=score["gold_class"] in {"forward", "yaw_left", "yaw_right", "stop"},
        raw_selected_class=selected,
        raw_selected_class_correct=selected == score["gold_class"],
        selected_scale_rejected=row["selected_scale_rejected"],
    )
    return score


def always_forward_reference(rows, target, samples, api, env):
    """Mathematical bias reference; unique scale from old86, never fitted on dev59."""
    counts = Counter(row["score"]["gold_class"] for row in rows)
    metrics = (
        [
            api.score_prediction(
                canonical(target).decode(), api.gold_mapping(samples[r["sample_id"]]), env.inference
            )
            for r in rows
        ]
        if target is not None
        else []
    )
    return {
        "rows": len(rows),
        "gold_class_counts": dict(counts),
        "action_correct": counts["forward"],
        "action_accuracy": counts["forward"] / len(rows) if rows else None,
        "macro_action_recall_present_classes": (1 / len(counts) if counts["forward"] else 0)
        if rows
        else None,
        "train_derived_constant_target": target,
        "joint_correct": sum(
            r["action_class_correct"] and r["metric_correct"] is True for r in metrics
        )
        if metrics
        else None,
        "metric_comparable": sum(r["metric_comparable"] for r in metrics) if metrics else None,
        "scope": "mathematical_always_forward_unique_old86_scale_no_dev_fit_tokens_or_timing",
    }


def compare_generative_reference(plan, scores, samples, api, env, observations):
    """Re-score retained model text; no invented tokens, budgets or timings."""
    result = {}
    for name, binding, condition, ids in (
        ("old86", plan["generative_reference86"], "loeo", plan["old86_ids"]),
        ("dev59", plan["generative_reference59"], "locked_final86_dev59", plan["dev59_ids"]),
    ):
        path = api.verify(binding)
        if binding["bytes"] > 2 * 1024 * 1024:
            raise ValueError("Retained generative reference cap")
        raw = path.read_bytes()
        reference = (
            api.strict_json(raw)
            if name == "old86"
            else [api.strict_json(line) for line in raw.splitlines() if line.strip()]
        )
        if [r["sample_id"] for r in reference] != ids:
            raise ValueError("Retained generative reference exact ordered sample IDs differ")
        observed, elapsed = [], []
        for row in reference:
            current = observations[row["sample_id"]]
            tensors = row["actual_prefix_tensors"] if name == "old86" else row["prefix"]["tensors"]
            four = {"input_ids", "attention_mask", "pixel_values", "image_grid_thw"}
            if (
                set(tensors) != four
                or set(current["prefix_tensors"]) != four
                or canonical(tensors) != canonical(current["prefix_tensors"])
                or row["actor_identity_sha256"] != current["actor_identity_sha256"]
            ):
                raise ValueError(
                    "Historical text/readout Actor or exact four prefix tensors differ"
                )
            generated = row["generated"]
            api.validate_generated(generated)
            eligible = api.generation_eligibility(generated)["bounded_generation_eligible"]
            score = api.score_prediction(
                generated["raw_output"], api.gold_mapping(samples[row["sample_id"]]), env.inference
            )
            observed.append((score, eligible))
            elapsed.append(generated["elapsed_s"])
        head = {r["sample_id"]: r["score"] for r in scores if r["condition"] == condition}
        pairs = {}
        for metric in ("action", "joint"):
            for gated in (False, True):
                outcomes = Counter()
                for sid, (reference_score, eligible) in zip(ids, observed, strict=True):
                    a = head[sid]["action_class_correct"]
                    b = reference_score["action_class_correct"]
                    if metric == "joint":
                        a = a and head[sid]["metric_correct"] is True
                        b = b and reference_score["metric_correct"] is True
                    b = b and (eligible or not gated)
                    outcomes[
                        "both_correct"
                        if a and b
                        else "head_only"
                        if a
                        else "JSON_only"
                        if b
                        else "both_wrong"
                    ] += 1
                pairs[metric + ("_reference_budget_eligible" if gated else "_raw")] = dict(outcomes)
        result[name] = {
            "reference": binding,
            "rows": len(ids),
            "paired": pairs,
            "recorded_generation_elapsed_mean_s": sum(elapsed) / len(elapsed),
            "timing_scope": "historical_different_run_not_paired_end_to_end_latency",
            "no_generation_budget_assigned_to_readout": True,
            "same_actor_and_four_prefix_tensors_verified": True,
        }
    return result


def run(args):
    plan, plan_binding, api = admit_plan(args.plan, args.plan_sha256)
    before = api.capture_sources(plan, plan_binding)
    output = safe_path(plan["output_dir"], fresh=True)
    output.mkdir(exist_ok=False)
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        "plan": plan_binding,
        "native_exit_code": 1,
        "success": False,
        "claims": dict.fromkeys(FALSE_CLAIMS, False),
        "optimizer_steps": dict.fromkeys(("attempted", "returned", "synchronized", "completed"), 0),
        "source_bindings_before": before,
        "source_bindings_unchanged": False,
        "model_loaded": False,
        "head_locked_before_dev59": False,
        "feature_count": 0,
        "raw_prediction_count": 0,
        "rule": plan["rule"],
    }
    backend = None
    try:
        report["runtime_admission"] = admit_runtime(plan, api)
        runtime_modules = verify_runtime_modules(plan, api)
        report["runtime_modules_before"] = runtime_modules
        env = api.apis()
        env.eval = api
        closed = importlib.import_module("scripts.onboard_navigation_closed_sample_encoding")
        head_api = importlib.import_module("scripts.onboard_navigation_action_readout")
        legacy = api.read_bound(plan["legacy_plan"])
        old = api.read_bound(legacy["original_plan"])
        samples = load_samples(old["dataset_samples"], api)
        if (
            episode_folds(samples) != plan["folds"]
            or [s.sample_id for s in samples] != plan["old86_ids"]
            or train_forward_reference(samples, api)
            != plan["train_derived_forward_reference_target"]
        ):
            raise ValueError("Original86 fold identities changed")
        prepared = api.read_bound(plan["prepared_inputs"])
        references = {
            r["sample_id"]: r for r in api.read_bound(legacy["cpu_prefix_report"])["samples"]
        }
        inherited = plan["evaluation_plan"]
        recovery = api.read_bound(inherited["recovery_plan"])
        budget = env.recovery.RecoveryModelBudget(**inherited["model_budget"])
        budget.validate()
        run_dir = Path(recovery["output_dir"])
        loader = {"phase": "reload", "pid": os.getpid()}
        report["loader_observation"] = loader
        report["model_loading_attempted"] = True
        with env.old.loading_scope(legacy, old, run_dir / "reload", loader):
            backend = env.old.load_backend(
                legacy, budget, old, env.pilot, "reload", run_dir / "reload", loader
            )
        env.recovery.check_loader_cleanup(loader, legacy, old, run_dir)
        report["model_loaded"] = loader["model_loaded"] is True
        initial = freeze_backend(backend, env)
        if initial != {
            "base": plan["expected_base_parameter_sha256"],
            "adapter": plan["expected_adapter_parameter_sha256"],
        }:
            raise ValueError("Exact actual frozen candidate parameters differ")
        report["model_parameter_identity_before"] = initial
        features, observations = {}, {}
        runtime_path = next(
            r["path"]
            for r in plan["runtime_sources"]
            if r["path"].replace("\\", "/").endswith("/qwen3_vl/modeling_qwen3_vl.py")
        )

        def extract(sample, *, expected=None, reference=None):
            started = time.perf_counter()
            prefix, identity = closed_prefix(
                sample, backend, env, closed, expected=expected, reference=reference
            )
            records = {k: env.pilot.tensor_record(v, backend.torch) for k, v in prefix.items()}
            if expected is not None and records != expected["prefix_tensors"]:
                raise ValueError("Closed old86 actual prefix tensor bytes differ")
            if expected is None and identity != plan["dev_actor_identities"][sample.sample_id]:
                raise ValueError("Original59 shared Actor identity differs")
            feature, elapsed = extract_feature(
                backend, prefix, head_api, expected_runtime_path=runtime_path
            )
            del prefix
            observations[sample.sample_id] = {
                "sample_id": sample.sample_id,
                "actor_identity_sha256": identity,
                "prefix_tensors": records,
                "feature": env.pilot.tensor_record(feature, backend.torch),
                "fused_forward_elapsed_s": elapsed,
                "source_encoding_feature_elapsed_s": time.perf_counter() - started,
                "source_session_closed_verified": True,
                "gold_in_model_inputs": False,
            }
            features[sample.sample_id] = feature
            report["feature_count"] += 1
            print(
                json.dumps(
                    {
                        "event": "readout_feature_admitted",
                        "sample_id": sample.sample_id,
                        "feature_count": report["feature_count"],
                    }
                ),
                flush=True,
            )

        for sample in samples:
            extract(
                sample, expected=prepared[sample.sample_id], reference=references[sample.sample_id]
            )
        head_records, raw_rows = {}, []
        by_id = {s.sample_id: s for s in samples}
        with (
            (output / "head-updates.jsonl").open("xb") as updates,
            (output / "predictions.jsonl").open("xb") as predictions,
        ):
            for number, fold in enumerate(
                plan["folds"] + [{"train_ids": plan["old86_ids"], "heldout_ids": []}]
            ):
                name = f"fold-{number}" if number < 3 else "final86"
                ids = fold["train_ids"]
                matrix = backend.torch.cat([features[sid] for sid in ids])
                head, identity = fit_head(
                    matrix,
                    labels_for([by_id[sid] for sid in ids], env, head_api),
                    head_api=head_api,
                    torch=backend.torch,
                    seed=RULE["seed"] + number,
                    updates=RULE["updates_per_head"],
                    stream=updates,
                    ledger=report["optimizer_steps"],
                    head_id=name,
                )
                locked, files = save_reload_head(
                    head,
                    output / name,
                    {
                        "contract": CONTRACT + "-head",
                        "rule": plan["rule"],
                        "head_id": name,
                        "train_ids": ids,
                        "feature_matrix": env.pilot.tensor_record(matrix, backend.torch),
                        **identity,
                    },
                    head_api,
                    backend.torch,
                    api,
                )
                with backend.torch.no_grad():
                    original_raw = head(matrix)
                    reloaded_raw = locked(matrix)
                if not backend.torch.equal(original_raw, reloaded_raw):
                    raise ValueError("Saved/reloaded head changed actual six-output tensor bytes")
                files["saved_reload_raw_outputs_exact"] = True
                files["raw_reload_comparison"] = env.pilot.tensor_record(
                    original_raw, backend.torch
                )
                del original_raw, reloaded_raw
                head_records[name] = {**identity, **files}
                bindings = {**plan, **files}
                held = fold["heldout_ids"]
                for condition, sources in (
                    ("loeo", held),
                    (
                        "same_episode_permutation",
                        permuted_ids(held, RULE["seed"] + number) if held else [],
                    ),
                ):
                    for sid, feature_sid in zip(held, sources, strict=True):
                        row = predict_head(
                            locked,
                            sid,
                            features[feature_sid],
                            observations[feature_sid],
                            bindings,
                            head_api,
                        )
                        row.update(condition=condition, head_id=name)
                        row.update(partition=by_id[sid].partition, episode_id=by_id[sid].episode_id)
                        api.append_row(predictions, row)
                        raw_rows.append(row)
                        report["raw_prediction_count"] += 1
                if number == 3:
                    final_head, final_bindings = locked, bindings
                del head, matrix
            report["head_locked_before_dev59"] = True
            report["heads"] = head_records
            check_frozen(backend)
            if parameter_identity(backend, env) != initial:
                raise ValueError("Base/LoRA changed during feature/head stage")
            _all, dev, _actors = api.admit_dataset(api.read_bound(inherited["dataset_report"]), env)
            if [s.sample_id for s in dev] != plan["dev59_ids"]:
                raise ValueError("Only locked17+42 dev rows may enter model")
            for sample in dev:
                extract(sample)
                row = predict_head(
                    final_head,
                    sample.sample_id,
                    features[sample.sample_id],
                    observations[sample.sample_id],
                    final_bindings,
                    head_api,
                )
                row.update(condition="locked_final86_dev59", head_id="final86")
                row.update(partition=sample.partition, episode_id=sample.episode_id)
                api.append_row(predictions, row)
                raw_rows.append(row)
                report["raw_prediction_count"] += 1
            # All raw outputs are fsynced and the prediction file closed before gold scoring.
        all_samples = by_id | {s.sample_id: s for s in dev}
        scores = []
        for row in raw_rows:
            sample = all_samples[row["sample_id"]]
            score = score_readout_row(row, sample, api, env)
            scores.append(
                {
                    "sample_id": row["sample_id"],
                    "condition": row["condition"],
                    "partition": row["partition"],
                    "score": score,
                }
            )
        report["score_counts"] = {
            condition: summarize_scores([r for r in scores if r["condition"] == condition])
            for condition in ("loeo", "same_episode_permutation", "locked_final86_dev59")
        }
        report["generative_reference_comparison"] = compare_generative_reference(
            plan, scores, all_samples, api, env, observations
        )
        report["mathematical_always_forward_dev59"] = always_forward_reference(
            [r for r in scores if r["condition"] == "locked_final86_dev59"],
            plan["train_derived_forward_reference_target"],
            all_samples,
            api,
            env,
        )
        report["dev_partition_scores"] = {
            part: summarize_scores(
                [
                    r
                    for r in scores
                    if r["condition"] == "locked_final86_dev59" and r["partition"] == part
                ]
            )
            for part in ("dev-validation", "dev-test")
        }
        if (
            report["feature_count"] != 145
            or report["raw_prediction_count"] != 231
            or report["optimizer_steps"] != dict.fromkeys(report["optimizer_steps"], 800)
        ):
            raise ValueError(
                "Exact145 features/231 predictions/800 completed head updates required"
            )
        api.create_json(output / "scores.json", scores)
        api.create_json(output / "features.json", list(observations.values()))
        after_identity = parameter_identity(backend, env)
        report["model_parameter_identity_after"] = after_identity
        check_frozen(backend)
        if after_identity != initial:
            raise ValueError("Actual base/LoRA parameter bytes changed")
        for row in head_records.values():
            api.verify(row["weights"])
            api.verify(row["config"])
        report["runtime_modules_after"] = verify_runtime_modules(plan, api)
        if canonical(report["runtime_modules_after"]) != canonical(runtime_modules):
            raise ValueError("Actual imported runtime identity changed before seal")
        after = api.capture_sources(plan, plan_binding)
        report["source_bindings_after"] = after
        if canonical(after) != canonical(before):
            raise ValueError("Source/runtime inventory changed before seal")
        report.update(
            native_exit_code=0,
            success=True,
            source_bindings_unchanged=True,
            predictions=api.record(output / "predictions.jsonl"),
            scores=api.record(output / "scores.json"),
            features=api.record(output / "features.json"),
            head_updates=api.record(output / "head-updates.jsonl"),
            raw_durable_before_gold_scoring=True,
            unsupported_dev_rows_retained=2,
        )
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        traceback.print_exc()
    finally:
        if backend is not None or report.get("loader_observation", {}).get("model_loaded") is True:
            report["model_loaded"] = True
        backend = None
        gc.collect()
        api.create_json(output / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit-plan", action="store_true")
    mode.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--output-dir")
    parser.add_argument("--plan-output")
    args = parser.parse_args(argv)
    try:
        if args.emit_plan:
            if not args.output_dir or args.plan_sha256:
                raise ValueError("Plan emission requires only a fresh output directory")
            plan = build_plan(args.output_dir)
            if args.plan_output:
                path = safe_path(args.plan_output, fresh=True)
                if (
                    path.parent != ROOT / "configs/training"
                    or path.suffix != ".json"
                    or not path.name.startswith("onboard-navigation-action-readout-")
                ):
                    raise ValueError("Only a NEW configs/training action-readout plan is allowed")
                path.parent.mkdir(parents=True, exist_ok=True)
                safe_path(path, fresh=True)
                api = evaluation_api()
                api.create_json(path, plan)
                print(json.dumps(api.record(path), ensure_ascii=False, sort_keys=True))
                return 0
            print(
                json.dumps(
                    plan,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                )
            )
            return 0
        if not args.plan_sha256 or args.output_dir or args.plan_output:
            raise ValueError("Execute requires only explicit plan and SHA")
        return run(args)["native_exit_code"]
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
