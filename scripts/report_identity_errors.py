"""Report identity false accepts/rejects by task and candidate strata."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VIEWS = {"front", "oblique", "edge_truncated", "recovery"}
DIMENSIONS = ("task_category", "candidate_category", "view", "size_bucket", "world")
ACCEPT = {"approach", "approach_track", "confirm", "confirm_target", "match", "true"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _rows(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        return value["rows"]
    if isinstance(value, list):
        return value
    raise ValueError(f"prediction file must contain a rows list: {path}")


def _id(row: dict[str, Any]) -> str:
    value = row.get("sample_id", row.get("id"))
    if value is None:
        raise ValueError("dataset and predictions require sample_id or id")
    return str(value)


def _decision(row: dict[str, Any]) -> str:
    value = row.get("decision")
    if isinstance(value, dict):
        value = value.get("decision")
    return str(value or "")


def _accepted(row: dict[str, Any]) -> bool:
    value = _decision(row).casefold()
    return value in ACCEPT or value.startswith(("approach_", "confirm_"))


def _short_side(row: dict[str, Any]) -> float | None:
    candidates = row.get("candidates") or []
    focus = row.get("focus_track_id")
    candidate = next((item for item in candidates if item.get("track_id") == focus), candidates[0] if candidates else None)
    box = candidate.get("box_xyxy_normalized") if isinstance(candidate, dict) else None
    if not isinstance(box, list) or len(box) != 4:
        return None
    image = row.get("image_size") or row.get("camera_info") or {}
    width, height = float(image.get("width", 1280)), float(image.get("height", 960))
    x1, y1, x2, y2 = map(float, box)
    return min(abs(x2 - x1) * width, abs(y2 - y1) * height)


def _size_bucket(row: dict[str, Any]) -> str:
    size = _short_side(row)
    if size is None:
        return "unknown"
    for upper, label in ((8, "<8"), (12, "8-12"), (16, "12-16"), (24, "16-24"), (32, "24-32")):
        if size < upper:
            return label
    return ">=32"


def _truth_targets(world: str, cache: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if world not in cache:
        matches = sorted((ROOT / "configs/scenarios").glob(f"*/{world}.truth.json"))
        if not matches:
            cache[world] = {}
        else:
            truth = json.loads(matches[0].read_text(encoding="utf-8"))
            cache[world] = {str(obj.get("target_id")): obj for obj in truth.get("targets", []) if obj.get("target_id")}
    return cache[world]


def _category(obj: dict[str, Any] | None) -> str:
    if not obj:
        return "unknown"
    return str(obj.get("class") or obj.get("asset_slug") or obj.get("category") or obj.get("label") or "unknown")


def _merge(dataset: list[dict[str, Any]], predictions: list[dict[str, Any]], truth_cache: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    sources = {_id(row): row for row in dataset}
    preds = {_id(row): row for row in predictions}
    if len(preds) != len(predictions):
        raise ValueError("prediction rows contain duplicate sample IDs")
    if sources.keys() != preds.keys():
        raise ValueError(f"prediction sample IDs differ from dataset: missing={len(sources.keys()-preds.keys())}, extra={len(preds.keys()-sources.keys())}")
    merged = []
    for sample_id, source in sources.items():
        pred = preds[sample_id]
        row = {**source, **pred}
        world = str(source.get("world") or pred.get("world") or "unknown")
        target_id = str(source.get("target_id") or source.get("truth_target_id") or "")
        if not target_id:
            continue
        truth = _truth_targets(world, truth_cache)
        candidate = truth.get(target_id)
        intended = next((obj for obj in truth.values() if obj.get("true_target")), None)
        task = source.get("task_category") or source.get("truth_task_category") or source.get("task_query")
        if not task:
            task = _category(intended)
        candidate_category = source.get("candidate_category") or source.get("truth_category") or _category(candidate)
        view_keys = ("view", "view_name", "observation_view", "sequence_view")
        has_view = any(key in source or key in pred for key in view_keys)
        view = next((source.get(key) or pred.get(key) for key in view_keys if source.get(key) or pred.get(key)), None)
        if view not in VIEWS:
            if has_view:
                continue
            view = "unknown"
        label = str(source.get("label_type", "")).casefold()
        is_distractor = "distractor" in label or "no_match" in label or "negative" in label
        is_positive = "true_target" in label or "proposal_true" in label or "truth_match" in label or "positive" in label
        # Legacy v1 labels use truth_match/truth_distractor.
        if not is_positive and not is_distractor:
            continue
        row.update({"sample_id": sample_id, "world": world, "task_category": str(task or "unknown"),
                    "candidate_category": str(candidate_category or "unknown"), "view": str(view),
                    "size_bucket": _size_bucket(source), "is_distractor": is_distractor,
                    "is_positive": is_positive, "accepted": _accepted(pred)})
        merged.append(row)
    return merged


def _aggregate(rows: list[dict[str, Any]], error_key: str) -> dict[str, dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        counted = row["is_distractor"] if error_key == "false_accept" else row["is_positive"]
        if counted:
            key = tuple(str(row[key]) for key in DIMENSIONS)
            groups[key].append(row)
    result = {}
    for key, values in sorted(groups.items()):
        errors = sum(v["accepted"] if error_key == "false_accept" else not v["accepted"] for v in values)
        result["|".join(key)] = {"dimensions": dict(zip(DIMENSIONS, key)), "count": len(values), "errors": errors,
                                  "rate": errors / len(values) if values else None}
    return result


def build_report(dataset_path: Path, baseline_path: Path | None, candidate_path: Path, top_n: int = 20) -> dict[str, Any]:
    dataset = _read_jsonl(dataset_path)
    cache: dict[str, dict[str, Any]] = {}
    candidate_rows = _merge(dataset, _rows(candidate_path), cache)
    baseline_rows = _merge(dataset, _rows(baseline_path), cache) if baseline_path else None
    models = {"candidate": candidate_rows}
    if baseline_rows is not None:
        models["baseline"] = baseline_rows
    report: dict[str, Any] = {"dataset": str(dataset_path), "dimensions": list(DIMENSIONS), "models": {}, "comparisons": {}, "top_n": top_n}
    for name, rows in models.items():
        distractors = [row for row in rows if row["is_distractor"]]
        positives = [row for row in rows if row["is_positive"]]
        report["models"][name] = {"rows": len(rows), "distractor_false_accept": _aggregate(rows, "false_accept"),
                                  "true_target_false_reject": _aggregate(rows, "false_reject"),
                                  "summary": {
                                      "distractor_false_accept": {"errors": sum(row["accepted"] for row in distractors), "count": len(distractors)},
                                      "true_target_false_reject": {"errors": sum(not row["accepted"] for row in positives), "count": len(positives)},
                                  }}
        for error in ("distractor_false_accept", "true_target_false_reject"):
            summary = report["models"][name]["summary"][error]
            summary["rate"] = summary["errors"] / summary["count"] if summary["count"] else None
    if baseline_rows is not None:
        for error in ("distractor_false_accept", "true_target_false_reject"):
            base = report["models"]["baseline"][error]
            cand = report["models"]["candidate"][error]
            comparison = {}
            for key in sorted(base.keys() | cand.keys()):
                b, c = base.get(key), cand.get(key)
                comparison[key] = {"dimensions": (c or b)["dimensions"],
                                   "baseline": b, "candidate": c,
                                   "delta_errors": None if b is None or c is None else c["errors"] - b["errors"],
                                   "delta_rate": None if b is None or c is None else c["rate"] - b["rate"]}
            report["comparisons"][error] = comparison
        report["comparisons"]["summary"] = {}
        for error in ("distractor_false_accept", "true_target_false_reject"):
            base = report["models"]["baseline"]["summary"][error]
            cand = report["models"]["candidate"]["summary"][error]
            report["comparisons"]["summary"][error] = {
                "baseline": base, "candidate": cand,
                "delta_errors": cand["errors"] - base["errors"],
                "delta_rate": cand["rate"] - base["rate"],
            }
    candidate_model = report["models"]["candidate"]
    for error in ("distractor_false_accept", "true_target_false_reject"):
        groups = candidate_model[error]
        candidate_model[f"{error}_top"] = sorted(groups.values(), key=lambda g: (-g["errors"], -g["rate"], tuple(g["dimensions"].values())))[:top_n]
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = ["# 身份错误分层报告", "", f"数据集：`{report['dataset']}`", "", "比率 = 错误数 / 该分层计分行数。尺寸按候选框短边像素分档。", ""]
    models = report["models"]
    names = ["baseline", "candidate"] if "baseline" in models else ["candidate"]
    for error, title in (("distractor_false_accept", "干扰物误收"), ("true_target_false_reject", "真目标误拒")):
        lines.extend([f"## {title}", ""])
        lines.append(f"总览：" + "；".join(
            f"{name} {models[name]['summary'][error]['errors']}/{models[name]['summary'][error]['count']} ({models[name]['summary'][error]['rate']:.4f})"
            for name in names
        ))
        if "baseline" in models:
            summary_delta = report["comparisons"]["summary"][error]
            lines.append(f"候选相对基线：错误 {summary_delta['delta_errors']:+d}，比率 {summary_delta['delta_rate']:+.4f}")
        lines.append("")
        lines.extend(["", "| 模型 | 任务类别 | 候选类别 | 视角 | 尺寸档 | 世界 | 错误/计分数 | 比率 |", "|---|---|---|---|---|---|---:|---:|"])
        for name in names:
            for group in models[name][error].values():
                d = group["dimensions"]
                lines.append(f"| {name} | {d['task_category']} | {d['candidate_category']} | {d['view']} | {d['size_bucket']} | {d['world']} | {group['errors']}/{group['count']} | {group['rate']:.4f} |")
        if "baseline" in models:
            lines.extend(["", "候选相对基线差值（负数表示错误减少）："]) 
            for key, item in report["comparisons"][error].items():
                if item["delta_errors"] is not None and item["delta_errors"]:
                    lines.append(f"- `{key}`：错误 {item['delta_errors']:+d}，比率 {item['delta_rate']:+.4f}")
        lines.extend(["", f"候选 Top-{report['top_n']} 错误组合："])
        for group in models["candidate"][f"{error}_top"]:
            lines.append(f"- `{ ' | '.join(group['dimensions'].values()) }`：{group['errors']}/{group['count']} ({group['rate']:.4f})")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--top-n", type=int, default=20)
    args = parser.parse_args()
    if args.top_n < 0:
        parser.error("--top-n must be non-negative")
    report = build_report(args.dataset, args.baseline, args.candidate, args.top_n)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output_md.write_text(markdown_report(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
