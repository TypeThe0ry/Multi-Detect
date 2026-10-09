"""Exact reload, weak train probes, and an untrained schedule stay distinct."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/evidence/onboard-navigation-exact-reload-20261007-r1.json"
SCHEDULE_RECEIPT = ROOT / "docs/evidence/onboard-navigation-balanced-schedule-20261007-r2.json"
RECOVERY = "onboard-navigation-adapter-recovery-20261007-r1"
SUPERVISOR = "onboard-navigation-adapter-recovery-supervisor-20261007-r1"
IDENTITY = "onboard-navigation-recovery-identity-followup-20261007-r1"
AUDIT = "onboard-navigation-recovery-final-audit-20261007-r1"
GATES = (
    "quality_claim",
    "candidate_promoted",
    "physical_performance_claimed",
    "hil_real_evidence",
    "outdoor_gnss_evidence",
    "final_release_allowed",
    "execution_authority",
    "transport_created",
    "mission_created",
)


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def bound(row):
    path = ROOT / row["path"]
    assert path.resolve().is_relative_to(ROOT.resolve())
    if not path.is_file() and row["path"].startswith("artifacts/"):
        pytest.skip("native artifacts are intentionally absent from clean clones")
    assert path.stat().st_size == row["bytes"]
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    assert digest.hexdigest() == row["sha256"]
    return path


def artifact(value, suffix, *, collection="bindings"):
    rows = [row for row in value[collection] if row["path"].endswith(suffix)]
    assert len(rows) == 1
    return bound(rows[0])


def native_document(value, directory, name):
    return read(artifact(value, f"/{directory}/{name}"))


def test_native_worker_and_identity_pass_do_not_reclassify_original_failures():
    value = read(RECEIPT)
    assert value["contract"] == "multidetect.onboard-navigation-exact-reload-evidence-v1"
    assert len(value["bindings"]) == len({row["path"] for row in value["bindings"]}) == 19
    for row in value["bindings"]:
        bound(row)
    worker = native_document(value, RECOVERY, "report.json")
    completion = native_document(value, SUPERVISOR, "completion.json")
    preflight = native_document(value, SUPERVISOR, "preflight.json")
    observation = native_document(value, SUPERVISOR, "process-observation.json")
    identity = native_document(value, IDENTITY, "report.json")
    audit = native_document(value, AUDIT, "independent-review.json")
    trained = read(artifact(value, "/onboard-navigation-trained-adapter-20261007-r2.json"))
    original = read(artifact(trained, "/report.json", collection="native_outputs"))
    assert value["native_executions"]["original_training"]["native_exit_code"] == 1
    assert original["native_exit_code"] == value["original_training_native_exit_code"] == 1
    assert original["failure_type"] == "OutOfMemoryError" and "saved_adapter_reload" not in original
    assert (
        completion["supervisor_exit_code"] == value["original_recovery_supervisor_exit_code"] == 1
    )
    assert completion["failure"] == "Recovery loader PID differs from the actual child process"
    assert completion["child_returncode_raw"] == worker["native_exit_code"] == 0
    assert identity["native_exit_code"] == audit["native_exit_code"] == 0
    assert identity["original_worker_result"] == worker
    assert (
        value["original_failures_preserved"]
        and not value["old_failed_attempts_reclassified_as_success"]
    )
    assert not value["original_training_reload_gate_passed"]
    assert not value["original_recovery_supervisor_gate_passed"]
    assert identity["original_pid_failure_preserved"] and not identity["pid_gate_removed"]
    assert (
        preflight["sources_before"] == completion["sources_before"] == completion["sources_after"]
    )
    assert (
        len(completion["sources_before"])
        == value["source_inventory"]["recovery_admitted_sources_with_manifest"]
        == 225
    )
    assert identity["sources_before"] == identity["sources_after"]
    assert (
        len(identity["sources_before"])
        == value["source_inventory"]["identity_before"]
        == value["source_inventory"]["identity_after"]
        == 236
    )
    assert (
        len({row["path"] for row in audit["input_file_bindings"]})
        == audit["input_unique_file_count"]
        == value["source_inventory"]["independent_audit_unique_inputs"]
        == 340
    )
    assert audit["all_input_byte_hashes_reverified_after_audit"]
    chain = value["process_identity"]["captured_process_chain"]
    assert chain == audit["process_chain"]["captured_pids"] == [36492, 42900, 7448, 34596]
    processes = {row["ProcessId"]: row for row in observation["processes"]}
    assert set(processes) == set(chain) and observation["tool_exit_code"] == 0
    observed = datetime.fromisoformat(observation["observed_at"].replace("Z", "+00:00"))
    for parent, child in zip(chain, chain[1:], strict=False):
        assert processes[child]["ParentProcessId"] == parent
        assert (
            datetime.fromisoformat(processes[parent]["CreationDate"])
            <= datetime.fromisoformat(processes[child]["CreationDate"])
            <= observed
        )
    assert completion["child_pid"] == identity["process_identity"]["launcher_pid"] == 7448
    assert (
        identity["process_identity"]["actual_worker_pid"]
        == value["process_identity"]["actual_worker_pid"]
        == 34596
    )
    loader = native_document(value, RECOVERY, "streaming-loader-report.json")
    warmup = native_document(value, RECOVERY, "allocator-warmup-report.json")
    assert loader["pid"] == warmup["warmup_observation"]["pid"] == 34596
    events = [
        json.loads(line)
        for line in artifact(value, f"/{SUPERVISOR}/stdout.bin").read_bytes().splitlines()
        if line.lstrip().startswith(b"{")
    ]
    imports = [row for row in events if row.get("event") == "constructor_importpath_installed"]
    assert len(imports) == 1 and imports[0]["pid"] == 34596
    for counter in ("attempted", "returned", "synchronized", "completed"):
        assert (
            original[f"optimizer_steps_{counter}"]
            == value["optimizer"][f"original_steps_{counter}"]
            == audit["original_optimizer_counts_preserved"][f"optimizer_steps_{counter}"]
            == 86
        )
    assert (
        worker["optimizer_steps_performed_by_this_check"]
        == audit["recovery_optimizer_updates"]
        == 0
    )
    assert (
        value["optimizer"]["recovery_steps"]
        == value["optimizer"]["identity_followup_steps"]
        == value["optimizer"]["audit_steps"]
        == 0
    )
    assert not worker["optimizer_performed"] and not worker["forward_backward_performed"]
    assert (
        not identity["model_reexecuted_by_this_check"]
        and not audit["model_reexecuted_by_this_audit"]
    )
    assert all(value[key] is False for key in GATES)
    assert value["fresh_saved_adapter_reload_verified"] is True
    assert value["training_acceptance_completed"] is False


def test_actual_625_bf16_and_112_fp32_reload_preserves_four_weak_train_probes():
    from scripts.train_onboard_vlm_navigation_pilot import diagnose

    value = read(RECEIPT)
    worker = native_document(value, RECOVERY, "report.json")
    loader = native_document(value, RECOVERY, "streaming-loader-report.json")
    audit = native_document(value, AUDIT, "independent-review.json")
    manifest = read(artifact(value, "/onboard-navigation-adapter-recovery-20261007-r1.json"))
    trained = read(artifact(value, "/onboard-navigation-trained-adapter-20261007-r2.json"))
    original = read(artifact(trained, "/report.json", collection="native_outputs"))
    plan = read(bound(trained["frozen_plan"]))
    candidate = read(artifact(trained, "/candidate-probes.json", collection="native_outputs"))
    prepared = read(artifact(trained, "/prepared-inputs.json", collection="native_outputs"))
    assert (
        len(manifest["source_files"])
        == value["source_inventory"]["recovery_manifest_explicit_sources"]
        == 224
    )
    readers = loader["reader_openings"]
    assert (
        len(readers)
        == value["reload"]["reader_handles_opened"]
        == value["reload"]["reader_handles_closed"]
        == 5
    )
    assert all(row["final_diagnostics"]["closed"] is True for row in readers)
    base = [
        row["final_diagnostics"]
        for row in readers
        if row["kind"] == "base" and row["final_diagnostics"]["tensor_calls_completed"]
    ]
    adapter = [row["final_diagnostics"] for row in readers if row["kind"] == "adapter"]
    metadata = [
        row["final_diagnostics"]
        for row in readers
        if row["kind"] == "base" and not row["final_diagnostics"]["tensor_calls_completed"]
    ]
    assert len(base) == len(adapter) == 1 and len(metadata) == 3
    assert base[0]["normalized_device"] == "cuda:0" and adapter[0]["normalized_device"] == "cpu"
    for actual, expected_count, expected_bytes, dtype in (
        (base[0], 625, 4255064064, "BF16"),
        (adapter[0], 112, 3211264, "F32"),
    ):
        reads = actual["tensor_reads"]
        assert (
            actual["tensor_calls_attempted"]
            == actual["tensor_calls_completed"]
            == len(reads)
            == len({row["name"] for row in reads})
            == expected_count
        )
        assert (
            actual["tensor_payload_bytes_read"]
            == sum(row["actual_bytes_read"] for row in reads)
            == expected_bytes
        )
        assert all(
            row["completed"] is True
            and row["dtype"] == dtype
            and row["actual_bytes_read"] == row["declared_bytes"]
            for row in reads
        )
    result = worker["reload_check"]
    exact = result["exact_adapter_load"]
    saved_digest = original["adapter_parameter_sha256_after"]
    assert (
        exact["saved_parameter_sha256"]
        == exact["loaded_parameter_sha256"]
        == result["loaded_adapter_parameter_sha256"]
        == value["reload"]["saved_adapter_parameter_sha256"]
        == audit["saved_adapter_payload"]["independently_reconstructed_original_parameter_sha256"]
        == saved_digest
    )
    checks = exact["tensor_checks"]
    assert (
        exact["tensor_count"] == len(checks) == value["reload"]["saved_adapter_tensor_count"] == 112
    )
    assert all(
        row["dtype"] == "torch.float32" and row["loaded_bytes_equal_saved"] is True
        for row in checks
    )
    assert (
        sum(row["numel"] for row in checks)
        == value["reload"]["saved_adapter_scalar_count"]
        == 802816
    )
    assert exact["exact_tensor_equality"] is True and exact["dtype_conversion_performed"] is False
    assert (
        result["fresh_base_parameter_sha256"]
        == result["base_parameter_sha256_after"]
        == original["base_parameter_sha256_before"]
        == original["base_parameter_sha256_after"]
        == value["reload"]["base_parameter_sha256_after"]
    )
    assert len(result["regenerated_probes"]) == value["reload"]["probe_count"] == 4
    for actual, expected in zip(result["regenerated_probes"], candidate, strict=True):
        sid = actual["sample_id"]
        assert sid == expected["sample_id"] and sid in plan["train_sample_ids"]
        assert actual["actual_encoded"] == prepared[sid]
        assert actual["actual_encoded"]["prefix_tensors"] == expected["actual_prefix_tensors"]
        assert actual["generated"]["output_token_ids"] == expected["generated"]["output_token_ids"]
        assert actual["generated"]["raw_output"] == expected["generated"]["raw_output"]
        assert actual["diagnostic"] == expected["diagnostic"]
    samples = {
        row["sample_id"]: row
        for row in map(json.loads, bound(plan["dataset_samples"]).read_bytes().splitlines())
    }
    for actual in result["regenerated_probes"]:
        sample = samples[actual["sample_id"]]
        assert sample["partition"] == sample["source"]["original_split"] == "train"
        action = sample["planned_action"]
        target = {
            "schema_version": 1,
            "contract": "multidetect.onboard-planned-navigation-target-v1",
            "kind": action["kind"],
            "translation_frame": "body_frd",
            "yaw_convention": "clockwise_right_positive",
            "translation_m": action["translation_m"],
            "yaw_delta_rad": action["yaw_delta_rad"],
        }
        assert diagnose(actual["generated"]["raw_output"], target) == actual["diagnostic"]
    for diagnostic, count in (
        ("strict_json_valid", 1),
        ("action_class_correct", 1),
        ("metric_correct", 0),
    ):
        assert sum(row["diagnostic"][diagnostic] for row in result["regenerated_probes"]) == count
    assert (
        value["diagnostics"]["candidate_strict_valid"]
        == value["diagnostics"]["candidate_action_correct"]
        == 1
    )
    assert value["diagnostics"]["candidate_metric_correct"] == 0
    assert (
        result["resubstitution_only"] and not value["diagnostics"]["independent_holdout_available"]
    )
    assert all(value[key] is False for key in GATES)


def test_balanced_280_draws_keep_all_86_sources_without_authorizing_training():
    value = read(SCHEDULE_RECEIPT)
    assert value["native_execution"]["native_exit_code"] == 0
    for row in value["bindings"]:
        bound(row)
    first = artifact(value, "/onboard-navigation-balanced-schedule-20261007-r1/schedule.json")
    second = artifact(value, "/onboard-navigation-balanced-schedule-20261007-r2/schedule.json")
    assert first.read_bytes() == second.read_bytes()
    schedule = read(second)
    plan = read(bound(schedule["source_plan"]))
    samples = {
        row["sample_id"]: row
        for row in map(json.loads, bound(schedule["dataset_samples"]).read_bytes().splitlines())
    }
    draws = schedule["draws"]
    assert len(draws) == schedule["draw_count"] == value["draw_count"] == 280
    assert {row["sample_id"] for row in draws} == set(plan["train_sample_ids"]) == set(samples)
    assert len(samples) == schedule["unique_draw_samples"] == value["source_unique_samples"] == 86
    classes = Counter()
    for sample in samples.values():
        assert sample["partition"] == sample["source"]["original_split"] == "train"
        action = sample["planned_action"]
        if action["kind"] == "stop":
            category = "stop"
        elif action["kind"] == "turn_yaw":
            category = "left" if action["yaw_delta_rad"] < 0 else "right"
        else:
            assert action["kind"] == "translate" and action["translation_m"][0] > 0
            assert action["translation_m"][1:] == [0, 0]
            category = "forward"
        sample["checked_action_class"] = category
        classes[category] += 1
    assert (
        classes
        == schedule["source_class_counts"]
        == value["source_class_counts"]
        == {"forward": 70, "left": 6, "right": 7, "stop": 3}
    )
    exposure, class_draws = Counter(), Counter()
    for index, draw in enumerate(draws, 1):
        sid, category = draw["sample_id"], draw["action_class"]
        assert draw["draw_index"] == index and samples[sid]["checked_action_class"] == category
        exposure[sid] += 1
        class_draws[category] += 1
        assert draw["per_sample_exposure"] == exposure[sid]
        assert draw["class_draw_index"] == class_draws[category]
    assert (
        class_draws
        == schedule["draw_class_counts"]
        == value["draw_class_counts"]
        == dict.fromkeys(classes, 70)
    )
    assert (
        plan["optimization"]["max_optimizer_steps"]
        == value["original_max_optimizer_steps"]
        == 100
        < len(draws)
    )
    assert (
        value["draw_count_exceeds_original_max_optimizer_steps"]
        and value["new_training_plan_required"]
    )
    assert schedule["new_training_plan_required"] and not schedule["original_single_complete_epoch"]
    assert schedule["source_bindings_reverified_before_publication"]
    assert len(schedule["source_bindings"]) == value["source_binding_count"] == 121
    for key in (
        *GATES,
        "optimizer_authorized",
        "optimizer_performed",
        "model_loaded",
        "training_completed",
        "schedule_is_actor_input",
        "schedule_classes_visible_to_actor",
        "target_labels_in_actor_input",
        "independent_holdout_available",
    ):
        assert value[key] is False
    assert schedule["optimizer_authorized"] is False and schedule["training_completed"] is False
    assert (
        value["resubstitution_only"] and value["scene_count"] == 1 and value["episode_count"] == 3
    )
