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


def host_path(env: str, windows: str, posix: Path) -> Path:
    """Environment override, else the original Windows path or a repo-local POSIX default."""
    value = os.environ.get(env)
    if value:
        return Path(value)
    return Path(windows) if os.name == "nt" else posix


SCRATCH = host_path("MULTIDETECT_PLANNER_SCRATCH", r"D:\Projects\mdwt\_scratch\cx-planner-round", ROOT / "artifacts/scratch/cx-planner-round")
VLM_PYTHON = host_path("MULTIDETECT_VLM_PYTHON", r"D:\Projects\Multi-Detect\.venv-vlm\Scripts\python.exe", ROOT / ".venv-vlm/bin/python")
PROBE_ROOT = host_path("MULTIDETECT_PLANNER_PROBE_ROOT", r"D:\Projects\mdwt\_scratch\plannercmp", ROOT / "artifacts/scratch/plannercmp")
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


def stage_outputs(name: str, out: Path, *, include_planning_v5: bool = False) -> tuple[Path, ...]:
    mapping = {
        "data": (out / "dataset/manifest.json", out / "dataset/train.jsonl", out / "dataset/val.jsonl"),
        "train": (out / "adapter/adapter/adapter_config.json", out / "adapter/training-report.json"),
        "export": (out / "slow-system-Q5_K_M.gguf",),
        "eval": tuple(
            path
            for path in (
                EVAL_ROOT / "candidate/planning-v3.json",
                EVAL_ROOT / "candidate/planning-v4.json",
                EVAL_ROOT / "baseline/planning-v3.json",
                EVAL_ROOT / "baseline/planning-v4.json",
                *(
                    (
                        EVAL_ROOT / "candidate/planning-v5.json",
                        EVAL_ROOT / "baseline/planning-v5.json",
                    ) if include_planning_v5 else ()
                ),
                EVAL_ROOT / "probe-candidate.json",
                EVAL_ROOT / "probe-baseline.json",
            )
        ),
        "report": (out / "planner-round-report.md",),
    }
    return mapping[name]


def stage_current(name: str, out: Path, state: dict, *, include_planning_v5: bool = False,
                  plan_pregate: bool = False) -> bool:
    entry = state.get("stages", {}).get(name, {})
    if name in {"eval", "report"} and entry.get("plan_pregate", False) != plan_pregate:
        return False
    outputs = stage_outputs(name, out, include_planning_v5=include_planning_v5)
    if entry.get("status") != "complete" or not all(p.is_file() for p in outputs):
        return False
    recorded = entry.get("sha256", {})
    return all(recorded.get(str(p)) == sha256(p) for p in outputs)


def replacement_decision(candidate: dict, baseline: dict, probe_candidate: list[dict], probe_baseline: list[dict],
                         baseline_label: str = "r14") -> dict:
    reasons = []
    cv3 = float(candidate["planning-v3"]["exact_sequence_rate"])
    bv3 = float(baseline["planning-v3"]["exact_sequence_rate"])
    if cv3 < bv3:
        reasons.append(f"planning-v3 低于 {baseline_label}")
    cvalid = candidate["planning-v3"].get("valid_rate")
    bvalid = baseline["planning-v3"].get("valid_rate")
    if cvalid is not None and bvalid is not None and float(cvalid) < float(bvalid):
        reasons.append(f"planning-v3 有效率低于 {baseline_label}")
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
    decision_contract = args.decision_contract
    if decision_contract is None:
        decision_contract = bool(args.r26_recipe or args.r27_recipe or args.r28_recipe)
    data_cmd = [sys.executable, str(ROOT / "scripts/build_plan_sft.py"), "--output", str(dataset), "--version", "v4", "--rows", str(args.rows), "--val-rows", str(args.val_rows), "--r20-plan-intent", "--r20-plan-intent-count", str(args.intent_count), "--r20-plan-intent-mix"]
    if args.replay_planner_train:
        data_cmd += ["--replay-planner-train", str(args.replay_planner_train)]
    if args.negative_target_mode != "safe_rejection":
        data_cmd += ["--negative-target-mode", args.negative_target_mode]
    learning_rate = args.learning_rate
    if args.r23_recipe or args.r24_recipe or args.r25_recipe or args.r26_recipe or args.r27_recipe or args.r28_recipe:
        data_cmd += ["--r23-recipe", "--r23-v3-count", str(args.r23_v3_count),
                     "--r23-deliver-count", str(args.r23_deliver_count)]
        if learning_rate == 1e-4:
            learning_rate = 2.5e-5
    if args.r24_recipe or args.r25_recipe or args.r26_recipe or args.r27_recipe or args.r28_recipe:
        data_cmd += ["--r24-recipe", "--r24-reference-count", str(args.r24_reference_count),
                     "--r24-unknown-count", str(args.r24_unknown_count),
                     "--r24-context-count", str(args.r24_context_count)]
        if args.learning_rate == 1e-4:
            learning_rate = 2.0e-5
    if args.r25_recipe:
        data_cmd += ["--r25-recipe",
                     "--r25-missing-payload-count", str(args.r25_missing_payload_count),
                     "--r25-negated-query-count", str(args.r25_negated_query_count)]
        if learning_rate == 1e-4:
            learning_rate = 2.0e-5
    if args.r26_recipe:
        data_cmd += ["--r26-recipe", "--r26-refusal-count", str(args.r26_refusal_count)]
        if learning_rate == 1e-4:
            learning_rate = 2.0e-5
    if args.r27_recipe or args.r28_recipe:
        data_cmd += ["--r27-recipe", "--r27-refusal-count", str(args.r27_refusal_count),
                     "--r27-complex-count", str(args.r27_complex_count)]
        if learning_rate == 1e-4:
            learning_rate = 2.0e-5
    if args.r28_recipe:
        data_cmd += ["--r28-recipe", "--r28-stop-count", str(args.r28_stop_count),
                     "--r28-fixed-wing-count", str(args.r28_fixed_wing_count),
                     "--r28-return-count", str(args.r28_return_count)]
    train = [py, str(ROOT / "scripts/train_slow_system_lora.py"), "--dataset", str(dataset), "--model", str(BASE_CACHE), "--output", str(adapter), "--epochs", str(args.epochs), "--accumulation", "4", "--rank", "16", "--alpha", "32", "--learning-rate", str(learning_rate), "--max-side", "448", "--crop-policy", "candidate_zoom_v2"]
    if args.init_adapter:
        train += ["--init-adapter", str(args.init_adapter)]
    export = [py, str(ROOT / "scripts/export_slow_system_r15.py"), "--base", str(BASE_CACHE), "--adapter", str(adapter / "adapter"), "--output", str(out), "--quantization", "Q5_K_M", "--drop-intermediates"]
    eval_cmds = []
    planning_stages = ["planning-v3", "planning-v4"]
    if args.include_planning_v5:
        planning_stages.append("planning-v5")
    for label, model in (("candidate", gguf), ("baseline", args.baseline_gguf)):
        eval_cmds.append([sys.executable, str(ROOT / "scripts/run_model_round_eval.py"), "--model", f"r21-{label}", "--gguf", str(model), "--stages", *planning_stages, "--out", str(report / label), "--data-root", str(ROOT), "--min-free-disk-gb", "8",
                          # Planner rows count semantic plan errors as invalid; those are
                          # model quality, scored by the replacement gate (r14 itself is
                          # 93.75% valid on v3), not a broken server, which
                          # --max-transport-rate still guards.
                          "--min-valid-rate", "0"]
                         + (["--decision-contract"] if decision_contract else [])
                         + (["--plan-pregate"] if getattr(args, "plan_pregate", False) else []))
    for label, model in (("candidate", gguf), ("baseline", args.baseline_gguf)):
        eval_cmds.append([sys.executable, str(ROOT / "scripts/run_planner_probe.py"), "--tasks", str(PROBE_ROOT / "probe_tasks.json"), "--model", str(model), "--label", label, "--out", str(EVAL_ROOT / f"probe-{label}.json")]
                         + (["--decision-contract"] if decision_contract else [])
                         + (["--plan-pregate"] if getattr(args, "plan_pregate", False) else []))
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
    parser.add_argument("--r24-recipe", action="store_true",
                        help="extend r23 with grounded-reference and local/home data")
    parser.add_argument("--r24-reference-count", type=int, default=80)
    parser.add_argument("--r24-unknown-count", type=int, default=80)
    parser.add_argument("--r24-context-count", type=int, default=80)
    parser.add_argument("--r25-recipe", action="store_true",
                        help="extend r24 with missing-payload and negated-query contrasts")
    parser.add_argument("--r25-missing-payload-count", type=int, default=40)
    parser.add_argument("--r25-negated-query-count", type=int, default=40)
    parser.add_argument("--r26-recipe", action="store_true",
                        help="extend r24 with structured reject/clarify targets")
    parser.add_argument("--r26-refusal-count", type=int, default=60,
                        help="structured refusal rows per reason_code (r26 default: 60)")
    parser.add_argument("--r27-recipe", action="store_true",
                        help="extend r24 with narrow structured refusals; enables the decision contract")
    parser.add_argument("--r27-refusal-count", type=int, default=40,
                        help="refusal rows per supported reason code (default: 40)")
    parser.add_argument("--r27-complex-count", type=int, default=400,
                        help="difficult executable positives (default: 400)")
    parser.add_argument("--include-planning-v5", action="store_true",
                        help="include planning-v5 in eval (default keeps legacy v3/v4 comparison)")
    parser.add_argument("--plan-pregate", action="store_true",
                        help="enable host preflight for candidate and baseline planning evaluations")
    parser.add_argument("--r28-recipe", action="store_true",
                        help="extend r27 with threshold-stop and fixed-wing capability data")
    parser.add_argument("--r28-stop-count", type=int, default=80)
    parser.add_argument("--r28-fixed-wing-count", type=int, default=30)
    parser.add_argument("--r28-return-count", type=int, default=30)
    contract = parser.add_mutually_exclusive_group()
    contract.add_argument("--decision-contract", dest="decision_contract", action="store_true",
                          help="enable structured reject/clarify evaluation")
    contract.add_argument("--no-decision-contract", dest="decision_contract", action="store_false",
                          help="disable structured reject/clarify evaluation")
    parser.set_defaults(decision_contract=None)
    parser.add_argument("--init-adapter", type=Path, default=R14_ADAPTER,
                        help="adapter used to initialize training (default: r14)")
    parser.add_argument("--baseline-gguf", type=Path, default=R14_GGUF,
                        help="baseline GGUF used in evaluation (default: r14)")
    parser.add_argument("--baseline-label", default="r14",
                        help="display label for the baseline in decisions and reports")
    parser.add_argument("--eval-root", type=Path, default=EVAL_ROOT)
    parser.add_argument("--min-free-disk-gb", type=float, default=12)
    parser.add_argument("--min-memory-gb", type=float, default=8)
    parser.add_argument("--no-lock", action="store_true")
    parser.add_argument("--no-skip-completed", action="store_true")
    parser.add_argument("--force-stage", choices=STAGES, help="invalidate this stage and all later stages before running")
    parser.add_argument("--lock-path", type=Path, default=host_path("MULTIDETECT_SITL_LOCK", r"D:\Projects\mdwt\_scratch\sitl.lock", ROOT / "artifacts/scratch/sitl.lock"))
    args = parser.parse_args(argv)
    EVAL_ROOT = args.eval_root.absolute()  # keep the worktree spelling; artifacts/ may be nested junctions
    if args.rows < 1 or args.val_rows < 0 or args.intent_count < 1 or args.epochs < 1:
        parser.error("row counts must be positive (val rows may be zero)")
    if any(value < 0 for value in (args.r24_reference_count, args.r24_unknown_count,
                                   args.r24_context_count, args.r25_missing_payload_count,
                                   args.r25_negated_query_count, args.r26_refusal_count,
                                   args.r27_refusal_count, args.r27_complex_count,
                                   args.r28_stop_count, args.r28_fixed_wing_count,
                                   args.r28_return_count)):
        parser.error("recipe counts must be non-negative")
    if args.r28_fixed_wing_count % 2:
        parser.error("r28 fixed-wing contrast count must be even")
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
        if not args.no_skip_completed and stage_current(
            stage, out, state, include_planning_v5=args.include_planning_v5,
            plan_pregate=args.plan_pregate,
        ):
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
                versions = ("v3", "v4", "v5") if args.include_planning_v5 else ("v3", "v4")
                metrics = {name: {f"planning-{version}": load_json(EVAL_ROOT / name / f"planning-{version}.json")["summary"] for version in versions} for name in ("candidate", "baseline")}
                probe_reports = {name: load_json(EVAL_ROOT / f"probe-{name}.json") for name in ("candidate", "baseline")}
                probes = {name: value.get("rows", value) if isinstance(value, dict) else value
                          for name, value in probe_reports.items()}
                decision = replacement_decision(metrics["candidate"], metrics["baseline"], probes["candidate"], probes["baseline"], args.baseline_label)
                decision["plan_pregate"] = args.plan_pregate
                baseline_label = args.baseline_label
                title = "# r21 规划模型对比报告" if baseline_label == "r14" else "# 规划模型对比报告"
                lines = [
                    title, "",
                    f"- 主机预检：{'开启（候选和基线同条件）' if args.plan_pregate else '关闭'}",
                    f"- planning-v3：候选 {decision['planning_v3_candidate']:.3f} / {baseline_label} {decision['planning_v3_r14']:.3f}",
                    f"- planning-v4：候选 {metrics['candidate']['planning-v4']['exact_sequence_rate']:.3f} / {baseline_label} {metrics['baseline']['planning-v4']['exact_sequence_rate']:.3f}",
                ]
                for model_name in ("candidate", "baseline"):
                    for version in versions:
                        preflight = metrics[model_name][f"planning-{version}"].get("host_pregate", 0)
                        processed = metrics[model_name][f"planning-{version}"].get("model_processed", 0)
                        lines.append(f"- {model_name} planning-{version}：预检命中 {preflight} / 模型处理 {processed}")
                    probe_summary = probe_reports[model_name].get("summary", {}) if isinstance(probe_reports[model_name], dict) else {}
                    if probe_summary:
                        lines.append(f"- {model_name} 返航探测：预检命中 {probe_summary.get('host_pregate', 0)} / 模型处理 {probe_summary.get('model_processed', 0)}")
                lines += ["", "## planning-v4 类别指标", "", "| 模型 | 类别 | 准确率 | 拒绝正确率 | 意图违规率 | 能力违规率 |", "|---|---|---:|---:|---:|---:|"]
                for model_name in ("candidate", "baseline"):
                    for category, values in sorted(metrics[model_name]["planning-v4"].get("category_metrics", {}).items()):
                        fmt = lambda value: "N/A" if value is None else f"{value:.3f}"
                        lines.append(f"| {model_name} | {category} | {fmt(values.get('accuracy'))} | {fmt(values.get('rejection_correct_rate'))} | {fmt(values.get('intent_violation_rate'))} | {fmt(values.get('capability_violation_rate'))} |")
                if args.include_planning_v5:
                    lines += ["", "## planning-v5 类别指标", "", "| 模型 | 类别 | 准确率 | 拒绝正确率 | 意图违规率 | 能力违规率 |", "|---|---|---:|---:|---:|---:|"]
                    for model_name in ("candidate", "baseline"):
                        for category, values in sorted(metrics[model_name]["planning-v5"].get("category_metrics", {}).items()):
                            fmt = lambda value: "N/A" if value is None else f"{value:.3f}"
                            lines.append(f"| {model_name} | {category} | {fmt(values.get('accuracy'))} | {fmt(values.get('rejection_correct_rate'))} | {fmt(values.get('intent_violation_rate'))} | {fmt(values.get('capability_violation_rate'))} |")
                decision_line = (f"- r21 替换 r14：{'建议' if decision['replace_r14'] else '不建议'}"
                                 if baseline_label == "r14" else
                                 f"- 是否替换 {baseline_label}：{'建议' if decision['replace_r14'] else '不建议'}")
                lines += ["", f"- 返航探测：候选 {decision['probe_candidate_passed']}/15 / {baseline_label} {decision['probe_r14_passed']}/15", decision_line]
                default_reason = "- 判据：planning-v3 不低于 r14，planning-v4 至少一个重点类别提升 >=10 个百分点，15 条探测全通过"
                custom_reason = f"- 判据：planning-v3 不低于 {baseline_label}，planning-v4 至少一个重点类别提升 >=10 个百分点，15 条探测全通过"
                lines += [f"- 判据：{reason}" for reason in decision["reasons"]] or [default_reason if baseline_label == "r14" else custom_reason]
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
            outputs = stage_outputs(stage, out, include_planning_v5=args.include_planning_v5)
            missing = [str(p) for p in outputs if not p.is_file()]
            if missing:
                raise RuntimeError(f"stage {stage} missing outputs: {missing}")
            state.setdefault("stages", {})[stage] = {
                "status": "complete", "completed_utc": datetime.now(timezone.utc).isoformat(),
                "sha256": {str(p): sha256(p) for p in outputs},
                **({"plan_pregate": args.plan_pregate} if stage in {"eval", "report"} else {}),
            }
            atomic_json(state_path, state)
        finally:
            if lock_context:
                lock_context.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
