"""Sequential, guarded model-round evaluation; see docs/model-round-evaluation.md."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.model_round_guard import GPULock, check_gpu, ensure_free_disk  # noqa: E402

STAGES = (
    "offline",
    "flight",
    "planning",
    "planning-v2",
    "planning-v3",
    "find",
    "suite-v1",
    "suite-v2",
)
MMPROJ = (
    "artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/"
    "52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf"
)
TRANSPORT = re.compile(
    r"connection[ _-]?(?:refused|reset|aborted|error)|[wu]rlerror|http(?:error| error)|"
    r"timeout(?:error)?|timed out|remote.?disconnected|broken pipe|winerror (?:10061|10054)|"
    r"cuda.*(?:out of memory|oom)|server.*(?:died|exited|unavailable)|transport.?error|"
    r"failed to (?:connect|establish)|max retries exceeded",
    re.I,
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def has_transport_error(row: Any) -> bool:
    if isinstance(row, dict):
        return any(
            (
                key in {"error", "failure", "transport_error", "exception"}
                and bool(value)
                and (key == "transport_error" or bool(TRANSPORT.search(str(value))))
            )
            or (isinstance(value, (dict, list)) and has_transport_error(value))
            for key, value in row.items()
        )
    return isinstance(row, list) and any(has_transport_error(item) for item in row)


def validate_result(
    document: dict,
    *,
    min_valid_rate: float = 0.99,
    max_transport_rate: float = 0.01,
    expected: int | None = None,
    episode_root: Path | None = None,
) -> dict:
    rows = document.get("rows", document.get("episodes", document.get("missions")))
    if not isinstance(rows, list) or not rows or any(not isinstance(r, dict) for r in rows):
        raise RuntimeError("Invalid evaluation: missing/empty result rows")
    if expected is not None and len(rows) != expected:
        raise RuntimeError(f"Incomplete evaluation: expected {expected} rows, got {len(rows)}")
    errors = sum(has_transport_error(row) for row in rows)
    transport_count = len(rows)
    if episode_root is not None:
        # Per-call evidence, not just episode totals: host fallbacks can hide a dead server.
        calls = []
        for steps in episode_root.rglob("steps.jsonl"):
            if "retry-history" in steps.relative_to(episode_root).parts:
                continue
            for line in steps.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    step_calls = [
                        c
                        for c in ((row.get("labels") or {}).get("onboard") or {}).get(
                            "slow_calls", []
                        )
                        if not c.get("remembered", False)
                    ]
                    calls.extend(step_calls)
                    if has_transport_error(row) and not has_transport_error(step_calls):
                        calls.append({"transport_error": True})
        if calls:
            call_errors = sum(has_transport_error(call) for call in calls)
            # Do not dilute the per-call error share by adding healthy episodes.
            if call_errors / len(calls) > errors / transport_count:
                errors, transport_count = call_errors, len(calls)
    rate = errors / transport_count
    if rate > max_transport_rate:
        raise RuntimeError(
            f"INVALID evaluation: transport errors {errors}/{transport_count} ({rate:.2%}) "
            f"> {max_transport_rate:.2%}; accuracy suppressed"
        )
    if document.get("errors"):
        raise RuntimeError(f"Stage reported execution errors: {document['errors']}")
    if "rows" in document:
        valid_rate = sum(
            row.get("valid") is True
            or (row.get("expect_invalid") is True and row.get("exact") is True)
            for row in rows
        ) / len(rows)
        if valid_rate < min_valid_rate:
            raise RuntimeError(
                f"INVALID evaluation: JSON valid rate {valid_rate:.2%} < {min_valid_rate:.2%}"
            )
    else:
        valid_rate = sum(row.get("status") == "complete" for row in rows) / len(rows)
        if valid_rate != 1:
            raise RuntimeError(
                f"Incomplete episodes: {sum(r.get('status') != 'complete' for r in rows)} "
                "failed/missing"
            )
    metrics = document.get("metrics", document.get("summary", {}))
    if not isinstance(metrics, dict) or not metrics:
        raise RuntimeError("Invalid evaluation: missing metrics/summary")
    return {
        "rows": len(rows),
        "json_valid_rate": valid_rate,
        "transport_errors": errors,
        "transport_rate": rate,
        "metrics": metrics,
    }


@dataclass
class Stage:
    name: str
    command: list[str]
    report: Path
    expected: int
    episode_root: Path | None = None


def local_output(path: Path) -> Path:
    path = path.resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError(f"Output must stay inside this worktree: {path}")
    return path


def build_stages(args) -> list[Stage]:
    out = local_output(args.out)
    data = args.data_root.resolve()
    gguf = (
        args.gguf.resolve()
        if args.gguf
        else data
        / (
            f"artifacts/models/slow-system-lora-{args.model}-merged-gguf/"
            f"slow-system-{args.model.replace('-', '')}-merged-Q5_K_M.gguf"
        )
    )
    mmproj = args.mmproj.resolve() if args.mmproj else data / MMPROJ
    server = (
        args.llama_server.resolve()
        if args.llama_server
        else data / "artifacts/cache/llama-cpp/b11552/llama-server.exe"
    )
    common = ["--mmproj", str(mmproj), "--llama-server", str(server)]
    disk = [
        "--min-free-disk-gb",
        str(args.min_free_disk_gb),
        "--disk-check-every",
        str(args.disk_check_every),
    ]
    if args.retry_failed_episodes:
        disk += ["--retry-failed-episodes"]
    disk += ["--strict"]
    stages = []
    for name in args.stages:
        report = out / f"{name}.json"
        prefix = [sys.executable]
        episode_root = None
        if name in {"offline", "flight"}:
            dataset = (
                data
                / "artifacts/datasets"
                / ("slow-system-sft-v2-r4-prop" if name == "offline" else "flight-identity-eval-v1")
            )
            expected = sum(
                bool(line.strip())
                for line in (dataset / "test.jsonl").read_text(encoding="utf-8").splitlines()
            )
            command = (
                prefix
                + [
                    str(ROOT / "scripts/eval_slow_system.py"),
                    "--dataset",
                    str(dataset),
                    "--data-root",
                    str(data),
                    "--backend",
                    "gguf",
                    "--gguf-model",
                    str(gguf),
                    "--crop-policy",
                    "candidate_zoom_v2",
                    "--identity-only",
                    "--fast-identity",
                    "--output",
                    str(report),
                ]
                + common
            )
        elif name.startswith("planning"):
            suffix = name.removeprefix("planning")
            tasks = ROOT / f"configs/onboard/plan-eval-tasks{suffix}.json"
            expected = len(read_json(tasks)["tasks"])
            command = (
                prefix
                + [
                    str(ROOT / "scripts/plan_from_task.py"),
                    "--tasks",
                    str(tasks),
                    "--gguf",
                    str(gguf),
                    "--sorted-keys",
                    "--output",
                    str(report),
                ]
                + common
            )
        else:
            episode_root = out / name
            if name == "find":
                index = args.index.resolve()
                expected = (
                    sum(w["split"] == "test" for w in read_json(index)["worlds"]) * args.episodes
                )
                source = read_json(args.onboard_config)
                source.update(
                    gguf=str(gguf), mmproj=str(mmproj), llama_server=str(server), fast_identity=True
                )
                source.setdefault(
                    "yoloe_weights",
                    str(
                        data
                        / "artifacts/experiments/yoloe-v2-r3-seg-drop-full/train/weights/best.pt"
                    ),
                )
                config = out / "find-onboard.json"
                write_json(config, source)
                command = (
                    prefix
                    + [
                        str(ROOT / "scripts/eval_onboard_loop_campaign.py"),
                        "--index",
                        str(index),
                        "--onboard-config",
                        str(config),
                        "--episodes",
                        str(args.episodes),
                        "--out",
                        str(episode_root),
                        "--report",
                        str(report),
                    ]
                    + disk
                )
            else:
                source = read_json(args.suite_v1 if name == "suite-v1" else args.suite_v2)
                source["defaults"].update(
                    gguf=str(gguf), mmproj=str(mmproj), llama_server=str(server), fast_identity=True
                )
                source["defaults"].setdefault(
                    "yoloe_weights",
                    str(
                        data
                        / "artifacts/experiments/yoloe-v2-r3-seg-drop-full/train/weights/best.pt"
                    ),
                )
                for mission in source["missions"]:
                    # Explicit mission overrides must not accidentally test the baseline model.
                    for key in ("gguf", "mmproj", "llama_server", "fast_identity"):
                        if key in mission:
                            mission[key] = source["defaults"][key]
                config = out / f"{name}-config.json"
                write_json(config, source)
                expected = len(source["missions"]) * args.episodes
                report = episode_root / "suite-report.json"
                command = (
                    prefix
                    + [
                        str(ROOT / "scripts/run_mission_suite.py"),
                        "--suite",
                        str(config),
                        "--episodes",
                        str(args.episodes),
                        "--out",
                        str(episode_root),
                    ]
                    + disk
                )
        stages.append(Stage(name, command, report, expected, episode_root))
    return stages


def comparison(current: dict, baseline: dict | None) -> list[dict]:
    rows = []
    old = baseline or {}
    for key, value in current.items():
        if key == "size_buckets" and isinstance(value, dict):
            old_buckets = old.get(key, {}) if isinstance(old.get(key, {}), dict) else {}
            for bucket, bucket_metrics in value.items():
                if not isinstance(bucket_metrics, dict):
                    continue
                previous_bucket = old_buckets.get(bucket, {}) if isinstance(old_buckets, dict) else {}
                for metric, metric_value in bucket_metrics.items():
                    if isinstance(metric_value, (int, float)) and not isinstance(metric_value, bool):
                        previous = previous_bucket.get(metric) if isinstance(previous_bucket, dict) else None
                        if not isinstance(previous, (int, float)) or isinstance(previous, bool):
                            previous = None
                        rows.append({"metric": f"size_buckets.{bucket}.{metric}", "current": metric_value,
                                     "baseline": previous, "delta": metric_value - previous if previous is not None else None})
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            previous = old.get(key)
            if not isinstance(previous, (int, float)) or isinstance(previous, bool):
                previous = None
            rows.append(
                {
                    "metric": key,
                    "current": value,
                    "baseline": previous,
                    "delta": value - previous if previous is not None else None,
                }
            )
    return rows


def save_summary(out: Path, summary: dict) -> None:
    write_json(out / "summary.json", summary)
    lines = [
        f"# Model round: {summary['model']}",
        "",
        f"Status: **{summary['status']}**",
        "",
        f"Baseline: {summary.get('baseline_model') or 'not supplied'}",
        "",
        "| Stage | Metric | Current | Baseline | Delta |",
        "| --- | --- | --- | --- | --- |",
    ]
    for stage in summary["stages"]:
        if stage["status"] != "valid":
            lines += [
                f"| {stage['name']} | INVALID: {stage.get('error', '').replace('|', '/')} "
                "| — | — | — |"
            ]
        else:
            for row in stage["comparison"]:
                cells = [stage["name"], row["metric"]] + [
                    "—" if row[key] is None else f"{row[key]:.6g}"
                    for key in ("current", "baseline", "delta")
                ]
                lines.append("| " + " | ".join(cells) + " |")
            if stage.get("baseline_error"):
                lines += [
                    "",
                    f"Baseline {stage['name']} unavailable: {stage['baseline_error']}",
                    "",
                ]
    if summary.get("error"):
        lines += ["", "Error: " + summary["error"]]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None, *, runner=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="e.g. v2-r13-multi")
    parser.add_argument("--gguf", type=Path)
    parser.add_argument("--stages", nargs="+", choices=STAGES, default=list(STAGES))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT)
    parser.add_argument("--mmproj", type=Path)
    parser.add_argument("--llama-server", type=Path)
    parser.add_argument(
        "--baseline", type=Path, help="stored round summary.json or stage->report-path manifest"
    )
    parser.add_argument(
        "--baseline-root", type=Path, help="resolve relative baseline report paths here"
    )
    parser.add_argument("--baseline-model", default="v2-r12-multi")
    parser.add_argument(
        "--lock-file", type=Path, default=ROOT / "artifacts/locks/model-evaluation.lock"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="bypass GPU process inspection; never bypass lock/disk/validity",
    )
    parser.add_argument("--gpu-heavy-mib", type=int, default=512)
    parser.add_argument("--min-free-disk-gb", type=float, default=30)
    parser.add_argument("--disk-check-every", type=int, default=1)
    parser.add_argument("--min-valid-rate", type=float, default=0.99)
    parser.add_argument("--max-transport-rate", type=float, default=0.01)
    parser.add_argument("--retry-failed-episodes", action="store_true")
    parser.add_argument("--episodes", type=int, default=2)
    parser.add_argument(
        "--index", type=Path, default=ROOT / "configs/scenarios/generated-v2/index.json"
    )
    parser.add_argument(
        "--onboard-config",
        type=Path,
        default=ROOT / "configs/onboard/sim-loop-v2r13multi-lowsearch-fast.json",
    )
    parser.add_argument(
        "--suite-v1", type=Path, default=ROOT / "configs/onboard/mission-suite-v1-r13.json"
    )
    parser.add_argument(
        "--suite-v2", type=Path, default=ROOT / "configs/onboard/mission-suite-v2-r13.json"
    )
    args = parser.parse_args(argv)
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]+", args.model)
        or args.episodes < 1
        or args.disk_check_every < 1
        or not math.isfinite(args.min_free_disk_gb)
        or args.min_free_disk_gb < 0
        or not 0 <= args.min_valid_rate <= 1
        or not 0 <= args.max_transport_rate <= 0.01
        or args.gpu_heavy_mib < 1
        or len(set(args.stages)) != len(args.stages)
    ):
        parser.error("invalid model, duplicate stages, resource guard, or validity threshold")
    out = local_output(args.out)
    lock = local_output(args.lock_file)
    runner = runner or subprocess.run
    summary = {
        "model": args.model,
        "baseline_model": args.baseline_model if args.baseline else None,
        "status": "running",
        "stages": [],
    }
    try:
        with GPULock(lock, args.model):
            ensure_free_disk(out, args.min_free_disk_gb)
            out.mkdir(parents=True, exist_ok=True)
            check_gpu(force=args.force, heavy_mib=args.gpu_heavy_mib)
            baseline = read_json(args.baseline) if args.baseline else {}
            stored = (
                {s["name"]: s for s in baseline.get("stages", [])} if "stages" in baseline else {}
            )
            env = dict(
                os.environ,
                HF_HUB_OFFLINE="1",
                TRANSFORMERS_OFFLINE="1",
                PYTHONDONTWRITEBYTECODE="1",
            )
            temporary = out / "tmp"
            temporary.mkdir(exist_ok=True)
            env.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary))
            for stage in build_stages(args):
                record = {
                    "name": stage.name,
                    "status": "invalid",
                    "report": str(stage.report),
                    "command": stage.command,
                }
                summary["stages"].append(record)
                try:
                    ensure_free_disk(out, args.min_free_disk_gb)
                    check_gpu(force=args.force, heavy_mib=args.gpu_heavy_mib)
                    stage.report.parent.mkdir(parents=True, exist_ok=True)
                    stage.report.unlink(missing_ok=True)
                    with (out / f"{stage.name}.log").open("a", encoding="utf-8") as log:
                        result = runner(
                            stage.command,
                            cwd=ROOT,
                            env=env,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            check=False,
                        )
                    if result.returncode:
                        raise RuntimeError(
                            f"Stage exited {result.returncode}; see {stage.name}.log"
                        )
                    checked = validate_result(
                        read_json(stage.report),
                        expected=stage.expected,
                        min_valid_rate=args.min_valid_rate,
                        max_transport_rate=args.max_transport_rate,
                        episode_root=stage.episode_root,
                    )
                    previous = None
                    if args.baseline:
                        try:
                            entry = stored.get(stage.name)
                            if entry:
                                if entry.get("status") != "valid":
                                    raise RuntimeError(f"Baseline {stage.name} was not valid")
                                baseline_path = Path(entry["report"])
                            elif stage.name in baseline:
                                baseline_path = Path(baseline[stage.name])
                            else:
                                raise RuntimeError(f"Baseline missing stage {stage.name}")
                            if not baseline_path.is_absolute():
                                baseline_path = (
                                    args.baseline_root or args.baseline.resolve().parent
                                ) / baseline_path
                            baseline_doc = read_json(baseline_path)
                            baseline_evidence = None
                            if stage.episode_root is not None:
                                if stage.name.startswith("suite"):
                                    baseline_evidence = baseline_path.parent
                                else:
                                    baseline_episodes = baseline_doc.get("episodes", [])
                                    if not baseline_episodes or not baseline_episodes[0].get(
                                        "episode"
                                    ):
                                        raise RuntimeError(
                                            "Baseline episode evidence paths missing"
                                        )
                                    first_episode = Path(baseline_episodes[0]["episode"])
                                    if not first_episode.is_absolute():
                                        first_episode = (
                                            args.baseline_root or args.data_root
                                        ) / first_episode
                                    baseline_evidence = first_episode.parent.parent.parent
                                if not baseline_evidence.is_dir() or not any(
                                    baseline_evidence.rglob("steps.jsonl")
                                ):
                                    raise RuntimeError("Baseline closed-loop step evidence missing")
                            previous = validate_result(
                                baseline_doc,
                                expected=stage.expected,
                                min_valid_rate=args.min_valid_rate,
                                max_transport_rate=args.max_transport_rate,
                                episode_root=baseline_evidence,
                            )["metrics"]
                            baseline_rows = baseline_doc.get("rows", [])
                            current_rows = read_json(stage.report).get("rows", [])
                            key = "sample_id" if stage.name in {"offline", "flight"} else "text"
                            if baseline_rows and [r.get(key) for r in baseline_rows] != [
                                r.get(key) for r in current_rows
                            ]:
                                raise RuntimeError("Baseline row identities/order differ")
                            if stage.episode_root is not None:
                                current_doc = read_json(stage.report)
                                row_key = (
                                    "missions" if stage.name.startswith("suite") else "episodes"
                                )
                                fields = (
                                    ("id", "world", "episode", "expected_skills")
                                    if row_key == "missions"
                                    else ("world", "task_text", "target")
                                )

                                def identities(doc, row_key=row_key, fields=fields):
                                    return sorted(
                                        json.dumps({k: r.get(k) for k in fields}, sort_keys=True)
                                        for r in doc[row_key]
                                    )

                                if identities(baseline_doc) != identities(current_doc):
                                    raise RuntimeError("Baseline episode/mission identities differ")
                        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                            previous = None
                            record["baseline_error"] = str(exc)
                    record.update(
                        status="valid",
                        validation=checked,
                        comparison=comparison(checked["metrics"], previous),
                    )
                except Exception as exc:
                    record["error"] = str(exc)
                    raise
                finally:
                    save_summary(out, summary)
            summary["status"] = "complete"
            save_summary(out, summary)
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ) as exc:
        summary.update(status="failed", error=str(exc))
        # A contender must not overwrite a running owner's summary.
        if out.exists() and not lock.exists():
            save_summary(out, summary)
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
