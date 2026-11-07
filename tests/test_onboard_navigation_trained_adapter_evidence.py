"""Real saved updates, failed reload and weak train probes are separate evidence."""

from __future__ import annotations

import hashlib
import json
import math
import random
import struct
from pathlib import Path

import pytest

from tests.onboard_navigation_evidence_support import requires_local_artifacts

ROOT = Path(__file__).resolve().parents[1]
pytestmark = requires_local_artifacts(ROOT)
RECEIPT = ROOT / "docs/evidence/onboard-navigation-trained-adapter-20261007-r2.json"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def bound(row):
    path = ROOT / row["path"]
    assert path.resolve().is_relative_to(ROOT.resolve())
    if not path.is_file() and row["path"].startswith("artifacts/"):
        pytest.skip("native artifacts are intentionally absent from clean clones")
    assert path.stat().st_size == row["bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
    return path


def artifact(value, suffix):
    return bound(next(row for row in value["native_outputs"] if row["path"].endswith(suffix)))


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def test_all_86_actual_updates_follow_exact_fsynced_four_stage_journal():
    value = read(RECEIPT)
    plan = read(bound(value["frozen_plan"]))
    report = read(artifact(value, "/report.json"))
    completion = read(artifact(value, "/completion.json"))
    audit = read(artifact(value, "/independent-review.json"))
    events = [
        json.loads(line)
        for line in artifact(value, "/training-events.jsonl").read_bytes().splitlines()
    ]
    expected_ids = list(plan["train_sample_ids"])
    random.Random(plan["optimization"]["seed"]).shuffle(expected_ids)
    stages = (
        "optimizer_step_attempted",
        "optimizer_step_returned",
        "optimizer_step_synchronized",
        "optimizer_step_completed",
    )
    assert len(expected_ids) == len(set(expected_ids)) == value["train_samples"] == 86
    assert len(events) == value["journal_rows"] == 344
    assert [row["sample_id"] for row in events[::4]] == expected_ids
    for index, sample_id in enumerate(expected_ids):
        group = events[index * 4 : (index + 1) * 4]
        assert tuple(row["stage"] for row in group) == stages
        assert all(
            row["sample_id"] == sample_id
            and row["step"] == index + 1
            and row["epoch"] == 1
            and row["split"] == "train"
            for row in group
        )
        assert math.isfinite(group[0]["loss"])
        assert group[0]["gradient_absolute_sum"] > 0
    stdout_rows = []
    for line in artifact(value, "/stdout.bin").read_bytes().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("stage") in stages:
            stdout_rows.append(row)
    assert stdout_rows == events
    for stage in stages:
        counter = stage.replace("optimizer_step_", "optimizer_steps_")
        assert (
            value[counter]
            == report[counter]
            == completion["optimizer_journal"]["observed_counters"][counter]
            == audit["actual_updates"][counter]
            == 86
        )
    assert audit["actual_updates"]["journal_lines"] == 344
    assert (
        hashlib.sha256(canonical_bytes(expected_ids)).hexdigest()
        == audit["actual_updates"]["expected_shuffled_id_sequence_canonical_json_sha256"]
    )
    assert value["optimizer_epoch_completed"]
    assert report["optimizer_performed"] and report["forward_backward_performed"]
    assert report["optimizer_update_uncertain"] is value["optimizer_update_uncertain"] is False
    assert completion["sources_before"] == completion["sources_after"]
    assert len(completion["sources_after"]) == value["source_binding_count"] == 59


def test_saved_112_fp32_tensor_bytes_reconstruct_original_adapter_digest():
    value = read(RECEIPT)
    report = read(artifact(value, "/report.json"))
    audit = read(artifact(value, "/independent-review.json"))
    raw = artifact(value, "/adapter/adapter_model.safetensors").read_bytes()
    header_size = struct.unpack("<Q", raw[:8])[0]
    header = json.loads(raw[8 : 8 + header_size])
    payload = memoryview(raw)[8 + header_size :]
    entries = report["trainable_audit"]["trainable_parameters"]
    assert len(entries) == value["saved_adapter_tensor_count"] == 112
    assert set(header) - {"__metadata__"} == {
        entry["name"].replace(".default.", ".") for entry in entries
    }
    digest = hashlib.sha256()
    spans = []
    numel = 0
    for entry in entries:
        info = header[entry["name"].replace(".default.", ".")]
        assert info["dtype"] == value["saved_adapter_dtype"] == "F32"
        assert entry["dtype"] == "torch.float32"
        start, end = info["data_offsets"]
        assert 0 <= start < end <= len(payload)
        assert end - start == math.prod(info["shape"]) * 4 == entry["numel"] * 4
        assert all(math.isfinite(item[0]) for item in struct.iter_unpack("<f", payload[start:end]))
        digest.update(canonical_bytes([entry["name"], info["shape"], entry["dtype"]]))
        digest.update(payload[start:end])
        spans.append((start, end))
        numel += entry["numel"]
    spans.sort()
    assert spans[0][0] == 0 and spans[-1][1] == len(payload)
    assert all(left[1] == right[0] for left, right in zip(spans, spans[1:], strict=False))
    assert len(payload) == value["saved_adapter_payload_bytes"] == 3211264
    assert numel == report["trainable_audit"]["trainable_parameter_count"] == 802816
    assert (
        digest.hexdigest()
        == report["adapter_parameter_sha256_after"]
        == value["adapter_parameter_sha256_after"]
        == audit["saved_adapter_audit"]["saved_parameter_digest_independently_recomputed_stdlib"]
    )
    assert value["adapter_parameter_sha256_before"] != value["adapter_parameter_sha256_after"]
    assert (
        value["base_parameter_sha256_before"]
        == report["base_parameter_sha256_before"]
        == report["base_parameter_sha256_after"]
        == value["base_parameter_sha256_after"]
    )
    assert value["saved_adapter_integrity_verified"]
    assert value["saved_adapter_digest_independently_recomputed"]


def test_saved_updates_do_not_turn_failed_reload_or_train_probes_into_acceptance():
    from scripts.train_onboard_vlm_navigation_pilot import diagnose

    value = read(RECEIPT)
    plan = read(bound(value["frozen_plan"]))
    report = read(artifact(value, "/report.json"))
    completion = read(artifact(value, "/completion.json"))
    stream = read(artifact(value, "/streaming-completion.json"))
    warmup = read(artifact(value, "/allocator-warmup-completion.json"))
    audit = read(artifact(value, "/independent-review.json"))
    assert value["execution"]["native_exit_code"] == report["native_exit_code"] == 1
    assert completion["child_returncode_raw"] == completion["supervisor_exit_code"] == 1
    assert stream["supervisor_exit_code"] == warmup["supervisor_exit_code"] == 1
    assert not completion["child_completion_verified"]
    assert not stream["streaming_receipt_valid"] and not warmup["warmup_call_evidence_valid"]
    assert report["adapter_acceptance_uncertain"]
    assert "saved_adapter_reload" not in report
    assert "source_bytes_reverified" not in report
    assert report["failure_type"] == value["failure_type"] == "OutOfMemoryError"
    assert "20.00 MiB" in report["failure"]
    loader = read(artifact(value, "/streaming-loader-report.json"))
    first = loader["reader_openings"][3]["final_diagnostics"]
    assert first["tensor_calls_attempted"] == first["tensor_calls_completed"] == 625
    second = loader["reader_openings"][8]["final_diagnostics"]
    assert second["tensor_calls_attempted"] == value["second_base_tensor_reads_attempted"] == 498
    assert second["tensor_calls_completed"] == value["second_base_tensor_reads_completed"] == 497
    assert second["tensor_reads"][-1]["name"] == value["second_base_last_tensor"]
    assert not second["tensor_reads"][-1]["completed"]
    samples = {
        row["sample_id"]: row
        for row in (
            json.loads(line) for line in bound(plan["dataset_samples"]).read_bytes().splitlines()
        )
    }
    prepared = read(artifact(value, "/prepared-inputs.json"))
    for prefix in ("parent", "candidate"):
        probes = read(artifact(value, f"/{prefix}-probes.json"))
        assert len(probes) == value["diagnostics"]["total"] == 4
        assert [(row["sample_id"], row["action_class"]) for row in probes] == [
            (row["sample_id"], row["action_class"]) for row in plan["diagnostic_probes"]
        ]
        for row in probes:
            assert row["sample_id"] in plan["train_sample_ids"]
            sample = samples[row["sample_id"]]
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
            assert (
                hashlib.sha256(canonical_bytes(target)).hexdigest()
                == prepared[row["sample_id"]]["target_sha256"]
            )
            assert diagnose(row["generated"]["raw_output"], target) == row["diagnostic"]
        for field, receipt_field in (
            ("strict_json_valid", "strict_valid"),
            ("action_class_correct", "action_correct"),
            ("metric_correct", "metric_correct"),
        ):
            assert (
                sum(row["diagnostic"][field] for row in probes)
                == value["diagnostics"][f"{prefix}_{receipt_field}"]
            )
    assert value["diagnostics"]["candidate_strict_valid"] == 1
    assert value["diagnostics"]["candidate_action_correct"] == 1
    assert value["diagnostics"]["candidate_metric_correct"] == 0
    assert audit["probe_diagnostics"]["valid_forward_translation_error_m"] == 7
    for key in (
        "fresh_saved_adapter_reload_verified",
        "training_acceptance_completed",
        "oom_root_cause_established",
        "old_gpu_model_retention_confirmed",
        "quality_claim",
        "candidate_promoted",
        "physical_performance_claimed",
        "hil_real_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
        "execution_authority",
        "transport_created",
        "mission_created",
    ):
        assert value[key] is False
    progress = read(ROOT / "docs/current-progress.json")[
        "onboard_navigation_trained_adapter_20261007_r2"
    ]
    assert (
        bound(
            {
                "path": progress["evidence"],
                "bytes": progress["evidence_bytes"],
                "sha256": progress["evidence_sha256"],
            }
        )
        == RECEIPT
    )
    assert progress["optimizer_steps_completed"] == 86
    assert progress["training_acceptance_completed"] is False
    assert progress["diagnostics"] == value["diagnostics"]
