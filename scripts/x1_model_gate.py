#!/usr/bin/env python3
"""Preflight and grade a local Altair GGUF with the X1 closed-loop suite."""
from __future__ import annotations

import argparse
import hashlib
import math
import operator
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from scripts.run_x1_closed_loop_suite import CASES, ROOT, interrupted, read_json, terminate_owned, write_json

from multidetect.autonomy.serialization import strict_json_loads
from scripts.realworld.x1_lock import FLIGHT_LOCK_FD_ENV, LOCK, LockBusy, flight_lock

PROFILE = ROOT / "configs/simulation/x1-model-gate-v1.json"
OUTPUT_ROOT = Path("/data/scratch/cx/altair/x1-gate")
SHA256 = re.compile(r"[a-fA-F0-9]{64}")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
# Keys are (numerator, sampled population). A count threshold uses a sampled
# population as well, so missing instrumentation cannot pass by reporting zero.
METRICS = {
    "schema_validity": ("schema_valid", ("turns_sent",), "min"),
    "identity_echo": ("identity_valid", ("turns_sent",), "min"),
    "unnecessary_actions": ("unnecessary_actions", ("quiet_ticks",), "max"),
    "deadline_miss_rate": ("deadline_missed", ("deadline_missed", "deadline_on_time"), "lt"),
    "turn_latency_over_5000_rate": ("slow_turns", ("timed_turns",), "lt"),
    "event_tool_first": ("event_tool_first_compliant", ("event_tool_first_required",), "min"),
    "retask_structure": ("retasks_structure_valid", ("retasks_proposed",), "min"),
    "retask_timing": ("retasks_timing_valid", ("retasks_timing_checked",), "min"),
    "battery_structure": ("battery_structure_valid", ("battery_structure_checked",), "min"),
}
PER_CASE_METRICS = ("schema_validity", "identity_echo", "deadline_miss_rate", "turn_latency_over_5000_rate")
COMPARATORS = {"min": operator.ge, "max": operator.le, "lt": operator.lt}


def load_profile(path):
    profile = strict_json_loads(path.read_text(encoding="utf-8"))
    if (not isinstance(profile, dict) or set(profile) != {"version", "thresholds"}
            or type(profile["version"]) is not int or profile["version"] != 1):
        raise ValueError("profile must have version 1 and thresholds")
    thresholds = profile["thresholds"]
    if not isinstance(thresholds, dict) or set(thresholds) != set(METRICS):
        raise ValueError("profile must define exactly the v1 metrics")
    for name, (_, _, comparison) in METRICS.items():
        threshold = thresholds[name]
        if not isinstance(threshold, dict) or set(threshold) != {comparison}:
            raise ValueError(f"{name} requires a {comparison} threshold")
        value = threshold[comparison]
        if comparison == "max":
            valid = type(value) is int and value >= 0
        else:
            valid = type(value) in (int, float) and 0 <= value <= 1
        if not valid:
            raise ValueError(f"invalid threshold for {name}")
    return profile


def evaluate(name, counts, thresholds):
    numerator, denominator, comparison = METRICS[name]
    total = sum(counts.get(key, 0) for key in denominator)
    count = counts.get(numerator, 0)
    threshold = thresholds[name][comparison]
    value = (count if comparison == "max" else count / total) if total else None
    return {"verdict": "PASS" if value is not None and COMPARATORS[comparison](value, threshold) else "FAIL",
            "value": value, "count": count, "samples": total, "threshold": thresholds[name]}


def latency_counts(metrics):
    histogram = metrics.get("turn_latency_ms_histogram", {})
    return {"slow_turns": histogram.get("over_5000", 0), "timed_turns": sum(histogram.values())}


def evaluate_suite(summary, profile):
    thresholds = profile["thresholds"]
    by_case = {row["case"]["id"]: row for row in summary.get("cases", ())}
    aggregate = dict(summary.get("metrics", {}))
    aggregate.update(latency_counts(aggregate))
    cases = []
    for case in CASES:
        row = by_case.get(case.id, {})
        counts = {**row.get("metrics", {}), **row.get("observed", {})}
        for key in ("quiet_ticks", "unnecessary_actions"):
            aggregate[key] = aggregate.get(key, 0) + counts.get(key, 0)
        counts.update(latency_counts(counts))
        checks = {}
        for name in (*PER_CASE_METRICS, *case.gate_checks):
            check = evaluate(name, counts, thresholds)
            # Pure link loss can legitimately have no tool obligation. An
            # observed, labeled first turn is still required for this exception.
            if (name == "event_tool_first" and not check["samples"] and case.fault == "link_loss"
                    and counts.get("event_tool_first_oracle_evaluated", 0)
                    and counts.get("event_tool_first_eligible", 0) == counts["event_tool_first_oracle_evaluated"]):
                check.update(verdict="PASS", applicability="oracle_did_not_require_tool")
            checks[name] = check
        cases.append({"case_id": case.id, "flight_status": row.get("status", "missing"),
                      "flight_checks": row.get("checks", {}), "model_checks": checks,
                      "evidence_scope_rejections": counts.get("evidence_scope_rejections", 0),
                      "verdict": "PASS" if row.get("status") == "pass"
                      and all(check["verdict"] == "PASS" for check in checks.values()) else "FAIL"})
    metrics = {name: evaluate(name, aggregate, thresholds) for name in METRICS}
    # Unknown timing on interrupted/stale windows remains visible. Require one
    # known deadline and one observed latency globally through the denominators.
    coverage = {key: aggregate.get(key, 0) for key in (
        "turns_sent", "timed_turns", "deadline_unknown", "requests_unsent", "disrupted_requests",
        "retasks_required", "retasks_structure_package_checked", "event_tool_first_eligible",
        "event_tool_first_oracle_evaluated", "battery_structure_eligible")}
    coverage["evidence_scope_blocked_cases"] = sum(case["flight_status"] == "blocked_evidence_scope"
                                                  for case in cases)
    return metrics, cases, aggregate, coverage


def compare(baseline, metrics, cases, evidence_count, baseline_dir):
    if (not isinstance(baseline, dict) or baseline.get("report_version") != 1
            or not isinstance(baseline.get("metrics"), dict) or not all(
                isinstance(row, dict) for row in baseline["metrics"].values())):
        raise ValueError("baseline must contain a v1 gate.json with metrics")
    old_cases = {row["case_id"]: row["verdict"] for row in baseline.get("cases", [])}
    deltas = {}
    for name, current in metrics.items():
        previous = baseline["metrics"].get(name, {})
        old, new = previous.get("value"), current["value"]
        deltas[name] = {
            "baseline_verdict": previous.get("verdict"), "current_verdict": current["verdict"],
            "baseline_value": old, "current_value": new,
            "baseline_samples": previous.get("samples"), "current_samples": current["samples"],
            "delta": new - old if new is not None and type(old) in (int, float) and math.isfinite(old) else None,
            "same_threshold": previous.get("threshold") == current["threshold"],
        }
    return {"path": str(baseline_dir), "baseline_verdict": baseline.get("verdict"),
            "baseline_sha256": baseline.get("provenance", {}).get("sha256"), "metrics": deltas,
            "cases": {row["case_id"]: {"baseline": old_cases.get(row["case_id"]), "current": row["verdict"]}
                      for row in cases},
            "baseline_evidence_scope_rejections": baseline.get("evidence_scope_rejections"),
            "current_evidence_scope_rejections": evidence_count}


def expected_sha(checkpoint, explicit):
    """Use the export manifest when present, and cross-check an explicit pin."""
    manifest = checkpoint.with_name("SHA256SUMS")
    matches = set()
    if manifest.is_file():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and parts[1].removeprefix("*") in {checkpoint.name, f"./{checkpoint.name}"}:
                if not SHA256.fullmatch(parts[0]):
                    raise ValueError(f"invalid SHA256SUMS entry for {checkpoint.name}")
                matches.add(parts[0].lower())
    if len(matches) > 1 or (matches and explicit and explicit not in matches):
        raise ValueError("conflicting expected GGUF SHA-256 values")
    digest = explicit or next(iter(matches), None)
    source = "--sha256" if explicit else str(manifest) if matches else None
    return digest, source


def fingerprint(checkpoint):
    with checkpoint.open("rb") as handle:
        stat = os.fstat(handle.fileno())
        if handle.read(4) != b"GGUF":
            raise ValueError("checkpoint is missing the GGUF magic")
        handle.seek(0)
        sha = hashlib.file_digest(handle, "sha256").hexdigest()
        after = os.fstat(handle.fileno())
    def stamp(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    if stamp(stat) != stamp(after) or stamp(stat) != stamp(checkpoint.stat()):
        raise ValueError("GGUF changed while its SHA-256 was being read")
    return sha, stamp(stat)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gguf", type=lambda value: Path(value).resolve())
    parser.add_argument("--cuda-device", type=int, help="physical CUDA index for Altair (default: CPU)")
    parser.add_argument("--label", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    parser.add_argument("--sha256", type=str.lower, help="expected checkpoint SHA-256")
    parser.add_argument("--baseline", type=lambda value: Path(value).resolve(), help="previous gate output directory")
    parser.add_argument("--profile", type=lambda value: Path(value).resolve(), default=PROFILE)
    parser.add_argument("--output-root", type=lambda value: Path(value).resolve(), default=OUTPUT_ROOT)
    parser.add_argument("--llama-server", type=lambda value: Path(value).resolve())
    parser.add_argument("--port", type=int, default=9957)
    args = parser.parse_args(argv)
    if not LABEL.fullmatch(args.label):
        parser.error("--label must be one safe path component (1–80 characters)")
    if (args.cuda_device is not None and args.cuda_device < 0) or not 1 <= args.port <= 65535:
        parser.error("invalid CUDA index or port")
    if args.sha256 is not None and not SHA256.fullmatch(args.sha256):
        parser.error("--sha256 requires 64 hexadecimal digits")
    if not args.gguf.is_file():
        parser.error("GGUF must be a local file")
    if args.baseline is not None and not (args.baseline / "gate.json").is_file():
        parser.error("--baseline directory must contain gate.json")
    return args


def run_stage(args, run_dir, name, lock_fd):
    command = [str(ROOT / "scripts/run_x1_closed_loop_suite.sh"), "--checkpoint", str(args.gguf),
               "--output", str(run_dir / name), "--port", str(args.port)]
    if args.cuda_device is not None:
        command += ["--cuda-device", str(args.cuda_device)]
    if args.llama_server is not None:
        command += ["--llama-server", str(args.llama_server)]
    if name == "preflight":
        command += ["--plan-only"]
    env = {**os.environ, FLIGHT_LOCK_FD_ENV: str(lock_fd), "TMPDIR": str(run_dir)}
    with (run_dir / f"{name}.log").open("wb") as log:
        proc = subprocess.Popen(command, cwd=ROOT, env=env, pass_fds=(lock_fd,),
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            write_json(run_dir / f"{name}-process.json", {"argv": command, "pid": proc.pid})
            rc = proc.wait()
        except BaseException:
            terminate_owned(proc, grace_s=30)
            raise
    return {"exit_status": rc, "command": command, "pid": proc.pid}


def split_failures(run_dir):
    log = run_dir / "live/failures.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    scopes = Counter()
    with (
        (run_dir / "failures.jsonl").open("x", encoding="utf-8") as failures,
        (run_dir / "model-failures.jsonl").open("x", encoding="utf-8") as model,
        (run_dir / "evidence-scope-rejections.jsonl").open("x", encoding="utf-8") as evidence,
    ):
        for line in lines:
            row = strict_json_loads(line)
            scope = row.get("failure_scope", "unclassified")
            scopes[scope] += 1
            failures.write(line + "\n")
            if scope == "model":
                model.write(line + "\n")
            if scope == "evidence_scope":
                evidence.write(line + "\n")
    return dict(scopes)


def markdown(report):
    lines = [f"# X1 模型门禁 · {report['verdict']}", "",
             f"- 标签 `{report['label']}`", f"- GGUF SHA-256 `{report['provenance'].get('sha256', '未知')}`",
             f"- 证据范围拒绝 {report['evidence_scope_rejections']} 次",
             f"- SFT 失败轮分类 `{report['failure_scopes']}`"]
    if report["error"]:
        lines.append(f"- 执行错误 `{report['error']}`")
    lines += ["", "| 指标 | 判定 | 分子 / 样本 | 门槛 |", "| --- | --- | ---: | --- |"]
    for name, metric in report["metrics"].items():
        op, target = next(iter(metric["threshold"].items()))
        count = f"{metric['count']} / {metric['samples']}" if metric["samples"] else "无样本"
        lines.append(f"| {name} | {metric['verdict']} | {count} | {op} {target} |")
    lines += ["", "| 用例 | 门禁 | 飞行状态 | 未满足的检查 | 范围拒绝 |", "| --- | --- | --- | --- | ---: |"]
    for case in report["cases"]:
        failed = [key for key, ok in case["flight_checks"].items() if not ok]
        failed += [key for key, result in case["model_checks"].items() if result["verdict"] != "PASS"]
        lines.append(f"| {case['case_id']} | {case['verdict']} | {case['flight_status']} | "
                     f"{', '.join(failed)} | {case['evidence_scope_rejections']} |")
    lines += ["", f"- 证据范围拒绝原因 `{report['evidence_scope_reasons']}`",
              f"- 模型轮延时直方图（ms）`{report['turn_latency_ms_histogram']}`",
              f"- 决策回执延时直方图（ms）`{report['receipt_latency_ms_histogram']}`",
              f"- 计时覆盖 `{report['coverage']}`"]
    if baseline := report["baseline"]:
        lines += ["", f"## 基线比较 · {baseline['baseline_verdict']} → {report['verdict']}", "",
                  f"目录 `{baseline['path']}`", "", "| 指标 | 基线 → 当前 | 变化 | 门槛一致 |",
                  "| --- | --- | ---: | --- |"]
        for name, row in baseline["metrics"].items():
            delta = f"{row['delta']:+.4g}" if row["delta"] is not None else "—"
            lines.append(f"| {name} | {row['baseline_verdict']} → {row['current_verdict']} | "
                         f"{delta} | {row['same_threshold']} |")
    return "\n".join(lines) + "\n"


def run_gate(args, *, run=run_stage, lock_path=LOCK):
    profile = load_profile(args.profile)
    baseline = read_json(args.baseline / "gate.json") if args.baseline else None
    if args.baseline is not None:
        # Fail on malformed comparison inputs before starting any work.
        compare(baseline, {}, [], 0, args.baseline)
    scratch = ROOT / ".tmp"
    scratch.mkdir(exist_ok=True)
    destination = args.output_root / args.label
    destination.mkdir(parents=True, exist_ok=False)
    run_dir = Path(tempfile.mkdtemp(prefix=f"x1-model-gate-{args.label}-", dir=scratch))
    write_json(run_dir / "profile.json", profile)
    stages = {}
    provenance = {"checkpoint": str(args.gguf), "cuda_device": args.cuda_device,
                  "profile_source": str(args.profile), "rtf_matrix_start_load_max": os.environ.get(
                      "RTF_MATRIX_START_LOAD_MAX", "60")}
    error = None
    exit_status = 1
    try:
        pin, source = expected_sha(args.gguf, args.sha256)
        sha, stamp = fingerprint(args.gguf)
        provenance.update(sha256=sha, bytes=stamp[2], expected_sha256=pin, sha256_source=source,
                          sha256_verified=False)
        if pin and sha != pin:
            raise ValueError("GGUF SHA-256 mismatch")
        provenance["sha256_verified"] = pin is not None
        provenance["sha256_verification"] = "manifest_or_pin" if pin else "no_expected_sha256"
        with flight_lock(lock_path, wait=0) as lease:
            stages["preflight"] = run(args, run_dir, "preflight", lease.fileno())
            if stages["preflight"]["exit_status"] == 130:
                raise KeyboardInterrupt
            if stages["preflight"]["exit_status"]:
                error = "plan-only preflight failed"
            else:
                confirmed_sha, confirmed_stamp = fingerprint(args.gguf)
                if (confirmed_sha, confirmed_stamp) != (sha, stamp):
                    raise ValueError("GGUF changed after plan-only preflight")
                stages["live"] = run(args, run_dir, "live", lease.fileno())
                if stages["live"]["exit_status"] == 130:
                    raise KeyboardInterrupt
            if fingerprint(args.gguf) != (sha, stamp):
                raise ValueError("GGUF changed during the gate")
            provenance["sha256_verified"] = True
            provenance["sha256_verification"] = "manifest_or_pin" if pin else "local_repeat_read"
    except LockBusy as exc:
        error, exit_status = str(exc), 75
    except KeyboardInterrupt:
        error, exit_status = "interrupted", 130
    except (OSError, RuntimeError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"

    live = run_dir / "live/summary.json"
    summary = read_json(live) if live.is_file() else {}
    metrics, cases, counts, coverage = evaluate_suite(summary, profile)
    scopes = split_failures(run_dir)
    evidence_count = counts.get("evidence_scope_rejections", 0)
    if error is None:
        error = summary.get("error")
        if not summary:
            error = "live suite produced no summary"
    passed = (error is None and stages.get("live", {}).get("exit_status") == 0
              and all(row["verdict"] == "PASS" for row in (*metrics.values(), *cases)))
    report = {
        "report_version": 1, "verdict": "PASS" if passed else "FAIL", "label": args.label,
        "error": error, "stages": stages, "provenance": provenance, "cases": cases, "metrics": metrics,
        "evidence_scope_rejections": evidence_count, "failure_scopes": scopes, "coverage": coverage,
        "evidence_scope_reasons": counts.get("evidence_scope_reason_counts", {}),
        "turn_latency_ms_histogram": counts.get("turn_latency_ms_histogram", {}),
        "receipt_latency_ms_histogram": counts.get("receipt_latency_ms_histogram", {}),
        "baseline": compare(baseline, metrics, cases, evidence_count, args.baseline) if args.baseline else None,
    }
    (run_dir / "gate.md").write_text(markdown(report), encoding="utf-8")
    write_json(run_dir / "gate.json", report)
    # The fresh destination is reserved before execution. Publish the JSON
    # report atomically last, so a baseline reader sees only completed copies.
    for artifact in run_dir.iterdir():
        if artifact.name == "gate.json":
            continue
        target = destination / artifact.name
        if artifact.is_dir():
            shutil.copytree(artifact, target, symlinks=True)
        else:
            shutil.copy2(artifact, target)
    write_json(destination / "gate.json", read_json(run_dir / "gate.json"))
    failed = ",".join(name for name, metric in metrics.items() if metric["verdict"] == "FAIL") or "none"
    print(f"X1 gate {report['verdict']} {args.label} sha256={provenance.get('sha256', 'unknown')} "
          f"metrics_failed={failed} cases_failed={sum(c['verdict'] == 'FAIL' for c in cases)} "
          f"model_failure_rows={scopes.get('model', 0)} evidence_scope_rejections={evidence_count} "
          f"report={destination / 'gate.json'}")
    if error:
        print(error, file=sys.stderr)
    return 0 if passed else exit_status


def main(argv=None):
    args = parse_args(argv)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return run_gate(args)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        print(f"X1 gate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
