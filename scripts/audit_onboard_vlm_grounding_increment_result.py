"""Receive-only, CPU audit of a sealed factual VLM increment, never promotion.

Raw replies are parsed without repair. Reported metrics are compared with fresh
calculations, not used as truth. A valid audit can preserve a failed/partial
native run; successful learning requires every predeclared gate and native
verification. No model is loaded and no optimizer, transport or GPU is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from scripts import build_onboard_vlm_grounding_increment_dataset as dataset  # noqa: E402
from scripts import train_onboard_vlm_grounding as factual  # noqa: E402
from scripts import train_onboard_vlm_grounding_increment as increment  # noqa: E402
from scripts import verify_onboard_vlm_grounding_adapter as recovery  # noqa: E402

CONTRACT = "multidetect.onboard-vlm-grounding-increment-result-audit-v1"
EXPECTED_SPLITS = {"train": 154, "val": 41, "test": 43}
NAVIGATION = ("gps", "ned", "aircraft_motion")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha(value) -> str:
    return _sha(factual.canonical_bytes(value))


def _finite(value, name: str, *, minimum: float = 0.0) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum}")
    return value


def _json(path: Path, expected: str | None = None) -> tuple[dict, bytes]:
    payload = path.read_bytes()
    if len(payload) > 32 * 1024 * 1024:
        raise ValueError("audit JSON exceeds the bounded 32 MiB input")
    if expected is not None:
        factual.probe.require_sha256(expected)
        if _sha(payload) != expected:
            raise ValueError(f"JSON SHA256 mismatch: {path}")
    value = factual.strict_factual_json(payload)
    if not isinstance(value, dict):
        raise ValueError("audit input must be a JSON object")
    return value, payload


def _records(records: list[dict], snapshot: dict[Path, dict]) -> None:
    if not isinstance(records, list):
        raise ValueError("file bindings must be a list")
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "bytes", "sha256"}:
            raise ValueError("unknown bound-file fields")
        path = Path(record["path"])
        if not path.is_absolute() or type(record["bytes"]) is not int or record["bytes"] < 0:
            raise ValueError("file binding must use an absolute path and actual bytes")
        factual.probe.require_sha256(record["sha256"])
        path = path.resolve(strict=True)
        current = factual.probe.file_record(path)
        if current != {**record, "path": str(path)}:
            raise ValueError(f"bound file bytes/SHA changed: {path}")
        if path in snapshot and snapshot[path] != current:
            raise ValueError("contradictory file identities")
        snapshot[path] = current


def _official_model_provenance(provenance: dict | None, snapshot: dict[Path, dict]) -> dict | None:
    """Verify the two exact official-file shapes without loosening _records.

    These are re-opened local content identities, not a new authentication of
    the reported metadata producer or a network/model-card revalidation.
    """
    if provenance is None:
        return None
    fields = {"expected_download_bytes", "files", "gated", "license", "model_id", "revision"}
    if (
        not isinstance(provenance, dict)
        or set(provenance) != fields
        or provenance["model_id"] != factual.MODEL_ID
        or provenance["revision"] != factual.REVISION
        or provenance["license"] != "apache-2.0"
        or provenance["gated"] is not False
        or type(provenance["expected_download_bytes"]) is not int
        or not 0 < provenance["expected_download_bytes"] <= 6 * 1024**3
        or not isinstance(provenance["files"], list)
        or not provenance["files"]
    ):
        raise ValueError("unknown official model provenance or model/license identity")
    base_fields = {"path", "bytes", "sha256", "repository_path"}
    seen = set()
    total = 0
    for record in provenance["files"]:
        if not isinstance(record, dict) or set(record) not in (
            base_fields | {"official_git_blob_sha1"},
            base_fields | {"official_lfs_sha256"},
        ):
            raise ValueError("unknown official file provenance fields")
        repository_path = record["repository_path"]
        if not isinstance(repository_path, str) or not 1 <= len(repository_path) <= 512:
            raise ValueError("official repository_path must be a bounded relative POSIX path")
        relative = PurePosixPath(repository_path)
        if (
            relative.is_absolute()
            or "\\" in repository_path
            or ":" in repository_path
            or ".." in relative.parts
            or relative.as_posix() != repository_path
            or repository_path == "."
            or repository_path in seen
        ):
            raise ValueError("official repository_path is duplicated, absolute or noncanonical")
        seen.add(repository_path)
        if "official_lfs_sha256" in record:
            factual.probe.require_sha256(record["official_lfs_sha256"])
            if record["official_lfs_sha256"] != record["sha256"]:
                raise ValueError("official LFS digest differs from actual bound file SHA256")
            lfs = {"sha256": record["official_lfs_sha256"]}
            blob_id = None
        else:
            if (
                not isinstance(record["official_git_blob_sha1"], str)
                or re.fullmatch(r"[0-9a-f]{40}", record["official_git_blob_sha1"]) is None
            ):
                raise ValueError("official Git blob digest must be lowercase 40-hex")
            lfs = None
            blob_id = record["official_git_blob_sha1"]
        projected = {key: record[key] for key in ("path", "bytes", "sha256")}
        _records([projected], snapshot)
        checked = factual.probe.verify_official_file(
            Path(record["path"]),
            SimpleNamespace(
                size=record["bytes"], lfs=lfs, blob_id=blob_id, rfilename=repository_path
            ),
        )
        if checked != record:
            raise ValueError("official local file identity differs from original provenance")
        total += record["bytes"]
    if total != provenance["expected_download_bytes"]:
        raise ValueError("official declared download bytes differ from actual file sum")
    return provenance


def _samples(document: dict) -> tuple[list[dict], list[dict]]:
    samples = document["samples"]
    if Counter(s["split"] for s in samples) != EXPECTED_SPLITS:
        raise ValueError("only the exact 154/41/43 preregistered split is supported")
    if len({s["sample_id"] for s in samples}) != len(samples):
        raise ValueError("duplicate dataset sample IDs")
    for sample in samples:
        if re.fullmatch(r"[0-9a-f]{32}", sample["sample_id"]) is None:
            raise ValueError("sample identity is not opaque 32-hex")
        model_input = sample["model_input"]
        if sample["model_input_sha256"] != _canonical_sha(model_input):
            raise ValueError("dataset model-input identity differs from actual fields")
        if sample["assistant_target_sha256"] != _canonical_sha(sample["assistant_target"]):
            raise ValueError("dataset target identity differs from actual fields")
        factual.validate_target(sample["assistant_target"], model_input["target_reference_id"])
    train = [s for s in samples if s["split"] == "train"]
    holdout = [s for split in ("val", "test") for s in samples if s["split"] == split]
    return train, holdout


def _parse_reply(row: dict, sample: dict, *, max_new_tokens: int) -> tuple[dict, dict]:
    if type(max_new_tokens) is not int or not 32 <= max_new_tokens <= 256:
        raise ValueError("unverified or unsupported generation token budget")
    fields = {
        "sample_id": sample["sample_id"],
        "split": sample["split"],
        "model_input_sha256": sample["model_input_sha256"],
        "expected_target": sample["assistant_target"],
        "source_clip_id": sample["provenance"]["source_clip_id"],
        "reference_rgb_sha256": sample["model_input"]["images"][0]["sha256"],
        "current_rgb_sha256": sample["model_input"]["images"][1]["sha256"],
        "prompt_sha256": _sha(sample["model_input"]["prompt"].encode("utf-8")),
    }
    if any(row.get(key) != value for key, value in fields.items()):
        raise ValueError("evaluation is not bound to the exact heldout input/target")
    if row.get("native_generation_completed") is not True:
        raise ValueError("an incomplete generation cannot become a factual reply")
    raw = row.get("raw_output")
    tokens = row.get("output_token_ids")
    if (
        not isinstance(raw, str)
        or len(raw.encode("utf-8")) > 64 * 1024
        or not isinstance(tokens, list)
        or not 1 <= len(tokens) <= max_new_tokens
        or any(type(token) is not int or token < 0 for token in tokens)
        or type(row.get("generated_tokens")) is not int
        or row["generated_tokens"] != len(tokens)
        or type(row.get("token_cap_reached")) is not bool
    ):
        raise ValueError("malformed native generation/raw token evidence")
    # The frozen generator records len(ids) >= budget.max_new_tokens. Do not
    # let a caller hide truncation behind a contradictory False flag.
    if row["token_cap_reached"] is not (len(tokens) >= max_new_tokens):
        raise ValueError("token-cap flag differs from bound generated length")
    elapsed = _finite(row["elapsed_s"], "generation elapsed_s")
    raw_parsed = None
    try:
        raw_parsed = factual.strict_factual_json(raw.encode("utf-8"))
    except (ValueError, TypeError):
        pass
    parsed = None
    accepted = False
    if len(tokens) < max_new_tokens and elapsed <= 30.0:
        parsed = raw_parsed
        try:
            factual.validate_target(parsed, sample["model_input"]["target_reference_id"])
            accepted = True
        except (ValueError, TypeError, KeyError):
            pass
    exact = accepted and parsed == sample["assistant_target"]
    if (
        row.get("parsed_output") != parsed
        or row.get("strict_fact_reply_accepted") is not accepted
        or row.get("exact_factual_match") is not exact
    ):
        raise ValueError("reported parser/strict/exact facts disagree with original raw reply")
    rebuilt = {
        **row,
        "parsed_output": parsed,
        "strict_fact_reply_accepted": accepted,
        "exact_factual_match": exact,
    }
    diagnostic = {
        "sample_id": sample["sample_id"],
        "split": sample["split"],
        "visibility": raw_parsed.get("current_target_visibility")
        if isinstance(raw_parsed, dict)
        else None,
        "target_reference_id_correct": isinstance(raw_parsed, dict)
        and raw_parsed.get("target_reference_id")
        == fields["expected_target"]["target_reference_id"],
        "unknown_navigation_fields": {
            name: isinstance(raw_parsed, dict) and raw_parsed.get(name) == "unknown"
            for name in NAVIGATION
        },
        "original_raw_sha256": _sha(raw.encode("utf-8")),
    }
    return rebuilt, diagnostic


def _confusion(rows: list[dict]) -> dict:
    result = dict.fromkeys(("tp", "tn", "fp", "fn", "invalid"), 0)
    for row in rows:
        gold = row["expected_target"]["current_target_visibility"] == "visible"
        prediction = (
            row["parsed_output"].get("current_target_visibility")
            if (row["strict_fact_reply_accepted"] and isinstance(row["parsed_output"], dict))
            else None
        )
        if prediction not in {"visible", "not_confirmed"}:
            result["invalid"] += 1
            result["fn" if gold else "fp"] += 1
        elif prediction == "visible":
            result["tp" if gold else "fp"] += 1
        else:
            result["fn" if gold else "tn"] += 1
    return result


def _summarize(rows: list[dict], diagnostics: list[dict]) -> dict:
    unknown = {
        name: sum(
            isinstance(row["parsed_output"], dict) and row["parsed_output"].get(name) == "unknown"
            for row in rows
        )
        for name in NAVIGATION
    }
    return {
        "count": len(rows),
        "strict_reply_count": sum(row["strict_fact_reply_accepted"] for row in rows),
        "exact_match_count": sum(row["exact_factual_match"] for row in rows),
        "visibility_confusion": _confusion(rows),
        "per_split_visibility": {
            split: _confusion([r for r in rows if r["split"] == split]) for split in ("val", "test")
        },
        "unknown_field_counts": unknown,
        "per_split_unknown_field_counts": {
            split: {
                name: sum(
                    isinstance(row["parsed_output"], dict)
                    and row["parsed_output"].get(name) == "unknown"
                    for row in rows
                    if row["split"] == split
                )
                for name in NAVIGATION
            }
            for split in ("val", "test")
        },
        "unknown_navigation_fact_count": sum(
            isinstance(row["parsed_output"], dict)
            and all(row["parsed_output"].get(name) == "unknown" for name in NAVIGATION)
            for row in rows
        ),
        "raw_target_reference_id_correct_count": sum(
            item["target_reference_id_correct"] for item in diagnostics
        ),
        "raw_unknown_field_counts": {
            name: sum(item["unknown_navigation_fields"][name] for item in diagnostics)
            for name in NAVIGATION
        },
        "raw_diagnostics_do_not_relax_strict_reply": True,
        "samples": diagnostics,
    }


def _evaluation(
    report: dict,
    directory: Path,
    role: str,
    holdout: list[dict],
    bound_outputs: set[Path],
    *,
    max_new_tokens: int,
) -> tuple[dict, list[dict]]:
    declared = report.get(f"{role}_evaluation")
    rows = []
    diagnostics = []
    failed = []
    valid_ids = {s["sample_id"] for s in holdout}
    row_dir = directory / f"{role}-rows"
    if row_dir.exists():
        if {p.stem for p in row_dir.glob("*.json")} - valid_ids:
            raise ValueError("foreign/optimizer sample entered holdout evaluation")
        for sample in holdout:
            path = row_dir / f"{sample['sample_id']}.json"
            if not path.exists():
                continue
            if path.resolve() not in bound_outputs:
                raise ValueError("raw evaluation artifact was not sealed in report")
            row, _ = _json(path)
            if row.get("native_generation_completed") is not True:
                if (
                    row.get("sample_id") != sample["sample_id"]
                    or row.get("split") != sample["split"]
                ):
                    raise ValueError("failed generation is associated with a foreign input")
                failed.append(sample["sample_id"])
                continue
            rebuilt, diagnostic = _parse_reply(row, sample, max_new_tokens=max_new_tokens)
            rows.append(rebuilt)
            diagnostics.append(diagnostic)
    metrics = factual.grounding_metrics(rows)
    summary = _summarize(rows, diagnostics)
    for key in ("tp", "tn", "fp", "fn", "invalid"):
        if summary["visibility_confusion"][key] != metrics["visibility_confusion"][key]:
            raise ValueError("independent visibility computation disagrees with frozen formula")
    if declared is not None:
        path = directory / f"{role}-evaluation.json"
        if path.resolve() not in bound_outputs or _json(path)[0] != declared:
            raise ValueError("embedded evaluation differs from sealed original evaluation")
        if (
            declared["samples"] != rows
            or any(
                declared[name] != summary[name]
                for name in ("count", "strict_reply_count", "exact_match_count")
            )
            or declared["metrics"] != metrics
        ):
            raise ValueError("reported metrics/rows differ from independently re-parsed evidence")
    complete = len(rows) == 84 and not failed and declared is not None
    return {
        **summary,
        "complete": complete,
        "failed_generation_sample_ids": failed,
        "frozen_metrics_recomputed": metrics,
    }, rows


def _optimizer(report: dict, directory: Path, train: list[dict], outputs: set[Path]) -> dict:
    log = directory / "training-log.jsonl"
    rows = []
    if log.exists():
        if log.resolve() not in outputs:
            raise ValueError("optimizer log is not sealed")
        payload = log.read_bytes()
        if len(payload) > 8 * 1024 * 1024:
            raise ValueError("optimizer log exceeds bounded epoch")
        rows = [factual.strict_factual_json(line) for line in payload.splitlines()]
    allowed = {s["sample_id"] for s in train}
    seen = set()
    for step, row in enumerate(rows, 1):
        if (
            not isinstance(row, dict)
            or type(row.get("step")) is not int
            or row["step"] != step
            or type(row.get("epoch")) is not int
            or row["epoch"] != 1
            or row.get("split") != "train"
            or row.get("sample_id") not in allowed
            or row["sample_id"] in seen
        ):
            raise ValueError("optimizer rows duplicate, leave train, or break the single epoch")
        seen.add(row["sample_id"])
        for key in ("loss", "gradient_norm_before_clip", "elapsed_s"):
            _finite(row[key], f"optimizer {key}")
    if "optimization_rows" in report and report["optimization_rows"] != rows:
        raise ValueError("reported optimizer rows differ from original log bytes")
    counters = (
        "optimizer_steps_attempted",
        "optimizer_native_calls_returned",
        "optimizer_cuda_sync_completed_count",
        "optimizer_steps_completed",
    )
    for key in counters:
        value = report.get(key, 0)
        if type(value) is not int or not 0 <= value <= 154:
            raise ValueError("invalid actual optimizer count")
    attempted, returned, synchronized, completed = (report.get(key, 0) for key in counters)
    if not completed == synchronized <= returned <= attempted:
        raise ValueError("optimizer counters violate attempted/returned/CUDA/completed causality")
    if completed > 0 and report.get("vlm_training_performed") is not True:
        raise ValueError("completed optimizer updates require true training-performed fact")
    if report.get("optimizer_steps_completed", 0) < len(rows):
        raise ValueError("logged optimizer rows exceed completed updates")
    if "optimizer_log_rows_completed" in report and report["optimizer_log_rows_completed"] != len(
        rows
    ):
        raise ValueError("partial-run log count differs from actual bytes")
    complete = len(rows) == 154 and seen == allowed and all(report.get(k) == 154 for k in counters)
    complete = complete and report.get("optimizer_update_uncertain") is False
    return {
        "logged_updates": len(rows),
        "each_original_train_id_exactly_once": seen == allowed,
        "holdout_optimizer_rows": 0,
        "finite_loss_and_gradient_norm": True,
        "complete": complete,
        "logged_sample_ids": [r["sample_id"] for r in rows],
        "unlogged_completed_update_count": report.get("optimizer_steps_completed", 0) - len(rows),
        "optimizer_update_uncertain": report.get("optimizer_update_uncertain", False),
    }


def _adapter(report: dict, directory: Path, outputs: set[Path], candidate_rows: list[dict]) -> dict:
    path = directory / "adapter/adapter_model.safetensors"
    status = {
        "saved_fp32_digest_verified": False,
        "base_unchanged": False,
        "adapter_changed": False,
        "reported_reload_raw_tokens_match": False,
    }
    before = report.get("base_parameter_sha256_before")
    after = report.get("base_parameter_sha256_after")
    if before is not None and after is not None:
        factual.probe.require_sha256(before)
        factual.probe.require_sha256(after)
        status["base_unchanged"] = (
            before == after and report.get("base_parameter_bytes_unchanged") is True
        )
    old = report.get("adapter_parameter_sha256_before")
    trained = report.get("adapter_parameter_sha256_after")
    if old is not None and trained is not None:
        factual.probe.require_sha256(old)
        factual.probe.require_sha256(trained)
        status["adapter_changed"] = old != trained
    if path.exists():
        if path.resolve() not in outputs:
            raise ValueError("saved adapter bytes are not sealed")
        try:
            from safetensors.torch import load_file

            digest = recovery.saved_parameter_digest(
                load_file(str(path), device="cpu"),
                report["trainable_audit"],
                rank=report["budget"]["rank"],
            )
            status["saved_parameter_sha256"] = digest
            status["saved_fp32_digest_verified"] = digest == trained
        except (ValueError, KeyError, TypeError) as exc:
            status["saved_tensor_verification_failure"] = str(exc)
    reload = report.get("saved_adapter_reload", {})
    if status["saved_fp32_digest_verified"] and reload:
        generation = reload.get("generation", {})
        row = next((r for r in candidate_rows if r["sample_id"] == reload.get("sample_id")), None)
        status["reported_reload_raw_tokens_match"] = (
            row is not None
            and reload.get("saved_parameter_sha256") == trained
            and reload.get("loaded_parameter_sha256") == trained
            and reload.get("exact_tensor_equality") is True
            and reload.get("fresh_base") is True
            and reload.get("generation_token_ids_equal") is True
            and reload.get("raw_output_equal") is True
            and generation.get("output_token_ids") == row["output_token_ids"]
            and generation.get("raw_output") == row["raw_output"]
            and reload.get("dtype_conversion_performed") is False
        )
    status["independent_model_generation_replayed"] = False
    return status


def quality_gates(parent: dict, candidate: dict) -> dict:
    gates = {
        "strict_reply_count_nonregression": candidate["strict_reply_count"]
        >= parent["strict_reply_count"],
        "exact_match_count_nonregression": candidate["exact_match_count"]
        >= parent["exact_match_count"],
    }
    for split in ("val", "test"):
        old, new = parent["per_split_visibility"][split], candidate["per_split_visibility"][split]
        gates[f"{split}_visible_nonregression"] = new["tp"] >= old["tp"]
        gates[f"{split}_not_confirmed_nonregression"] = new["tn"] >= old["tn"]
    gates["at_least_one_extra_correct_zero_pixel_holdout"] = (
        candidate["visibility_confusion"]["tn"] > parent["visibility_confusion"]["tn"]
    )
    gates["all_navigation_fields_remain_unknown"] = candidate["unknown_navigation_fact_count"] == 84
    return gates


def _parent_and_plan(
    report: dict,
    document: dict,
    dataset_manifest: Path,
    review_receipt: Path,
    snapshot: dict[Path, dict],
) -> dict:
    def source(expected: str) -> Path:
        factual.probe.require_sha256(expected)
        matches = [record for record in report["source_files"] if record["sha256"] == expected]
        if len(matches) != 1:
            raise ValueError("exact plan/parent report is absent or ambiguous in bound sources")
        return Path(matches[0]["path"]).resolve(strict=True)

    plan_path = source(report["plan_sha256"])
    plan, budget = increment.load_plan(plan_path, report["plan_sha256"])
    if (
        plan["dataset_sha256"] != report["dataset_sha256"]
        or plan["review_receipt_sha256"] != factual.probe.sha256_file(review_receipt)
        or plan["split_counts"] != EXPECTED_SPLITS
        or report["budget"] != asdict(budget)
    ):
        raise ValueError("bound plan dataset/review/split/budget differs from actual training")
    parent_path = source(increment.PARENT_REPORT_SHA256)
    original, _ = _json(parent_path, increment.PARENT_REPORT_SHA256)
    parent_record = document["parent_adapter"]
    if (
        report.get("parent_adapter_sha256") != increment.PARENT_ADAPTER_SHA256
        or parent_record["sha256"] != increment.PARENT_ADAPTER_SHA256
    ):
        raise ValueError("the increment did not preserve the fixed r1 parent adapter identity")
    parent_adapter = (dataset.ROOT / parent_record["path"]).resolve(strict=True)
    _records([{**parent_record, "path": str(parent_adapter)}], snapshot)
    from safetensors.torch import load_file

    digest = recovery.saved_parameter_digest(
        load_file(str(parent_adapter), device="cpu"),
        original["trainable_audit"],
        rank=budget.rank,
    )
    if (
        original["model_id"] != report["model_id"]
        or original["revision"] != report["revision"]
        or digest != original["adapter_parameter_sha256_after"]
    ):
        raise ValueError("actual parent FP32 bytes differ from the frozen r1 trained digest")
    # A partial failure before model construction may have no warm-start facts.
    warm = report.get("warm_start")
    if warm is not None and (
        report.get("adapter_parameter_sha256_before") != digest
        or report.get("base_parameter_sha256_before") != original["base_parameter_sha256_after"]
        or report.get("trainable_audit") != original["trainable_audit"]
        or warm.get("saved_parameter_sha256") != digest
        or warm.get("loaded_parameter_sha256") != digest
        or warm.get("exact_tensor_equality") is not True
        or warm.get("dtype_conversion_performed") is not False
    ):
        raise ValueError("reported warm-start/base/before digest is not the actual fixed parent")
    if report.get("optimizer_steps_completed", 0) > 0 and warm is None:
        raise ValueError("actual optimizer rows have no exact parent warm-start binding")
    return {
        "plan": factual.probe.file_record(plan_path),
        "plan_dataset_review_budget_and_splits_exact": True,
        "parent_report": factual.probe.file_record(parent_path),
        "parent_adapter": factual.probe.file_record(parent_adapter),
        "parent_saved_fp32_parameter_sha256": digest,
        "reported_warm_start_verified": warm is not None,
        "bound_generation_max_new_tokens": budget.max_new_tokens,
        "parent_original_native_exit_code": original["native_exit_code"],
        "dataset_manifest": factual.probe.file_record(dataset_manifest),
    }


def audit_increment_result(
    report_path: Path,
    *,
    expected_report_sha256: str,
    dataset_manifest: Path,
    expected_dataset_sha256: str,
    review_receipt: Path,
) -> dict:
    """Audit exact source/output bytes; a partial run never gains a success gate."""
    report_path = report_path.resolve(strict=True)
    report, payload = _json(report_path, expected_report_sha256)
    native = report.get("native_exit_code")
    if (
        report.get("contract") != increment.CONTRACT
        or report.get("model_id") != factual.MODEL_ID
        or report.get("revision") != factual.REVISION
        or type(native) is not int
        or native not in {0, 1}
        or any(report.get(flag) is not False for flag in increment.FALSE_FLAGS)
    ):
        raise ValueError("unknown run contract/native status or authority escalation")
    if report.get("dataset_sha256") != expected_dataset_sha256:
        raise ValueError("training run used another dataset")
    document = dataset.validate_grounding_increment_dataset(
        dataset_manifest,
        expected_dataset_sha256,
        review_receipt=review_receipt,
        require_review=True,
    )
    train, holdout = _samples(document)
    old_binding = document["base_manifest"]
    original, _ = _json(dataset.ROOT / old_binding["path"], old_binding["sha256"])
    if holdout != [
        s for split in ("val", "test") for s in original["samples"] if s["split"] == split
    ]:
        raise ValueError("heldout sample fields are not exact original inputs")
    snapshot = {}
    for key in ("source_files", "output_artifact_files", "runtime_files", "adapter_files"):
        _records(report.get(key, []), snapshot)
    official_provenance = _official_model_provenance(report.get("model_provenance"), snapshot)
    _records(
        [
            factual.probe.file_record(dataset_manifest),
            factual.probe.file_record(review_receipt),
            factual.probe.file_record(report_path),
        ],
        snapshot,
    )
    for sample in document["samples"]:
        for image in sample["model_input"]["images"]:
            _records(
                [
                    {
                        **{k: image[k] for k in ("bytes", "sha256")},
                        "path": str((dataset_manifest.parent / image["path"]).resolve()),
                    }
                ],
                snapshot,
            )
    source_paths = {Path(r["path"]).resolve() for r in report["source_files"]}
    if not {dataset_manifest.resolve(), review_receipt.resolve()} <= source_paths:
        raise ValueError("report does not seal actual dataset and independent review sources")
    provenance = _parent_and_plan(report, document, dataset_manifest, review_receipt, snapshot)
    outputs = {Path(r["path"]).resolve() for r in report["output_artifact_files"]}
    actual = {
        p.resolve()
        for p in report_path.parent.rglob("*")
        if p.is_file() and p.resolve() != report_path
    }
    if outputs != actual or any(not p.is_relative_to(report_path.parent) for p in outputs):
        raise ValueError(
            "sealed output inventory is incomplete or outside exclusive model directory"
        )
    parent, parent_rows = _evaluation(
        report,
        report_path.parent,
        "parent",
        holdout,
        outputs,
        max_new_tokens=provenance["bound_generation_max_new_tokens"],
    )
    candidate, candidate_rows = _evaluation(
        report,
        report_path.parent,
        "candidate",
        holdout,
        outputs,
        max_new_tokens=provenance["bound_generation_max_new_tokens"],
    )
    optimization = _optimizer(report, report_path.parent, train, outputs)
    adapter = _adapter(report, report_path.parent, outputs, candidate_rows)
    complete = parent["complete"] and candidate["complete"]
    gates = quality_gates(parent, candidate)
    if complete and "quality_comparison" in report:
        expected_quality = increment.quality_comparison(
            {
                "samples": parent_rows,
                **{k: parent[k] for k in ("count", "strict_reply_count", "exact_match_count")},
                "metrics": parent["frozen_metrics_recomputed"],
            },
            {
                "samples": candidate_rows,
                **{k: candidate[k] for k in ("count", "strict_reply_count", "exact_match_count")},
                "metrics": candidate["frozen_metrics_recomputed"],
            },
        )
        if report["quality_comparison"] != expected_quality or expected_quality["gates"] != gates:
            raise ValueError("reported quality gate differs from independent raw evaluation")
    verification = (
        complete
        and official_provenance is not None
        and optimization["complete"]
        and report.get("vlm_training_performed") is True
        and report.get("pre_optimizer_source_revalidation_passed") is True
        and all(
            adapter[key]
            for key in (
                "saved_fp32_digest_verified",
                "base_unchanged",
                "adapter_changed",
                "reported_reload_raw_tokens_match",
            )
        )
    )
    if native == 0 and (not verification or "failure" in report):
        raise ValueError("native success is contradicted by exact artifact/optimizer verification")
    quality_passed = complete and all(gates.values())
    result = {
        "contract": CONTRACT,
        "training_report": factual.probe.file_record(report_path),
        "dataset_manifest": factual.probe.file_record(dataset_manifest),
        "review_receipt": factual.probe.file_record(review_receipt),
        "training_native_exit_code": native,
        "training_failure": report.get("failure"),
        "partial_run": not verification,
        "split_counts": EXPECTED_SPLITS,
        "original_holdout_inputs_exact": True,
        "paired_holdout_count": len(
            {r["sample_id"] for r in parent_rows} & {r["sample_id"] for r in candidate_rows}
        ),
        "parent": parent,
        "candidate": candidate,
        "optimizer": optimization,
        "saved_adapter": adapter,
        "provenance": provenance,
        "official_model_provenance": official_provenance,
        "official_file_hashes_reverified": official_provenance is not None,
        "official_metadata_source_authentication_granted": False,
        "quality_gates": gates,
        "quality_passed": quality_passed,
        "native_verification_completed": native == 0 and verification,
        "successfully_learned_gate": native == 0 and verification and quality_passed,
        "verified_files": sorted(snapshot.values(), key=lambda r: r["path"]),
        "audit_sources": [
            factual.probe.file_record(path)
            for path in (
                Path(__file__),
                ROOT / "tests/test_audit_onboard_vlm_grounding_increment_result.py",
                Path(factual.__file__),
                Path(increment.__file__),
                Path(recovery.__file__),
                Path(dataset.__file__),
            )
        ],
        "known_development_holdout_not_blind": True,
        "same_reference_pairs_are_independent_episodes": False,
        "optimizer_execution_independently_replayed": False,
        "source_authentication_granted": False,
        "identity_learning_claimed": False,
        **{name: False for name in increment.FALSE_FLAGS},
    }
    if report_path.read_bytes() != payload:
        raise ValueError("training report changed while auditing")
    _records(list(snapshot.values()), {})
    return result


def write_increment_audit(output: Path, **kwargs) -> Path:
    if output.exists() or output.is_symlink():
        raise FileExistsError("audit output must be new; never overwrite sealed facts")
    if any(parent.is_symlink() for parent in output.absolute().parents):
        raise ValueError("audit output cannot use a symbolic-link ancestor")
    result = audit_increment_result(**kwargs)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    with output.open("xb") as stream:
        stream.write(payload)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--report-sha256", required=True)
    parser.add_argument("--dataset-manifest", type=Path, required=True)
    parser.add_argument("--dataset-sha256", required=True)
    parser.add_argument("--review-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = write_increment_audit(
        args.output,
        report_path=args.report,
        expected_report_sha256=args.report_sha256,
        dataset_manifest=args.dataset_manifest,
        expected_dataset_sha256=args.dataset_sha256,
        review_receipt=args.review_receipt,
    )
    print(json.dumps({"audit": factual.probe.file_record(output), "candidate_promoted": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
