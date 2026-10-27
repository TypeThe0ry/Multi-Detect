#!/usr/bin/env python3
"""Resumable planner SFT, LoRA, GGUF and evaluation pipeline."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path(r"D:\Projects\mdwt\_scratch\cx-planner-round")
VLM_PYTHON = Path(r"D:\Projects\Multi-Detect\.venv-vlm\Scripts\python.exe")
PROBE_ROOT = Path(r"D:\Projects\mdwt\_scratch\plannercmp")
R14_GGUF = ROOT / "artifacts/models/slow-system-lora-v2-r14-multi-merged-gguf/slow-system-v2r14multi-merged-Q5_K_M.gguf"
R14_ADAPTER = ROOT / "artifacts/models/slow-system-lora-v2-r14-multi/adapter"
BASE_CACHE = ROOT / "artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots"
STAGES = ("data", "train", "export", "eval", "report")
GIB = 1024**3
EVAL_ROOT = ROOT / "artifacts/evaluation/r21-planner-round"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def stage_outputs(name: str, out: Path) -> tuple[Path, ...]:
    mapping = {
        "data": (out / "dataset/manifest.json", out / "dataset/train.jsonl", out / "dataset/val.jsonl"),
        "train": (out / "adapter/adapter/adapter_config.json", out / "adapter/training-report.json"),
        "export": (out / "slow-system-Q5_K_M.gguf",),
        "eval": (EVAL_ROOT / "candidate/planning-v3.json", EVAL_ROOT / "candidate/planning-v4.json", EVAL_ROOT / "baseline/planning-v3.json", EVAL_ROOT / "baseline/planning-v4.json", EVAL_ROOT / "probe-candidate.json", EVAL_ROOT / "probe-baseline.json"),
        "report": (out / "planner-round-report.md",),
    }
    return mapping[name]


def stage_current(name: str, out: Path, state: dict) -> bool:
    entry = state.get("stages", {}).get(name, {})
    outputs = stage_outputs(name, out)
    if entry.get("status") != "complete" or not all(p.is_file() for p in outputs):
        return False
    recorded = entry.get("sha256", {})
    return all(recorded.get(str(p)) == sha256(p) for p in outputs)


def replacement_decision(candidate: dict, baseline: dict, probe_candidate: list[dict], probe_baseline: list[dict]) -> dict:
    reasons = []
    cv3 = float(candidate["planning-v3"]["exact_sequence_rate"])
    bv3 = float(baseline["planning-v3"]["exact_sequence_rate"])
    if cv3 < bv3:
        reasons.append("planning-v3 低于 r14")
    cm = candidate["planning-v4"].get("category_metrics", {})
    bm = baseline["planning-v4"].get("category_metrics", {})
    categories = ("return_home", "unknown_location", "capability")
    improved = []
    prefixes = {"return_home": ("return_",), "unknown_location": ("unknown_place",),
                "capability": ("missing_", "fixed_wing_")}
    def grouped(metrics, category):
        matches = [(name, value) for name, value in metrics.items()
                   if any(name == prefix or name.startswith(prefix) for prefix in prefixes[category])]
        if not matches:
            return {"accuracy": 0, "rejection_correct_rate": 0}
        count = sum(int(value.get("count", 1)) for _, value in matches)
        out = {"accuracy": sum(float(value.get("accuracy", 0)) * int(value.get("count", 1))
                                for _, value in matches) / max(1, count)}
        reject = [(value, int(value.get("count", 1))) for _, value in matches
                  if value.get("rejection_correct_rate") is not None]
        out["rejection_correct_rate"] = (sum(float(v["rejection_correct_rate"]) * n for v, n in reject)
                                          / sum(n for _, n in reject)) if reject else None
        return out
    for category in categories:
        c, b = grouped(cm, category), grouped(bm, category)
        deltas = [c["accuracy"] - b["accuracy"]]
        if c["rejection_correct_rate"] is not None and b["rejection_correct_rate"] is not None:
            deltas.append(c["rejection_correct_rate"] - b["rejection_correct_rate"])
        delta = max(deltas)
        if delta >= 0.10:
            improved.append(category)
    if not improved:
        reasons.append("planning-v4 返航/未知地点/能力类别无 >=10 个百分点提升")
    probe_pass = sum(bool(row.get("ok")) or bool(row.get("correct_rejection")) for row in probe_candidate)
    if len(probe_candidate) != 15 or probe_pass != 15:
        reasons.append(f"15 条返航探测未全部规划 return_home 或正确拒绝 ({probe_pass}/15)")
    return {"replace_r14": not reasons, "reasons": reasons, "planning_v3_candidate": cv3,
            "planning_v3_r14": bv3, "v4_improved_categories": improved,
            "probe_candidate_passed": probe_pass, "probe_candidate_total": len(probe_candidate),
            "probe_r14_passed": sum(bool(row.get("ok")) or bool(row.get("correct_rejection")) for row in probe_baseline)}


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def command_plan(args, out: Path) -> dict[str, list[list[str]]]:
    py = str(VLM_PYTHON)
    dataset = out / "dataset"
    adapter = out / "adapter"
    gguf = out / "slow-system-Q5_K_M.gguf"
    report = EVAL_ROOT
    data_cmd = [sys.executable, str(ROOT / "scripts/build_plan_sft.py"), "--output", str(dataset), "--version", "v4", "--rows", str(args.rows), "--val-rows", str(args.val_rows), "--r20-plan-intent", "--r20-plan-intent-count", str(args.intent_count), "--r20-plan-intent-mix"]
    if args.replay_planner_train:
        data_cmd += ["--replay-planner-train", str(args.replay_planner_train)]
    if args.negative_target_mode != "safe_rejection":
        data_cmd += ["--negative-target-mode", args.negative_target_mode]
    learning_rate = args.learning_rate
    if args.r23_recipe:
        data_cmd += ["--r23-recipe", "--r23-v3-count", str(args.r23_v3_count),
                     "--r23-deliver-count", str(args.r23_deliver_count)]
        if learning_rate == 1e-4:
            learning_rate = 2.5e-5
    train = [py, str(ROOT / "scripts/train_slow_system_lora.py"), "--dataset", str(dataset), "--model", str(BASE_CACHE), "--output", str(adapter), "--epochs", str(args.epochs), "--accumulation", "4", "--rank", "16", "--alpha", "32", "--learning-rate", str(learning_rate), "--max-side", "448", "--crop-policy", "candidate_zoom_v2"]
    if R14_ADAPTER.is_dir():
        train += ["--init-adapter", str(R14_ADAPTER)]
    export = [py, str(ROOT / "scripts/export_slow_system_r15.py"), "--base", str(BASE_CACHE), "--adapter", str(adapter / "adapter"), "--output", str(out), "--quantization", "Q5_K_M", "--drop-intermediates"]
    eval_cmds = []
    for label, model in (("candidate", gguf), ("baseline", R14_GGUF)):
        eval_cmds.append([sys.executable, str(ROOT / "scripts/run_model_round_eval.py"), "--model", f"r21-{label}", "--gguf", str(model), "--stages", "planning-v3", "planning-v4", "--out", str(report / label), "--data-root", str(ROOT), "--min-free-disk-gb", "8",
                          # Planner rows count semantic plan errors as invalid; those are
                          # model quality (scored by the replacement gate), not a broken
                          # server, which --max-transport-rate still guards.
                          "--min-valid-rate", "0.95"])
    for label, model in (("candidate", gguf), ("baseline", R14_GGUF)):
        eval_cmds.append([sys.executable, str(ROOT / "scripts/run_planner_probe.py"), "--tasks", str(PROBE_ROOT / "probe_tasks.json"), "--model", str(model), "--label", label, "--out", str(EVAL_ROOT / f"probe-{label}.json")])
    return {"data": [data_cmd], "train": [train], "export": [export], "eval": eval_cmds, "report": []}


def main(argv=None) -> int:
    global EVAL_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("all", *STAGES), default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", type=Path, default=SCRATCH / "r21")
    parser.add_argument("--rows", type=int, default=2000)
    parser.add_argument("--val-rows", type=int, default=100)
    parser.add_argument("--intent-count", type=int, default=40)
    parser.add_argument("--replay-planner-train", type=Path, default=None)
    parser.add_argument("--negative-target-mode", choices=("safe_rejection", "rejected_candidate"), default="safe_rejection")
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--r23-recipe", action="store_true",
                        help="add v3 coverage and faithful deliver rows; defaults LR to 2.5e-5")
    parser.add_argument("--r23-v3-count", type=int, default=600)
    parser.add_argument("--r23-deliver-count", type=int, default=80)
    parser.add_argument("--eval-root", type=Path, default=EVAL_ROOT)
    parser.add_argument("--min-free-disk-gb", type=float, default=12)
    parser.add_argument("--min-memory-gb", type=float, default=8)
    parser.add_argument("--no-lock", action="store_true")
    parser.add_argument("--no-skip-completed", action="store_true")
    parser.add_argument("--force-stage", choices=STAGES, help="invalidate this stage and all later stages before running")
    parser.add_argument("--lock-path", type=Path, default=Path(r"D:\Projects\mdwt\_scratch\sitl.lock"))
    args = parser.parse_args(argv)
    EVAL_ROOT = args.eval_root.absolute()  # keep the worktree spelling; artifacts/ may be nested junctions
    if args.rows < 1 or args.val_rows < 0 or args.intent_count < 1 or args.epochs < 1:
        parser.error("row counts must be positive (val rows may be zero)")
    out = args.out.resolve()
    allowed_roots = (ROOT.resolve(), SCRATCH.resolve())
    if not any(out == allowed or out.is_relative_to(allowed) for allowed in allowed_roots):
        parser.error("--out must stay inside this worktree or the configured scratch directory")
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "round-state.json"
    state = load_json(state_path) if state_path.is_file() else {"schema_version": 1, "stages": {}}
    if args.force_stage:
        start = STAGES.index(args.force_stage)
        for stage in STAGES[start:]:
            state.get("stages", {}).pop(stage, None)
        atomic_json(state_path, state)
    commands = command_plan(args, out)
    selected = STAGES if args.stage == "all" else (args.stage,)
    if args.dry_run:
        print(f"dry-run: python={sys.executable}; train/export python={VLM_PYTHON}; output={out}")
        print(f"guards: disk>={args.min_free_disk_gb:g} GB, memory>={args.min_memory_gb:g} GB; lock={args.lock_path}")
    for stage in selected:
        if not args.no_skip_completed and stage_current(stage, out, state):
            print(f"[skip] {stage}: completed outputs match SHA-256")
            continue
        if args.dry_run:
            for cmd in commands[stage]:
                print(f"[{stage}] " + subprocess.list2cmdline(cmd))
            continue
        if stage in {"train", "export", "eval"}:
            free = shutil.disk_usage(out).free / 1e9
            try:
                import psutil
                available = psutil.virtual_memory().available / 1e9
            except ImportError:
                available = float("inf")
            if free < args.min_free_disk_gb:
                raise RuntimeError(f"disk guard: {free:.1f} GB available, need {args.min_free_disk_gb:g} GB")
            if available < args.min_memory_gb:
                raise RuntimeError(f"memory guard: {available:.1f} GB available, need {args.min_memory_gb:g} GB")
        lock_context = None
        if stage in {"train", "eval"} and not args.no_lock:
            from scripts.run_nightly_regression import SitlLock
            lock_context = SitlLock(args.lock_path, label="planner-round", retry_s=30)
            lock_context.acquire()
        try:
            started = datetime.now(timezone.utc).isoformat()
            if stage == "report":
                metrics = {name: {f"planning-{version}": load_json(EVAL_ROOT / name / f"planning-{version}.json")["summary"] for version in ("v3", "v4")} for name in ("candidate", "baseline")}
                probes = {name: load_json(EVAL_ROOT / f"probe-{name}.json") for name in ("candidate", "baseline")}
                decision = replacement_decision(metrics["candidate"], metrics["baseline"], probes["candidate"], probes["baseline"])
                lines = ["# r21 规划模型对比报告", "", f"- planning-v3：候选 {decision['planning_v3_candidate']:.3f} / r14 {decision['planning_v3_r14']:.3f}", f"- planning-v4：候选 {metrics['candidate']['planning-v4']['exact_sequence_rate']:.3f} / r14 {metrics['baseline']['planning-v4']['exact_sequence_rate']:.3f}", "", "## planning-v4 类别指标", "", "| 模型 | 类别 | 准确率 | 拒绝正确率 | 意图违规率 | 能力违规率 |", "|---|---|---:|---:|---:|---:|"]
                for model_name in ("candidate", "baseline"):
                    for category, values in sorted(metrics[model_name]["planning-v4"].get("category_metrics", {}).items()):
                        fmt = lambda value: "N/A" if value is None else f"{value:.3f}"
                        lines.append(f"| {model_name} | {category} | {fmt(values.get('accuracy'))} | {fmt(values.get('rejection_correct_rate'))} | {fmt(values.get('intent_violation_rate'))} | {fmt(values.get('capability_violation_rate'))} |")
                lines += ["", f"- 返航探测：候选 {decision['probe_candidate_passed']}/15 / r14 {decision['probe_r14_passed']}/15", f"- r21 替换 r14：{'建议' if decision['replace_r14'] else '不建议'}"]
                lines += [f"- 判据：{reason}" for reason in decision["reasons"]] or ["- 判据：planning-v3 不低于 r14，planning-v4 至少一个重点类别提升 >=10 个百分点，15 条探测全通过"]
                (out / "planner-round-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
                atomic_json(out / "replacement-decision.json", decision)
            else:
                for cmd in commands[stage]:
                    env = os.environ.copy()
                    env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT)))
                    env["PYTHONUTF8"] = "1"
                    log_dir = SCRATCH / "logs"
                    log_dir.mkdir(parents=True, exist_ok=True)
                    log_path = log_dir / f"{stage}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
                    with log_path.open("a", encoding="utf-8") as log:
                        log.write("$ " + subprocess.list2cmdline(cmd) + "\n")
                        subprocess.run(cmd, cwd=ROOT, env=env, check=True, stdout=log, stderr=subprocess.STDOUT)
                if stage == "data":
                    manifest = load_json(out / "dataset/manifest.json")
                    atomic_json(out / "dataset/provenance.json", {"created_utc": started, "builder": str(ROOT / "scripts/build_plan_sft.py"), "builder_sha256": sha256(ROOT / "scripts/build_plan_sft.py"), "command": commands[stage][0], "manifest_sha256": sha256(out / "dataset/manifest.json"), "manifest": manifest})
            outputs = stage_outputs(stage, out)
            missing = [str(p) for p in outputs if not p.is_file()]
            if missing:
                raise RuntimeError(f"stage {stage} missing outputs: {missing}")
            state.setdefault("stages", {})[stage] = {"status": "complete", "completed_utc": datetime.now(timezone.utc).isoformat(), "sha256": {str(p): sha256(p) for p in outputs}}
            atomic_json(state_path, state)
        finally:
            if lock_context:
                lock_context.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
