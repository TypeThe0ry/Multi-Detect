"""Evaluate r21id identity metrics from frozen per-row model predictions.

No inference is performed.  Predictions use the ``eval_slow_system.py`` JSON
shape (``{"rows": [...]}``), while view/target metadata is joined from the
test dataset by ``sample_id``.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

VIEWS = ("front", "oblique", "edge_truncated", "recovery")
THRESHOLDS = {
    "multiview_consistency": 0.90,
    "recovery_rate": 0.85,
    "max_recovery_observations": 2.0,
    "true_target_false_reject": 0.08,
    "fire_hydrant_false_reject": 0.12,
    "distractor_false_accept": 0.026,
}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_predictions(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        return value["rows"]
    if isinstance(value, list):
        return value
    raise ValueError(f"prediction file must be a JSON list or eval result object: {path}")


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) is not None:
            return row[key]
    return None


def _view(row: dict[str, Any]) -> str | None:
    value = _first(row, "view", "view_name", "observation_view", "sequence_view")
    if value is None and isinstance(row.get("teacher_intent"), dict):
        value = row["teacher_intent"].get("view")
    if value is None:
        text = str(row.get("sample_id", ""))
        for candidate in VIEWS:
            if candidate in text:
                value = candidate
                break
    return str(value) if value in VIEWS else None


def _target_id(row: dict[str, Any]) -> str | None:
    value = _first(row, "target_id", "true_target_id", "identity_target_id")
    if value is None and isinstance(row.get("metadata"), dict):
        value = _first(row["metadata"], "target_id", "true_target_id")
    return str(value) if value is not None else None


def _is_match(value: Any) -> bool:
    if isinstance(value, dict):
        value = value.get("decision")
    return str(value) in {"confirm_target", "approach_track", "match", "true"}


def _merge(dataset: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {str(row.get("sample_id", row.get("id"))): row for row in dataset}
    prediction_ids = [str(row.get("sample_id", row.get("id"))) for row in predictions]
    if len(prediction_ids) != len(set(prediction_ids)):
        raise ValueError("prediction rows contain duplicate sample IDs")
    missing = set(by_id) - set(prediction_ids)
    extra = set(prediction_ids) - set(by_id)
    if missing or extra:
        raise ValueError(f"prediction sample IDs differ from test split: missing={len(missing)}, extra={len(extra)}")
    merged = []
    for pred in predictions:
        sample_id = str(pred.get("sample_id", pred.get("id")))
        source = by_id.get(sample_id, {})
        row = {**source, **pred}
        row["sample_id"] = sample_id
        row["view"] = _view(row) or _view(source)
        row["target_id"] = _target_id(row) or _target_id(source)
        if row["view"] is None or row["target_id"] is None:
            raise ValueError(f"sample {sample_id} lacks view/target_id metadata")
        row["category"] = str(_first(row, "target_category", "task_category", "category", "class_name", "asset_slug") or "")
        row["label_type"] = str(row.get("label_type", ""))
        row["expected_match"] = _is_match(_first(row, "expected", "assistant_target"))
        row["predicted_match"] = _is_match(row.get("decision"))
        merged.append(row)
    return merged


def _bootstrap(values: list[float], cluster_ids: list[str] | None = None, seed: int = 2101, samples: int = 2000) -> list[float | None]:
    if not values:
        return [None, None]
    rng = random.Random(seed)
    clusters: dict[str, list[float]] = {}
    for index, value in enumerate(values):
        key = cluster_ids[index] if cluster_ids is not None else str(index)
        clusters.setdefault(key, []).append(value)
    units = list(clusters.values())
    means = []
    for _ in range(samples):
        draw = [rng.choice(units) for _ in units]
        sample = [value for cluster in draw for value in cluster]
        means.append(sum(sample) / len(sample))
    means.sort()
    return [round(means[int(samples * 0.025)], 4), round(means[int(samples * 0.975) - 1], 4)]


def _metric(value: float | None, observations: list[float], cluster_ids: list[str] | None = None) -> dict[str, Any]:
    return {"value": None if value is None else round(value, 4), "count": len(observations), "ci95": _bootstrap(observations, cluster_ids)}


def _small_target(row: dict[str, Any]) -> bool:
    explicit = _first(row, "target_short_side_px", "short_side_px")
    if explicit is not None:
        try:
            return float(explicit) <= 12
        except (TypeError, ValueError):
            pass
    candidates = row.get("candidates") or []
    focus = row.get("focus_track_id")
    candidate = next((item for item in candidates if item.get("track_id") == focus), candidates[0] if candidates else None)
    box = candidate.get("box_xyxy_normalized") if isinstance(candidate, dict) else None
    if not isinstance(box, list) or len(box) != 4:
        return False
    size = row.get("image_size") or row.get("camera_info") or {}
    width, height = float(size.get("width", 1280)), float(size.get("height", 960))
    return min(abs(float(box[2]) - float(box[0])) * width, abs(float(box[3]) - float(box[1])) * height) <= 12


def calculate_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row["target_id"]), {})[str(row["view"])] = row
    complete = [g for g in groups.values() if all(view in g for view in VIEWS)]
    consistency_obs = [float(len({g[v]["predicted_match"] for v in VIEWS}) == 1) for g in complete]
    consistency_ids = [str(g[VIEWS[0]]["target_id"]) for g in complete]
    recovery_obs, recovery_counts = [], []
    for g in complete:
        edge = g["edge_truncated"]["predicted_match"]
        recovery = g["recovery"]["predicted_match"]
        if not edge and g["recovery"]["expected_match"]:
            recovery_obs.append(float(recovery))
            recovery_counts.append(float(g["recovery"].get("recovery_observation_count", g["recovery"].get("observation_count", 1))))
    positives = [r for r in rows if r["expected_match"]]
    hydrants = [r for r in positives if ("hydrant" in r["category"].lower() or "消防栓" in str(r.get("task", r.get("task_text", "")))) and _small_target(r)]
    distractors = [r for r in rows if "distractor" in r["label_type"].lower()]
    def false_reject(items: list[dict[str, Any]]) -> tuple[float | None, list[float]]:
        obs = [float(not r["predicted_match"]) for r in items]
        return (sum(obs) / len(obs) if obs else None, obs)
    overall_fr, overall_obs = false_reject(positives)
    hyd_fr, hyd_obs = false_reject(hydrants)
    da_obs = [float(r["predicted_match"]) for r in distractors]
    recovery_ids = [str(g[VIEWS[0]]["target_id"]) for g in complete if not g["edge_truncated"]["predicted_match"] and g["recovery"]["expected_match"]]
    consistency = sum(consistency_obs) / len(consistency_obs) if consistency_obs else None
    recovery = sum(recovery_obs) / len(recovery_obs) if recovery_obs else None
    avg_recovery = sum(recovery_counts) / len(recovery_counts) if recovery_counts else None
    return {
        "multiview_consistency": _metric(consistency, consistency_obs, consistency_ids),
        "recovery_rate": _metric(recovery, recovery_obs, recovery_ids),
        "average_recovery_observations": avg_recovery,
        "average_recovery_observations_ci95": _bootstrap(recovery_counts, recovery_ids),
        "true_target_false_reject": _metric(overall_fr, overall_obs, [str(r["target_id"]) for r in positives]),
        "fire_hydrant_false_reject": _metric(hyd_fr, hyd_obs, [str(r["target_id"]) for r in hydrants]),
        "distractor_false_accept": _metric(sum(da_obs) / len(da_obs) if da_obs else None, da_obs, [str(r["target_id"]) for r in distractors]),
        "target_count": len(groups),
        "complete_target_count": len(complete),
    }


def _pass(metrics: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons = []
    checks = [
        ("multiview_consistency", metrics["multiview_consistency"]["value"] is not None and metrics["multiview_consistency"]["value"] >= THRESHOLDS["multiview_consistency"], "跨视角一致率不足"),
        ("recovery_rate", metrics["recovery_rate"]["value"] is not None and metrics["recovery_rate"]["value"] >= THRESHOLDS["recovery_rate"], "拒绝后恢复率不足"),
        ("average_recovery_observations", metrics["average_recovery_observations"] is not None and metrics["average_recovery_observations"] <= THRESHOLDS["max_recovery_observations"], "平均恢复观察次数过高"),
        ("true_target_false_reject", metrics["true_target_false_reject"]["value"] is not None and metrics["true_target_false_reject"]["value"] <= THRESHOLDS["true_target_false_reject"], "真目标总体拒绝率过高"),
        ("fire_hydrant_false_reject", metrics["fire_hydrant_false_reject"]["value"] is None or metrics["fire_hydrant_false_reject"]["value"] <= THRESHOLDS["fire_hydrant_false_reject"], "消防栓小目标拒绝率过高"),
        ("distractor_false_accept", metrics["distractor_false_accept"]["value"] is not None and metrics["distractor_false_accept"]["value"] <= THRESHOLDS["distractor_false_accept"], "干扰物接受率过高"),
    ]
    for key, ok, message in checks:
        if not ok:
            reasons.append(message)
    return not reasons, reasons


def evaluate(dataset: Path, baseline: Path, candidate: Path) -> dict[str, Any]:
    rows = _load_jsonl(dataset)
    baseline_rows = _merge(rows, _load_predictions(baseline))
    candidate_rows = _merge(rows, _load_predictions(candidate))
    baseline_targets = {row["target_id"] for row in baseline_rows}
    candidate_targets = {row["target_id"] for row in candidate_rows}
    if baseline_targets != candidate_targets:
        raise ValueError("r18 and candidate predictions do not cover the same target_id set")
    result = {"baseline": calculate_metrics(baseline_rows), "candidate": calculate_metrics(candidate_rows), "thresholds": THRESHOLDS}
    result["candidate_pass"], result["reasons"] = _pass(result["candidate"])
    baseline_fa = result["baseline"]["distractor_false_accept"]["value"]
    candidate_fa = result["candidate"]["distractor_false_accept"]["value"]
    result["distractor_gate_vs_r18"] = {
        "pass": baseline_fa is not None and candidate_fa is not None and candidate_fa <= baseline_fa,
        "r18_false_accept": baseline_fa,
        "candidate_false_accept": candidate_fa,
    }
    if not result["distractor_gate_vs_r18"]["pass"]:
        result["candidate_pass"] = False
        result["reasons"].append("候选干扰物接受率高于 r18 或缺少可比较样本")
    result["decision"] = "接受 r21id" if result["candidate_pass"] else "保留 r18"
    return result


def markdown_report(report: dict[str, Any]) -> str:
    lines = ["# r21id identity 专项评测", "", f"**结论：{report['decision']}**", "", "真目标拒绝率越低越好；干扰物接受率越低越好。置信区间以 target_id 为 bootstrap 抽样单位。", "", "| 指标 | r18 | 候选 | 门槛 |", "|---|---:|---:|---:|"]
    keys = [("multiview_consistency", "实例级跨视角一致率", ">= 0.90"), ("recovery_rate", "拒绝后恢复率", ">= 0.85"), ("average_recovery_observations", "平均恢复观察次数", "<= 2"), ("true_target_false_reject", "真目标总体拒绝率", "<= 0.08"), ("fire_hydrant_false_reject", "消防栓小目标拒绝率", "<= 0.12"), ("distractor_false_accept", "显式干扰物接受率", "<= 0.026")]
    for key, name, gate in keys:
        base = report["baseline"].get(key); cand = report["candidate"].get(key)
        def fmt(v: Any) -> str:
            if v is None:
                return "-"
            if isinstance(v, dict):
                value = v.get("value")
                ci = v.get("ci95")
                return "-" if value is None else f"{value} [{ci[0]}, {ci[1]}]"
            return str(v)
        if key == "average_recovery_observations":
            base = {"value": base, "ci95": report["baseline"].get("average_recovery_observations_ci95")}
            cand = {"value": cand, "ci95": report["candidate"].get("average_recovery_observations_ci95")}
        lines.append(f"| {name} | {fmt(base)} | {fmt(cand)} | {gate} |")
    if report["reasons"]:
        lines.extend(["", "不通过原因：" + "；".join(report["reasons"]) + "。明确保留 r18。"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True, help="r18 prediction JSON")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(args.dataset, args.baseline, args.candidate)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output_md.write_text(markdown_report(report), encoding="utf-8")
    return 0 if report["candidate_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
