#!/usr/bin/env python3
"""Run the reproducible YOLOE v3 old/new proposer evaluation on CPU.

The runner deliberately delegates model work to the existing evaluators.  It
only wires their inputs together, records hashes, checks the r3 cache key set,
and writes a small Chinese summary for a post-training handoff.
"""
# The command construction is intentionally kept visually close to the
# documented commands; long command lines are accepted here.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OLD_DEFAULT = Path(
    "D:/Projects/Multi-Detect/artifacts/experiments/"
    "yoloe-v2-r3-seg-drop-full/train/weights/best.pt"
)
TEXT_DEFAULT = Path("D:/Projects/Multi-Detect/weights/mobileclip_blt.ts")
INDEX_DEFAULT = [
    ROOT / "configs/scenarios/generated-v2/index.json",
    ROOT / "configs/scenarios/generated-confusers/index.json",
    ROOT / "configs/scenarios/generated-confusers-2/index.json",
]
EVAL_ROOTS = [
    Path("D:/Projects/Multi-Detect/artifacts/datasets/onboard-loop/eval-v2-test-r12multi"),
    Path("D:/Projects/Multi-Detect/artifacts/datasets/onboard-loop/eval-v2-test-r8multi"),
]
CACHE_CAMPAIGNS = [
    Path("D:/Projects/Multi-Detect/artifacts/datasets/onboard-loop/dagger-cfs-r8-low"),
    Path("D:/Projects/Multi-Detect/artifacts/datasets/onboard-loop/dagger-train-r8-low"),
    Path("D:/Projects/Multi-Detect/artifacts/datasets/sim-teacher/campaign-20261012-v2-r3-seg"),
]
R3_CACHE_DEFAULT = Path(
    "D:/Projects/Multi-Detect/artifacts/datasets/onboard-proposals/"
    "v2r3seg-dagger-r8-cfs.jsonl"
)
HOLDOUT_CLASSES = ("stop_sign", "postbox", "pickup")


def sha256(path: Path) -> str:
    return hashlib.file_digest(path.open("rb"), "sha256").hexdigest()


def cache_keys(path: Path) -> set[str]:
    return {
        str(json.loads(line)["image"]).replace("\\", "/")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def make_smoke_sample(path: Path, count: int) -> None:
    """Build a deterministic bounded sample from r3 cache keys only.

    The full analyzer discovers and joins every cache row; that is needlessly
    expensive for smoke. Reuse its exact episode loader and sampler on a small
    prefix so the benchmark still consumes the normal enriched sample schema.
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.analyze_small_object_proposals import load_episode, sample_frames, write_jsonl

    source_root = Path("D:/Projects/Multi-Detect")
    grouped: dict[Path, list[tuple[str, list]]] = {}
    for line in R3_CACHE_DEFAULT.open(encoding="utf-8"):
        if not line.strip():
            continue
        row = json.loads(line)
        key = str(row["image"]).replace("\\", "/")
        episode = (source_root / key).parent.parent
        grouped.setdefault(episode, []).append((key, row["boxes"]))
        if sum(len(rows) for rows in grouped.values()) >= max(count * 8, 24):
            break
    frames = []
    for episode, entries in sorted(grouped.items()):
        loaded, _ = load_episode((str(episode), entries, str(source_root)))
        frames.extend(loaded)
        if len(frames) >= count:
            break
    sample = sample_frames(frames, count, 20261015)
    if not sample:
        raise RuntimeError("smoke sample builder found no joined r3 frames")
    write_jsonl(path, sample)


def run_step(name: str, command: list[str], out: Path, env: dict[str, str]) -> None:
    log = out / f"{name}.log"
    with log.open("w", encoding="utf-8") as handle:
        handle.write("$ " + subprocess.list2cmdline(command) + "\n")
        handle.flush()
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"步骤 {name} 失败（退出码 {result.returncode}），详见 {log}")


def _row(tables: dict[str, list[dict[str, Any]]], dimension: str, group: str) -> dict[str, Any]:
    return next((r for r in tables.get(dimension, []) if r.get("group") == group), {"objects": 0})


def _delta(old: float, new: float) -> dict[str, float]:
    return {"old": old, "new": new, "delta": new - old}


def summarize_recall(report: dict[str, Any]) -> dict[str, Any]:
    """Extract comparable overall/size/category recall rows from step (a)."""
    result: dict[str, Any] = {"by_imgsz": {}, "holdout_classes": list(HOLDOUT_CLASSES)}
    for imgsz, pair in report.get("metrics", {}).items():
        old_tables = pair["old"]["one_to_one_recall"]["tables"]
        new_tables = pair["new"]["one_to_one_recall"]["tables"]
        dimensions: dict[str, Any] = {}
        for dimension, groups in (("overall", ["all"]), ("size", ["<8", "8-16", "16-32", "32-64", ">=64"]), ("category", sorted({r.get("group") for r in old_tables.get("category", [])} | {r.get("group") for r in new_tables.get("category", [])}))):
            dimensions[dimension] = {}
            for group in groups:
                old = _row(old_tables, dimension, group)
                new = _row(new_tables, dimension, group)
                dimensions[dimension][group] = {
                    "objects": {"old": old.get("objects", 0), "new": new.get("objects", 0)},
                    "iou_0.3": _delta(float(old.get("recall_iou_0.3", 0.0)), float(new.get("recall_iou_0.3", 0.0))),
                    "iou_0.5": _delta(float(old.get("recall_iou_0.5", 0.0)), float(new.get("recall_iou_0.5", 0.0))),
                }
        dimensions["unseen_classes"] = {c: dimensions["category"].get(c) for c in HOLDOUT_CLASSES}
        result["by_imgsz"][str(imgsz)] = dimensions
    return result


def summarize_timing(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for mode in ("single", "tiles", "hybrid"):
        o = old.get("timing_seconds", {}).get(mode, {})
        n = new.get("timing_seconds", {}).get(mode, {})
        result[mode] = {key: _delta(float(o.get(key, 0.0)), float(n.get(key, 0.0))) for key in ("mean", "median", "p95")}
    return result


def write_summary(out: Path, step_a: dict[str, Any], small_old: dict[str, Any], small_new: dict[str, Any], cache_check: dict[str, Any] | None, hashes: dict[str, str]) -> None:
    summary = {
        "说明": "这是训练后新旧候选框模型的同口径 CPU 对比；smoke 结果只验证流水线，不代表模型效果。",
        "全体与尺寸桶召回": summarize_recall(step_a),
        "小目标样本召回": {
            "old_hybrid_onboard": small_old.get("hybrid_onboard", {}).get("tables", {}).get("overall", []),
            "new_hybrid_onboard": small_new.get("hybrid_onboard", {}).get("tables", {}).get("overall", []),
        },
        "每帧CPU耗时秒": summarize_timing(small_old, small_new),
        "r3缓存key检查": cache_check or {"status": "skipped"},
        "输入sha256": hashes,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# YOLOE v3 新旧候选框对比", "", summary["说明"], "", "## 召回变化", ""]
    for imgsz, dimensions in summary["全体与尺寸桶召回"]["by_imgsz"].items():
        overall = dimensions["overall"]["all"]["iou_0.3"]
        lines.append(f"- imgsz {imgsz} 全体 IoU≥0.3：旧 {overall['old']:.4f}，新 {overall['new']:.4f}，变化 {overall['delta']:+.4f}")
        for bucket, row in dimensions["size"].items():
            d = row["iou_0.3"]
            lines.append(f"- 尺寸 {bucket}：旧 {d['old']:.4f}，新 {d['new']:.4f}，变化 {d['delta']:+.4f}")
        unseen = dimensions["unseen_classes"]
        lines.append("- 未见类：" + "；".join(f"{c} {v['iou_0.3']['delta']:+.4f}" if v else f"{c} 无样本" for c, v in unseen.items()))
    lines += ["", "## 小目标样本 CPU 耗时（每帧）", ""]
    for mode, values in summary["每帧CPU耗时秒"].items():
        m = values["mean"]
        lines.append(f"- {mode} mean：旧 {m['old']:.4f}s，新 {m['new']:.4f}s，变化 {m['delta']:+.4f}s")
    lines += ["", "## 复现信息", "", f"- r3 key 检查：{summary['r3缓存key检查']}", "- 详细命令输出见各步骤 .log 文件。"]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--new-weights", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--old-weights", type=Path, default=OLD_DEFAULT)
    parser.add_argument("--text-weights", type=Path, default=TEXT_DEFAULT)
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--skip-r3-cache", action="store_true")
    parser.add_argument("--include-confusers", action="store_true")
    parser.add_argument(
        "--device", default="cpu",
        help="device for the recall and r3-cache steps (e.g. 0 once the GPU is free); "
        "the per-frame timing step always runs on CPU like the onboard comparison",
    )
    args = parser.parse_args(argv)
    if args.device.isdigit():
        # torch.nn.Module.to() rejects a bare index; YoloeProposer moves the model with it.
        args.device = f"cuda:{args.device}"
    if args.threads < 1 or args.threads > 2:
        parser.error("--threads 必须在 1..2（本机内存约束）")
    max_frames = args.max_frames or (4 if args.smoke else None)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    for path in (args.new_weights, args.old_weights, args.text_weights, *INDEX_DEFAULT):
        if not path.is_file():
            raise FileNotFoundError(path)
    cpu_env = os.environ.copy()
    cpu_env.update({"CUDA_VISIBLE_DEVICES": "-1", "PYTHONPATH": str(ROOT / "src"), "YOLO_OFFLINE": "true", "YOLO_AUTOINSTALL": "false"})
    if args.device == "cpu":
        env = cpu_env
    else:
        env = dict(cpu_env)
        env.pop("CUDA_VISIBLE_DEVICES")
    py = sys.executable

    step_a_path = out / "recall" / "report.json"
    step_a_path.parent.mkdir(exist_ok=True)
    command = [py, "scripts/evaluate_yoloe_proposer_round.py"]
    for root in EVAL_ROOTS:
        command += ["--campaign-root", str(root)]
    for index in INDEX_DEFAULT:
        command += ["--index", str(index)]
    command += ["--old-weights", str(args.old_weights), "--new-weights", str(args.new_weights), "--imgsz", "1280", "--imgsz", "960", "--device", str(args.device), "--out", str(step_a_path)]
    if max_frames is not None:
        command += ["--max-frames", str(max_frames)]
    if args.include_confusers:
        command.append("--include-confusers")
    run_step("01_recall", command, out, env)

    audit = out / "small_object" / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    sample = audit / "sample.jsonl"
    if args.smoke:
        make_smoke_sample(sample, max_frames or 4)
        (out / "02_sample.log").write_text(
            "Smoke: re-used analyzer load_episode/sample_frames with seed 20261015; "
            "bounded to a prefix of the r3 cache.\n",
            encoding="utf-8",
        )
    else:
        run_step("02_sample", [py, "scripts/analyze_small_object_proposals.py", "--source-root", "D:/Projects/Multi-Detect", "--output", str(audit), "--sample-size", str(max_frames or 200), "--seed", "20261015", "--workers", str(min(args.threads, 2))], out, env)
    small_reports = {}
    for label, weights in (("old", args.old_weights), ("new", args.new_weights)):
        small_out = out / "small_object" / label
        run_step(f"03_small_{label}", [py, "scripts/evaluate_small_object_tiling.py", "--sample", str(sample), "--weights", str(weights), "--text-weights", str(args.text_weights), "--output", str(small_out), "--threads", str(args.threads)], out, cpu_env)
        small_reports[label] = json.loads((small_out / "metrics.json").read_text(encoding="utf-8"))

    cache_check = None
    if not args.skip_r3_cache:
        cache_out = out / "r3" / "new-cache.jsonl"
        command = [py, "scripts/cache_onboard_proposals.py"]
        for campaign in CACHE_CAMPAIGNS:
            command += ["--campaign", str(campaign)]
        command += ["--weights", str(args.new_weights), "--imgsz", "1280", "--confidence", "0.05", "--output", str(cache_out), "--device", str(args.device), "--source-root", "D:/Projects/Multi-Detect"]
        if max_frames is not None:
            command += ["--max-frames", str(max_frames)]
        run_step("04_r3_cache", command, out, env)
        reference = R3_CACHE_DEFAULT
        compare_reference = reference
        if max_frames is not None:
            keys = cache_keys(cache_out)
            compare_reference = out / "r3" / "reference-subset.jsonl"
            with compare_reference.open("w", encoding="utf-8") as handle:
                for line in reference.read_text(encoding="utf-8").splitlines():
                    if line.strip() and str(json.loads(line)["image"]).replace("\\", "/") in keys:
                        handle.write(line + "\n")
        from scripts.build_yoloe_proposer_round import assert_cache_keys_match
        cache_check = assert_cache_keys_match(compare_reference, cache_out)
        cache_check["status"] = "passed"
        (out / "r3" / "key-check.json").write_text(json.dumps(cache_check, indent=2) + "\n", encoding="utf-8")
    hashes = {str(path.resolve()): sha256(path) for path in (args.new_weights, args.old_weights, args.text_weights, *INDEX_DEFAULT, sample)}
    if not args.skip_r3_cache:
        hashes[str((out / "r3" / "new-cache.jsonl").resolve())] = sha256(out / "r3" / "new-cache.jsonl")
    write_summary(out, json.loads(step_a_path.read_text(encoding="utf-8")), small_reports["old"], small_reports["new"], cache_check, hashes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
