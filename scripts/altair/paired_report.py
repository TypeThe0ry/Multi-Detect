"""Create a paired candidate-vs-incumbent identity evaluation report.

Both normal inputs are ``eval-v3.json``/``identity-eval.json`` objects with a
top-level ``rows`` list.  A small compatibility loader also accepts a bare
row list, ``samples``/``results`` lists, or a mapping containing one of those
keys; this is useful for older incumbent exports.  Rows are paired by
``sample_id`` and only the intersection is scored.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from typing import Any, Callable

DECISIONS = ("confirm_target", "reject_track", "approach_track")
CONFUSION_COLUMNS = (*DECISIONS, "other")
METRICS = (
    "confirm_recall",
    "decision_accuracy",
    "distractor_rejection_rate",
    "identity_accuracy",
    "json_validity",
)
DEFAULT_DATASET = Path("artifacts/datasets/flight-identity-eval-v2-v3prop/test.jsonl")


def load_rows(path: Path) -> list[dict[str, Any]]:
    """Load per-sample rows from the incumbent or candidate JSON export."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("rows")
        if rows is None:
            for key in ("samples", "results", "evaluations"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
    else:
        rows = None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"{path} has no supported per-sample rows list")
    if any(row.get("sample_id") is None for row in rows):
        raise ValueError(f"{path} contains a row without sample_id")
    return rows


def _by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row["sample_id"])
        if key in result:
            raise ValueError(f"duplicate sample_id: {key}")
        result[key] = row
    return result


def align_rows(incumbent: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> tuple[list[tuple[str, dict, dict]], dict]:
    inc, cand = _by_id(incumbent), _by_id(candidate)
    ids = sorted(set(inc) & set(cand))
    alignment = {
        "incumbent_rows": len(inc),
        "candidate_rows": len(cand),
        "intersection_rows": len(ids),
        "only_incumbent": sorted(set(inc) - set(cand)),
        "only_candidate": sorted(set(cand) - set(inc)),
    }
    return [(sample_id, inc[sample_id], cand[sample_id]) for sample_id in ids], alignment


def _identity(value: Any) -> str | None:
    value = str(value)
    if value in ("confirm_target", "approach_track"):
        return "match"
    if value == "reject_track":
        return "no_match"
    return None


def _decision(row: dict[str, Any]) -> str | None:
    value = row.get("decision")
    if value is None and isinstance(row.get("parsed_output"), dict):
        value = row["parsed_output"].get("decision")
    return value if isinstance(value, str) else None


def _valid(row: dict[str, Any]) -> bool:
    return bool(row.get("valid"))


def _metric_value(name: str, row: dict[str, Any]) -> bool | None:
    expected = row.get("expected")
    decision = _decision(row)
    if name == "confirm_recall":
        return decision == "confirm_target" if expected == "confirm_target" else None
    if name == "decision_accuracy":
        return decision == expected
    if name == "distractor_rejection_rate":
        return decision == "reject_track" if expected == "reject_track" else None
    if name == "identity_accuracy":
        expected_identity = _identity(expected)
        return (_identity(decision) == expected_identity) if expected_identity else None
    if name == "json_validity":
        return _valid(row)
    raise KeyError(name)


def recompute_metric(name: str, rows: list[dict[str, Any]]) -> float | None:
    values = [_metric_value(name, row) for row in rows]
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def confusion(rows: list[dict[str, Any]]) -> dict[str, Any]:
    matrix = [[0 for _ in CONFUSION_COLUMNS] for _ in DECISIONS]
    for row in rows:
        expected = row.get("expected")
        if expected not in DECISIONS:
            continue
        column = _decision(row) if _valid(row) else None
        column_index = CONFUSION_COLUMNS.index(column) if column in CONFUSION_COLUMNS else len(CONFUSION_COLUMNS) - 1
        matrix[DECISIONS.index(expected)][column_index] += 1
    return {"rows": len(rows), "row_labels": list(DECISIONS), "column_labels": list(CONFUSION_COLUMNS), "matrix": matrix}


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact binomial McNemar p-value for discordant counts b/c."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(b, c) + 1)) / (2**n)
    return min(1.0, 2.0 * tail)


def _bootstrap_ci(
    inc_values: list[bool], cand_values: list[bool], *, bootstrap: int, seed: int
) -> list[float] | None:
    if not inc_values:
        return None
    rng = random.Random(seed)
    n = len(inc_values)
    diffs: list[float] = []
    for _ in range(bootstrap):
        inc_sum = cand_sum = 0
        for _ in range(n):
            index = rng.randrange(n)
            inc_sum += inc_values[index]
            cand_sum += cand_values[index]
        diffs.append(cand_sum / n - inc_sum / n)
    diffs.sort()
    return [diffs[int(0.025 * (bootstrap - 1))], diffs[int(0.975 * (bootstrap - 1))]]


def _metric_report(name: str, pairs: list[tuple[str, dict, dict]], bootstrap: int, seed: int) -> dict[str, Any]:
    usable = [(sample_id, _metric_value(name, inc), _metric_value(name, cand)) for sample_id, inc, cand in pairs]
    usable = [(sample_id, inc, cand) for sample_id, inc, cand in usable if inc is not None and cand is not None]
    inc_values = [bool(inc) for _, inc, _ in usable]
    cand_values = [bool(cand) for _, _, cand in usable]
    b = sum(inc and not cand for inc, cand in zip(inc_values, cand_values))
    c = sum((not inc) and cand for inc, cand in zip(inc_values, cand_values))
    incumbent = sum(inc_values) / len(inc_values) if inc_values else None
    candidate = sum(cand_values) / len(cand_values) if cand_values else None
    return {
        "incumbent": incumbent,
        "candidate": candidate,
        "difference": None if incumbent is None else candidate - incumbent,
        "n": len(usable),
        "mcnemar": {"b_incumbent_only_success": b, "c_candidate_only_success": c, "p_value": mcnemar_exact(b, c)},
        "bootstrap_95_ci": _bootstrap_ci(inc_values, cand_values, bootstrap=bootstrap, seed=seed),
    }


def _dataset_rows(path: Path) -> dict[str, dict[str, Any]]:
    result = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                result[str(row["sample_id"])] = row
    return result


def _area(record: dict[str, Any]) -> float | None:
    focus = record.get("focus_track_id")
    if focus is None:
        target = record.get("assistant_target") or {}
        if isinstance(target, str):
            try:
                target = json.loads(target)
            except json.JSONDecodeError:
                target = {}
        focus = (target.get("args") or {}).get("track_id") if isinstance(target, dict) else None
    for candidate in record.get("candidates") or []:
        if candidate.get("track_id") == focus:
            box = candidate.get("box_xyxy_normalized")
            if isinstance(box, list) and len(box) == 4:
                value = (float(box[2]) - float(box[0])) * (float(box[3]) - float(box[1]))
                return value if value > 0 else None
    return None


def area_bins(
    pairs: list[tuple[str, dict, dict]], dataset: Path, *, bins: int = 8
) -> dict[str, Any]:
    records = _dataset_rows(dataset)
    confirms = []
    for sample_id, incumbent, candidate in pairs:
        if incumbent.get("expected") != "confirm_target":
            continue
        area = _area(records.get(sample_id, {}))
        if area is not None:
            confirms.append((area, sample_id, incumbent, candidate))
    confirms.sort(key=lambda item: (item[0], item[1]))
    result = []
    for index in range(bins):
        start = index * len(confirms) // bins
        end = (index + 1) * len(confirms) // bins
        group = confirms[start:end]
        inc_recall = sum(_decision(item[2]) == "confirm_target" for item in group) / len(group) if group else None
        cand_recall = sum(_decision(item[3]) == "confirm_target" for item in group) / len(group) if group else None
        result.append({
            "bin": index,
            "n": len(group),
            "area_min": group[0][0] if group else None,
            "area_max": group[-1][0] if group else None,
            "incumbent_recall": inc_recall,
            "candidate_recall": cand_recall,
            "sample_ids": [item[1] for item in group],
        })
    return {"bins": bins, "confirm_rows_with_area": len(confirms), "method": "sorted equal-count rank bins; ties are split by sample_id", "dataset": str(dataset), "values": result}


def build_report(
    incumbent_rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]], *, bootstrap: int = 10000, seed: int = 7, dataset: Path = DEFAULT_DATASET
) -> dict[str, Any]:
    pairs, alignment = align_rows(incumbent_rows, candidate_rows)
    report = {
        "alignment": alignment,
        "confusion": {"incumbent": confusion([inc for _, inc, _ in pairs]), "candidate": confusion([cand for _, _, cand in pairs])},
        "metrics": {name: _metric_report(name, pairs, bootstrap, seed + index) for index, name in enumerate(METRICS)},
    }
    try:
        report["confirm_recall_by_area_bin"] = area_bins(pairs, dataset)
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        report["confirm_recall_by_area_bin"] = {"dataset": str(dataset), "error": f"{type(exc).__name__}: {exc}"}
    return report


def write_report(incumbent: Path, candidate: Path, output: Path, *, bootstrap: int = 10000, seed: int = 7, dataset: Path = DEFAULT_DATASET) -> dict[str, Any]:
    report = build_report(load_rows(incumbent), load_rows(candidate), bootstrap=bootstrap, seed=seed, dataset=dataset)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incumbent", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--bootstrap", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    report = write_report(args.incumbent, args.candidate, args.output, bootstrap=args.bootstrap, seed=args.seed, dataset=args.dataset)
    print(json.dumps({"output": str(args.output), "intersection_rows": report["alignment"]["intersection_rows"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
