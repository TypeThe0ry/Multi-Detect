#!/usr/bin/env python3
"""Aggregate X1 fixed-wing gimbal evaluation results by site."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

DEFAULT_ROOT = Path("/mnt/nas/AdminHome/volatic/x1-gimbal-eval/collection")
TRACK = ("track_diagnostics",)
HOLD = (*TRACK, "hold")
FW = (*HOLD, "fw")
HORIZON = ("horizon_acceptance",)
METRICS = {
    "turn_p95_deg": (*FW, "turn", "pointing_error_deg", "p95"),
    "cruise_p95_deg": (*FW, "cruise", "pointing_error_deg", "p95"),
    "turn_lock_rate": (*FW, "turn", "lock_rate"),
    "cruise_lock_rate": (*FW, "cruise", "lock_rate"),
    "reachable_coverage": (*FW, "reachable_coverage"),
    "unwind_fraction": (*FW, "unwind_fraction"),
    "horizon_p95_deg": (*HORIZON, "feasible_error", "p95_deg"),
    "horizon_max_deg": (*HORIZON, "feasible_error", "max_deg"),
    "horizon_degraded_fraction": (*HORIZON, "degraded_fraction_of_feasible"),
    "hover_p95_deg": (*HOLD, "overall", "pointing_error_deg", "p95"),
}
COLUMNS = (
    "site", "valid_flights", "accepted", "all_gates_pass", *METRICS,
    "invalid_or_voided_rounds", "invalid_rounds", "voided_rounds", "voided_reasons",
)


def get(obj, *keys):
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def stats(values):
    numbers = [v for v in values if type(v) in (int, float) and math.isfinite(v)]
    return {
        "min": min(numbers) if numbers else None,
        "median": median(numbers) if numbers else None,
        "max": max(numbers) if numbers else None,
    }


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def report_tag(row, fallback):
    tag = next((value for value in (
        row.get("eval_tag"), get(row, "inputs", "eval_tag"),
        row.get("tag"), get(row, "inputs", "tag"),
    ) if isinstance(value, str) and value), None)
    if tag:
        return tag
    match = re.search(r"(?:^|[-_])([a-f0-9]{20})$", fallback)
    return match.group(1) if match else fallback


def all_gates_pass(row):
    # Batch acceptance and diagnostic gates are recorded independently.
    return (
        row.get("errors") == [] and not row.get("round_errors")
        and get(row, *FW, "passed") is True
        and get(row, *HORIZON, "passed") is True
        and all(get(row, *path, "passed") is not False
                for path in (TRACK, (*HOLD, "lock_gate"), (*HOLD, "preaim_readiness")))
    )


def metric_value(row, path):
    value = get(row, *path)
    if value is None and path == METRICS["hover_p95_deg"]:
        return get(row, *path[1:])
    return value


def summarize(root=DEFAULT_ROOT, *, since=None, sites=None, tag_prefix=None):
    """Count fixed-wing attempts, using valid, non-voided rows for metrics."""
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"collection directory not found: {root}")
    after = timestamp(since)
    if since is not None and after is None:
        raise ValueError(f"invalid --since ISO time: {since}")

    allowed = set(sites or ())
    flights = defaultdict(list)
    invalid = Counter()
    voided = defaultdict(Counter)
    files = sorted(root.glob("rw_*/*/results.jsonl"))
    malformed = 0
    for path in files:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if not isinstance(row, dict):
                    malformed += 1
                    continue
                if get(row, "inputs", "gimbal_eval_fw") is not True and not get(row, *FW):
                    continue
                site = get(row, "inputs", "site") or path.parent.parent.name.split("__", 1)[0]
                if allowed and site not in allowed:
                    continue
                if tag_prefix:
                    run_tag = report_tag(row, path.parent.name)
                    if not (isinstance(run_tag, str) and run_tag.startswith(tag_prefix)):
                        continue
                if after is not None:
                    when = timestamp(row.get("started_at")) or timestamp(row.get("finished_at"))
                    if when is None or when < after:
                        continue
                if reason := row.get("voided_reason"):
                    voided[site][str(reason)] += 1
                elif row.get("valid_round") is not True:
                    invalid[site] += 1
                else:
                    flights[site].append(row)

    site_rows = []
    for site in sorted(flights.keys() | invalid.keys() | voided.keys()):
        rows = flights[site]
        void_count = sum(voided[site].values())
        site_rows.append({
            "site": site,
            "valid_flights": len(rows),
            "accepted": sum(row.get("accepted") is True for row in rows),
            "all_gates_pass": sum(all_gates_pass(row) for row in rows),
            **{name: stats(metric_value(row, path) for row in rows) for name, path in METRICS.items()},
            "invalid_or_voided_rounds": invalid[site] + void_count,
            "invalid_rounds": invalid[site],
            "voided_rounds": void_count,
            "voided_reasons": dict(sorted(voided[site].items())),
        })
    return {
        "source": str(root), "since": since, "site_filter": sorted(allowed),
        "tag_prefix": tag_prefix, "input_files": len(files),
        "malformed_rows": malformed, "sites": site_rows,
    }


def markdown(report):
    def cell(value):
        if isinstance(value, dict) and {"min", "median", "max"} == value.keys():
            return " / ".join("n/a" if value[key] is None else f"{value[key]:.4g}"
                              for key in ("min", "median", "max"))
        text = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value)
        return text.replace("|", r"\|").replace("\r", " ").replace("\n", " ")

    def table_row(cells):
        return "| " + " | ".join(cells) + " |\n"

    return (
        "Metrics in each cell: min / median / max across valid flights.\n\n"
        + table_row(COLUMNS) + table_row(["---"] * len(COLUMNS))
        + "".join(table_row(cell(row[column]) for column in COLUMNS) for row in report["sites"])
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="collection directory")
    parser.add_argument("--since", help="inclusive ISO start time (UTC if no offset is supplied)")
    parser.add_argument("--sites", help="comma-separated receipt site IDs")
    parser.add_argument("--tag-prefix", help="match a receipt tag or, if absent, the batch directory name")
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    sites = [site.strip() for site in (args.sites or "").split(",") if site.strip()]
    if args.markdown_out and args.json_out and args.markdown_out.resolve() == args.json_out.resolve():
        parser.error("Markdown and JSON output paths must differ")
    try:
        report = summarize(args.root, since=args.since, sites=sites, tag_prefix=args.tag_prefix)
        for path, render in (
            (args.markdown_out, markdown),
            (args.json_out, lambda result: json.dumps(result, indent=2, allow_nan=False) + "\n"),
        ):
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(render(report), encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.markdown_out is None and args.json_out is None:
        print(markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
