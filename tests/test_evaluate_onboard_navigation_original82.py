"""CPU stand-ins only; no actual images, processor, model, GPU or terminal proof.

Small files exercise native-receipt admission, typed59 selection, generic scorer,
prefix observer restoration, offline orchestration and create-only publication.
The real campaign terminal artifacts are NOT produced or modified by this suite.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.autonomy import onboard_navigation_inference as inference
from multidetect.autonomy import onboard_navigation_supervision as navigation
from scripts import build_onboard_openfly_original82_supervision as bridge
from scripts import evaluate_onboard_navigation_original82 as evaluation
from scripts import onboard_navigation_host_memory as host_memory

REPO = Path(__file__).resolve().parents[1]
SHA = "a" * 64
CANDIDATE = "b" * 64
BASE = "c" * 64


def target(kind="translate", vector=None, yaw=None):
    return {
        "schema_version": 1,
        "contract": navigation.TARGET_CONTRACT,
        "kind": kind,
        "translation_frame": "body_frd",
        "yaw_convention": "clockwise_right_positive",
        "translation_m": vector,
        "yaw_delta_rad": yaw,
    }


@pytest.mark.parametrize(
    "gold,expected",
    [
        (target(vector=[3, 0, 0]), "forward"),
        (target(vector=[-3, 0, 0]), "backward"),
        (target(vector=[0, 3, 0]), "right"),
        (target(vector=[0, -3, 0]), "left"),
        (target(vector=[0, 0, -3]), "up"),
        (target(vector=[0, 0, 3]), "down"),
        (target(vector=[3, 1, -2]), "mixed_translation"),
        (target(), "translation_unknown"),
        (target("turn_yaw", yaw=0.5), "yaw_right"),
        (target("turn_yaw", yaw=-0.5), "yaw_left"),
        (target("turn_yaw"), "yaw_unknown"),
        (target("stop"), "stop"),
    ],
)
def test_generic_categories_and_seven_field_score(gold, expected):
    parsed = inference.parse_navigation_target(evaluation.canonical(gold))
    assert evaluation.primitive_class(parsed) == expected
    score = evaluation.score_prediction(json.dumps(gold), gold, inference)
    assert score["strict_target_valid"] and score["action_class_correct"]
    assert score["metric_correct"] is (
        True if parsed.metric_known or parsed.kind == "stop" else None
    )
    assert score["metric_comparable"] is (parsed.metric_known or parsed.kind == "stop")


@pytest.mark.parametrize(
    "raw,json_valid",
    [
        ('{"kind":', False),
        ("NaN", False),
        ('{"x":1,"x":2}', False),
        ('{"value":1e999}', False),
        ("```json\n{}\n```", False),
        ("{}", True),
        ("[]", True),
        ("null", True),
    ],
)
def test_invalid_json_vs_strict_contract_are_distinct(raw, json_valid):
    score = evaluation.score_prediction(raw, target(vector=[3, 0, 0]), inference)
    assert score["json_valid"] is json_valid
    assert score["strict_target_valid"] is False
    assert score["metric_correct"] is None and score["translation_l2_error_m"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "bool",
        "zero",
        "too_far",
        "wrong_frame",
        "wrong_contract",
        "stop_move",
        "kind_and_yaw",
    ],
)
def test_generic_strict_response_rejections(mutation):
    raw = target(vector=[3, 0, 0])
    if mutation == "extra":
        raw["confidence"] = 1
    elif mutation == "bool":
        raw["translation_m"][0] = True
    elif mutation == "zero":
        raw["translation_m"] = [0, 0, 0]
    elif mutation == "too_far":
        raw["translation_m"][0] = 501
    elif mutation == "wrong_frame":
        raw["translation_frame"] = "world"
    elif mutation == "wrong_contract":
        raw["schema_version"] = True
    elif mutation == "stop_move":
        raw["kind"] = "stop"
    else:
        raw["yaw_delta_rad"] = 0.5
    score = evaluation.score_prediction(json.dumps(raw), target(vector=[3, 0, 0]), inference)
    assert score["json_valid"] is True and score["strict_target_valid"] is False


def test_metric_errors_and_unknowns_do_not_invent_zero():
    gold = target(vector=[0, 0, -3])
    score = evaluation.score_prediction(json.dumps(target(vector=[1, 2, -2])), gold, inference)
    assert score["translation_component_absolute_error_m"] == [1, 2, 1]
    assert score["translation_l2_error_m"] == pytest.approx(6**0.5)
    assert score["metric_correct"] is False
    null = evaluation.score_prediction(json.dumps(target()), gold, inference)
    assert null["predicted_translation_is_null"] is True
    assert null["metric_comparable"] is False and null["translation_l2_error_m"] is None
    yaw = evaluation.score_prediction(
        json.dumps(target("turn_yaw", yaw=-0.5)), target("turn_yaw", yaw=0.5), inference
    )
    assert yaw["yaw_absolute_error_rad"] == 1
    assert yaw["action_class_correct"] is False


def generated(raw=None, count=2, elapsed=7.0):
    return {
        "raw_output": json.dumps(target(vector=[3, 0, 0])) if raw is None else raw,
        "output_token_ids": list(range(count)),
        "generated_tokens": count,
        "token_cap_reached": count >= 192,
        "elapsed_s": elapsed,
        "peak_allocated_bytes": 1,
        "peak_reserved_bytes": 2,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "bool_token",
        "count",
        "cap",
        "negative",
        "nan",
        "bool_time",
        "extra",
        "too_many",
        "no_tokens",
        "bool_peak",
    ],
)
def test_raw_generation_metadata_is_strict(mutation):
    row = generated()
    if mutation == "bool_token":
        row["output_token_ids"][0] = True
    elif mutation == "count":
        row["generated_tokens"] = 1
    elif mutation == "cap":
        row["token_cap_reached"] = True
    elif mutation == "negative":
        row["elapsed_s"] = -1
    elif mutation == "nan":
        row["elapsed_s"] = float("nan")
    elif mutation == "bool_time":
        row["elapsed_s"] = False
    elif mutation == "extra":
        row["invented"] = 1
    elif mutation == "too_many":
        row = generated(count=193)
    elif mutation == "no_tokens":
        row = generated(count=0)
    else:
        row["peak_reserved_bytes"] = True
    with pytest.raises(ValueError):
        evaluation.validate_generated(row)


def test_summary_counts_null_errors_caps_and_timing():
    rows = []
    for elapsed, raw in ((7.0, json.dumps(target(vector=[3, 0, 0]))), (31.0, json.dumps(target()))):
        rows.append(
            {
                "generated": generated(raw, count=192 if elapsed > 30 else 2, elapsed=elapsed),
                "score": evaluation.score_prediction(raw, target(vector=[3, 0, 0]), inference),
            }
        )
    summary = evaluation.summarize(rows)
    assert summary["sample_count"] == 2 and summary["strict_target_valid_count"] == 2
    assert summary["metric_comparable_count"] == 1 and summary["metric_correct_count"] == 1
    assert summary["translation_l2_error_m"] == {"count": 1, "mean": 0.0}
    assert (
        summary["token_cap_reached_count"] == 1
        and summary["elapsed_over_cooperative_30s_count"] == 1
    )
    assert summary["generation_elapsed_s"]["mean"] == 19
    assert summary["generation_elapsed_s"]["p95"] == pytest.approx(29.8)
    assert evaluation.summarize([])["translation_l2_error_m"]["mean"] is None


def test_cap_and_timeout_raw_correctness_do_not_grant_budget_eligibility():
    raw = json.dumps(target(vector=[3, 0, 0]))
    rows = [
        {
            "generated": generated(raw, count=count, elapsed=elapsed),
            "score": evaluation.score_prediction(raw, target(vector=[3, 0, 0]), inference),
        }
        for count, elapsed in ((191, 30.0), (192, 30.0), (191, 30.00000001), (192, 31.0))
    ]
    assert (
        evaluation.generation_eligibility(rows[0]["generated"])["bounded_generation_eligible"]
        is True
    )
    assert all(
        evaluation.generation_eligibility(r["generated"])["bounded_generation_eligible"] is False
        for r in rows[1:]
    )
    result = evaluation.summarize(rows)
    assert result["metric_correct_count"] == result["strict_target_valid_count"] == 4
    assert result["bounded_generation_eligible_count"] == result["eligible_correct_count"] == 1
    assert result["eligible_diagnostic_counts"]["metric_correct_count"] == 1
    assert result["budget_rejection_counts"] == {
        "token_cap_reached": 1,
        "cooperative_elapsed_exceeded": 1,
        "token_cap_and_cooperative_timeout": 1,
    }


def typed_dataset(root):
    fixture = root / "fixture.txt"
    fixture.write_bytes(b"small stand-in file, not an image")
    bound = evaluation.record(fixture)
    samples = []
    for annotation_index, (scene, part, count) in zip(
        (139, 220, 90), evaluation.ROLES, strict=True
    ):
        episode = hashlib.sha256(scene.encode()).hexdigest()
        frames = [
            {
                "frame_id": f"{episode}:{i}",
                "episode_id": episode,
                "scene_id": scene,
                "step_index": i,
                **bound,
                "width": 853,
                "height": 480,
                "encoding": "png",
                "source_clock_id": None,
                "source_stamp": None,
                "stamp_unit": None,
            }
            for i in range(count)
        ]
        source = {
            "repository_id": "IPEC-COMMUNITY/OpenFly",
            "revision": "fixture",
            "original_split": "train",
            "annotation_record_id": str(annotation_index),
            "annotation": bound,
            "pose": bound,
            "conversion": bound,
            "license": bound,
            "review_receipt": None,
            "unit_verification": bound,
            "alignment_verification": bound,
        }
        route = {
            "annotation_index": annotation_index,
            "scene_id": scene,
            "development_partition": part,
        }
        for i in range(count):
            raw = {
                "imageid": i,
                "pos": [float(i), 0.0, 0.0],
                "yaw": 0.0,
                "type": "go straight",
                "value": 3,
            }
            primitive = {
                "kind": "translate",
                "translation_m": [3.0, 0.0, 0.0],
                "yaw_delta_rad": None,
            }
            if i == count - 1:
                raw.update(type="stop", value=0)
                primitive.update(kind="stop", translation_m=None)
            elif scene == "env_airsim_23" and i in (0, 1, 2):
                if i < 2:
                    raw["type"] = "go up" if i == 0 else "go down"
                    primitive["translation_m"] = [0.0, 0.0, -3.0 if i == 0 else 3.0]
                else:
                    raw.update(type="turn right", value=30)
                    primitive.update(kind="turn_yaw", translation_m=None, yaw_delta_rad=0.5)
            samples.append(
                bridge.make_sample(
                    route,
                    {"gpt_instruction": "保留 exact prompt\n"},
                    raw,
                    primitive,
                    frames,
                    i,
                    source,
                    navigation,
                )
            )
    navigation.validate_navigation_episode_samples(tuple(samples))
    return tuple(samples)


def dataset_report(root, samples):
    file = root / "samples.jsonl"
    file.write_bytes(b"\n".join(evaluation.canonical(s.to_mapping()) for s in samples) + b"\n")
    return {
        "contract": "multidetect.openfly-original82-supervision-bridge-v1",
        "native_exit_code": 0,
        "sample_count": 82,
        "partition_counts": {part: count for _, part, count in evaluation.ROLES},
        "source_validation_session_verified": True,
        "sources_unchanged": True,
        "pose_future_truth_or_label_in_actor_input": False,
        "aggregate_annotation_actions_used_as_gold": False,
        "training_ready": False,
        "optimizer_authorized": False,
        "official_seen_unseen_holdout_verified": False,
        "source_bindings_before": [evaluation.record(root / "fixture.txt")],
        "source_bindings_after": [evaluation.record(root / "fixture.txt")],
        "converter": {
            "sha256": evaluation.PINNED_CODE[
                "scripts/build_onboard_openfly_original82_supervision.py"
            ]
        },
        "dataset": evaluation.record(file),
        "sample_actor_target_bindings": [
            {
                "sample_id": s.sample_id,
                "actor_identity_sha256": SHA,
                "target": evaluation.gold_mapping(s),
                "ordered_frame_steps": [f.step_index for f in s.frames],
            }
            for s in samples
        ],
    }


@pytest.fixture
def dataset(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    samples = typed_dataset(tmp_path)
    return SimpleNamespace(
        root=tmp_path,
        samples=samples,
        report=dataset_report(tmp_path, samples),
        api=SimpleNamespace(navigation=navigation),
    )


def test_only_ordered59_dev_rows_admitted(dataset):
    all_rows, selected, actors = evaluation.admit_dataset(dataset.report, dataset.api)
    assert len(all_rows) == 82 and len(selected) == 59
    assert [s.partition for s in selected] == ["dev-validation"] * 17 + ["dev-test"] * 42
    assert not any(s.scene_id == "env_airsim_26" for s in selected)
    assert len(actors) == 82
    classes = Counter(
        evaluation.primitive_class(
            inference.parse_navigation_target(evaluation.canonical(evaluation.gold_mapping(s)))
        )
        for s in selected
    )
    assert classes == {"forward": 54, "up": 1, "down": 1, "yaw_right": 1, "stop": 2}


@pytest.mark.parametrize(
    "mutation",
    [
        "native_bool",
        "not_verified",
        "changed_sources",
        "wrong_roles",
        "unreviewed_gold",
        "missing_actor",
        "reordered_actor",
        "future_history",
        "fake_target",
    ],
)
def test_dataset_admission_rejects_coverage_identity_or_receipt_change(dataset, mutation):
    report = dataset.report
    if mutation == "native_bool":
        report["native_exit_code"] = False
    elif mutation == "not_verified":
        report["source_validation_session_verified"] = False
    elif mutation == "changed_sources":
        report["source_bindings_after"] = []
    elif mutation == "wrong_roles":
        report["partition_counts"]["train"] = 24
    elif mutation == "unreviewed_gold":
        report["aggregate_annotation_actions_used_as_gold"] = True
    elif mutation == "missing_actor":
        report["sample_actor_target_bindings"].pop()
    elif mutation == "reordered_actor":
        report["sample_actor_target_bindings"].reverse()
    elif mutation == "future_history":
        report["sample_actor_target_bindings"][0]["ordered_frame_steps"] = [0, 1]
    else:
        report["sample_actor_target_bindings"][0]["target"]["translation_m"] = [5, 0, 0]
    with pytest.raises(ValueError):
        evaluation.admit_dataset(report, dataset.api)


@pytest.fixture
def terminal(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    run = tmp_path / "campaign"
    (run / "train/adapter").mkdir(parents=True)
    (run / "reload").mkdir()

    def write(name, value, raw=False):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if raw else evaluation.canonical(value) + b"\n")
        return evaluation.record(path)

    source = write("source.py", b"fixture source", raw=True)
    plan = {
        "output_dir": str(run),
        "source_files": [source],
        "schedule": [{"fixture": True}],
        "expected_base_parameter_sha256": BASE,
    }
    plan_binding = write(evaluation.R2_PLAN, plan)
    monkeypatch.setattr(evaluation, "R2_SHA", plan_binding["sha256"])
    rows = [
        {
            "sample_id": str(i),
            "actor_identity_sha256": SHA,
            "actual_prefix_tensors": {"fixture": i},
            "diagnostic": {"fixture": True},
            "generated": generated(),
        }
        for i in range(86)
    ]
    candidate = write("campaign/train/candidate-all86.json", rows)
    reloaded = write("campaign/reload/reloaded-all86.json", rows)
    weights = write(
        "campaign/train/adapter/adapter_model.safetensors", b"fixture-not-tensors", raw=True
    )
    config = write("campaign/train/adapter/adapter_config.json", {"fixture": True})
    journal = write(
        "campaign/train/training-events.jsonl", b"fixture-not-optimizer-evidence\n", raw=True
    )
    journal_summary = {"complete": True, "fixture_verifier": True}
    false_claims = ("quality_claim", "candidate_promoted", "execution_authority", "budget_reset")
    common = {
        "contract": "fixture-recovery",
        "plan_sha256": plan_binding["sha256"],
        "native_exit_code": 0,
        "model_loaded": True,
        "source_bindings_unchanged": True,
        "sources_before": [source, plan_binding],
        "sources_after": [source, plan_binding],
        "base_parameter_sha256_after": BASE,
        "adapter_parameter_sha256_after": CANDIDATE,
        **dict.fromkeys(false_claims, False),
    }
    train = {
        **common,
        "phase": "train",
        "journal": journal_summary,
        "optimizer_update_uncertain": False,
        "training_completed": True,
        "optimizer_performed": True,
        "cumulative_campaign_attempts": 280,
        **{f"optimizer_steps_{s}": 267 for s in evaluation.STAGES},
        "saved_adapter_weights": weights,
        "saved_adapter_config": config,
        "candidate_all86": candidate,
    }
    reload = {
        **common,
        "phase": "reload",
        "fresh_saved_adapter_reload_verified": True,
        "optimizer_performed": False,
        "adapter_parameter_sha256_before": CANDIDATE,
        **{f"optimizer_steps_{s}": 0 for s in evaluation.STAGES},
    }
    train_binding = write("campaign/train/report.json", train)
    reload_binding = write("campaign/reload/report.json", reload)
    bindings = [
        plan_binding,
        train_binding,
        reload_binding,
        journal,
        weights,
        config,
        candidate,
        reloaded,
    ]
    evidence = {
        "schema_version": 1,
        "contract": evaluation.TERMINAL_CONTRACT,
        "bindings": bindings,
        "actual_execution": {
            "train_native_exit_code": 0,
            "reload_native_exit_code": 0,
            "train_terminal_tool_chunk": "fixture-native-handle-not-real",
            "reload_terminal_tool_chunk": "fixture-native-handle-not-real",
            "processes_terminated": True,
        },
        **dict.fromkeys(
            (
                "complete267_verified",
                "fresh_saved_adapter_reload_verified",
                "saved_parameter_digest_verified",
                "source_bindings_unchanged",
            ),
            True,
        ),
        **dict.fromkeys(
            (
                "quality_claim",
                "candidate_promoted",
                "official_holdout_verified",
                "execution_authority",
                "final_release_allowed",
            ),
            False,
        ),
    }

    def journal_verifier(_path, schedule, *, require_complete):
        assert schedule == plan["schedule"] and require_complete is True
        return journal_summary

    api = SimpleNamespace(
        recovery=SimpleNamespace(
            CONTRACT="fixture-recovery", FALSE_CLAIMS=false_claims, verify_journal=journal_verifier
        )
    )
    return SimpleNamespace(
        root=tmp_path,
        run=run,
        plan=plan,
        train=train,
        reload=reload,
        rows=rows,
        evidence=evidence,
        api=api,
        write=write,
        plan_binding=plan_binding,
    )


def test_terminal_requires_native_handle_binding_and_complete_saved_reload(terminal):
    artifacts, train, reload = evaluation.verify_terminal(
        terminal.evidence, terminal.plan, terminal.api
    )
    assert len(artifacts) == 8
    assert train["optimizer_steps_completed"] == 267 and reload["optimizer_steps_completed"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "bool_native",
        "active_reload",
        "missing_native_chunk",
        "alive",
        "not_complete",
        "promoted",
        "missing_binding",
        "wrong_report_bytes",
        "unfinished",
        "uncertain",
        "not_saved",
        "wrong_digest",
        "source_drop",
        "source_drift",
        "reload_raw",
    ],
)
def test_terminal_gate_negative_cases(terminal, mutation):
    e = terminal.evidence
    if mutation == "bool_native":
        e["actual_execution"]["train_native_exit_code"] = False
    elif mutation == "active_reload":
        e["actual_execution"]["reload_native_exit_code"] = None
    elif mutation == "missing_native_chunk":
        e["actual_execution"]["reload_terminal_tool_chunk"] = ""
    elif mutation == "alive":
        e["actual_execution"]["processes_terminated"] = False
    elif mutation == "not_complete":
        e["complete267_verified"] = False
    elif mutation == "promoted":
        e["candidate_promoted"] = True
    elif mutation == "missing_binding":
        e["bindings"].pop()
    elif mutation == "wrong_report_bytes":
        (terminal.run / "reload/report.json").write_bytes(b"{}")
    else:
        train, reload = copy.deepcopy(terminal.train), copy.deepcopy(terminal.reload)
        if mutation == "unfinished":
            train["optimizer_steps_completed"] = 266
        elif mutation == "uncertain":
            train["optimizer_update_uncertain"] = True
        elif mutation == "not_saved":
            train["saved_adapter_weights"] = None
        elif mutation == "wrong_digest":
            reload["adapter_parameter_sha256_after"] = SHA
        elif mutation == "source_drop":
            train["sources_before"] = train["sources_after"] = [terminal.plan_binding]
        elif mutation == "source_drift":
            reload["sources_after"] = []
        elif mutation == "reload_raw":
            rows = copy.deepcopy(terminal.rows)
            rows[0]["generated"]["raw_output"] = "changed"
            terminal.write("campaign/reload/reloaded-all86.json", rows)
        for name, report in (("train", train), ("reload", reload)):
            row = terminal.write(f"campaign/{name}/report.json", report)
            e["bindings"] = [row if b["path"] == row["path"] else b for b in e["bindings"]]
        if mutation == "reload_raw":
            row = evaluation.record(terminal.run / "reload/reloaded-all86.json")
            e["bindings"] = [row if b["path"] == row["path"] else b for b in e["bindings"]]
    with pytest.raises(ValueError):
        evaluation.verify_terminal(e, terminal.plan, terminal.api)


class Tensor:
    """List-backed toy, not a torch tensor."""

    def __init__(self, value, dtype="int64"):
        self.value, self.dtype = value, dtype
        self.ndim = 2 if value and isinstance(value[0], list) else 1
        self.shape = (len(value), len(value[0])) if self.ndim == 2 else (len(value),)

    def __getitem__(self, item):
        return Tensor(self.value[item], self.dtype)

    def detach(self):
        return self

    def cpu(self):
        return self

    def tolist(self):
        return copy.deepcopy(self.value)


def toy_backend(mode="normal", owned=False):
    raw = json.dumps(target(vector=[3, 0, 0]))
    state = SimpleNamespace(model_calls=0, backend_calls=0, mode=mode)

    class Model:
        def generate(self, **kwargs):
            state.model_calls += 1
            if state.mode == "model_error":
                raise ValueError("fixture generation failure")
            prefix = kwargs["input_ids"].tolist()[0]
            if state.mode == "prefix_mismatch":
                prefix[0] += 1
            return Tensor([prefix + [7, 8]], dtype="float32" if state.mode == "dtype" else "int64")

    class Backend:
        def __init__(self):
            self.model = Model()
            if owned:
                self.model.generate = self.model.generate
            self.torch = SimpleNamespace(int64="int64")
            self.processor = SimpleNamespace(
                tokenizer=SimpleNamespace(decode=lambda *_args, **_kw: raw)
            )
            self.budget = SimpleNamespace()

        def generate(self, encoded, *, parent):
            state.backend_calls += 1
            assert set(encoded) == {"prefix"} and parent is False
            output = self.model.generate(
                **encoded["prefix"],
                do_sample=False,
                max_new_tokens=192,
                max_time=30.0,
                use_cache=True,
            )
            suffix = output.tolist()[0][3:]
            result = generated(raw)
            result["output_token_ids"] = suffix
            if state.mode == "suffix_mismatch":
                result["output_token_ids"][0] += 1
            if state.mode == "raw_mismatch":
                result["raw_output"] = "different"
            return result

    return Backend(), state


@pytest.mark.parametrize("owned", [False, True])
def test_prefix_observer_once_and_exact_hook_restoration(owned):
    backend, state = toy_backend(owned=owned)
    previous = backend.model.generate
    result, observation = evaluation.generate_observed(backend, {"input_ids": Tensor([[1, 2, 3]])})
    assert state.model_calls == state.backend_calls == 1
    assert backend.model.generate == previous and ("generate" in backend.model.__dict__) is owned
    assert result["output_token_ids"] == [7, 8]
    assert (
        observation["hook_restored"] is True
        and observation["actual_output_prefix_verified"] is True
    )
    assert observation["raw_decode_independently_rechecked"] is True


@pytest.mark.parametrize(
    "mode", ["model_error", "prefix_mismatch", "dtype", "suffix_mismatch", "raw_mismatch"]
)
def test_prefix_observer_rejects_and_restores_on_failure(mode):
    backend, _state = toy_backend(mode)
    previous = backend.model.generate
    with pytest.raises(ValueError):
        evaluation.generate_observed(backend, {"input_ids": Tensor([[1, 2, 3]])})
    assert backend.model.generate == previous and "generate" not in backend.model.__dict__


@pytest.fixture
def orchestration(monkeypatch, dataset):
    # This suite exercises orchestration with CPU stand-ins, not live WinAPI
    # telemetry. Keep the sampler's Windows-only production contract intact.
    monkeypatch.setattr(host_memory, "snapshot", lambda: {"fixture_only": True})
    root = dataset.root
    output = root / "artifacts/evaluation/onboard-navigation-original82-fixture"
    old_run = root / "old_r2"
    (old_run / "reload").mkdir(parents=True)
    marker = old_run / "reload/frozen-marker.txt"
    marker.write_bytes(b"original artifacts untouched")

    def file(name, document):
        path = root / name
        path.write_bytes(evaluation.canonical(document) + b"\n")
        return evaluation.record(path)

    bridge_report = file("bridge-report.json", dataset.report)
    runtime = file("runtime.json", {"python_executable": sys.executable, "package_versions": {}})
    original = file("original.json", {"fixture": True})
    legacy = file("legacy.json", {"original_plan": original})
    recovery = file("recovery.json", {"output_dir": str(old_run)})
    plan = {
        "output_dir": str(output),
        "claims": dict.fromkeys(evaluation.FALSE_CLAIMS, False),
        "runtime_reference": runtime,
        "dataset_report": bridge_report,
        "recovery_plan": recovery,
        "legacy_plan": legacy,
        "model_budget": {},
        "expected_base_parameter_sha256": BASE,
        "expected_candidate_parameter_sha256": CANDIDATE,
        "sample_ids": [s.sample_id for s in dataset.samples if s.partition != "train"],
        "actor_identity_sha256": {s.sample_id: SHA for s in dataset.samples},
        "split_scope": "fixture_only_internal_TRAIN_dev",
        "old86_reference_records_reused_not_rerun": {},
    }
    plan_binding = file("eval-plan.json", plan)
    backend, state = toy_backend()
    state.processed_ids, state.loader_paths = [], []

    @contextmanager
    def loading_scope(_legacy, _old, selected_path, report):
        assert selected_path == old_run / "reload" and selected_path != output
        assert report["phase"] == "reload" and "not_original_reload" in report["scope"]
        state.loader_paths.append(selected_path)
        yield

    def load_backend(_legacy, _budget, _old, _pilot, phase, selected_path, report):
        assert phase == "reload" and selected_path == old_run / "reload"
        report.update(
            model_loaded=True,
            adapter_parameter_sha256_before=CANDIDATE,
            base_parameter_sha256_before=BASE,
        )
        return backend

    def process_actor(actor, _processor, _actor_module):
        state.processed_ids.append(actor.sample_id)
        return {"input_ids": Tensor([[1, 2, 3]])}

    def checker(inputs, **kwargs):
        assert kwargs["image_count"] in (1, 2) and set(inputs) == {"input_ids"}
        return {"prefix_tokens": 3, "fixture": True}

    api = SimpleNamespace(
        navigation=navigation,
        inference=inference,
        actor=SimpleNamespace(),
        recovery=SimpleNamespace(
            RecoveryModelBudget=lambda **_kw: SimpleNamespace(validate=lambda: None),
            check_loader_cleanup=lambda *_a: None,
        ),
        old=SimpleNamespace(loading_scope=loading_scope, load_backend=load_backend),
        prefix=SimpleNamespace(process_actor=process_actor, validate_prefix_inputs=checker),
        shared_backend=SimpleNamespace(
            PreloadedNavigationInferenceBackend=SimpleNamespace(
                _check_adapter_state=lambda _m: None
            )
        ),
        pilot=SimpleNamespace(
            tensor_record=lambda *_a: None,
            check_memory=lambda *_a: {},
            core=SimpleNamespace(
                parameter_digest=lambda _m, *, trainable: CANDIDATE if trainable else BASE
            ),
        ),
    )
    monkeypatch.setattr(evaluation, "admit_plan", lambda *_a: (plan, plan_binding))
    monkeypatch.setattr(evaluation, "capture_sources", lambda *_a: {"fixture_inventory": True})
    monkeypatch.setattr(evaluation, "apis", lambda: api)
    monkeypatch.setattr(
        evaluation,
        "actor_for_sample",
        lambda sample, _api: SimpleNamespace(identity_sha256=SHA, sample_id=sample.sample_id),
    )
    return SimpleNamespace(
        root=root,
        output=output,
        old_run=old_run,
        marker=marker,
        backend=backend,
        state=state,
        plan=plan,
        api=api,
        args=SimpleNamespace(plan="eval-plan.json", plan_sha256=plan_binding["sha256"]),
    )


def test_orchestration_durable_raw_before_offline_gold59_and_no_old_writes(
    orchestration, monkeypatch
):
    original_scorer = evaluation.score_prediction

    def scorer(raw, gold, api):
        predictions = (orchestration.output / "predictions.jsonl").read_bytes().splitlines()
        assert len(predictions) == 59
        assert all("gold" not in evaluation.strict_json(line) for line in predictions)
        return original_scorer(raw, gold, api)

    monkeypatch.setattr(evaluation, "score_prediction", scorer)
    assert evaluation.run(orchestration.args) == 0
    report = evaluation.strict_json((orchestration.output / "report.json").read_bytes())
    assert (
        report["generation_attempts"]
        == report["generation_returns"]
        == report["durable_prediction_count"]
        == 59
    )
    assert report["partition_summaries"]["dev-validation"]["sample_count"] == 17
    assert report["partition_summaries"]["dev-test"]["sample_count"] == 42
    assert report["original_train_rows_entered_model"] == 0
    assert orchestration.state.processed_ids == orchestration.plan["sample_ids"]
    assert orchestration.state.backend_calls == 59
    assert report["gold_scored_only_after_all_raw_predictions_durable"] is True
    assert [row["stage"] for row in report["host_memory_observations"]] == [
        "load_before",
        "load_after",
        "model_released",
    ]
    assert all(
        row["snapshot"] == {"fixture_only": True} for row in report["host_memory_observations"]
    )
    assert all(report[k] is False for k in evaluation.FALSE_CLAIMS)
    assert orchestration.marker.read_bytes() == b"original artifacts untouched"
    assert list((orchestration.old_run / "reload").iterdir()) == [orchestration.marker]
    with pytest.raises(ValueError, match="create-only"):
        evaluation.run(orchestration.args)


@pytest.mark.parametrize("mode", ["model_error", "prefix_mismatch"])
def test_partial_model_failure_no_success_or_automatic_retry(orchestration, mode):
    orchestration.state.mode = mode
    assert evaluation.run(orchestration.args) == 1
    report = evaluation.strict_json((orchestration.output / "report.json").read_bytes())
    assert report["generation_attempts"] == 1 and report["durable_prediction_count"] == 0
    assert report["optimizer_performed"] is False and report["budget_reset"] is False
    assert not (orchestration.output / "scores.json").exists()
    assert orchestration.marker.read_bytes() == b"original artifacts untouched"


def test_durable_write_failure_counts_no_saved_prediction(orchestration, monkeypatch):
    def fail(*_args):
        raise OSError("fixture fsync failure")

    monkeypatch.setattr(evaluation, "append_row", fail)
    assert evaluation.run(orchestration.args) == 1
    report = evaluation.strict_json((orchestration.output / "report.json").read_bytes())
    assert report["generation_attempts"] == report["generation_returns"] == 1
    assert report["durable_prediction_count"] == 0 and report["source_bindings_unchanged"] is False


def test_final_current_source_drift_preserves_partial_but_rejects_completion(
    orchestration, monkeypatch
):
    calls = []

    def capture(*_args):
        calls.append(True)
        return {"fixture_inventory": len(calls)}

    monkeypatch.setattr(evaluation, "capture_sources", capture)
    assert evaluation.run(orchestration.args) == 1
    report = evaluation.strict_json((orchestration.output / "report.json").read_bytes())
    assert report["durable_prediction_count"] == 59
    assert (
        report["source_bindings_unchanged"] is False and report["partial_outputs_preserved"] is True
    )
    assert (orchestration.output / "predictions.jsonl").exists()
    assert orchestration.marker.read_bytes() == b"original artifacts untouched"


def test_cleanup_failure_does_not_invent_no_model_load(orchestration):
    def fail(*_args):
        raise ValueError("fixture loader cleanup failed after exact load")

    orchestration.api.recovery.check_loader_cleanup = fail
    assert evaluation.run(orchestration.args) == 1
    report = evaluation.strict_json((orchestration.output / "report.json").read_bytes())
    assert report["model_loaded"] is True and report["generation_attempts"] == 0
    assert report["optimizer_performed"] is False


def test_current_missing_terminal_refuses_emission_before_import(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    monkeypatch.setattr(
        evaluation,
        "apis",
        lambda: pytest.fail("Missing terminal must fail before importing dependencies"),
    )
    with pytest.raises(FileNotFoundError):
        evaluation.build_plan(
            "missing-terminal.json",
            SHA,
            "missing-bridge.json",
            SHA,
            "artifacts/evaluation/onboard-navigation-original82-fixture",
        )
    assert list(tmp_path.iterdir()) == []


def test_tiny_plan_emission_and_admission_bind_terminal_dataset_and_external_runtime(
    terminal, dataset, monkeypatch
):
    """Toy native receipt/journal verifier; no real campaign admission is claimed."""
    root = terminal.root
    code = {}
    for name in (
        *evaluation.NEW_FILES,
        *evaluation.EXTRA_CODE,
        "scripts/build_onboard_openfly_original82_supervision.py",
    ):
        code[name] = terminal.write(name, b"fixture Python source", raw=True)
    monkeypatch.setattr(
        evaluation,
        "PINNED_CODE",
        {
            "scripts/build_onboard_openfly_original82_supervision.py": code[
                "scripts/build_onboard_openfly_original82_supervision.py"
            ]["sha256"]
        },
    )
    dataset.report["converter"]["sha256"] = evaluation.PINNED_CODE[
        "scripts/build_onboard_openfly_original82_supervision.py"
    ]
    external = root.parent / (root.name + "-external-runtime.py")
    external.write_bytes(b"fixture external runtime source")
    external_row = evaluation.record(external)
    runtime = terminal.write("runtime.json", {"source_files": [external_row]})
    original = terminal.write("original.json", {"fixture": True})
    prior = terminal.write("prior.json", {"fixture": True})
    legacy = terminal.write(
        "legacy.json",
        {
            "runtime_reference": runtime,
            "original_plan": original,
            "original_training_report": prior,
        },
    )
    terminal.plan.update(
        legacy_plan=legacy,
        model_budget={},
        warm_start={
            "weights": terminal.evidence["bindings"][4],
            "config": terminal.evidence["bindings"][5],
        },
        base_round=terminal.evidence["bindings"][6],
        warm_round=terminal.evidence["bindings"][7],
    )
    terminal.plan["source_files"].append(external_row)
    old_plan = terminal.plan_binding
    plan_binding = terminal.write(evaluation.R2_PLAN, terminal.plan)
    monkeypatch.setattr(evaluation, "R2_SHA", plan_binding["sha256"])
    for report in (terminal.train, terminal.reload):
        report.update(
            plan_sha256=plan_binding["sha256"],
            sources_before=[terminal.plan["source_files"][0], external_row, plan_binding],
            sources_after=[terminal.plan["source_files"][0], external_row, plan_binding],
        )
    replacements = [
        plan_binding,
        terminal.write("campaign/train/report.json", terminal.train),
        terminal.write("campaign/reload/report.json", terminal.reload),
    ]
    terminal.evidence["bindings"] = [
        next((r for r in replacements if r["path"] == b["path"]), b)
        for b in terminal.evidence["bindings"]
    ]
    assert old_plan["sha256"] != plan_binding["sha256"]
    evidence = terminal.write("terminal.json", terminal.evidence)
    bridge_report = terminal.write("bridge.json", dataset.report)
    api = SimpleNamespace(
        recovery=terminal.api.recovery, navigation=navigation, inference=inference
    )
    monkeypatch.setattr(evaluation, "apis", lambda: api)
    output = "artifacts/evaluation/onboard-navigation-original82-fixture"
    first = evaluation.build_plan(
        evidence["path"], evidence["sha256"], bridge_report["path"], bridge_report["sha256"], output
    )
    assert first["sample_count"] == 59 and first["partitions"] == {
        "dev-validation": 17,
        "dev-test": 42,
    }
    assert first["gold_class_counts"] == {
        "forward": 54,
        "up": 1,
        "down": 1,
        "yaw_right": 1,
        "stop": 2,
    }
    assert external_row in first["runtime_sources"]
    assert external_row not in first["source_files"]
    assert all(first["claims"][k] is False for k in evaluation.FALSE_CLAIMS)
    binding = terminal.write("evaluation-plan.json", first)
    admitted, actual_binding = evaluation.admit_plan(binding["path"], binding["sha256"])
    assert (
        evaluation.canonical(first) == evaluation.canonical(admitted) and actual_binding == binding
    )
    changed = copy.deepcopy(first)
    changed["sample_ids"].pop()
    tampered = terminal.write("tampered-plan.json", changed)
    with pytest.raises(ValueError, match="Evaluation plan differs"):
        evaluation.admit_plan(tampered["path"], tampered["sha256"])
    assert not (root / output).exists()


def test_isolated_import_help_no_heavy_dependencies():
    path = REPO / "scripts/evaluate_onboard_navigation_original82.py"
    script = (
        "import sys,runpy;runpy.run_path(sys.argv[1],run_name='import_only');"
        "assert not any(k.split('.')[0] in "
        "{'torch','transformers','PIL','numpy','pyarrow','peft'} for k in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script, str(path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    help_result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(path), "--help"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert help_result.returncode == 0 and "--terminal-evidence-sha256" in help_result.stdout
