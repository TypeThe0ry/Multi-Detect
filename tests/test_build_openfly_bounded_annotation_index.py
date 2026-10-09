"""Synthetic CPU metadata only; never scan the retained405MB annotation."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_openfly_bounded_annotation_index as index


def record(scene="env_airsim_18", *, name="route", frames=3, prompt="沿街向前，再停止。"):
    return {
        "image_path": f"{scene}/astar_data/low_short/{name}",
        "gpt_instruction": prompt,
        "action": [8] * (frames - 1) + [0],
        "index_list": [f"20251008_000000_{i}" for i in range(frames)],
        "pos": [[float(i * 3), 0.0, 0.0] for i in range(frames)],
        "yaw": [0.0] * frames,
    }


@pytest.fixture
def source(tmp_path, monkeypatch):
    actual_root = index.ROOT
    for name in (index.ENTRY, index.TEST, index.PARSER):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((actual_root / name).read_bytes())
    monkeypatch.setattr(index, "ROOT", tmp_path)
    manifest = tmp_path / "artifacts/training/sealed-fixture/manifest.json"
    annotation = manifest.parent / "raw/Annotation/train.json"
    annotation.parent.mkdir(parents=True)
    raw = index.canonical([record()])

    def replace(raw_bytes):
        annotation.write_bytes(raw_bytes)
        pin = (len(raw_bytes), hashlib.sha256(raw_bytes).hexdigest())
        monkeypatch.setattr(index, "ANNOTATION_PIN", pin)
        value = {
            "contract": "multidetect.openfly-navigation-pilot-v1",
            "seen_unseen_downloaded": False,
            "dataset": {
                "repository": "IPEC-COMMUNITY/OpenFly",
                "revision": index.REVISION,
                "split": "train",
            },
            "annotation": {
                "path": str(annotation),
                "bytes": pin[0],
                "sha256": pin[1],
                "repository_path": "Annotation/train.json",
            },
        }
        manifest.write_bytes(index.canonical(value))
        bound = index.small_binding(manifest)
        monkeypatch.setattr(index, "MANIFEST_PIN", (bound["bytes"], bound["sha256"]))

    replace(raw)
    return SimpleNamespace(root=tmp_path, manifest=manifest, annotation=annotation, replace=replace)


def plan(source, **budget):
    return index.build_plan(source.manifest, budget=index.ScanBudget(**budget))


def test_complete_small_fixture_has_correct_metadata_and_no_admission(source):
    rows = [
        record(scene, name=str(i), prompt=f"真实原文任务 {i}")
        for i, scene in enumerate(
            ("env_airsim_18", "env_airsim_26", "env_airsim_16", "env_airsim_23", "env_airsim_gz")
        )
    ]
    source.replace(index.canonical(rows))
    result = index.scan_index(plan(source))
    assert result["scan_complete"] and result["complete_array_syntax_validated"]
    assert result["records_inspected"] == 5 and result["unique_route_count"] == 5
    assert result["candidate_frame_count"] == 15 and result["retained_unique_prompt_count"] == 5
    assert result["consumed_prefix"]["sha256"] == index.ANNOTATION_PIN[1]
    assert result["whole_annotation_sha256_verified_by_this_scan"] is True
    assert result["scanned_annotation_action_code_counts"] == {"0": 5, "8": 10}
    assert result["retained_scene_unique_prompt_counts"]["env_airsim_18"] == 1
    assert result["retained_scene_annotation_action_code_counts"]["env_airsim_26"] == {
        "0": 1,
        "8": 2,
    }
    assert [row["locked_development_role"] for row in result["routes"]] == [
        "train",
        "train",
        "dev-validation",
        "dev-test",
        "unassigned_no_training_admission",
    ]
    assert [row["historically_trained_scene"] for row in result["routes"]] == [
        True,
        False,
        False,
        False,
        False,
    ]
    for row, original in zip(result["routes"], rows, strict=True):
        assert row["prompt_original"] == original["gpt_instruction"]
        assert (
            row["prompt_sha256"] == hashlib.sha256(original["gpt_instruction"].encode()).hexdigest()
        )
        assert (
            row["annotation_record_sha256"] == hashlib.sha256(index.canonical(original)).hexdigest()
        )
        assert row["raw_pose_primitive_counts"] is None
        assert row["raw_pose_actions_verified"] is False
        assert row["may_enter_train_without_new_admission"] is False
        assert "pos" not in row and "yaw" not in row and "target" not in row
    for key in (
        "training_ready",
        "independent_holdout_verified",
        "scene_units_verified",
        "raw_pose_geometry_verified",
        "rgb_payload_available_verified",
        "model_loaded",
        "GPU_used",
        "download_performed",
        "quality_claim",
        "candidate_promoted",
    ):
        assert result[key] is False
    assert result == index.scan_index(plan(source))


def test_hard_bytes_cap_retains_completed_records_and_does_not_claim_full_sha(source):
    first, second = record(name="first"), record(name="next", prompt="中😀文" * 30)
    prefix = b"[" + index.canonical(first) + b","
    raw = prefix + index.canonical(second) + b"]"
    cap = len(prefix) + len(index.canonical(second)) // 2
    source.replace(raw)
    result = index.scan_index(plan(source, max_bytes=cap))
    assert result["records_inspected"] == 1 and result["unique_route_count"] == 1
    assert result["consumed_prefix"]["bytes"] == cap
    assert result["consumed_prefix"]["sha256"] == hashlib.sha256(raw[:cap]).hexdigest()
    assert result["stop_reason"] == "byte_limit" and result["scan_complete"] is False
    assert result["whole_annotation_sha256_verified_by_this_scan"] is False
    assert result["historical_annotation_binding"]["sha256"] == hashlib.sha256(raw).hexdigest()


def test_incremental_utf8_handles_every_character_boundary_and_closes(source):
    raw = "A中😀éZ".encode()
    source.replace(raw)
    proxy = index.BudgetedAnnotationPath(source.annotation, index.ScanBudget(), len(raw))
    text = []
    with proxy.open("r", encoding="utf-8") as reader:
        while chunk := reader.read(1):
            text.append(chunk)
    assert "".join(text) == raw.decode()
    assert proxy.raw.closed and proxy.raw.stream.closed
    assert proxy.raw.bytes_read == len(raw)
    assert proxy.raw.sha256.hexdigest() == hashlib.sha256(raw).hexdigest()


def test_byte_cap_inside_utf8_is_partial_not_a_fake_eof(source):
    raw = "中😀后".encode()
    source.replace(raw)
    proxy = index.BudgetedAnnotationPath(source.annotation, index.ScanBudget(max_bytes=4), len(raw))
    with pytest.raises(index.ByteBudgetReached):
        with proxy.open("r", encoding="utf-8") as reader:
            assert reader.read(4) == "中"
            reader.read(4)
    assert proxy.raw.closed and proxy.raw.bytes_read == 4


@pytest.mark.parametrize(
    "budget,reason,count",
    [
        ({"max_records": 2}, "record_limit", 2),
        ({"max_routes": 2}, "route_limit", 2),
        ({"max_candidate_frames": 5}, "candidate_frame_limit", 1),
        ({"max_candidate_frames": 6}, "candidate_frame_limit", 2),
    ],
)
def test_record_route_and_sample_caps_have_truthful_partial_scope(source, budget, reason, count):
    source.replace(index.canonical([record(name=str(i)) for i in range(4)]))
    result = index.scan_index(plan(source, **budget))
    assert result["stop_reason"] == reason and result["scan_complete"] is False
    assert result["unique_route_count"] == count and result["candidate_frame_count"] == count * 3
    assert result["records_inspected"] <= result["budget"]["max_records"]
    assert result["candidate_frame_count"] <= result["budget"]["max_candidate_frames"]
    # Binary read-ahead may cover this tiny complete fixture without validating
    # all its records: source bytes proof and array-schema coverage stay separate.
    assert result["complete_array_syntax_validated"] is False


def test_duplicate_oversized_and_non_airsim_candidates_are_counted_not_relabelled(source):
    first = record()
    source.replace(
        index.canonical(
            [
                first,
                {**first, "gpt_instruction": "另一指令"},
                record(name="large", frames=129),
                record("env_gs_ecust", name="unreviewed"),
                record(name="longprompt", prompt="x" * 16385),
                record(name="ok", prompt="其他任务"),
            ]
        )
    )
    result = index.scan_index(plan(source))
    assert result["records_inspected"] == 6 and result["unique_route_count"] == 2
    assert result["skipped_record_counts"] == {
        "duplicate_route_first_original_record_retained": 1,
        "route_frame_cap": 1,
        "non_airsim_unreviewed_renderer": 1,
        "prompt_byte_cap": 1,
    }
    assert result["routes"][0]["prompt_original"] == first["gpt_instruction"]
    assert "8" in result["scanned_annotation_action_code_counts"]
    assert "forward" not in result["scanned_annotation_action_code_counts"]


def padded_record():
    row = record("env_airsim_sh", name="padded", frames=7)
    row["action"] = [-1, -1, 8, 0, -2, -2, -2]
    return row


def test_recognized_padding_is_quarantined_not_relabelled_or_retained(source):
    padded = padded_record()
    original_bytes = index.canonical(padded)
    valid = record("env_airsim_16", name="valid")
    source.replace(index.canonical([padded, valid]))
    result = index.scan_index(plan(source))
    assert result["scan_complete"] and result["records_inspected"] == 2
    assert result["skipped_record_counts"] == {"unsupported_padded_action_codes": 1}
    assert result["unique_route_count"] == 1 and result["candidate_frame_count"] == 3
    assert result["routes"][0]["image_path"] == valid["image_path"]
    assert result["routes"][0]["locked_development_role"] == "dev-validation"
    assert result["scanned_annotation_action_code_counts"] == {
        "-1": 2,
        "-2": 3,
        "0": 2,
        "8": 3,
    }
    assert result["retained_scene_annotation_action_code_counts"] == {
        "env_airsim_16": {"0": 1, "8": 2}
    }
    assert result["raw_pose_geometry_verified"] is result["training_ready"] is False
    assert index.canonical(padded) == original_bytes
    assert (
        "schema_only_action_copy_not_original_codes_or_targets"
        in result["unsupported_padding_scope"]
    )


def test_quarantined_record_still_consumes_record_budget(source):
    source.replace(index.canonical([padded_record(), record(name="must_not_be_inspected")]))
    result = index.scan_index(plan(source, max_records=1))
    assert result["records_inspected"] == 1 and result["stop_reason"] == "record_limit"
    assert result["unique_route_count"] == result["candidate_frame_count"] == 0
    assert result["scan_complete"] is False
    assert result["skipped_record_counts"] == {"unsupported_padded_action_codes": 1}


@pytest.mark.parametrize("code", [10, -3, 8.0, True, False, "-1", None])
def test_padding_does_not_hide_unknown_or_untyped_action_codes(source, code):
    row = padded_record()
    row["action"][2] = code
    source.replace(index.canonical([row]))
    with pytest.raises(ValueError, match="unknown original action code"):
        index.scan_index(plan(source))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row.update(extra=True),
        lambda row: row.update(gpt_instruction=7),
        lambda row: row["pos"][0].__setitem__(0, True),
        lambda row: row["pos"][0].append(0),
        lambda row: row["yaw"].__setitem__(0, "0"),
        lambda row: row["index_list"].__setitem__(1, row["index_list"][0]),
        lambda row: row["index_list"].__setitem__(0, "../image.png"),
        lambda row: row.update(image_path="env_airsim_sh/../low_average_updown/padded"),
        lambda row: row.update(image_path="env_airsim_sh/astar_data/low_average_updown/%2froute"),
        lambda row: row["yaw"].pop(),
    ],
)
def test_padding_does_not_hide_non_action_schema_type_or_path_errors(source, mutate):
    row = padded_record()
    mutate(row)
    source.replace(index.canonical([row]))
    with pytest.raises(ValueError):
        index.scan_index(plan(source))


def test_record_byte_cap_precedes_strict_schema_validation():
    row = record(prompt="x" * (index.SMALL_CAP + 1))
    parser = SimpleNamespace(
        validate_annotation=lambda _: pytest.fail("Byte cap must precede parser")
    )
    with pytest.raises(ValueError, match="record exceeds metadata cap"):
        index.validate_index_record(parser, row)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(extra=True),
        lambda r: r.update(action=[8, True, 0]),
        lambda r: r.update(pos=[[True, 0, 0], [3, 0, 0], [6, 0, 0]]),
        lambda r: r.update(action=[8, 8, 9]),
        lambda r: r.update(action=[0, 8, 0]),
        lambda r: r.update(index_list=["20251008_000000_0"] * 3),
        lambda r: r.update(image_path="env_airsim_18/../low_short/route"),
        lambda r: r.update(image_path="env_airsim_18/astar_data/low_short/%2froute"),
    ],
)
def test_actual_frozen_annotation_schema_and_path_validation_fails_closed(source, mutate):
    row = record()
    mutate(row)
    source.replace(index.canonical([row]))
    with pytest.raises(ValueError):
        index.scan_index(plan(source))


@pytest.mark.parametrize(
    "raw",
    [
        b'[{"image_path":"a","image_path":"b"}]',
        b'[{"x":NaN}]',
        b'[{"x":Infinity}]',
        b"[{}",
        b"[{},]",
        b"[]garbage",
        b'["not object"]',
        b'["\xff"]',
    ],
)
def test_malformed_json_utf8_or_duplicates_are_not_successful_partial_scans(source, raw):
    source.replace(raw)
    with pytest.raises((ValueError, UnicodeError)):
        index.scan_index(plan(source))


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_bytes", True),
        ("max_records", 0),
        ("max_routes", 129),
        ("max_frames_per_route", 129),
        ("max_candidate_frames", 4097),
        ("max_bytes", 1.0),
    ],
)
def test_budget_types_and_fixed_ceilings(key, value):
    with pytest.raises(ValueError):
        index.ScanBudget(**{key: value}).validate()


@pytest.mark.parametrize("change", ["roles", "budget", "source", "annotation_stat"])
def test_plan_drift_is_rejected_before_parser_import(source, monkeypatch, change):
    value = plan(source)
    if change == "roles":
        value["locked_scene_roles"]["env_airsim_16"] = "train"
    elif change == "budget":
        value["budget"]["max_routes"] = 129
    elif change == "source":
        value["source_files"][0]["sha256"] = "a" * 64
    else:
        value["annotation_stat"]["mtime_ns"] += 1
    monkeypatch.setattr(index, "load_parser", lambda: pytest.fail("Admission precedes import"))
    with pytest.raises(ValueError):
        index.scan_index(value)


@pytest.mark.parametrize("changed", ["annotation", "entry", "manifest"])
def test_final_source_checks_reject_drift_after_reader_is_closed(source, monkeypatch, changed):
    value = plan(source)
    parser = index.load_parser()
    original = parser.validate_annotation
    captured = []

    def validate(row):
        answer = original(row)
        target = {
            "annotation": source.annotation,
            "entry": source.root / index.ENTRY,
            "manifest": source.manifest,
        }[changed]
        target.write_bytes(target.read_bytes() + b" ")
        return answer

    def observed(proxy):
        captured.append(proxy)
        yield from actual_iterator(proxy)

    actual_iterator = parser.iter_annotation_records
    parser.iter_annotation_records = observed
    parser.validate_annotation = validate
    monkeypatch.setattr(index, "load_parser", lambda: parser)
    with pytest.raises(ValueError, match="changed before sealing"):
        index.scan_index(value)
    assert captured[0].raw.closed and captured[0].raw.stream.closed


def test_schema_exception_closes_stream_before_any_result_admission(source, monkeypatch):
    parser = index.load_parser()
    observed = []
    actual = parser.iter_annotation_records

    def capture(proxy):
        observed.append(proxy)
        yield from actual(proxy)

    def fail(_row):
        raise MemoryError("synthetic validation failure")

    parser.iter_annotation_records, parser.validate_annotation = capture, fail
    monkeypatch.setattr(index, "load_parser", lambda: parser)
    with pytest.raises(MemoryError):
        index.scan_index(plan(source))
    assert observed[0].raw.closed and observed[0].raw.stream.closed


def test_truncated_actual_file_is_not_treated_as_eof(source):
    proxy = index.BudgetedAnnotationPath(
        source.annotation, index.ScanBudget(), source.annotation.stat().st_size + 1
    )
    with pytest.raises(ValueError, match="ended before"):
        with proxy.open("r", encoding="utf-8") as reader:
            while reader.read(7):
                pass
    assert proxy.raw.closed


def test_emit_plan_is_deterministic_small_metadata_only_no_annotation_reads(
    source, monkeypatch, capsys
):
    actual = Path.open

    def guarded(path, *args, **kwargs):
        assert path != source.annotation
        assert path.name not in {"seen.json", "unseen.json"}
        assert path.suffix not in {".png", ".safetensors", ".parquet"}
        return actual(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    monkeypatch.setattr(index, "load_parser", lambda: pytest.fail("Emit must not import parser"))
    args = ["--emit-plan", "--source-manifest", str(source.manifest)]
    assert index.main(args) == 0
    first = capsys.readouterr().out
    assert index.main(args) == 0
    assert capsys.readouterr().out == first
    assert json.loads(first)["budget"]["max_bytes"] == 64 * 1024 * 1024


def test_cli_success_is_create_only_bound_and_does_not_read_payloads(source, monkeypatch):
    bound_plan = source.root / "configs/training/index-fixture.json"
    bound_plan.parent.mkdir(parents=True)
    bound_plan.write_bytes(index.canonical(plan(source)))
    binding = index.small_binding(bound_plan)
    output = source.root / "artifacts/diagnostics/openfly-bounded-annotation-index-fixture"
    actual = Path.open

    def guarded(path, *args, **kwargs):
        assert path.suffix not in {".png", ".parquet", ".safetensors"}
        assert path.name not in {"seen.json", "unseen.json", "training-events.jsonl"}
        return actual(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    args = [
        "--plan",
        str(bound_plan),
        "--plan-sha256",
        binding["sha256"],
        "--output-dir",
        str(output),
    ]
    assert index.main(args) == 0
    saved = (output / "report.json").read_bytes()
    assert index.main(args) == 1 and (output / "report.json").read_bytes() == saved
    other = output.parent / "openfly-bounded-annotation-index-wrongsha"
    args[-1], args[3] = str(other), "0" * 64
    assert index.main(args) == 1 and not other.exists()


def test_failure_publishes_no_success_directory(source):
    source.replace(b"[{}]")
    file = source.root / "plan.json"
    file.write_bytes(index.canonical(plan(source)))
    output = source.root / "artifacts/diagnostics/openfly-bounded-annotation-index-failed"
    assert (
        index.main(
            [
                "--plan",
                str(file),
                "--plan-sha256",
                index.small_binding(file)["sha256"],
                "--output-dir",
                str(output),
            ]
        )
        == 1
    )
    assert not output.exists()


def test_outside_evidence_and_unapproved_output_paths_are_rejected(source):
    with pytest.raises(ValueError, match="escapes repository"):
        index.local_path(source.root.parent / "outside.json", exists=False)
    with pytest.raises(ValueError, match="create-only"):
        index.output_target(
            source.root / "artifacts/training/openfly-bounded-annotation-index-wrong"
        )


def test_module_import_is_guarded_and_stdlib_only():
    file = Path(index.__file__).resolve()
    code = (
        "import runpy,sys; "
        f"runpy.run_path({str(file)!r},run_name='guarded_import'); "
        "assert not {'torch','transformers','PIL','numpy','requests','pyarrow'}"
        ".intersection(sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
