#!/usr/bin/env python3
"""离线统计小候选框拒判转为“待近看”的数量。

本脚本只读取评测输出和 test.jsonl，不调用模型。评测行按 ``sample_id`` 与
数据集记录连接；候选框来自数据集中的 ``box_xyxy_normalized``，按 1280x960
画布换算后取短边。只有模型决策为 ``reject_track`` 且短边严格小于阈值的
记录会计入。``label_type`` 为 ``truth_match`` 的记录归为真目标，其余
``truth_distractor`` 归为干扰物。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_R12 = ROOT / "artifacts/benchmarks/flight-identity-eval-v1-r12.json"
DEFAULT_R14FAST = ROOT / "artifacts/benchmarks/flight-identity-eval-v1-r14fast.json"
DEFAULT_DATASET = ROOT / "artifacts/datasets/flight-identity-eval-v1/test.jsonl"
DEFAULT_THRESHOLDS = (12.0, 16.0, 20.0)
CANVAS_WIDTH = 1280.0
CANVAS_HEIGHT = 960.0


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_dataset(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: 无法解析 JSONL: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: 记录不是 JSON 对象")
            rows.append(value)
    return rows


def _candidate_short_side(record: dict[str, Any]) -> float:
    candidates = record.get("candidates")
    if not isinstance(candidates, list) or len(candidates) == 0:
        raise ValueError(f"sample_id={record.get('sample_id')!r} 没有 candidates")

    focus_id = record.get("focus_track_id")
    matching = [candidate for candidate in candidates if candidate.get("track_id") == focus_id]
    candidate = matching[0] if matching else candidates[0]
    box = candidate.get("box_xyxy_normalized")
    if not isinstance(box, list) or len(box) != 4:
        raise ValueError(f"sample_id={record.get('sample_id')!r} 没有四元组 box_xyxy_normalized")
    try:
        x1, y1, x2, y2 = (float(value) for value in box)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"sample_id={record.get('sample_id')!r} 候选框坐标不是数字") from exc
    width = (x2 - x1) * CANVAS_WIDTH
    height = (y2 - y1) * CANVAS_HEIGHT
    if width < 0 or height < 0:
        raise ValueError(f"sample_id={record.get('sample_id')!r} 候选框坐标反向")
    return min(width, height)


def _category_counts(
    dataset_by_id: dict[str, dict[str, Any]], benchmark_rows: list[dict[str, Any]], threshold: float
) -> dict[str, Any]:
    target = Counter()
    distractor = Counter()
    missing_sample_ids: list[str] = []
    non_reject_rows = 0
    selected_rows = 0

    for result in benchmark_rows:
        sample_id = result.get("sample_id")
        if sample_id not in dataset_by_id:
            missing_sample_ids.append(str(sample_id))
            continue
        if result.get("decision") != "reject_track":
            non_reject_rows += 1
            continue
        record = dataset_by_id[sample_id]
        if _candidate_short_side(record) >= threshold:
            continue
        selected_rows += 1
        category = str(record.get("truth_category") or "unknown")
        if record.get("label_type") == "truth_match":
            target[category] += 1
        elif record.get("label_type") == "truth_distractor":
            distractor[category] += 1
        else:
            # 保留未知标签，避免静默丢数；正常数据集中应只有上面两个标签。
            distractor[f"unknown_label:{record.get('label_type')}"] += 1

    return {
        "threshold_px": threshold,
        "selected_total": selected_rows,
        "true_target_rejects": {
            "total": sum(target.values()),
            "by_category": dict(sorted(target.items())),
        },
        "distractor_rejects": {
            "total": sum(distractor.values()),
            "by_category": dict(sorted(distractor.items())),
        },
        "audit": {
            "benchmark_rows_without_dataset_join": len(missing_sample_ids),
            "missing_sample_ids": missing_sample_ids,
            "benchmark_non_reject_rows": non_reject_rows,
        },
    }


def analyze_one(
    name: str,
    benchmark_path: Path,
    dataset_by_id: dict[str, dict[str, Any]],
    thresholds: tuple[float, ...],
) -> dict[str, Any]:
    payload = _read_json(benchmark_path)
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ValueError(f"{benchmark_path}: 期望包含 rows 数组的 JSON 对象")
    rows = payload["rows"]
    by_threshold = {
        _threshold_key(threshold): _category_counts(dataset_by_id, rows, threshold)
        for threshold in thresholds
    }
    return {
        "path": str(benchmark_path),
        "benchmark_rows": len(rows),
        "thresholds": by_threshold,
    }


def _threshold_key(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def _md_table(analyses: dict[str, Any], thresholds: tuple[float, ...]) -> str:
    lines = [
        "| 评测输出 | 阈值（px） | 真目标拒判→待近看 | 干扰物拒绝→待近看 | 合计 |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, analysis in analyses.items():
        for threshold in thresholds:
            item = analysis["thresholds"][_threshold_key(threshold)]
            target = item["true_target_rejects"]["total"]
            distractor = item["distractor_rejects"]["total"]
            lines.append(
                f"| {name} | {int(threshold) if threshold.is_integer() else threshold:g} | "
                f"{target} | {distractor} | {target + distractor} |"
            )
    return "\n".join(lines)


def _category_lines(analyses: dict[str, Any], thresholds: tuple[float, ...]) -> list[str]:
    lines: list[str] = []
    for name, analysis in analyses.items():
        lines.append(f"### {name} 按类别")
        for threshold in thresholds:
            item = analysis["thresholds"][_threshold_key(threshold)]
            target = item["true_target_rejects"]["by_category"]
            distractor = item["distractor_rejects"]["by_category"]
            lines.append(
                f"\n阈值 **{int(threshold) if threshold.is_integer() else threshold:g} px**："
            )
            lines.append(
                "- 真目标："
                + ("、".join(f"{key} {value}" for key, value in target.items()) or "无")
            )
            lines.append(
                "- 干扰物："
                + ("、".join(f"{key} {value}" for key, value in distractor.items()) or "无")
            )
    return lines


def build_markdown(
    analyses: dict[str, Any], dataset_path: Path, thresholds: tuple[float, ...], dataset_count: int
) -> str:
    lines = [
        "# 小候选框拒判复查离线统计",
        "",
        "本报告只统计决定会被改成“待近看”的记录，没有执行第二次模型判断，因此不能推断复查后的确认准确率。",
        "",
        f"- 数据集：`{dataset_path}`（{dataset_count} 条）",
        "- 候选框：`box_xyxy_normalized` 按 1280×960 画布换算；短边 = min(宽、高)。",
        "- 纳入条件：评测 `decision == reject_track` 且候选短边 **严格小于** 阈值；`truth_match` "
        "计为真目标，`truth_distractor` 计为干扰物。",
        "- 连接键：`sample_id`。",
        "",
        _md_table(analyses, thresholds),
        "",
        "## 按类别",
        "",
        *_category_lines(analyses, thresholds),
        "",
        "## 解释",
        "",
        "真目标数字代表当前模型拒绝了、且尺寸门控认为值得再靠近看的目标；干扰物数字代表额外的接近代价。该统计不模拟预算、轨迹去重或复查次数上限，也不代表复查一定会转为确认。默认关闭该规则时，线上行为与当前版本相同。",
        "",
    ]
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--r12", type=Path, default=DEFAULT_R12, help="r12 评测 JSON")
    parser.add_argument("--r14fast", type=Path, default=DEFAULT_R14FAST, help="r14fast 评测 JSON")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET, help="test.jsonl")
    parser.add_argument(
        "--out", type=Path, required=True, help="输出目录（写入 JSON 和中文 Markdown）"
    )
    parser.add_argument(
        "--thresholds",
        type=float,
        nargs="+",
        default=list(DEFAULT_THRESHOLDS),
        help="短边阈值（像素），默认 12 16 20",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    thresholds = tuple(args.thresholds)
    if not thresholds or any(value <= 0 for value in thresholds):
        raise SystemExit("--thresholds 必须是正数")
    dataset_rows = _read_dataset(args.dataset)
    dataset_by_id = {str(row.get("sample_id")): row for row in dataset_rows}
    if len(dataset_by_id) != len(dataset_rows):
        raise SystemExit("数据集存在重复 sample_id，无法安全连接")

    analyses = {
        "r12": analyze_one("r12", args.r12, dataset_by_id, thresholds),
        "r14fast": analyze_one("r14fast", args.r14fast, dataset_by_id, thresholds),
    }
    result = {
        "schema_version": 1,
        "inputs": {
            "dataset": str(args.dataset),
            "r12": str(args.r12),
            "r14fast": str(args.r14fast),
        },
        "canvas_px": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
        "thresholds_px": list(thresholds),
        "dataset_rows": len(dataset_rows),
        "analyses": analyses,
    }

    args.out.mkdir(parents=True, exist_ok=True)
    json_path = args.out / "small_reject_recheck_analysis.json"
    md_path = args.out / "small_reject_recheck_analysis.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(
        build_markdown(analyses, args.dataset, thresholds, len(dataset_rows)), encoding="utf-8"
    )
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
