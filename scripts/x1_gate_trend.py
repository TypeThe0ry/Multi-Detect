#!/usr/bin/env python3
"""Print a Markdown trend table across several X1 model-gate directories.

Each directory is expected to contain ``gate.json`` as written by
``scripts/x1_model_gate.sh``.  Optional ``model-failures.jsonl`` (or
``failures.jsonl``) files provide the per-reason failure counts.  The output is
intended to be pasted into the human summary after each model gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

FAILURE_FILES = ("model-failures.jsonl", "failures.jsonl", "all-failures.jsonl")
MISSING = "—"


def _read_gate(gate_dir: Path) -> dict:
    path = gate_dir / "gate.json"
    if not path.is_file():
        raise FileNotFoundError(f"{gate_dir}: missing gate.json")
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError(f"{gate_dir}: gate.json must be an object")
    return report


def _label(gate_dir: Path, report: dict) -> str:
    label = report.get("label")
    return label if isinstance(label, str) and label else gate_dir.name


def _reason_counts(gate_dir: Path) -> Counter:
    counts = Counter()
    for name in FAILURE_FILES:
        path = gate_dir / name
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                scope = row.get("failure_scope")
                if name != "model-failures.jsonl" and scope not in (None, "model"):
                    continue
                for reason in row.get("failure_reasons") or ():
                    counts[str(reason)] += 1
        break
    return counts


def load_gates(paths):
    gates = []
    for path in paths:
        gate_dir = Path(path)
        report = _read_gate(gate_dir)
        label = _label(gate_dir, report)
        gates.append(
            {
                "dir": gate_dir,
                "label": label,
                "report": report,
                "reasons": _reason_counts(gate_dir),
            }
        )
    return gates


def _value(value):
    if value is None:
        return MISSING
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:.4f}"
    return str(value)


def _delta(current, previous):
    if not isinstance(current, (int, float)) or not isinstance(previous, (int, float)):
        return MISSING
    return f"{current - previous:+.4f}"


def _metric_names(gates):
    names = []
    for gate in gates:
        for name in (gate["report"].get("metrics") or {}):
            if name not in names:
                names.append(name)
    return names


def markdown(gates) -> str:
    labels = [gate["label"] for gate in gates]
    lines = ["# X1 模型门禁趋势", ""]
    lines += [
        "| 标签 | 判定 | GGUF SHA-256 |",
        "| --- | --- | --- |",
    ]
    for gate in gates:
        report = gate["report"]
        sha = (report.get("provenance") or {}).get("sha256")
        lines.append(
            f"| {gate['label']} | {report.get('verdict', MISSING)} | {sha or MISSING} |"
        )

    lines += ["", "## 指标（每指标每标签，Δ 相对首个与上一个标签）", ""]
    lines += [
        "| 指标 | 标签 | 数值 | 判定 | Δ 首个 | Δ 上一个 |",
        "| --- | --- | ---: | --- | ---: | ---: |",
    ]
    for name in _metric_names(gates):
        first = None
        previous = None
        for index, gate in enumerate(gates):
            metric = (gate["report"].get("metrics") or {}).get(name)
            if isinstance(metric, dict):
                value = metric.get("value")
                verdict = metric.get("verdict", MISSING)
            else:
                value, verdict = None, MISSING
            if index == 0:
                first = value
            lines.append(
                f"| {name} | {gate['label']} | {_value(value)} | {verdict} | "
                f"{_delta(value, first) if index else MISSING} | "
                f"{_delta(value, previous) if index else MISSING} |"
            )
            previous = value

    lines += ["", "## 模型失败原因计数", ""]
    reason_names = []
    for gate in gates:
        for name in gate["reasons"]:
            if name not in reason_names:
                reason_names.append(name)
    reason_names.sort()
    if reason_names:
        lines.append("| 原因 | " + " | ".join(labels) + " |")
        lines.append("| --- | " + " | ".join("---:" for _ in labels) + " |")
        for reason in reason_names:
            cells = [str(gate["reasons"].get(reason, 0)) for gate in gates]
            lines.append(f"| {reason} | " + " | ".join(cells) + " |")
        totals = [str(sum(gate["reasons"].values())) for gate in gates]
        lines.append("| **合计** | " + " | ".join(totals) + " |")
    else:
        lines.append("无模型失败记录。")

    lines += ["", "## 证据范围与失败范围", ""]
    lines += [
        "| 标签 | 证据范围拒绝 | 模型失败 | 基础设施失败 | 证据范围原因 |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for gate in gates:
        report = gate["report"]
        scopes = report.get("failure_scopes") or {}
        reasons = report.get("evidence_scope_reasons") or {}
        reason_text = ", ".join(f"{name}={count}" for name, count in sorted(reasons.items()))
        lines.append(
            f"| {gate['label']} | {report.get('evidence_scope_rejections', MISSING)} | "
            f"{scopes.get('model', MISSING)} | {scopes.get('infrastructure', MISSING)} | "
            f"{reason_text or MISSING} |"
        )

    lines += ["", "## 覆盖计数", ""]
    coverage_names = []
    for gate in gates:
        for name in (gate["report"].get("coverage") or {}):
            if name not in coverage_names:
                coverage_names.append(name)
    if coverage_names:
        lines.append("| 计数 | " + " | ".join(labels) + " |")
        lines.append("| --- | " + " | ".join("---:" for _ in labels) + " |")
        for name in coverage_names:
            cells = [
                str((gate["report"].get("coverage") or {}).get(name, MISSING)) for gate in gates
            ]
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
    else:
        lines.append("无覆盖计数。")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gates", nargs="+", type=Path, help="gate output directories")
    parser.add_argument("--output", type=Path, help="write Markdown here instead of stdout")
    args = parser.parse_args(argv)
    try:
        gates = load_gates(args.gates)
        text = markdown(gates)
    except (OSError, ValueError) as exc:
        print(f"x1 gate trend: {exc}", file=sys.stderr)
        return 2
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())