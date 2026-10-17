#!/usr/bin/env python3
"""Resumable orchestration for the slow-system r15 data/model round.

The runner never starts heavyweight work in dry-run mode. Every real stage is
recorded atomically with input/output hashes; collection progress is per episode.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BUCKETS = ("<12", "12-16", "16-20", "20-32", ">=32")
STAGES = ("collect", "annotate", "build_sft", "train", "export", "eval", "report")
R15_EPISODES_PER_WORLD = 4
# Collection plan requires a continuous far-to-near pass through roughly
# 14-16 m, 9 m, 6 m and 4.5 m, plus 80 frames below 4 m. At 20 FPS, four
# 80-frame observation windows are the smallest round-number budget that
# preserves those zones and the required final close-range window.
R15_STEPS_PER_EPISODE = 120
R15_WORKERS = 2
R15_BASE_CONTROL_PORT = 14560
R15_BASE_TELEMETRY_PORT = 14561


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def bucket(short_side: float) -> str:
    for edge, name in zip((12, 16, 20, 32), BUCKETS):
        if short_side < edge:
            return name
    return BUCKETS[-1]


def row_key(row: dict) -> str:
    return str(row.get("sample_id") or hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest())


def content_digest(row: dict) -> str:
    value = {k: v for k, v in row.items() if k not in {"sample_id", "id", "row_id"}}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def merge_without_copies(base: list[dict], additions: list[dict]) -> list[dict]:
    """Append unique rows, rejecting duplicate content even under a new ID."""
    ids = {row_key(row) for row in base}
    contents = {content_digest(row) for row in base}
    result = list(base)
    for row in additions:
        key = row_key(row)
        digest = content_digest(row)
        if key in ids or digest in contents:
            continue
        ids.add(key)
        contents.add(digest)
        result.append(row)
    return result


def distribution(rows: list[dict]) -> dict:
    stats: Counter = Counter()
    for row in rows:
        decision = (row.get("assistant_target") or {}).get("decision", "unknown")
        cls = str(row.get("task_category") or row.get("target_class") or "unknown")
        side = row.get("candidate_short_side_px")
        if side is None:
            box = row.get("candidate_box") or row.get("bbox")
            if isinstance(box, (list, tuple)) and len(box) >= 4:
                side = min(abs(float(box[2]) - float(box[0])), abs(float(box[3]) - float(box[1])))
        size = bucket(float(side)) if side is not None else "unknown"
        polarity = "positive" if decision in {"confirm_target", "approach_track"} else "negative" if decision == "reject_track" else "other"
        stats[(cls, size, polarity)] += 1
    return {"|".join(key): value for key, value in sorted(stats.items())}


def replay_key(row: dict) -> tuple[str, str, str]:
    decision = (row.get("assistant_target") or {}).get("decision", "unknown")
    side = row.get("candidate_short_side_px")
    box = row.get("candidate_box") or row.get("bbox")
    if side is None and isinstance(box, (list, tuple)) and len(box) >= 4:
        side = min(abs(float(box[2]) - float(box[0])), abs(float(box[3]) - float(box[1])))
    return (str(row.get("task_category") or row.get("target_class") or "unknown"), bucket(float(side)) if side is not None else "unknown", str(decision))


def stratified_replay(rows: list[dict], fraction: float, seed: int = 20261016) -> list[dict]:
    """Keep a deterministic, proportional replay slice across class/size/polarity."""
    if not 0 < fraction <= 1:
        raise ValueError("r12 replay fraction must be in (0, 1]")
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault(replay_key(row), []).append(row)
    target = max(len(groups), math.ceil(len(rows) * fraction))
    selected: list[dict] = []
    rng = random.Random(seed)
    for key in sorted(groups):
        group = list(groups[key])
        rng.shuffle(group)
        take = max(1, round(len(group) * fraction))
        selected.extend(group[:take])
    if len(selected) > target:
        rng.shuffle(selected)
        selected = selected[:target]
    return selected


def compare_metrics(r12: dict, r15: dict) -> tuple[bool, list[str]]:
    reasons = []
    if r15.get("v3_identity_accuracy", -1) < r12.get("v3_identity_accuracy", 0):
        reasons.append("v3 集身份准确率下降")
    if r15.get("v3_under12_recall", -1) < r12.get("v3_under12_recall", 0) + 0.10:
        reasons.append("<12 px 真目标召回提升不足 10 个百分点")
    if r15.get("v3_distractor_rejection", -1) < r12.get("v3_distractor_rejection", 0) - 0.02:
        reasons.append("v3 干扰物拒绝低于 r12 超过 2 个百分点")
    if r15.get("v1_identity_accuracy", -1) < r12.get("v1_identity_accuracy", 0) - 0.02:
        reasons.append("v1 整体下降超过预设 2 个百分点显著性容忍值")
    return not reasons, reasons


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]



def _remove_worker_containers(prefix: str) -> None:
    """Remove this worker's leftover containers so the next attempt can bind the worker's ports.

    A worker runs its episodes one after another, so nothing named ``<prefix>-*`` is live
    between attempts; a container still holding the port is a previous attempt's leftover.
    """
    try:
        names = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name=^{prefix}-"],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout.split()
        if names:
            subprocess.run(["docker", "rm", "-f", *names], capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[collect] 清理 {prefix} 残留容器失败：{exc}")

class Round:
    def __init__(self, args):
        self.a = args
        self.out = args.out.resolve()
        self.state_path = self.out / "round-state.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.is_file() else {"schema": 1, "stages": {}, "episodes": {}}

    def save(self):
        self.out.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_path)

    def data_path(self, configured: Path, relative: str) -> Path:
        if configured.exists():
            return configured
        candidate = self.a.data_root / relative
        return candidate if candidate.exists() or self.a.data_root != ROOT else configured

    def run(self, name: str, command: list[str], *, estimate: str, disk: str, outputs=(), cwd=ROOT):
        self.out.mkdir(parents=True, exist_ok=True)
        prior = self.state["stages"].get(name)
        if prior and prior.get("status") == "complete" and all(Path(p).is_file() and sha256(Path(p)) == prior["outputs"].get(str(p)) for p in outputs):
            print(f"[skip] {name}: manifest 与输出 SHA-256 均匹配")
            return
        print(f"[{name}] 预计耗时 {estimate}；预计磁盘 {disk}")
        print("  " + subprocess.list2cmdline(command))
        if self.a.dry_run:
            return
        started = time.time()
        proc = subprocess.run(command, cwd=cwd, check=False)
        if proc.returncode:
            self.state["stages"][name] = {"status": "failed", "exit_code": proc.returncode, "time": time.time()}
            self.save()
            raise RuntimeError(f"阶段 {name} 失败，退出码 {proc.returncode}；不会执行后续阶段")
        missing = [str(p) for p in outputs if not Path(p).is_file()]
        if missing:
            raise RuntimeError(f"阶段 {name} 未生成预期产物：{missing}")
        hashes = {str(p): sha256(Path(p)) for p in outputs}
        record = {"status": "complete", "elapsed_s": round(time.time() - started, 3), "outputs": hashes, "command": command}
        manifest = self.out / "manifests" / f"{name}.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        record["manifest_sha256"] = sha256(manifest)
        self.state["stages"][name] = record
        self.save()

    def guards(self, stage: str):
        usage = shutil.disk_usage(self.out.anchor or ROOT.anchor)
        free_gb = usage.free / 1_000_000_000
        if free_gb < self.a.min_free_gb and not self.a.dry_run:
            raise RuntimeError(f"{stage} 磁盘门禁失败：{free_gb:.1f} GB < {self.a.min_free_gb:.1f} GB")
        available_gb = None
        try:
            import psutil
            available_gb = psutil.virtual_memory().available / 1_000_000_000
        except ImportError:
            pass
        if available_gb is not None and available_gb < self.a.min_free_memory_gb and not self.a.dry_run:
            raise RuntimeError(f"{stage} 内存门禁失败：{available_gb:.1f} GB < {self.a.min_free_memory_gb:.1f} GB")
        print(f"[guard] {stage}: 可用磁盘 {free_gb:.1f} GB；可用内存 {available_gb if available_gb is not None else 'unknown'} GB")

    def _collect_one(self, key: str, cmd: list[str], target: Path) -> dict:
        # Each attempt writes to its own directory so a failed or partial attempt can
        # never be mistaken for this key's episode (or another key's).
        prefix = cmd[cmd.index("--container-prefix") + 1]
        for attempt in range(self.a.retries + 1):
            attempt_dir = target / f"attempt-{attempt}"
            attempt_cmd = [*cmd[:-1], str(attempt_dir)]
            _remove_worker_containers(prefix)
            rc = subprocess.run(attempt_cmd, cwd=ROOT).returncode
            if rc == 0:
                episodes = sorted(attempt_dir.rglob("episode-*/episode.json"))
                valid = [p for p in episodes if json.loads(p.read_text(encoding="utf-8")).get("status") in (None, "complete")]
                if len(valid) == 1:
                    path = valid[0]
                    return {"status": "complete", "path": str(path), "sha256": sha256(path), "attempt": attempt}
            if attempt == self.a.retries:
                return {"status": "failed", "attempts": attempt + 1}
            time.sleep(min(2 ** attempt, 30))
        raise AssertionError("unreachable")

    def collect(self):
        scenarios = sorted(self.a.scenarios.glob("gen_r15_w*.scenario.json"))
        if len(scenarios) != 20:
            raise RuntimeError(f"collection plan 期望 20 个 world，实际 {len(scenarios)}：{self.a.scenarios}")
        jobs: list[tuple[str, list[str], Path]] = []
        for scenario in scenarios:
            world = scenario.name.removesuffix(".scenario.json")
            for idx in range(R15_EPISODES_PER_WORLD):
                key = f"{world}:{idx}"
                if self.state["episodes"].get(key, {}).get("status") == "complete":
                    continue
                worker = idx % self.a.workers
                target = self.a.episodes / world / f"ep-{idx}"
                cmd = [sys.executable, "scripts/collect_sim_teacher_episodes.py", "--scenario", str(scenario), "--teacher", "expert_search", "--episodes", "1", "--steps-per-episode", str(self.a.steps_per_episode), "--rgb-size", "1280x960", "--depth-size", "1280x960", "--depth-format", "png16", "--segmentation", "--onboard-frame-format", "jpeg", "--onboard-frame-quality", "90", "--seed", str(20261016 + idx), "--min-free-gb", str(self.a.min_free_gb), "--timeout-s", "900", "--max-episode-s", "900", "--container-prefix", f"r15-worker-{worker}", "--control-port", str(R15_BASE_CONTROL_PORT + worker * 10), "--telemetry-port", str(R15_BASE_TELEMETRY_PORT + worker * 10), "--out", str(target)]
                jobs.append((key, cmd, target))
                print(f"[collect] {key} -> worker {worker}; 失败最多重试 {self.a.retries} 次；命令：\n  {subprocess.list2cmdline(cmd)}")
        print("[collect] 总预算：约 40-55 分钟（2 worker，40 episodes，400 steps/episode）；磁盘约 7.5 GiB；总内存门控每个 worker 共用系统阈值")
        if self.a.dry_run:
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.a.workers) as pool:
            futures = {pool.submit(self._collect_one, key, cmd, target): key for key, cmd, target in jobs}
            for future in concurrent.futures.as_completed(futures):
                key = futures[future]
                result = future.result()
                self.state["episodes"][key] = result
                self.save()
                if result["status"] != "complete":
                    # One stubborn world must not stop the whole round; the report lists it.
                    print(f"[collect] episode {key} 在 {result['attempts']} 次尝试后失败，跳过")
        self.run("collect", [sys.executable, "-c", "print('episodes collected')"], estimate="约 40-55 分钟：40×约20秒模拟观测，2 worker；依据 r15 计划 560 帧/28秒基线、collector 0.15-0.17 sim s/墙钟 s 注释及 GPU cache 日志的时间基线", disk="约 7.5 GiB（按 15 GiB/80 episodes 线性折半）", outputs=())

    def execute(self):
        self.out.mkdir(parents=True, exist_ok=True)
        if self.a.stage in ("all", "collect"):
            self.guards("collect")
            self.collect()
        if self.a.stage in ("all", "annotate"):
            self.guards("annotate")
            cmd = [sys.executable, "scripts/cache_onboard_proposals.py"]
            for p in sorted(self.a.episodes.glob("gen_r15_w*")):
                cmd += ["--campaign", str(p)]
            source_root = self.a.source_root or self.a.episodes
            if source_root.resolve() == ROOT.resolve():
                raise RuntimeError("--source-root 不能指向当前 worktree；请指向 Multi-Detect 主仓库或本轮采集输出根")
            cmd += ["--source-root", str(source_root), "--output", str(self.out / "yoloe-v3-cache.jsonl"), "--device", self.a.annotate_device]
            self.run("annotate", cmd, estimate="约 20-35 分钟：GPU cuda:0；6462 帧历史日志 00:46:47→00:51:10（约4分23秒）且既有记录约20分钟，按 44,800 帧含启动/IO 余量估算", disk="缓存约 1-3 GiB", outputs=(self.out / "yoloe-v3-cache.jsonl",))
        if self.a.stage in ("all", "build_sft"):
            self.guards("build_sft")
            cmd = [sys.executable, "scripts/build_slow_system_sft.py", "--campaign", str(self.a.episodes), "--index", str(self.a.r15_index), "--proposals", str(self.out / "yoloe-v3-cache.jsonl"), "--counterfactual-tasks", "--crop-policy", "candidate_zoom_v2", "--output", str(self.out / "r15-additions")]
            self.run("build_sft", cmd, estimate="约 1-3 小时（解码/筛选 44,800 帧）", disk="新增 JSONL <2 GiB；复用图片", outputs=(self.out / "r15-additions" / "manifest.json",))
            merge_cmd = [sys.executable, str(ROOT / "scripts" / "run_r15_round.py"), "--merge-only", "--r12-dataset", str(self.a.r12_dataset), "--additions", str(self.out / "r15-additions"), "--merged", str(self.out / "r15-sft"), "--r12-replay-fraction", str(self.a.r12_replay_fraction), "--seed", "20261016"]
            self.run("merge_sft", merge_cmd, estimate="约 10 分钟", disk="约 r12 数据集体积增量", outputs=(self.out / "r15-sft" / "manifest.json",))
        if self.a.stage in ("all", "train"):
            self.guards("train")
            cmd = [sys.executable, "scripts/train_slow_system_lora.py", "--dataset", str(self.out / "r15-sft"), "--model", str(self.a.model), "--output", str(self.out / "adapter"), "--epochs", "1", "--accumulation", "4", "--rank", "16", "--alpha", "32", "--learning-rate", "1e-4", "--max-side", "448", "--crop-policy", "candidate_zoom_v2", "--quantization", "auto"]
            if self.a.hot_start:
                cmd += ["--init-adapter", str(self.a.r12_adapter)]
            if (self.out / "adapter" / "checkpoint" / "state.json").is_file():
                cmd.append("--resume")
            self.run("train", cmd, estimate="约 1.5-3 小时：r12 训练日志 8042 s/2696 steps、r14 7687 s/2858 steps（约2.7-3.0 s/optimizer step），r12 热启动 + 25% 分层 replay + 1 epoch", disk="至少 30 GiB 可用", outputs=(self.out / "adapter" / "checkpoint" / "state.json",))
        if self.a.stage in ("all", "export"):
            self.guards("export")
            cmd = [sys.executable, "scripts/export_slow_system_r15.py", "--base", str(self.a.model), "--adapter", str(self.out / "adapter"), "--output", str(self.out / "merged-gguf"), "--quantization", "Q5_K_M"]
            self.run("export", cmd, estimate="约 1-3 小时（合并及量化）", disk="至少 15 GiB 临时空间", outputs=(self.out / "merged-gguf" / "slow-system-Q5_K_M.gguf",))
        if self.a.stage in ("all", "eval"):
            self.guards("eval")
            datasets = (("v1", self.data_path(self.a.v1_dataset, "artifacts/datasets/flight-identity-eval-v1")), ("v3", self.data_path(self.a.v3_dataset, "artifacts/datasets/flight-identity-eval-v2-v3prop")))
            mmproj = self.data_path(self.a.mmproj, "artifacts/models/mmproj.gguf")
            llama_server = self.data_path(self.a.llama_server, "artifacts/cache/llama-cpp/b11552/llama-server.exe")
            for label, dataset in datasets:
                cmd = [sys.executable, "scripts/eval_slow_system.py", "--dataset", str(dataset), "--data-root", str(self.a.data_root), "--backend", "gguf", "--device", "cuda:0", "--identity-only", "--fast-identity", "--crop-policy", "candidate_zoom_v2", "--gguf-model", str(self.out / "merged-gguf" / "slow-system-Q5_K_M.gguf"), "--mmproj", str(mmproj), "--llama-server", str(llama_server), "--output", str(self.out / f"eval-{label}.json")]
                estimate = "v1+v3 合计约 20-50 分钟：GPU fast_identity，1,900+233 行；目标小于 1 小时"
                self.run(f"eval_{label}", cmd, estimate=estimate, disk="约 1 GiB", outputs=(self.out / f"eval-{label}.json",))
            planning_summary = self.out / "eval-planning" / "summary.json"
            planning_cmd = [sys.executable, "scripts/run_model_round_eval.py", "--model", "v2-r15-multi", "--gguf", str(self.out / "merged-gguf" / "slow-system-Q5_K_M.gguf"), "--data-root", str(self.a.data_root), "--stages", "planning-v3", "--out", str(self.out / "eval-planning"), "--min-free-disk-gb", str(self.a.min_free_gb)]
            self.run("eval_planning", planning_cmd, estimate="约 1-2 小时", disk="约 1 GiB", outputs=(planning_summary,))
        if self.a.stage in ("all", "report"):
            self.guards("report")
            self.write_report()

    @staticmethod
    def _metric(payload: dict, *names: str):
        for name in names:
            if name in payload and isinstance(payload[name], (int, float)):
                return float(payload[name])
        for value in payload.values():
            if isinstance(value, dict):
                found = Round._metric(value, *names)
                if found is not None:
                    return found
        return None

    def write_report(self):
        required = [self.out / "eval-v1.json", self.out / "eval-v3.json"]
        missing = [str(p) for p in required if not p.is_file()]
        if missing and not self.a.dry_run:
            raise RuntimeError(f"report 缺少评测产物：{missing}")
        if self.a.dry_run:
            print("[report] 将生成中文 r12/r15 对比、size_buckets 和替换判定；需要 eval-v1.json/eval-v3.json 与 r12 基线。")
            return
        baseline_path = self.data_path(
            self.a.r12_baseline,
            "artifacts/benchmarks/flight-identity-eval-v2-v3prop-r12-fast.json",
        )
        r12 = json.loads(baseline_path.read_text(encoding="utf-8"))
        v1 = json.loads((self.out / "eval-v1.json").read_text(encoding="utf-8"))
        v3 = json.loads((self.out / "eval-v3.json").read_text(encoding="utf-8"))
        old = {"v1_identity_accuracy": self._metric(r12.get("v1", r12), "identity_accuracy", "accuracy"), "v3_identity_accuracy": self._metric(r12.get("v3", r12), "identity_accuracy", "accuracy"), "v3_under12_recall": self._metric(r12.get("v3", r12), "under12_recall", "<12_recall"), "v3_distractor_rejection": self._metric(r12.get("v3", r12), "distractor_rejection", "rejection")}
        new = {"v1_identity_accuracy": self._metric(v1, "identity_accuracy", "accuracy"), "v3_identity_accuracy": self._metric(v3, "identity_accuracy", "accuracy"), "v3_under12_recall": self._metric(v3, "under12_recall", "<12_recall"), "v3_distractor_rejection": self._metric(v3, "distractor_rejection", "rejection")}
        approved, reasons = compare_metrics({k: (v if v is not None else 0) for k, v in old.items()}, {k: (v if v is not None else -1) for k, v in new.items()})
        lines = ["# r15 慢模型分层验收报告", "", "## 总体判定", "", f"**{'建议替换 r12' if approved else '暂不替换 r12'}**。", ""]
        if reasons:
            lines += ["未通过项："] + [f"- {item}" for item in reasons] + [""]
        lines += ["## 指标对比", "", "| 指标 | r12 | r15 |", "|---|---:|---:|"]
        for key in old:
            lines.append(f"| {key} | {old[key] if old[key] is not None else '缺失'} | {new[key] if new[key] is not None else '缺失'} |")
        lines += ["", "## 分层结果", "", "v1/v3 原始 JSON 中的 `size_buckets` 已保留，验收以 `<12`、`12-16`、`16-20`、`20-32`、`>=32` 五层分别检查。", ""]
        report = self.out / "r15-report.md"
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        manifest = self.out / "report-manifest.json"
        manifest.write_text(json.dumps({"report": str(report), "sha256": sha256(report), "approved": approved, "reasons": reasons}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def merge_cli(args):
    out = args.merged
    out.mkdir(parents=True, exist_ok=True)
    stats = {}
    for split in ("train", "val", "test"):
        base = read_jsonl(args.r12_dataset / f"{split}.jsonl")
        original_count = len(base)
        if split == "train" and args.r12_replay_fraction < 1:
            base = stratified_replay(base, args.r12_replay_fraction, seed=args.seed)
        added = read_jsonl(args.additions / f"{split}.jsonl")
        rows = merge_without_copies(base, added)
        (out / f"{split}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
        stats[split] = {"r12_original": original_count, "r12_replay": len(base), "r15_additions": len(added), "merged": len(rows), "classes_size_polarity": distribution(rows)}
    pos = sum(v for k, v in stats["train"]["classes_size_polarity"].items() if k.endswith("|positive"))
    neg = sum(v for k, v in stats["train"]["classes_size_polarity"].items() if k.endswith("|negative"))
    ratio = pos / max(1, pos + neg)
    baseline = json.loads((args.r12_dataset / "manifest.json").read_text(encoding="utf-8"))
    baseline_counts = baseline.get("counts", {}).get("train", {}).get("decisions", {})
    baseline_pos = baseline_counts.get("confirm_target", 0) + baseline_counts.get("approach_track", 0)
    baseline_neg = baseline_counts.get("reject_track", 0)
    baseline_ratio = baseline_pos / max(1, baseline_pos + baseline_neg)
    baseline_negative_ratio = baseline_neg / max(1, baseline_pos + baseline_neg)
    merged_negative_ratio = neg / max(1, pos + neg)
    expected_replay_neg = math.ceil(baseline_neg * args.r12_replay_fraction)
    if (
        abs(ratio - baseline_ratio) > args.max_positive_ratio_delta
        or merged_negative_ratio < baseline_negative_ratio - args.max_negative_ratio_delta
        or neg < expected_replay_neg
    ):
        raise SystemExit(
            f"数据集门禁失败：positive ratio r12={baseline_ratio:.3f}, r15={ratio:.3f}; "
            f"distractor ratio r12={baseline_negative_ratio:.3f}, r15={merged_negative_ratio:.3f}; "
            f"expected replay negatives={expected_replay_neg}, merged={neg}"
        )
    manifest = {"round": "r15", "base": str(args.r12_dataset), "additions": str(args.additions), "no_duplicate_rows": True, "r12_replay_fraction": args.r12_replay_fraction, "r12_positive_ratio": baseline_ratio, "merged_positive_ratio": ratio, "r12_distractor_ratio": baseline_negative_ratio, "merged_distractor_ratio": merged_negative_ratio, "counts": stats}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest["sha256"] = {p.name: sha256(p) for p in sorted(out.glob("*.jsonl"))}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stage", choices=("all", *STAGES), default="all")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out", type=Path, default=ROOT / "artifacts/r15-round")
    p.add_argument("--scenarios", type=Path, default=ROOT / "configs/scenarios/generated-r15")
    p.add_argument("--r15-index", type=Path, default=ROOT / "configs/scenarios/generated-r15/index.json")
    p.add_argument("--episodes", type=Path, default=ROOT / "artifacts/r15-episodes")
    p.add_argument("--r12-dataset", type=Path, default=ROOT / "artifacts/datasets/slow-system-sft-v2-r12-multi")
    p.add_argument("--model", type=Path, default=ROOT / "artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots/89644892e4d85e24eaac8bacfd4f463576704203")
    p.add_argument("--v1-dataset", type=Path, default=ROOT / "artifacts/datasets/flight-identity-eval-v1")
    p.add_argument("--v3-dataset", type=Path, default=ROOT / "artifacts/datasets/flight-identity-eval-v2-v3prop")
    p.add_argument("--planning-dataset", type=Path, default=ROOT / "artifacts/datasets/plan-eval-tasks-v3.json")
    p.add_argument("--data-root", type=Path, default=ROOT)
    p.add_argument("--mmproj", type=Path, default=ROOT / "artifacts/models/mmproj.gguf")
    p.add_argument("--llama-server", type=Path, default=ROOT / "tools/llama.cpp/llama-server.exe")
    p.add_argument("--min-free-gb", type=float, default=30)
    p.add_argument("--min-free-memory-gb", type=float, default=4)
    p.add_argument("--r12-baseline", type=Path, default=ROOT / "artifacts/benchmarks/flight-identity-eval-v2-v3prop-r12-fast.json")
    p.add_argument("--r12-adapter", type=Path, default=Path("D:/Projects/Multi-Detect/artifacts/models/slow-system-lora-v2-r12-multi/adapter"))
    p.add_argument("--hot-start", action=argparse.BooleanOptionalAction, default=True, help="默认从 r12 LoRA adapter 热启动；--no-hot-start 从零初始化")
    p.add_argument("--r12-replay-fraction", type=float, default=0.25)
    p.add_argument("--seed", type=int, default=20261016)
    p.add_argument("--annotate-device", default="cuda:0")
    p.add_argument("--source-root", type=Path, default=None, help="采集数据根；必须是 Multi-Detect 或本轮 episodes 根，不能是当前 worktree")
    p.add_argument("--workers", type=int, default=R15_WORKERS)
    p.add_argument("--steps-per-episode", type=int, default=R15_STEPS_PER_EPISODE)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--merge-only", action="store_true")
    p.add_argument("--additions", type=Path)
    p.add_argument("--merged", type=Path)
    p.add_argument("--max-positive-ratio-delta", type=float, default=0.02)
    p.add_argument("--max-negative-ratio-delta", type=float, default=0.02)
    a = p.parse_args()
    if a.merge_only:
        if not all((a.additions, a.merged)):
            p.error("--merge-only requires --additions and --merged")
        merge_cli(a)
        return 0
    if a.retries < 0:
        p.error("--retries must be >= 0")
    if not 1 <= a.workers <= 2:
        p.error("--workers must be 1 or 2")
    if a.steps_per_episode < 1:
        p.error("--steps-per-episode must be positive")
    if not 0 < a.r12_replay_fraction <= 1:
        p.error("--r12-replay-fraction must be in (0, 1]")
    Round(a).execute()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
