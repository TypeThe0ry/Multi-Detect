"""Compare identity rounds using frozen evaluations; never perform inference."""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from scripts import report_identity_errors as errors

VIEW_METRICS = ("multiview_consistency", "true_target_false_reject", "distractor_false_accept")
DIRECTIONS = ("task_text", "candidate_category", "size_bucket", "view")


def _json(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _rate(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"invalid rate: {value!r}")
    return float(value)


def _round(label: str, pipeline: Path, views: Path) -> dict[str, Any]:
    metrics: dict[str, float | None] = {}
    pending = []
    sources = {}
    for split in ("v1", "v3"):
        path = pipeline / f"eval-{split}.json"
        sources[split] = str(path)
        data = _json(path)
        summary = (data or {}).get("metrics", {})
        if summary.get("count") == 0:
            summary = {}
        metrics[f"{split}.distractor_rejection"] = _rate(summary.get("distractor_rejection_rate"))
        if split == "v3":
            bucket = summary.get("size_buckets", {}).get("<12", {})
            metrics["v3.small_recall"] = _rate(bucket.get("true_target_recall")) if bucket.get("sample_count") != 0 else None
        if data is None:
            pending.append(f"缺少输入：{path}")
    path = views / "identity-report.json"
    sources["four_view"] = str(path)
    data = _json(path)
    # Each --views entry names the round evaluated on the report's candidate side.
    summary = (data or {}).get("candidate", {})
    for key in VIEW_METRICS:
        metric = summary.get(key, {})
        metrics[f"four_view.{key}"] = _rate(metric.get("value")) if metric.get("count") != 0 else None
    if data is None:
        pending.append(f"缺少输入：{path}")
    for key, value in metrics.items():
        if value is None:
            pending.append(f"缺少指标或计分样本：{label}/{key}")
    return {"pipeline": str(pipeline), "views": str(views), "sources": sources,
            "metrics": metrics, "pending": pending}


def _gate(key: str, value: float | None, threshold: float | None, op: str,
          reference: str | None = None) -> dict[str, Any]:
    ok = None if value is None or threshold is None else (
        value >= threshold - 1e-12 if op == ">=" else value <= threshold + 1e-12)
    return {"metric": key, "value": value, "operator": op, "threshold": threshold,
            "reference": reference, "status": "pending" if ok is None else "pass" if ok else "fail"}


def _status(checks: list[dict[str, Any]]) -> str:
    states = {item["status"] for item in checks}
    return "fail" if "fail" in states else "pending" if "pending" in states else "pass"


def new_false_accepts(dataset: Path, baseline: Path, candidate: Path) -> dict[str, Any]:
    missing = [str(path) for path in (dataset, baseline, candidate) if not path.is_file()]
    if missing:
        return {"status": "pending", "missing": missing, "count": None, "groups": []}
    source = errors._read_jsonl(dataset)
    if len({errors._id(row) for row in source}) != len(source):
        raise ValueError(f"duplicate dataset sample IDs: {dataset}")
    cache: dict[str, dict[str, Any]] = {}
    base = {row["sample_id"]: row for row in errors._merge(source, errors._rows(baseline), cache)}
    cand = errors._merge(source, errors._rows(candidate), cache)
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in cand:
        previous = base.get(row["sample_id"])
        if previous is None:
            raise ValueError(f"unaligned scored sample: {row['sample_id']}")
        if not row["is_distractor"] or previous["accepted"] or not row["accepted"]:
            continue
        # Use the evaluated task text, not recorded_task_text from a different prompt.
        row["task_text"] = str(row.get("task_text") or row.get("task") or row.get("task_query") or "unknown")
        groups[tuple(str(row[key]) for key in DIRECTIONS)].append({
            "sample_id": row["sample_id"], "world": row["world"],
            "baseline_decision": errors._decision(previous), "candidate_decision": errors._decision(row),
        })
    return {"status": "complete", "count": sum(map(len, groups.values())), "groups": [
        {"dimensions": dict(zip(DIRECTIONS, key)), "count": len(rows),
         "samples": sorted(rows, key=lambda row: row["sample_id"])}
        for key, rows in sorted(groups.items())]}


def build_report(baseline: str, candidates: dict[str, Path], baseline_dir: Path,
                 views: dict[str, Path], datasets: dict[str, Path],
                 recall_reference: str = "r21id-r4") -> dict[str, Any]:
    if baseline in candidates:
        raise ValueError("baseline must not also be a candidate")
    pipelines = {baseline: baseline_dir, **candidates}
    rounds = {label: _round(label, path, views.get(label, Path("artifacts/evaluation") / f"{label}-view-eval"))
              for label, path in pipelines.items()}
    base = rounds[baseline]["metrics"]
    reference = rounds.get(recall_reference, {}).get("metrics", {}).get("v3.small_recall")
    comparisons = {}
    for label in candidates:
        current = rounds[label]["metrics"]
        hard = []
        for split in ("v1", "v3"):
            key = f"{split}.distractor_rejection"
            hard.append(_gate(key, current[key], None if base[key] is None else base[key] - .02, ">=", baseline))
        hard.append(_gate("v3.small_recall", current["v3.small_recall"],
                          None if reference is None else reference - .03, ">=", recall_reference))
        absolute = []
        for key, op, limit, absolute_limit in zip(VIEW_METRICS, (">=", "<=", "<="),
                                                  (.833, .042, .075), (.90, .08, .026)):
            name = f"four_view.{key}"
            hard.append(_gate(name, current[name], limit, op))
            absolute.append(_gate(name, current[name], absolute_limit, op))
        changes = {}
        for split, dataset in datasets.items():
            b = Path(rounds[baseline]["views"]) / f"{baseline}-predictions.json" if split == "four_view" else baseline_dir / f"eval-{split}.json"
            c = Path(rounds[label]["views"]) / f"{label}-predictions.json" if split == "four_view" else candidates[label] / f"eval-{split}.json"
            changes[split] = new_false_accepts(dataset, b, c)
        comparisons[label] = {
            "hard_gate_status": _status(hard), "absolute_gate_status": _status(absolute),
            "hard_gates": hard, "absolute_gates": absolute,
            "delta_vs_baseline": {key: None if base[key] is None or value is None else value - base[key]
                                  for key, value in current.items()},
            "new_false_accepts": changes,
            "evidence_status": "pending" if any(value["status"] == "pending" for value in changes.values())
                               or rounds[label]["pending"] or rounds[baseline]["pending"] or reference is None else "complete",
        }
    return {"baseline": baseline, "recall_reference": recall_reference,
            "gate_source": "docs/slow-system-r21id-plan.md：r21id-r5 配方/验收",
            "scope": "固定集门槛报告；不能替代新增未见 world 留出测试，也不自动作出模型替换决定。",
            "datasets": {key: str(path) for key, path in datasets.items()},
            "rounds": rounds, "candidates": comparisons}


def _fmt(value: float | None) -> str:
    return "pending" if value is None else f"{value:.6f}"


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def markdown_report(report: dict[str, Any]) -> str:
    lines = ["# 身份轮次决策报告", "", f"基线：{report['baseline']}；小目标召回参考：{report['recall_reference']}。",
             f"门槛来源：{report['gate_source']}。", "", report["scope"], "",
             "数值为原始比率，差值为绝对百分点；门槛用原始精度判断。四视角数值沿用 identity-report.json 的精度。",
             "新增误收 = 基线不接受、候选接受的显式干扰物；复用 report_identity_errors 的对齐、类别、尺寸与视角逻辑。", ""]
    lines += ["## 输入与指标", "", "| 轮次 | 指标 | 数值 | 相对基线 (pp) |", "|---|---|---:|---:|"]
    for label, data in report["rounds"].items():
        for key, value in data["metrics"].items():
            delta = report["candidates"].get(label, {}).get("delta_vs_baseline", {}).get(key)
            lines.append(f"| {_cell(label)} | {key} | {_fmt(value)} | {'—' if label == report['baseline'] else 'pending' if delta is None else f'{delta * 100:+.3f}'} |")
    for label, data in report["rounds"].items():
        lines += ["", f"### {_cell(label)} 输入", ""]
        lines += [f"- {key}：`{path}`" for key, path in data["sources"].items()]
        lines += [f"- pending：{item}" for item in data["pending"]]
    for label, comparison in report["candidates"].items():
        lines += ["", f"## {_cell(label)} 对比", "",
                  f"r5 硬门槛：**{comparison['hard_gate_status']}**；绝对门槛：**{comparison['absolute_gate_status']}**；输入证据：**{comparison['evidence_status']}**。",
                  "", "| 门槛 | 指标 | 候选 | 条件 | 参考 | 结果 |", "|---|---|---:|---|---|---|"]
        for family in ("hard", "absolute"):
            for gate in comparison[f"{family}_gates"]:
                lines.append(f"| {family} | {gate['metric']} | {_fmt(gate['value'])} | {gate['operator']} {_fmt(gate['threshold'])} | {_cell(gate['reference'] or '固定阈值')} | {gate['status']} |")
        for split, changes in comparison["new_false_accepts"].items():
            lines += ["", f"### {split} 新增误收", ""]
            if changes["status"] == "pending":
                lines += ["pending；缺少：" + "、".join(f"`{path}`" for path in changes["missing"])]
                continue
            lines += [f"合计 {changes['count']} 行（完整清单，无 Top-N 截断）。", "",
                      "| task text | 候选类别 | 尺寸档 | view | 数量 | sample_id / world / 基线→候选决策 |",
                      "|---|---|---|---|---:|---|"]
            for group in changes["groups"]:
                detail = "; ".join(f"{r['sample_id']} / {r['world']} / {r['baseline_decision']}→{r['candidate_decision']}" for r in group["samples"])
                lines.append("| " + " | ".join(_cell(group["dimensions"][key]) for key in DIRECTIONS)
                             + f" | {group['count']} | {_cell(detail)} |")
    return "\n".join(lines) + "\n"


def _named(value: str) -> tuple[str, Path]:
    label, sep, path = value.partition("=")
    if not sep or not label or not path:
        raise argparse.ArgumentTypeError("expected LABEL=PATH")
    return label, Path(path)


def _mapping(items: list[tuple[str, Path]]) -> dict[str, Path]:
    result = dict(items)
    if len(result) != len(items):
        raise ValueError("duplicate labels")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=_named, required=True, metavar="LABEL=PIPELINE")
    parser.add_argument("--candidate", type=_named, action="append", required=True, metavar="LABEL=PIPELINE")
    parser.add_argument("--views", type=_named, action="append", default=[], metavar="LABEL=VIEW_DIR",
                        help="identity-report candidate side for LABEL; default artifacts/evaluation/LABEL-view-eval")
    parser.add_argument("--recall-reference", default="r21id-r4")
    parser.add_argument("--v1-dataset", type=Path, default=Path("artifacts/datasets/flight-identity-eval-v1/test.jsonl"))
    parser.add_argument("--v3-dataset", type=Path, default=Path("artifacts/datasets/flight-identity-eval-v2-v3prop/test.jsonl"))
    parser.add_argument("--view-dataset", type=Path, help="default BASELINE_PIPELINE/r15-additions/test.jsonl")
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args()
    try:
        candidates, views = _mapping(args.candidate), _mapping(args.views)
        if set(views) - ({args.baseline[0]} | candidates.keys()):
            raise ValueError("--views label must name the baseline or a candidate")
        if args.output_json.resolve() == args.output_md.resolve():
            raise ValueError("output JSON and Markdown must be different paths")
        for output in (args.output_json, args.output_md):
            if output.resolve().is_relative_to((errors.ROOT / "artifacts").resolve()):
                raise ValueError("artifacts is read-only; choose outputs under .tmp/")
        report = build_report(args.baseline[0], candidates, args.baseline[1], views, {
            "v1": args.v1_dataset, "v3": args.v3_dataset,
            "four_view": args.view_dataset or args.baseline[1] / "r15-additions/test.jsonl",
        }, args.recall_reference)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    for output, content in ((args.output_json, json.dumps(report, ensure_ascii=False, indent=2) + "\n"),
                            (args.output_md, markdown_report(report))):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
    print(f"JSON: {args.output_json}\nMarkdown: {args.output_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
