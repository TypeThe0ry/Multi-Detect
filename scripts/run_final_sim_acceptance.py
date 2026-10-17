"""Run the final onboard simulation acceptance matrix with resumable checkpoints.

The normal invocation executes the existing campaign and mission-suite runners.  It
does not start Docker/Gazebo itself; ``--dry-run`` is therefore safe on development
machines and prints the exact commands that a simulator host will execute.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
TEST_WORLDS = [f"gen_v2_w{i:04d}" for i in range(34, 40)]
HISTORY = {
    "test_worlds_success": "6-8/12 (CHANGELOG.md historical baseline)",
    "suite_r8": "13/16 (CHANGELOG.md historical baseline)",
    "suite_r9": "11/16 (CHANGELOG.md historical baseline)",
}


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def config_variants() -> dict[str, Path]:
    return {
        "triangulation": ROOT / "configs/onboard/sim-loop-v3-r12id-r14plan-triangulation.json",
        "triangulation_near_depth": ROOT / "configs/onboard/sim-loop-v3-r12id-r14plan-triangulation-near-depth.json",
        "control_no_triangulation": ROOT / "configs/onboard/sim-loop-v3-r12id-r14plan-no-triangulation.json",
    }


def _free_memory_gb() -> float | None:
    try:
        import psutil  # type: ignore
        return psutil.virtual_memory().available / 1024**3
    except (ImportError, AttributeError):
        return None


def resource_gate(root: Path, min_free_disk_gb: float, min_free_memory_gb: float) -> None:
    usage = shutil.disk_usage(root)
    if usage.free / 1024**3 < min_free_disk_gb:
        raise RuntimeError(f"磁盘余量不足：{usage.free / 1024**3:.2f} GiB")
    memory = _free_memory_gb()
    if memory is not None and memory < min_free_memory_gb:
        raise RuntimeError(f"内存余量不足：{memory:.2f} GiB")


@contextmanager
def gpu_lock(path: Path) -> Iterator[None]:
    """Portable single-task lock; stale locks are never silently removed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f"GPU 单任务锁已被占用：{path}") from exc
    try:
        os.write(fd, f"pid={os.getpid()}\n".encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def _done(marker: Path) -> bool:
    return marker.exists() and marker.read_text(encoding="utf-8").strip() == "complete"


def _mark(marker: Path) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("complete\n", encoding="utf-8", newline="\n")


def build_world_command(config: Path, world: str, out: Path, report: Path) -> list[str]:
    return [PYTHON, str(ROOT / "scripts/eval_onboard_loop_campaign.py"), "--split", "test",
            "--worlds", world, "--episodes", "1", "--steps", "80", "--onboard-config",
            str(config), "--out", str(out), "--report", str(report), "--strict"]


def build_suite_command(suite: Path, mission: str, out: Path) -> list[str]:
    return [PYTHON, str(ROOT / "scripts/run_mission_suite.py"), "--suite",
            str(suite), "--only",
            mission, "--episodes", "1", "--out", str(out), "--strict"]


def materialize_suite(config: Path, destination: Path) -> Path:
    base = _json(ROOT / "configs/onboard/mission-suite-v2-r12id-r14plan.json")
    defaults = _json(config)
    base.setdefault("defaults", {}).update(defaults)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination


def _command_text(command: list[str]) -> str:
    return " ".join('"' + x + '"' if " " in x else x for x in command)


def _result_metrics(report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary", {})
    rows = report.get("episodes", report.get("missions", []))
    if not isinstance(rows, list):
        rows = []
    sources: dict[str, list[float]] = {"ground": [], "triangulation": [], "fused": []}
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = row.get("position_source")
        error = row.get("position_error_m")
        if source in sources and isinstance(error, (int, float)):
            sources[source].append(float(error))
    success = summary.get("success_rate", summary.get("completion_rate"))
    if success is None and rows:
        values = [r.get("mission_completed") for r in rows if isinstance(r, dict) and isinstance(r.get("mission_completed"), bool)]
        success = sum(values) / len(values) if values else None
    count_values = [r.get("count_exact") for r in rows if isinstance(r, dict) and isinstance(r.get("count_exact"), bool)]
    elapsed_values = [r.get(key) for r in rows for key in ("elapsed_s", "duration_s", "mission_elapsed_s", "episode_elapsed_s") if isinstance(r.get(key), (int, float))]
    step_values = [r.get("steps") for r in rows if isinstance(r, dict) and isinstance(r.get("steps"), (int, float))]
    return {
        "success_rate": success,
        "false_confirmation_rate": summary.get("false_confirm_rate"),
        "count_exact_rate": summary.get("count_exact_rate", (sum(count_values) / len(count_values) if count_values else None)),
        "mean_time_s": summary.get("mean_episode_s", summary.get("mean_elapsed_s", (sum(elapsed_values) / len(elapsed_values) if elapsed_values else None))),
        "mean_steps": sum(step_values) / len(step_values) if step_values else None,
        "position_error_by_source_m": {
            source: (sum(values) / len(values) if values else None) for source, values in sources.items()
        },
    }


def render_report(results: list[dict[str, Any]], output: Path, dry_run: bool = False) -> None:
    lines = ["# 新组合仿真闭环验收报告", "", f"模式：{'dry-run' if dry_run else '执行'}", "",
             "## 历史基线", "", f"- 测试世界找单目标：{HISTORY['test_worlds_success']}",
             f"- 任务套件 r8：{HISTORY['suite_r8']}", f"- 任务套件 r9：{HISTORY['suite_r9']}", "",
             "## 本次矩阵", "", "| 场景 | 变体 | 架次/任务数 | 成功率 | 误确认率 | 定位误差 ground/triangulation/fused (m) | 计数准确度 | 平均耗时 (s) | 历史对比 |", "|---|---|---:|---:|---:|---|---:|---:|---|"]
    for item in results:
        m = item.get("metrics", {})
        p = m.get("position_error_by_source_m", {})
        baseline = item.get("baseline", "历史日志/CHANGELOG；旧 artifacts 缺失")
        current = m.get("success_rate")
        if item["stage"] == "测试世界":
            comparison = f"当前 {current}；历史 6/12-8/12（范围）"
        elif item["stage"] == "任务套件":
            comparison = f"当前 {current}；r8=13/16，r9=11/16"
        else:
            comparison = "无独立历史基线"
        elapsed = m.get("mean_time_s")
        if elapsed is None and m.get("mean_steps") is not None:
            elapsed = f"{m['mean_steps']} steps（未提供秒）"
        lines.append(f"| {item['stage']} | {item['variant']} | {item.get('units', 0)} | {m.get('success_rate')} | {m.get('false_confirmation_rate')} | {p.get('ground')} / {p.get('triangulation')} / {p.get('fused')} | {m.get('count_exact_rate')} | {elapsed} | {comparison} |")
    lines += ["", "## 默认开关判定", "", "规则：三角化默认打开须同时满足：相对关闭对照成功率不下降、误确认率不升高、fused/triangulation 定位误差改善且无计数准确度回归；near_depth 只有在近距样本成功率与耗时门槛均通过时才默认打开。", "", "结论：待真实仿真矩阵完成后填写；dry-run 不产生性能结论。", "", "## 坡地/未知地面专项", "", "当前测试世界为平地时，使用 ground_height_sigma_m 放大并注入地面高度偏置回放，属于仿真近似，不等价于真实坡地。"]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "output/final-sim-acceptance")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-free-disk-gb", type=float, default=2.0)
    parser.add_argument("--min-free-memory-gb", type=float, default=1.0)
    parser.add_argument("--suite-missions", nargs="*", default=None)
    args = parser.parse_args(argv)
    if args.min_free_disk_gb < 0 or args.min_free_memory_gb < 0:
        parser.error("资源门槛不能为负数")
    args.out = args.out.resolve()
    variants = config_variants()
    missions = args.suite_missions or [m["id"] for m in _json(ROOT / "configs/onboard/mission-suite-v2-r12id-r14plan.json").get("missions", [])]
    if not missions:
        raise RuntimeError("任务套件为空或 JSON 不可读")
    results: list[dict[str, Any]] = []
    commands: list[list[str]] = []
    for variant, config in variants.items():
        for world in TEST_WORLDS:
            world_dir = args.out / variant / "worlds" / world
            report = world_dir / "report.json"
            for episode in range(1, 3):
                marker = world_dir / f"episode-{episode}" / "complete"
                command = build_world_command(config, world, world_dir, report)
                commands.append(command)
                if not args.dry_run and not _done(marker):
                    resource_gate(args.out, args.min_free_disk_gb, args.min_free_memory_gb)
                    with gpu_lock(args.out / ".gpu.lock"):
                        code = subprocess.run(command, cwd=ROOT, check=False).returncode
                    if code:
                        raise RuntimeError(f"测试世界失败：{world} / {variant} / episode {episode}，退出码 {code}")
                    _mark(marker)
            results.append({"stage": "测试世界", "variant": variant, "units": 12, "baseline": HISTORY["test_worlds_success"], "metrics": _result_metrics(_json(report))})
        for mission in missions:
            marker = args.out / variant / "suite" / mission / "complete"
            suite_path = materialize_suite(config, args.out / variant / "suite-config.json")
            command = build_suite_command(suite_path, mission, marker.parent)
            commands.append(command)
            if not args.dry_run and not _done(marker):
                resource_gate(args.out, args.min_free_disk_gb, args.min_free_memory_gb)
                with gpu_lock(args.out / ".gpu.lock"):
                    code = subprocess.run(command, cwd=ROOT, check=False).returncode
                if code:
                    raise RuntimeError(f"任务套件失败：{mission} / {variant}，退出码 {code}")
                _mark(marker)
            results.append({"stage": "任务套件", "variant": variant, "units": 1, "baseline": f"r8={HISTORY['suite_r8']}；r9={HISTORY['suite_r9']}", "metrics": _result_metrics(_json(marker.parent / "suite-report.json"))})
        # Existing held-out worlds are flat.  This replay approximation raises the
        # ground-height uncertainty and records a deterministic height bias; it is
        # intentionally reported separately from the normal acceptance score.
        special_dir = args.out / variant / "ground-height-approximation"
        special_config = special_dir / "onboard.json"
        special_config.parent.mkdir(parents=True, exist_ok=True)
        if not args.dry_run and not special_config.exists():
            value = _json(config)
            value.update({"ground_height_sigma_m": 5.0, "ground_height_bias_m": 2.0})
            special_config.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        special_world = TEST_WORLDS[0]
        special_report = special_dir / "report.json"
        special_command = build_world_command(special_config, special_world, special_dir, special_report)
        commands.append(special_command)
        special_marker = special_dir / "complete"
        if not args.dry_run and not _done(special_marker):
            resource_gate(args.out, args.min_free_disk_gb, args.min_free_memory_gb)
            with gpu_lock(args.out / ".gpu.lock"):
                code = subprocess.run(special_command, cwd=ROOT, check=False).returncode
            if code:
                raise RuntimeError(f"地面高度偏置专项失败：{variant}，退出码 {code}")
            _mark(special_marker)
        results.append({"stage": "坡地/未知地面（高度偏置回放近似）", "variant": variant, "units": 1, "baseline": "无独立历史坡地基线", "metrics": _result_metrics(_json(special_report))})
    if args.dry_run:
        print("=== 新组合验收 dry-run ===")
        total_units = len(variants) * (len(TEST_WORLDS) * 2 + len(missions) + 1)
        print(f"变体：{len(variants)}；测试世界：{len(TEST_WORLDS)} 世界 x 2 架次 = {len(TEST_WORLDS) * 2} 架次/变体；任务套件：{len(missions)} 任务/变体；高度专项：1/变体；总调度单元：{total_units}")
        print(f"预计耗时：按 CHANGELOG.md 历史 12 架次闭环约 6-8/12 的完整回归估算，保守按每调度单元 20 分钟；三变体约 {total_units * 20 / 60:.1f} 小时，未含重试。来源：CHANGELOG.md 历史基线。")
        for command in commands:
            print(_command_text(command))
    render_report(results, args.out / "final-report.md", args.dry_run)
    (args.out / "final-report.json").write_text(json.dumps({"history": HISTORY, "results": results, "dry_run": args.dry_run}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
