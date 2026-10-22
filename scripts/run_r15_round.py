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


GIB = 1024**3
# Bytes per recorded step from the offline sample in docs/r19-low-disk.md: three r15
# episodes (56 steps) re-recorded with the round's options.  Normal: PNG RGB, png16
# depth, PNG segmentation, plain mavlink.jsonl.  Low-disk: JPEG q90 RGB, no depth,
# PNG segmentation, mavlink.jsonl.gz.
NORMAL_BYTES_PER_STEP = 833_069
LOW_DISK_BYTES_PER_STEP = 150_469
# r15-round/merged-gguf as measured: merged HF weights, F16 GGUF and Q5_K_M GGUF.
MERGED_HF_BYTES = 4_255_140_312
F16_GGUF_BYTES = 3_447_350_272
Q5_GGUF_BYTES = 1_257_880_576
# Proposal cache, SFT JSONL, adapter and eval JSON together stay well below this.
SMALL_OUTPUTS_BYTES = GIB // 4
LOW_DISK_COLLECTOR_ARGS = [
    "--rgb-format",
    "jpeg",
    "--rgb-quality",
    "90",
    "--no-depth",
    "--compress-mavlink",
]
LOW_DISK_LEDGER = "low-disk-cleanup.json"


def estimate_round_disk_gib(
    *,
    low_disk: bool,
    episodes: int = 20 * R15_EPISODES_PER_WORLD,
    steps: int = R15_STEPS_PER_EPISODE,
) -> dict[str, float]:
    """Peak and after-round disk use of a round, from the measured constants above.

    The export always needs merged HF weights and the F16 GGUF at the same time
    (convert_hf_to_gguf reads one to write the other), so low-disk mode cannot
    lower that peak; it drops both right after use instead of keeping them.
    """
    per_step = LOW_DISK_BYTES_PER_STEP if low_disk else NORMAL_BYTES_PER_STEP
    media = episodes * steps * per_step
    convert_peak = MERGED_HF_BYTES + F16_GGUF_BYTES
    kept_export = (
        Q5_GGUF_BYTES if low_disk else MERGED_HF_BYTES + F16_GGUF_BYTES + Q5_GGUF_BYTES
    )
    base = media + SMALL_OUTPUTS_BYTES
    return {
        "episodes_gib": round(media / GIB, 2),
        "export_peak_gib": round(convert_peak / GIB, 2),
        "peak_gib": round((base + max(convert_peak, kept_export)) / GIB, 2),
        "after_round_gib": round((base + kept_export) / GIB, 2),
    }


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def bucket(short_side: float) -> str:
    for edge, name in zip((12, 16, 20, 32), BUCKETS, strict=False):
        if short_side < edge:
            return name
    return BUCKETS[-1]


def row_key(row: dict) -> str:
    return str(
        row.get("sample_id") or hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
    )


def content_digest(row: dict) -> str:
    value = {k: v for k, v in row.items() if k not in {"sample_id", "id", "row_id"}}
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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
        polarity = (
            "positive"
            if decision in {"confirm_target", "approach_track"}
            else "negative"
            if decision == "reject_track"
            else "other"
        )
        stats[(cls, size, polarity)] += 1
    return {"|".join(key): value for key, value in sorted(stats.items())}


def _polarity(row: dict) -> str:
    decision = (row.get("assistant_target") or {}).get("decision", "unknown")
    return (
        "positive"
        if decision in {"confirm_target", "approach_track"}
        else "negative"
        if decision == "reject_track"
        else "other"
    )


def top_up_negatives(
    rows: list[dict], pool: list[dict], max_positive_ratio: float, seed: int = 20261016
) -> list[dict]:
    """Add held-back r12 rejections until the positive share is back under ``max_positive_ratio``.

    New rounds bring mostly positives (r15: small true targets).  r13 showed that
    tilting the mix toward positives makes the model say "yes" to distractors, so
    instead of dropping the new positives the replay is topped up with more of
    r12's own hard negatives that the proportional replay slice left out.
    """
    positives = sum(1 for row in rows if _polarity(row) == "positive")
    negatives = sum(1 for row in rows if _polarity(row) == "negative")
    if positives / max(1, positives + negatives) <= max_positive_ratio:
        return rows
    candidates = [row for row in pool if _polarity(row) == "negative"]
    random.Random(seed).shuffle(candidates)
    result = list(rows)
    for row in candidates:
        if positives / max(1, positives + negatives) <= max_positive_ratio:
            break
        before = len(result)
        result = merge_without_copies(result, [row])
        negatives += len(result) - before
    return result


def replay_key(row: dict) -> tuple[str, str, str]:
    decision = (row.get("assistant_target") or {}).get("decision", "unknown")
    side = row.get("candidate_short_side_px")
    box = row.get("candidate_box") or row.get("bbox")
    if side is None and isinstance(box, (list, tuple)) and len(box) >= 4:
        side = min(abs(float(box[2]) - float(box[0])), abs(float(box[3]) - float(box[1])))
    return (
        str(row.get("task_category") or row.get("target_class") or "unknown"),
        bucket(float(side)) if side is not None else "unknown",
        str(decision),
    )


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
    if (
        "v1_distractor_rejection" in r12
        and r15.get("v1_distractor_rejection", -1) < r12["v1_distractor_rejection"] - 0.02
    ):
        reasons.append("v1 真实飞行集干扰物拒绝低于 r12 超过 2 个百分点")
    return not reasons, reasons


def identity_summary(payload: dict) -> dict[str, float | None]:
    """Headline identity metrics from an eval_slow_system JSON (``metrics`` block)."""
    metrics = payload.get("metrics", payload)
    under12 = (metrics.get("size_buckets") or {}).get("<12") or {}
    return {
        "identity_accuracy": metrics.get("identity_accuracy"),
        "distractor_rejection": metrics.get("distractor_rejection_rate"),
        "under12_recall": under12.get("true_target_recall"),
    }


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _remove_worker_containers(prefix: str) -> None:
    """Remove this worker's leftover containers so the next attempt can bind the worker's ports.

    A worker runs its episodes one after another, so nothing named ``<prefix>-*`` is live
    between attempts; a container still holding the port is a previous attempt's leftover.
    """
    try:
        names = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name=^{prefix}-"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout.split()
        if names:
            subprocess.run(
                ["docker", "rm", "-f", *names], capture_output=True, timeout=120, check=False
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[collect] 清理 {prefix} 残留容器失败：{exc}")


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def ledger_removed(ledger: Path) -> int:
    """Number of ledger files already gone (removed, or deleted before an interruption)."""
    if not ledger.is_file():
        return 0
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    root = Path(payload.get("episodes_root", "."))
    return sum(
        1
        for episode in payload.get("episodes", {}).values()
        for rel, item in episode.get("files", {}).items()
        if item.get("removed") or not (root / rel).is_file()
    )


def cleanup_consumed_segmentation(
    *, episodes_root: Path, collected: dict, sft_manifest: Path, ledger: Path
) -> dict:
    """Delete segmentation PNGs this round recorded and build_sft already consumed.

    A file is removed only when all of these hold:

    * its episode is one this round collected (``round-state.json`` episodes entry,
      status complete), lies under ``episodes_root`` and its ``episode.json`` still
      has the SHA-256 recorded at collection time;
    * it is a ``seg/`` file that episode's own ``steps.jsonl`` references;
    * the build_sft manifest lists it in ``source_sha256`` with the same SHA-256
      (the labels were derived from exactly these bytes).

    Each file's path, size and hash go into ``ledger`` (written atomically) before
    anything is deleted.  RGB stays (SFT rows and training read it), and nothing
    outside those episodes -- other runs, ``--extra-episodes``, existing data -- is
    ever touched.
    """
    root = episodes_root.resolve()
    manifest = json.loads(sft_manifest.read_text(encoding="utf-8"))
    consumed: dict[tuple[str, ...], list[tuple[str, str]]] = {}
    for key, digest in (manifest.get("source_sha256") or {}).items():
        parts = tuple(Path(key.replace("\\", "/")).parts)
        consumed.setdefault(parts[-3:], []).append(("/".join(parts), digest))
    previous = json.loads(ledger.read_text(encoding="utf-8")) if ledger.is_file() else {}
    record = {
        "schema": 1,
        "episodes_root": str(root),
        "sft_manifest": str(sft_manifest),
        "sft_manifest_sha256": sha256(sft_manifest),
        "consumer": "build_slow_system_sft (source_sha256)",
        "episodes": dict(previous.get("episodes", {})),
    }
    pending: list[tuple[str, str, Path]] = []
    skipped: list[str] = []
    kept = 0
    for key, item in sorted(collected.items()):
        if item.get("status") != "complete" or not item.get("path"):
            continue
        episode_json = Path(item["path"]).resolve()
        if not episode_json.is_relative_to(root) or not episode_json.is_file():
            skipped.append(key)
            continue
        if sha256(episode_json) != item.get("sha256"):
            skipped.append(key)
            continue
        episode = episode_json.parent
        seg_dir = (episode / "seg").resolve()
        entry = record["episodes"].setdefault(
            key,
            {
                "episode": episode.relative_to(root).as_posix(),
                "episode_json_sha256": item["sha256"],
                "files": {},
            },
        )
        referenced: set[Path] = set()
        steps = episode / "steps.jsonl"
        for line in steps.read_text(encoding="utf-8").splitlines() if steps.is_file() else ():
            if not line.strip():
                continue
            row = json.loads(line)
            for side in ("obs_before", "obs_after"):
                value = (row.get(side) or {}).get("segmentation_path")
                if isinstance(value, str) and value:
                    path = (episode / value.replace("\\", "/")).resolve()
                    if path.parent == seg_dir:
                        referenced.add(path)
        for path in sorted(referenced):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            digest = sha256(path)
            matches = consumed.get(tuple(Path(rel).parts[-3:]), [])
            if not any(
                (source == rel or source.endswith("/" + rel)) and source_digest == digest
                for source, source_digest in matches
            ):
                kept += 1
                continue
            entry["files"][rel] = {
                "sha256": digest,
                "bytes": path.stat().st_size,
                "consumed_by": "build_sft",
                "removed": False,
            }
            pending.append((key, rel, path))
    _write_json_atomic(ledger, record)
    removed_bytes = 0
    for key, rel, path in pending:
        size = record["episodes"][key]["files"][rel]["bytes"]
        path.unlink()
        record["episodes"][key]["files"][rel]["removed"] = True
        removed_bytes += size
    for _key, _rel, path in pending:
        try:
            path.parent.rmdir()
        except OSError:
            pass
    _write_json_atomic(ledger, record)
    return {
        "removed_files": len(pending),
        "removed_bytes": removed_bytes,
        "kept_unconsumed": kept,
        "skipped_episodes": skipped,
    }


class Round:
    def __init__(self, args):
        self.a = args
        self.out = args.out.resolve()
        self.state_path = self.out / "round-state.json"
        self.state = (
            json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state_path.is_file()
            else {"schema": 1, "stages": {}, "episodes": {}}
        )

    def save(self):
        self.out.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, self.state_path)

    def data_path(self, configured: Path, relative: str) -> Path:
        if configured.exists():
            return configured
        candidate = self.a.data_root / relative
        return candidate if candidate.exists() or self.a.data_root != ROOT else configured

    def run(self, name: str, command: list[str], *, estimate: str, disk: str, outputs=(), cwd=ROOT):
        self.out.mkdir(parents=True, exist_ok=True)
        if self.stage_current(name, outputs):
            print(f"[skip] {name}: manifest 与输出 SHA-256 均匹配")
            return
        print(f"[{name}] 预计耗时 {estimate}；预计磁盘 {disk}")
        print("  " + subprocess.list2cmdline(command))
        if self.a.dry_run:
            return
        started = time.time()
        proc = subprocess.run(command, cwd=cwd, check=False)
        if proc.returncode:
            self.state["stages"][name] = {
                "status": "failed",
                "exit_code": proc.returncode,
                "time": time.time(),
            }
            self.save()
            raise RuntimeError(f"阶段 {name} 失败，退出码 {proc.returncode}；不会执行后续阶段")
        missing = [str(p) for p in outputs if not Path(p).is_file()]
        if missing:
            raise RuntimeError(f"阶段 {name} 未生成预期产物：{missing}")
        hashes = {str(p): sha256(Path(p)) for p in outputs}
        record = {
            "status": "complete",
            "elapsed_s": round(time.time() - started, 3),
            "outputs": hashes,
            "command": command,
        }
        manifest = self.out / "manifests" / f"{name}.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        record["manifest_sha256"] = sha256(manifest)
        self.state["stages"][name] = record
        self.save()

    def guards(self, stage: str):
        usage = shutil.disk_usage(self.out.anchor or ROOT.anchor)
        free_gb = usage.free / 1_000_000_000
        if free_gb < self.a.min_free_gb and not self.a.dry_run:
            raise RuntimeError(
                f"{stage} 磁盘门禁失败：{free_gb:.1f} GB < {self.a.min_free_gb:.1f} GB"
            )
        available_gb = None
        try:
            import psutil

            available_gb = psutil.virtual_memory().available / 1_000_000_000
        except ImportError:
            pass
        if (
            available_gb is not None
            and available_gb < self.a.min_free_memory_gb
            and not self.a.dry_run
        ):
            raise RuntimeError(
                f"{stage} 内存门禁失败：{available_gb:.1f} GB < {self.a.min_free_memory_gb:.1f} GB"
            )
        print(
            f"[guard] {stage}: 可用磁盘 {free_gb:.1f} GB；可用内存 "
            f"{available_gb if available_gb is not None else 'unknown'} GB"
        )
        if not getattr(self.a, "low_disk", False):
            return
        # Low-disk runs go with a small --min-free-gb, so also require that the
        # coming stage's own growth still leaves that margin.
        need = self.stage_disk_need_gib(stage)
        if need:
            projected = usage.free / GIB - need
            print(
                f"[guard] {stage}: 本阶段新增峰值约 {need:.2f} GiB；预计余量 {projected:.2f} GiB"
            )
            if projected < self.a.min_free_gb and not self.a.dry_run:
                raise RuntimeError(
                    f"{stage} 预计峰值后磁盘余量 {projected:.1f} GiB < 门槛 "
                    f"{self.a.min_free_gb:.1f} GiB"
                )

    def stage_disk_need_gib(self, stage: str) -> float:
        """Disk the stage itself adds at its peak (GiB), from the measured constants."""
        low_disk = getattr(self.a, "low_disk", False)
        if stage == "collect":
            done = sum(
                1 for item in self.state["episodes"].values() if item.get("status") == "complete"
            )
            remaining = max(0, 20 * R15_EPISODES_PER_WORLD - done)
            per_step = LOW_DISK_BYTES_PER_STEP if low_disk else NORMAL_BYTES_PER_STEP
            return remaining * self.a.steps_per_episode * per_step / GIB
        if stage == "export":
            return (MERGED_HF_BYTES + F16_GGUF_BYTES) / GIB
        if stage in {"annotate", "build_sft", "train", "eval", "report"}:
            return SMALL_OUTPUTS_BYTES / GIB / 4
        return 0.0

    def collector_min_free_gb(self) -> float:
        value = getattr(self.a, "collector_min_free_gb", None)
        return self.a.min_free_gb if value is None else value

    def stage_current(self, name: str, outputs) -> bool:
        """True when a complete stage's recorded output hashes still match."""
        prior = self.state["stages"].get(name)
        return bool(
            prior
            and prior.get("status") == "complete"
            and all(
                Path(p).is_file() and sha256(Path(p)) == prior["outputs"].get(str(p))
                for p in outputs
            )
        )

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
                valid = [
                    p
                    for p in episodes
                    if json.loads(p.read_text(encoding="utf-8")).get("status") in (None, "complete")
                ]
                if len(valid) == 1:
                    path = valid[0]
                    return {
                        "status": "complete",
                        "path": str(path),
                        "sha256": sha256(path),
                        "attempt": attempt,
                    }
            if attempt == self.a.retries:
                return {"status": "failed", "attempts": attempt + 1}
            time.sleep(min(2**attempt, 30))
        raise AssertionError("unreachable")

    def collect(self):
        scenarios = sorted(self.a.scenarios.glob("gen_r15_w*.scenario.json"))
        if len(scenarios) != 20:
            raise RuntimeError(
                f"collection plan 期望 20 个 world，实际 {len(scenarios)}：{self.a.scenarios}"
            )
        jobs: list[tuple[str, list[str], Path]] = []
        for scenario in scenarios:
            world = scenario.name.removesuffix(".scenario.json")
            for idx in range(R15_EPISODES_PER_WORLD):
                key = f"{world}:{idx}"
                if self.state["episodes"].get(key, {}).get("status") == "complete":
                    continue
                worker = idx % self.a.workers
                target = self.a.episodes / world / f"ep-{idx}"
                cmd = [
                    sys.executable,
                    "scripts/collect_sim_teacher_episodes.py",
                    "--scenario",
                    str(scenario),
                    "--teacher",
                    "expert_search",
                    "--episodes",
                    "1",
                    "--steps-per-episode",
                    str(self.a.steps_per_episode),
                    "--rgb-size",
                    "1280x960",
                    "--depth-size",
                    "1280x960",
                    "--depth-format",
                    "png16",
                    "--segmentation",
                    "--onboard-frame-format",
                    "jpeg",
                    "--onboard-frame-quality",
                    "90",
                    "--seed",
                    str(20261016 + idx),
                    "--min-free-gb",
                    str(self.collector_min_free_gb()),
                    "--timeout-s",
                    "900",
                    "--max-episode-s",
                    "900",
                    "--container-prefix",
                    f"r15-worker-{worker}",
                    "--control-port",
                    str(R15_BASE_CONTROL_PORT + worker * 10),
                    "--telemetry-port",
                    str(R15_BASE_TELEMETRY_PORT + worker * 10),
                    "--teacher-target",
                    self.a.teacher_target,
                    "--out",
                    str(target),
                ]
                if getattr(self.a, "low_disk", False):
                    # JPEG RGB (every reader decodes by content), no depth (nothing in
                    # this round reads it), gzip MAVLink (replay/energy readers accept
                    # .gz); segmentation stays lossless PNG.  "--out <target>" stays last.
                    cmd[-2:-2] = LOW_DISK_COLLECTOR_ARGS
                jobs.append((key, cmd, target))
                print(
                    f"[collect] {key} -> worker {worker}; 失败最多重试 {self.a.retries} "
                    f"次；命令：\n  {subprocess.list2cmdline(cmd)}"
                )
        print(
            f"[collect] 总预算：约 40-55 分钟（{len(jobs)} episodes，"
            f"{self.a.steps_per_episode} steps/episode 上限）；磁盘上限约 "
            f"{self.stage_disk_need_gib('collect'):.2f} GiB；"
            f"低磁盘={getattr(self.a, 'low_disk', False)}"
        )
        if self.a.dry_run:
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.a.workers) as pool:
            futures = {
                pool.submit(self._collect_one, key, cmd, target): key for key, cmd, target in jobs
            }
            for future in concurrent.futures.as_completed(futures):
                key = futures[future]
                result = future.result()
                self.state["episodes"][key] = result
                self.save()
                if result["status"] != "complete":
                    # One stubborn world must not stop the whole round; the report lists it.
                    print(f"[collect] episode {key} 在 {result['attempts']} 次尝试后失败，跳过")
        self.run(
            "collect",
            [sys.executable, "-c", "print('episodes collected')"],
            estimate="约 40-55 分钟：40×约20秒模拟观测，2 worker；依据 r15 计划 560 帧/"
            "28秒基线、collector 0.15-0.17 sim s/墙钟 s 注释及 GPU cache 日志的时间基线",
            disk="约 7.5 GiB（按 15 GiB/80 episodes 线性折半）",
            outputs=(),
        )

    def execute(self):
        self.out.mkdir(parents=True, exist_ok=True)
        if self.a.stage in ("all", "collect"):
            self.guards("collect")
            self.collect()
        if self.a.stage in ("all", "annotate"):
            self.guards("annotate")
            cmd = [sys.executable, "scripts/cache_onboard_proposals.py"]
            for root in (self.a.episodes, *self.a.extra_episodes):
                for p in sorted(root.glob("gen_r15_w*")):
                    cmd += ["--campaign", str(p)]
            # Cache keys must match the repository-relative image paths build_sft looks
            # up (artifacts/<campaign>/...); key them from the data root that holds the
            # episodes, otherwise no live proposal ever matches a frame.
            source_root = self.a.source_root
            if source_root is None:
                source_root = (
                    self.a.data_root
                    if self.a.episodes.resolve().is_relative_to(self.a.data_root.resolve())
                    else self.a.episodes
                )
            if source_root.resolve() == ROOT.resolve():
                raise RuntimeError(
                    "--source-root 不能指向当前 worktree；请指向 Multi-Detect "
                    "主仓库或本轮采集输出根"
                )
            cmd += [
                "--source-root",
                str(source_root),
                "--output",
                str(self.out / "yoloe-v3-cache.jsonl"),
                "--device",
                self.a.annotate_device,
            ]
            self.run(
                "annotate",
                cmd,
                estimate="约 20-35 分钟：GPU cuda:0；6462 帧历史日志 "
                "00:46:47→00:51:10（约4分23秒）且既有记录约20分钟，按 44,800 帧含启动/IO 余量估算",
                disk="缓存约 1-3 GiB",
                outputs=(self.out / "yoloe-v3-cache.jsonl",),
            )
        if self.a.stage in ("all", "build_sft"):
            self.guards("build_sft")
            sft_manifest = self.out / "r15-additions" / "manifest.json"
            ledger = self.out / LOW_DISK_LEDGER
            if not self.stage_current("build_sft", (sft_manifest,)) and ledger_removed(ledger):
                raise RuntimeError(
                    f"低磁盘清理已删除被 build_sft 消费过的 segmentation（见 {ledger}），"
                    "不能重建 SFT；请恢复原 r15-additions 或重新采集"
                )
            extra = [arg for root in self.a.extra_episodes for arg in ("--campaign", str(root))]
            cmd = [
                sys.executable,
                "scripts/build_slow_system_sft.py",
                "--campaign",
                str(self.a.episodes),
                *extra,
                "--index",
                str(self.a.r15_index),
                "--proposals",
                str(self.out / "yoloe-v3-cache.jsonl"),
                "--counterfactual-tasks",
                # Paired rejects: renamed-task rows alone taught "find the pickup" to
                # accept an SUV (r15/r16 on the v1 real-flight set).
                "--counterfactual-negatives",
                # The frozen v1/v3 test classes stay unseen in top-up rounds.
                *(arg for path in self.a.holdout_from for arg in ("--holdout-from", str(path))),
                "--crop-policy",
                "candidate_zoom_v2",
                "--output",
                str(self.out / "r15-additions"),
            ]
            self.run(
                "build_sft",
                cmd,
                estimate="约 1-3 小时（解码/筛选 44,800 帧）",
                disk="新增 JSONL <2 GiB；复用图片",
                outputs=(self.out / "r15-additions" / "manifest.json",),
            )
            if getattr(self.a, "low_disk", False) and not self.a.dry_run:
                summary = cleanup_consumed_segmentation(
                    episodes_root=self.a.episodes,
                    collected=self.state["episodes"],
                    sft_manifest=sft_manifest,
                    ledger=ledger,
                )
                print(
                    f"[low-disk] 清理 {summary['removed_files']} 个已被 build_sft 消费的 "
                    f"segmentation（{summary['removed_bytes'] / 1024**2:.1f} MiB，哈希见 "
                    f"{ledger}）；未消费保留 {summary['kept_unconsumed']}，"
                    f"跳过 episode {len(summary['skipped_episodes'])}"
                )
            merge_cmd = [
                sys.executable,
                str(ROOT / "scripts" / "run_r15_round.py"),
                "--merge-only",
                "--r12-dataset",
                str(self.a.r12_dataset),
                "--additions",
                str(self.out / "r15-additions"),
                "--merged",
                str(self.out / "r15-sft"),
                "--r12-replay-fraction",
                str(self.a.r12_replay_fraction),
                "--max-positive-ratio-delta",
                str(self.a.max_positive_ratio_delta),
                "--max-negative-ratio-delta",
                str(self.a.max_negative_ratio_delta),
                "--seed",
                "20261016",
            ]
            self.run(
                "merge_sft",
                merge_cmd,
                estimate="约 10 分钟",
                disk="约 r12 数据集体积增量",
                outputs=(self.out / "r15-sft" / "manifest.json",),
            )
        if self.a.stage in ("all", "train"):
            self.guards("train")
            cmd = [
                sys.executable,
                "scripts/train_slow_system_lora.py",
                "--dataset",
                str(self.out / "r15-sft"),
                "--model",
                str(self.a.model),
                "--output",
                str(self.out / "adapter"),
                "--epochs",
                "1",
                "--accumulation",
                "4",
                "--rank",
                "16",
                "--alpha",
                "32",
                "--learning-rate",
                "1e-4",
                "--max-side",
                "448",
                "--crop-policy",
                "candidate_zoom_v2",
                "--quantization",
                "auto",
            ]
            if self.a.hot_start:
                cmd += ["--init-adapter", str(self.a.r12_adapter)]
            if (self.out / "adapter" / "checkpoint" / "state.json").is_file():
                cmd.append("--resume")
            self.run(
                "train",
                cmd,
                estimate="约 1.5-3 小时：r12 训练日志 8042 s/2696 steps、r14 7687 s/2858 "
                "steps（约2.7-3.0 s/optimizer step），r12 热启动 + 25% 分层 replay + 1 epoch",
                disk="至少 30 GiB 可用",
                outputs=(self.out / "adapter" / "checkpoint" / "state.json",),
            )
        if self.a.stage in ("all", "export"):
            self.guards("export")
            cmd = [
                sys.executable,
                "scripts/export_slow_system_r15.py",
                "--base",
                str(self.a.model),
                "--adapter",
                str(self.out / "adapter" / "adapter"),
                "--output",
                str(self.out / "merged-gguf"),
                "--quantization",
                "Q5_K_M",
            ]
            if getattr(self.a, "low_disk", False):
                cmd.append("--drop-intermediates")
            self.run(
                "export",
                cmd,
                estimate="约 1-3 小时（合并及量化）",
                disk="至少 15 GiB 临时空间",
                outputs=(self.out / "merged-gguf" / "slow-system-Q5_K_M.gguf",),
            )
        if self.a.stage in ("all", "eval"):
            self.guards("eval")
            datasets = (
                (
                    "v1",
                    self.data_path(self.a.v1_dataset, "artifacts/datasets/flight-identity-eval-v1"),
                ),
                (
                    "v3",
                    self.data_path(
                        self.a.v3_dataset, "artifacts/datasets/flight-identity-eval-v2-v3prop"
                    ),
                ),
            )
            mmproj = self.data_path(self.a.mmproj, "artifacts/models/mmproj.gguf")
            llama_server = self.data_path(
                self.a.llama_server, "artifacts/cache/llama-cpp/b11552/llama-server.exe"
            )
            for label, dataset in datasets:
                cmd = [
                    sys.executable,
                    "scripts/eval_slow_system.py",
                    "--dataset",
                    str(dataset),
                    "--data-root",
                    str(self.a.data_root),
                    "--backend",
                    "gguf",
                    "--device",
                    "cuda:0",
                    "--identity-only",
                    "--fast-identity",
                    "--crop-policy",
                    "candidate_zoom_v2",
                    "--gguf-model",
                    str(self.out / "merged-gguf" / "slow-system-Q5_K_M.gguf"),
                    "--mmproj",
                    str(mmproj),
                    "--llama-server",
                    str(llama_server),
                    "--output",
                    str(self.out / f"eval-{label}.json"),
                ]
                estimate = (
                    "v1+v3 合计约 20-50 分钟：GPU fast_identity，1,900+233 行；目标小于 1 小时"
                )
                self.run(
                    f"eval_{label}",
                    cmd,
                    estimate=estimate,
                    disk="约 1 GiB",
                    outputs=(self.out / f"eval-{label}.json",),
                )
            # run_model_round_eval only writes inside this worktree; keep the planning
            # output here when the round directory lives in the main checkout.
            planning_out = self.out / "eval-planning"
            if not planning_out.resolve().is_relative_to(ROOT.resolve()):
                planning_out = ROOT / "artifacts" / f"{self.out.name}-eval-planning"
            planning_summary = planning_out / "summary.json"
            planning_cmd = [
                sys.executable,
                "scripts/run_model_round_eval.py",
                "--model",
                "v2-r15-multi",
                "--gguf",
                str(self.out / "merged-gguf" / "slow-system-Q5_K_M.gguf"),
                "--data-root",
                str(self.a.data_root),
                "--stages",
                "planning-v3",
                "--out",
                str(planning_out),
                "--min-free-disk-gb",
                str(self.a.min_free_gb),
                # WDDM lists every desktop app as a GPU process with N/A memory;
                # our own GPU jobs are still serialized by the model-round GPU lock.
                "--force",
            ]
            try:
                self.run(
                    "eval_planning",
                    planning_cmd,
                    estimate="约 1-2 小时",
                    disk="约 1 GiB",
                    outputs=(planning_summary,),
                )
            except RuntimeError as exc:
                # Planning runs on r14, not on this identity model; a failed or invalid
                # planning run is recorded but does not block the identity report.
                print(f"[eval_planning] 未通过（不影响身份判定）：{exc}")
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
            print(
                "[report] 将生成中文 r12/r15 对比、size_buckets 和替换判定；需要 eval-v1.json/eval-"
                "v3.json 与 r12 基线。"
            )
            return
        baseline_path = self.data_path(
            self.a.r12_baseline,
            "artifacts/benchmarks/flight-identity-eval-v2-v3prop-r12-fast.json",
        )
        r12_v3 = identity_summary(json.loads(baseline_path.read_text(encoding="utf-8")))
        v1_baseline_path = self.data_path(
            self.a.r12_v1_baseline, "artifacts/benchmarks/flight-identity-eval-v1-r12.json"
        )
        r12_v1 = identity_summary(json.loads(v1_baseline_path.read_text(encoding="utf-8")))
        v1 = identity_summary(json.loads((self.out / "eval-v1.json").read_text(encoding="utf-8")))
        v3 = identity_summary(json.loads((self.out / "eval-v3.json").read_text(encoding="utf-8")))
        old = {
            "v1_identity_accuracy": r12_v1["identity_accuracy"],
            "v1_distractor_rejection": r12_v1["distractor_rejection"],
            "v3_identity_accuracy": r12_v3["identity_accuracy"],
            "v3_under12_recall": r12_v3["under12_recall"],
            "v3_distractor_rejection": r12_v3["distractor_rejection"],
        }
        new = {
            "v1_identity_accuracy": v1["identity_accuracy"],
            "v1_distractor_rejection": v1["distractor_rejection"],
            "v3_identity_accuracy": v3["identity_accuracy"],
            "v3_under12_recall": v3["under12_recall"],
            "v3_distractor_rejection": v3["distractor_rejection"],
        }
        approved, reasons = compare_metrics(
            {k: (v if v is not None else 0) for k, v in old.items()},
            {k: (v if v is not None else -1) for k, v in new.items()},
        )
        lines = [
            "# r15 慢模型分层验收报告",
            "",
            "## 总体判定",
            "",
            f"**{'建议替换 r12' if approved else '暂不替换 r12'}**。",
            "",
        ]
        if reasons:
            lines += ["未通过项："] + [f"- {item}" for item in reasons] + [""]
        lines += ["## 指标对比", "", "| 指标 | r12 | r15 |", "|---|---:|---:|"]
        for key in old:
            lines.append(
                f"| {key} | {old[key] if old[key] is not None else '缺失'} | "
                f"{new[key] if new[key] is not None else '缺失'} |"
            )
        lines += [
            "",
            "## 分层结果",
            "",
            "v1/v3 原始 JSON 中的 `size_buckets` 已保留，验收以 `<12`、`12-16`、`16-20`、`20-"
            "32`、`>=32` 五层分别检查。",
            "",
        ]
        report = self.out / "r15-report.md"
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        manifest = self.out / "report-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "report": str(report),
                    "sha256": sha256(report),
                    "approved": approved,
                    "reasons": reasons,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def merge_cli(args):
    out = args.merged
    out.mkdir(parents=True, exist_ok=True)
    stats = {}
    baseline = json.loads((args.r12_dataset / "manifest.json").read_text(encoding="utf-8"))
    baseline_counts = baseline.get("counts", {}).get("train", {}).get("decisions", {})
    baseline_pos = baseline_counts.get("confirm_target", 0) + baseline_counts.get(
        "approach_track", 0
    )
    baseline_neg = baseline_counts.get("reject_track", 0)
    baseline_ratio = baseline_pos / max(1, baseline_pos + baseline_neg)
    for split in ("train", "val", "test"):
        full = read_jsonl(args.r12_dataset / f"{split}.jsonl")
        base = full
        original_count = len(base)
        if split == "train" and args.r12_replay_fraction < 1:
            base = stratified_replay(base, args.r12_replay_fraction, seed=args.seed)
        added = read_jsonl(args.additions / f"{split}.jsonl")
        rows = merge_without_copies(base, added)
        if split == "train":
            replayed = {row_key(row) for row in base}
            rows = top_up_negatives(
                rows,
                [row for row in full if row_key(row) not in replayed],
                baseline_ratio + args.max_positive_ratio_delta / 2,
                seed=args.seed,
            )
        (out / f"{split}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
            encoding="utf-8",
        )
        stats[split] = {
            "r12_original": original_count,
            "r12_replay": len(base),
            "r15_additions": len(added),
            "merged": len(rows),
            "classes_size_polarity": distribution(rows),
        }
    pos = sum(
        v for k, v in stats["train"]["classes_size_polarity"].items() if k.endswith("|positive")
    )
    neg = sum(
        v for k, v in stats["train"]["classes_size_polarity"].items() if k.endswith("|negative")
    )
    ratio = pos / max(1, pos + neg)
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
    manifest = {
        "round": "r15",
        "base": str(args.r12_dataset),
        "additions": str(args.additions),
        "no_duplicate_rows": True,
        "r12_replay_fraction": args.r12_replay_fraction,
        "r12_positive_ratio": baseline_ratio,
        "merged_positive_ratio": ratio,
        "r12_distractor_ratio": baseline_negative_ratio,
        "merged_distractor_ratio": merged_negative_ratio,
        "counts": stats,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest["sha256"] = {p.name: sha256(p) for p in sorted(out.glob("*.jsonl"))}
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stage", choices=("all", *STAGES), default="all")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out", type=Path, default=ROOT / "artifacts/r15-round")
    p.add_argument("--scenarios", type=Path, default=ROOT / "configs/scenarios/generated-r15")
    p.add_argument(
        "--r15-index", type=Path, default=ROOT / "configs/scenarios/generated-r15/index.json"
    )
    p.add_argument("--episodes", type=Path, default=ROOT / "artifacts/r15-episodes")
    p.add_argument(
        "--r12-dataset", type=Path, default=ROOT / "artifacts/datasets/slow-system-sft-v2-r12-multi"
    )
    p.add_argument(
        "--model",
        type=Path,
        default=ROOT
        / "artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots/"
        "89644892e4d85e24eaac8bacfd4f463576704203",
    )
    p.add_argument(
        "--v1-dataset", type=Path, default=ROOT / "artifacts/datasets/flight-identity-eval-v1"
    )
    p.add_argument(
        "--v3-dataset",
        type=Path,
        default=ROOT / "artifacts/datasets/flight-identity-eval-v2-v3prop",
    )
    p.add_argument(
        "--planning-dataset", type=Path, default=ROOT / "artifacts/datasets/plan-eval-tasks-v3.json"
    )
    p.add_argument("--data-root", type=Path, default=ROOT)
    p.add_argument(
        "--mmproj",
        type=Path,
        default=ROOT
        / (
            "artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/"
            "52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf"
        ),
    )
    p.add_argument("--llama-server", type=Path, default=ROOT / "tools/llama.cpp/llama-server.exe")
    p.add_argument("--min-free-gb", type=float, default=30)
    p.add_argument(
        "--collector-min-free-gb",
        type=float,
        default=None,
        help="collector free-disk gate (GB); defaults to --min-free-gb",
    )
    p.add_argument("--min-free-memory-gb", type=float, default=4)
    p.add_argument(
        "--low-disk",
        action="store_true",
        help="JPEG RGB, no depth, gzip MAVLink; drop consumed segmentation and export "
        "intermediates",
    )
    p.add_argument(
        "--r12-baseline",
        type=Path,
        default=ROOT / "artifacts/benchmarks/flight-identity-eval-v2-v3prop-r12-fast.json",
    )
    p.add_argument(
        "--holdout-from",
        type=Path,
        action="append",
        default=[ROOT / "configs/scenarios/generated-v2/index.json"],
        help="index whose held_out_target_combos stay unseen in the new rows (frozen test set)",
    )
    p.add_argument(
        "--r12-v1-baseline",
        type=Path,
        default=ROOT / "artifacts/benchmarks/flight-identity-eval-v1-r12.json",
    )
    p.add_argument(
        "--r12-adapter",
        type=Path,
        default=Path(
            "D:/Projects/Multi-Detect/artifacts/models/slow-system-lora-v2-r12-multi/adapter"
        ),
    )
    p.add_argument(
        "--hot-start",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="默认从 r12 LoRA adapter 热启动；--no-hot-start 从零初始化",
    )
    p.add_argument("--r12-replay-fraction", type=float, default=0.25)
    p.add_argument("--seed", type=int, default=20261016)
    p.add_argument("--annotate-device", default="cuda:0")
    p.add_argument(
        "--source-root",
        type=Path,
        default=None,
        help="采集数据根；必须是 Multi-Detect 或本轮 episodes 根，不能是当前 worktree",
    )
    p.add_argument("--workers", type=int, default=R15_WORKERS)
    p.add_argument("--steps-per-episode", type=int, default=R15_STEPS_PER_EPISODE)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument(
        "--extra-episodes",
        type=Path,
        action="append",
        default=[],
        help="more campaign roots (e.g. confuser flights) to annotate and build from",
    )
    p.add_argument(
        "--teacher-target",
        choices=("true", "confuser"),
        default="true",
        help="collect true-target flights or look-alike confuser flights (hard negatives)",
    )
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
    if (a.collector_min_free_gb is not None and a.collector_min_free_gb < 0) or a.min_free_gb < 0:
        p.error("free disk thresholds must be non-negative")
    if not 0 < a.r12_replay_fraction <= 1:
        p.error("--r12-replay-fraction must be in (0, 1]")
    if a.dry_run:
        for label, low in (("normal", False), ("low-disk", True)):
            est = estimate_round_disk_gib(low_disk=low, steps=a.steps_per_episode)
            print(
                f"[dry-run] {label}: 20 worlds x 4 episodes x {a.steps_per_episode} steps 上限 -> "
                f"episodes {est['episodes_gib']:.2f} GiB，峰值 {est['peak_gib']:.2f} GiB，"
                f"结束后 {est['after_round_gib']:.2f} GiB"
            )
    Round(a).execute()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
