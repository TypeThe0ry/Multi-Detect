"""Bind the sealed CPU phase, not a full GPU run or live residency measurement.

Only small receipts/metadata are reopened. The 180-source inventory is reconciled
against declarations; its approximately 5 GB of model/runtime bytes are NOT
rehashed here. No actual tensors/labels are recomputed and no processor is run.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from xml.etree import ElementTree

import pytest

from scripts import train_onboard_navigation_streamed as runner
from tests.onboard_navigation_evidence_support import require_local_artifacts

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/evidence/onboard-navigation-streamed-continuation-cpu-20261008-r1.json"
RUN = "artifacts/models/onboard-navigation-balanced-streamed-20261008-r1"
PLAN = "configs/training/onboard-navigation-streamed-continuation-20261008-r1.json"
FALSE_GATES = (
    "independent_holdout_available",
    "default_flight_model_or_release_parity_modified",
    "new_live_navigation_backend_integrated",
    "training_completed",
    "quality_claim",
    "candidate_promoted",
    "physical_performance_claimed",
    "hil_real_evidence",
    "outdoor_gnss_evidence",
    "final_release_allowed",
    "execution_authority",
    "transport_created",
    "mission_created",
    "actual_live_camera_parity_verified",
    "sim_real_parity_verified",
    "optimizer_state_restored",
    "scheduler_state_restored",
    "rng_state_restored",
)
EXPECTED_BINDINGS = {
    PLAN,
    "scripts/train_onboard_navigation_streamed.py",
    "tests/test_train_onboard_navigation_streamed.py",
    "artifacts/evaluation/onboard-navigation-streamed-implementation-tests-20261008-r1.xml",
    f"{RUN}/cpu-encode/report.json",
    f"{RUN}/cpu-encode/prepared-inputs.json",
    f"{RUN}/cpu-encode-accepted.json",
    f"{RUN}/cpu-encode-process-observation.json",
    f"{RUN}/cpu-encode-stdout.bin",
    f"{RUN}/cpu-encode-stderr.bin",
}


def read(path):
    assert path.stat().st_size <= 2 * 1024 * 1024
    return runner.strict_json(path.read_bytes())


def metadata_binding(row, *, allow_empty=False):
    assert type(row) is dict and set(row) == {"path", "bytes", "sha256"}
    assert type(row["path"]) is str and row["path"]
    assert type(row["bytes"]) is int and row["bytes"] >= (0 if allow_empty else 1)
    assert type(row["sha256"]) is str and re.fullmatch("[0-9a-f]{64}", row["sha256"])


def repository_path(value):
    assert type(value) is str and value and "\\" not in value and ":" not in value
    assert not PurePosixPath(value).is_absolute() and not PureWindowsPath(value).is_absolute()
    assert all(part not in ("", ".", "..") for part in value.split("/"))
    path = (ROOT / value).resolve()
    assert path.is_relative_to(ROOT.resolve())
    return path


def bound(row):
    metadata_binding(row, allow_empty=True)
    path = repository_path(row["path"])
    if not path.is_file() and row["path"].startswith("artifacts/"):
        pytest.skip("native runtime artifacts are intentionally absent from clean clones")
    assert path.is_file() and path.stat().st_size == row["bytes"]
    assert row["bytes"] <= 16 * 1024 * 1024, "do not rehash model/runtime source payloads"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
    return path


def declared_repository_bound(row):
    """Small frozen JSON references use absolute paths, but cannot escape ROOT."""
    metadata_binding(row)
    absolute = Path(row["path"])
    path = (absolute if absolute.is_absolute() else ROOT / absolute).resolve()
    assert path.is_relative_to(ROOT.resolve())
    return bound({**row, "path": path.relative_to(ROOT.resolve()).as_posix()})


@pytest.fixture(scope="module")
def evidence():
    require_local_artifacts(ROOT)
    value = read(RECEIPT)
    assert len(value["bindings"]) == len({row["path"] for row in value["bindings"]}) == 10
    assert {row["path"] for row in value["bindings"]} == EXPECTED_BINDINGS
    # Missing ignored runtime outputs should skip before resolving a frozen host's
    # absolute references. Missing tracked source files must remain failures.
    for row in value["bindings"]:
        path = repository_path(row["path"])
        if row["path"].startswith("artifacts/") and not path.is_file():
            pytest.skip("native runtime artifacts are intentionally absent from clean clones")
    paths = {row["path"]: bound(row) for row in value["bindings"]}
    return value, paths


def test_actual_cpu_phase_exit_zero_never_becomes_full_pipeline_exit_zero(evidence):
    value, paths = evidence
    report = read(paths[f"{RUN}/cpu-encode/report.json"])
    accepted = read(paths[f"{RUN}/cpu-encode-accepted.json"])
    assert value["schema_version"] == 1
    assert (
        value["contract"] == "multidetect.onboard-navigation-streamed-continuation-cpu-evidence-v1"
    )
    assert value["full_pipeline_terminal_native_exit_code"] is None
    assert (
        value["cpu_phase_native_exit_code"]
        == report["native_exit_code"]
        == accepted["native_exit_code"]
        == 0
    )
    assert (
        report["contract"] == runner.CONTRACT
        and report["phase"] == accepted["phase"] == "cpu-encode"
    )
    assert value["cpu_only"] is report["cpu_only"] is True
    assert value["optimizer_steps_completed_in_this_cpu_phase"] == 0
    for key in (
        "model_loaded",
        "inference_performed",
        "optimizer_performed",
        "forward_backward_performed",
    ):
        assert report[key] is value[f"{key}_in_cpu_phase"] is False
    assert value["cuda_initialized_in_cpu_phase"] is False
    assert all(value[key] is False for key in FALSE_GATES)
    assert all(report[key] is False for key in runner.FALSE_CLAIMS)
    assert value["resubstitution_only"] is report["resubstitution_only"] is True
    assert (
        value["continuation_kind"]
        == report["continuation_kind"]
        == "warm_start_adapter_weights_with_fresh_optimizer"
    )
    assert (
        value["official_local_processor_used"] is value["all86_full_chat_encoding_verified"] is True
    )


def test_exact_287_software_results_remain_distinct_from_runtime_acceptance(evidence):
    value, paths = evidence
    xml = paths[
        "artifacts/evaluation/onboard-navigation-streamed-implementation-tests-20261008-r1.xml"
    ]
    suites = ElementTree.parse(xml)
    assert len(list(suites.iter("testcase"))) == 287
    assert not list(suites.iter("failure")) and not list(suites.iter("error"))
    assert not list(suites.iter("skipped"))
    assert value["unit_tests"] == {
        "total": 287,
        "passed": 287,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "native_exit_code": 0,
        "native_terminal_tool_chunk": "b31918",
    }
    assert value["full_pipeline_terminal_native_exit_code"] is None
    assert value["training_completed"] is value["quality_claim"] is False


def test_captured_cim_parent_chain_chronology_argv_and_finished_report_binding(evidence):
    value, paths = evidence
    accepted = read(paths[f"{RUN}/cpu-encode-accepted.json"])
    observation = read(paths[f"{RUN}/cpu-encode-process-observation.json"])
    report = read(paths[f"{RUN}/cpu-encode/report.json"])
    plan = read(paths[PLAN])
    interpreters = [
        row["path"]
        for row in plan["source_files"]
        if Path(row["path"]).name.lower() == "python.exe"
    ]
    actual = runner.verify_process_observation(
        observation,
        value["launcher_pid"],
        accepted["argv"],
        interpreters,
        supervisor_pid=value["supervisor_pid"],
    )
    assert actual == accepted["process_identity"]
    assert (
        actual["actual_worker_pid"]
        == value["actual_worker_pid"]
        == report["pid"]
        == accepted["worker_pid"]
        == 24168
    )
    assert actual["launcher_pid"] == accepted["launcher_pid"] == 40156
    assert value["supervisor_pid"] == 53024
    assert actual["identity_verified"] is value["live_process_identity_verified"] is True
    observed = datetime.fromisoformat(observation["observed_at"])
    assert observation["observed_at"] == value["process_observed_at"]
    assert (
        datetime.fromisoformat(accepted["started_at"])
        <= observed
        <= datetime.fromisoformat(accepted["ended_at"])
    )
    argv = accepted["argv"]
    assert argv[1:7] == [
        "-I",
        "-B",
        "-X",
        "utf8",
        str(paths["scripts/train_onboard_navigation_streamed.py"]),
        "--worker",
    ]
    assert argv[argv.index("--phase") + 1] == "cpu-encode"
    assert Path(argv[argv.index("--plan") + 1]).resolve() == paths[PLAN]
    assert argv[argv.index("--plan-sha256") + 1] == report["plan_sha256"]
    assert argv[argv.index("--invocation-id") + 1] == value["invocation_id"]
    assert Path(argv[argv.index("--output-dir") + 1]).resolve() == ROOT / RUN
    binding = next(
        row for row in value["bindings"] if row["path"] == f"{RUN}/cpu-encode/report.json"
    )
    expected = {**binding, "path": str(paths[binding["path"]])}
    assert accepted["report"] == expected
    assert (
        runner.verify_finished_events(
            paths[f"{RUN}/cpu-encode-stdout.bin"], "cpu-encode", 24168, expected
        )
        == accepted["finished_events"]
    )
    assert (
        value["unique_admitted_and_finished_events_verified"]
        is value["worker_report_bound_by_actual_finished_event"]
        is True
    )
    assert paths[f"{RUN}/cpu-encode-stderr.bin"].read_bytes() == b""


def test_180_source_metadata_matches_declarations_without_rehashing_5gb(evidence):
    value, paths = evidence
    report = read(paths[f"{RUN}/cpu-encode/report.json"])
    plan = read(paths[PLAN])
    before = report["sources_before"]
    assert before == report["sources_after"]
    assert report["source_bindings_unchanged"] is value["source_bindings_unchanged"] is True
    assert (
        len(before) == len({row["path"] for row in before}) == value["source_binding_count"] == 180
    )
    declared = {}
    for row in runner.plan_bindings(plan):
        metadata_binding(row)
        path = Path(row["path"])
        key = str((path if path.is_absolute() else ROOT / path).resolve())
        canonical = {**row, "path": key}
        assert key not in declared or declared[key] == canonical
        declared[key] = canonical
    plan_binding = next(row for row in value["bindings"] if row["path"] == PLAN)
    declared[str(paths[PLAN])] = {**plan_binding, "path": str(paths[PLAN])}
    for row in before:
        metadata_binding(row)
    assert {row["path"]: row for row in before} == declared
    recheck = value["root_read_only_source_recheck"]
    assert recheck["native_exit_code"] == 0 and recheck["native_tool_chunk"] == "bd53b4"
    assert recheck["unique_bindings_reopened"] == 180
    assert recheck["streamed_source_bytes"] == sum(row["bytes"] for row in before) == 4993288005
    assert recheck["current_source_drift"] == []


def test_all86_prepared_and_official_prefix_records_cover_exact_source_identities(evidence):
    value, paths = evidence
    plan = read(paths[PLAN])
    old = read(declared_repository_bound(plan["original_plan"]))
    reference = read(declared_repository_bound(plan["cpu_prefix_report"]))
    prepared = read(paths[f"{RUN}/cpu-encode/prepared-inputs.json"])
    ids = [row["sample_id"] for row in reference["samples"]]
    assert ids == plan["diagnostics"]["sample_ids"] == old["train_sample_ids"]
    assert (
        len(ids)
        == len(set(ids))
        == len(prepared)
        == value["sample_count"]
        == value["unique_sample_count"]
        == 86
    )
    assert set(prepared) == set(ids)
    assert (
        runner.validate_prepared_inputs(prepared, plan, runner.BalancedTrainingBudget()) == prepared
    )
    assert (
        max(row["prefix_tokens"] for row in prepared.values()) == value["max_prefix_tokens"] == 453
    )
    assert max(row["total_tokens"] for row in prepared.values()) == value["max_total_tokens"] == 524
    assert (
        min(row["response_tokens"] for row in prepared.values())
        == value["response_tokens_min"]
        == 53
    )
    assert (
        max(row["response_tokens"] for row in prepared.values())
        == value["response_tokens_max"]
        == 71
    )
    for row in reference["samples"]:
        assert row["labels_pose_future_ids_entered_model_messages"] is False
        assert (
            row["retained_history_valid"]
            is row["actor_identities_equal"]
            is row["all_four_tensor_bytes_equal"]
            is True
        )
    for key in (
        "all_actual_prefix_tensor_records_match_frozen_official_cpu86",
        "full_chat_visual_tensor_records_equal_prefix",
        "prompt_image_padding_labels_masked_by_native_encoder",
    ):
        assert value[key] is True


def test_metadata_and_residency_claims_keep_their_explicit_verification_limits(evidence):
    value, paths = evidence
    report = read(paths[f"{RUN}/cpu-encode/report.json"])
    assert value["input_residency"] == report["input_residency"] == runner.INPUT_RESIDENCY
    assert (
        value["full_dataset_tensor_cache_retained_reported"]
        is report["full_dataset_tensor_cache_retained"]
        is False
    )
    assert value["live_tensor_residency_independently_measured"] is False
    assert value["streamed_sample_accesses_in_cpu_phase"] == report["streamed_sample_accesses"] == 0
    assert (
        value["streamed_unique_accessed_samples_in_cpu_phase"]
        == report["streamed_unique_accessed_samples"]
        == 0
    )
    audit = value["independent_read_only_metadata_audit"]
    assert audit["native_exit_code"] == 0 and audit["native_tool_chunk"] == "a82a07"
    assert audit["issues"] == []
    for key in (
        "inventory_independently_rederived",
        "process_identity_and_chronology_verified",
        "official_reference_coverage_verified",
    ):
        assert audit[key] is True
    for key in (
        "tensor_bytes_or_labels_independently_recomputed",
        "processor_rerun",
        "torch_hf_pil_imported",
    ):
        assert audit[key] is False


def test_new_cpu_success_cannot_overwrite_the_old_host_memory_failure(evidence):
    value, _ = evidence
    progress = read(ROOT / "docs/current-progress.json")
    old = progress["onboard_navigation_balanced_continuation_20261007_r1"]["full_pipeline"]
    assert old["supervisor_native_exit_code"] == old["baseline_worker_native_exit_code"] == 1
    assert old["failure_type"] == "MemoryError" and old["cuda_oom_observed"] is False
    assert old["original_failed_attempt_preserved"] is True
    assert (
        old["balanced_training_phase_started"]
        is old["balanced_280_draw_training_completed"]
        is False
    )
    report = read(bound(old["baseline_worker_report"]))
    completion = read(bound(old["completion"]))
    assert completion["native_exit_code"] == report["native_exit_code"] == 1
    assert report["failure_type"] == "MemoryError" and report["model_loaded"] is True
    assert all(
        report[key] is False
        for key in ("inference_performed", "forward_backward_performed", "optimizer_performed")
    )
    assert [phase["phase"] for phase in completion["phases"]] == ["cpu-encode", "baseline"]
    assert value["cpu_phase_native_exit_code"] == 0
    assert value["full_pipeline_terminal_native_exit_code"] is None


@pytest.mark.parametrize(
    "path",
    [
        "../outside.json",
        "/absolute.json",
        "C:/outside.json",
        "artifacts/../outside.json",
        "artifacts\\file.json",
    ],
)
def test_receipt_bindings_cannot_escape_the_repository(path):
    with pytest.raises(AssertionError):
        repository_path(path)


def test_missing_runtime_artifact_skips_instead_of_fabricating_native_evidence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    row = {"path": "artifacts/absent.json", "bytes": 1, "sha256": "a" * 64}
    with pytest.raises(pytest.skip.Exception, match="clean clones"):
        bound(row)
    with pytest.raises(AssertionError):
        bound({**row, "path": "scripts/absent.py"})


def test_duplicate_keys_and_nonfinite_json_metadata_are_rejected():
    for raw in ('{"native_exit_code":0,"native_exit_code":1}', '{"value":NaN}', '{"value":1e999}'):
        with pytest.raises(ValueError):
            runner.strict_json(raw)


def test_stdlib_verifiers_import_without_torch_hf_pil_or_processor_side_effects(tmp_path):
    code = (
        "import importlib.util,sys; "
        "spec=importlib.util.spec_from_file_location('streamed_evidence_check',sys.argv[1]); "
        "module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; "
        "spec.loader.exec_module(module); "
        "assert not {'torch','transformers','peft','PIL'} & sys.modules.keys()"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code, str(Path(runner.__file__).resolve())],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert list(tmp_path.iterdir()) == []
