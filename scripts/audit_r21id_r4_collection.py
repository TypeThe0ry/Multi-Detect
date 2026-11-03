#!/usr/bin/env python3
"""Read-only quality audit for the r21id-r4 collected episodes."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from scripts.check_sim_teacher_smoke import evaluate_episode


REQUIRED_VIEWS = ("front", "oblique", "edge_truncated", "recovery")
SFT_SPLITS = ("train", "val", "test")
SFT_ACCEPTANCE_DIRECTIONS = (
    "pickup->suv", "suv->pickup", "person->postbox", "postbox->person",
    "car->postbox", "postbox->car",
)
_SHORT_SIDE_BAND_RE = re.compile(r"^\s*(?:(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)|>=\s*(\d+(?:\.\d+)?)|<\s*(\d+(?:\.\d+)?))\s*$")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def _episode_dir(path: str | Path) -> Path:
    path = Path(path)
    return path.parent if path.name == "episode.json" else path


def _world_id(key: str, path: Path | None = None) -> str:
    match = re.search(r"gen_r21id_[A-Za-z0-9]+_w\d{4}", key)
    if match:
        return match.group(0)
    if path is not None:
        match = re.search(r"gen_r21id_[A-Za-z0-9]+_w\d{4}", str(path))
        if match:
            return match.group(0)
    return "unknown"


def _ep_index(key: str) -> int | None:
    match = re.search(r":(\d+)$", key)
    return int(match.group(1)) if match else None


def _scenario_id(episode: dict[str, Any], world_id: str) -> str:
    command = episode.get("provenance", {}).get("command", [])
    if isinstance(command, list):
        for index, value in enumerate(command[:-1]):
            if value == "--scenario":
                return Path(str(command[index + 1])).stem.removesuffix(".scenario")
    return world_id


def _find_scenario_file(scenarios: Path, scenario_id: str, suffix: str) -> Path:
    path = scenarios / f"{scenario_id}.{suffix}.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _short_side_ok(geometry: dict[str, Any]) -> bool:
    band_name = str(geometry.get("target_short_side_px", ""))
    estimate = geometry.get("target_short_side_px_estimate")
    if estimate is None:
        return False
    match = _SHORT_SIDE_BAND_RE.fullmatch(band_name)
    if match is None:
        return False
    value = float(estimate)
    if match.group(1) is not None:
        lower, upper = float(match.group(1)), float(match.group(2))
        return lower <= value < upper
    if match.group(3) is not None:
        return value >= float(match.group(3))
    return value < float(match.group(4))


def _failure_group(value: Any) -> str:
    text = str(value or "<missing failure>")
    if "did not confirm armed heartbeat" in text:
        return "PX4 未确认 armed heartbeat"
    if "episode wall-clock timeout" in text:
        return "episode wall-clock timeout"
    if "timed out waiting for land" in text:
        return "等待降落超时"
    return text.split(":", 1)[0] if ":" in text else text


def audit_sft_additions(sft_dir: str | Path) -> dict[str, Any]:
    """Summarize task-text-variant rows emitted by build_slow_system_sft."""
    root = Path(sft_dir)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = _load(manifest_path)
    counts: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
    for split in SFT_SPLITS:
        path = root / f"{split}.jsonl"
        if not path.is_file():
            raise FileNotFoundError(path)
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {path}:{line_number}") from exc
            if not isinstance(row, dict):
                continue
            pair = str(row.get("confusable_pair_type") or "")
            view = str(row.get("view") or "")
            label_type = str(row.get("label_type") or "")
            if not pair or view not in REQUIRED_VIEWS or label_type not in {"confusable_negative", "confusable_positive"}:
                continue
            polarity = "reject" if label_type == "confusable_negative" else "positive"
            by_view = counts.setdefault(split, {}).setdefault(pair, {}).setdefault(view, {"reject": 0, "positive": 0})
            by_view[polarity] += 1
    four_view_pairs: dict[str, dict[str, int]] = {}
    directions = sorted({pair for split in counts.values() for pair in split} | set(SFT_ACCEPTANCE_DIRECTIONS))
    acceptance = {}
    for pair in directions:
        for split in SFT_SPLITS:
            per_view = counts.get(split, {}).get(pair, {})
            four_view_pairs.setdefault(split, {})[pair] = {
                "reject": min((per_view.get(view, {}).get("reject", 0) for view in REQUIRED_VIEWS), default=0),
                "positive": min((per_view.get(view, {}).get("positive", 0) for view in REQUIRED_VIEWS), default=0),
            }
        paired_per_split = {split: min(
            four_view_pairs[split][pair]["reject"], four_view_pairs[split][pair]["positive"]
        ) for split in SFT_SPLITS}
        acceptance[pair] = {"four_view_pairs": paired_per_split, "met": sum(paired_per_split.values()) >= 30}
    return {
        "dir": str(root),
        "manifest": {"schema_version": manifest.get("schema_version"), "counts": manifest.get("counts", {})},
        "counts": counts,
        "four_view_pairs": four_view_pairs,
        "acceptance_targets": acceptance,
        "pickup_to_suv_reject_count": sum(
            counts.get(split, {}).get("pickup->suv", {}).get(view, {}).get("reject", 0)
            for split in SFT_SPLITS for view in REQUIRED_VIEWS
        ),
    }


def _failed_artifacts(episodes_root: Path, key: str) -> list[tuple[Path, dict[str, Any]]]:
    world = _world_id(key)
    ep_index = _ep_index(key)
    if ep_index is None:
        return []
    root = episodes_root / world / f"ep-{ep_index}"
    result = []
    for path in sorted(root.glob("attempt-*/episodes/episode-*/episode.json")):
        try:
            episode = _load(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if episode.get("status") == "failed":
            result.append((path, episode))
    return result


def _all_failed_artifacts(episodes_root: Path) -> list[tuple[str, Path, dict[str, Any]]]:
    result = []
    for path in sorted(episodes_root.rglob("episode.json")):
        try:
            episode = _load(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if episode.get("status") != "failed":
            continue
        world = _world_id("", path)
        match = re.search(r"/ep-(d+)/", str(path))
        key = f"{world}:{match.group(1)}" if match else world
        result.append((key, path, episode))
    return result


def audit_collection(
    *,
    episodes_root: str | Path,
    round_state: str | Path,
    index_path: str | Path,
    scenarios: str | Path,
    minimum_frames: int = 8,
    sft_dir: str | Path | None = None,
) -> dict[str, Any]:
    episodes_root = Path(episodes_root)
    round_state_data = _load(Path(round_state))
    index_data = _load(Path(index_path))
    scenario_root = Path(scenarios)
    index_by_world = {row["world_id"]: row for row in index_data.get("worlds", [])}

    complete_reports: list[dict[str, Any]] = []
    logical_failed: list[dict[str, Any]] = []
    failed_artifacts: list[dict[str, Any]] = []

    for key, entry in sorted(round_state_data.get("episodes", {}).items()):
        status = entry.get("status")
        world_id = _world_id(key, Path(entry.get("path", "")))
        if status == "complete":
            path = entry.get("path")
            if not path:
                complete_reports.append({"key": key, "world_id": world_id, "passed": False, "issues": ["round-state complete 条目缺 path"]})
                continue
            episode_dir = _episode_dir(path)
            try:
                episode = _load(episode_dir / "episode.json")
                scenario_id = _scenario_id(episode, world_id)
                intent_path = _find_scenario_file(scenario_root, scenario_id, "teacher_intent")
                truth_path = _find_scenario_file(scenario_root, scenario_id, "truth")
                smoke = evaluate_episode(episode_dir, intent_path=intent_path, truth_path=truth_path, minimum_frames=minimum_frames)
                geometry = index_by_world.get(world_id, {}).get("view_geometry", {})
                view_counts = {view: int(smoke.get("views", {}).get(view, {}).get("observed_frame_count", 0)) for view in REQUIRED_VIEWS}
                missing_views = [view for view, count in view_counts.items() if count == 0]
                short_ok = _short_side_ok(geometry)
                issues = []
                if missing_views:
                    issues.append("缺视角: " + ", ".join(missing_views))
                low_frames = [f"{view}={count}" for view, count in view_counts.items() if 0 < count < minimum_frames]
                if low_frames:
                    issues.append("观察帧不足: " + ", ".join(low_frames))
                if not smoke["checks"].get("ground_distance_within_25pct", False):
                    issues.append("目标距离不在设计距离 ±25%")
                if not short_ok:
                    issues.append("index 短边设计值不在声明档位")
                complete_reports.append({
                    "key": key,
                    "world_id": world_id,
                    "episode_id": episode.get("episode_id"),
                    "task_text": episode.get("task_text"),
                    "path": str(episode_dir),
                    "passed": bool(smoke.get("passed") and short_ok),
                    "view_counts": view_counts,
                    # The index ground_distance_m is measured from the world
                    # home after scene translation.  The smoke check compares
                    # camera-to-target distance, which remains invariant when
                    # target and observation waypoints shift together.
                    "designed_ground_distance_m": smoke.get("designed_ground_distance_m"),
                    "observed_ground_distance_mean_m": smoke.get("observed_ground_distance_mean_m"),
                    "short_side_band": geometry.get("target_short_side_px"),
                    "short_side_estimate_px": geometry.get("target_short_side_px_estimate"),
                    "short_side_design_ok": short_ok,
                    "issues": issues,
                })
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                complete_reports.append({"key": key, "world_id": world_id, "passed": False, "issues": [f"审计异常: {type(exc).__name__}: {exc}"]})
        elif status == "failed":
            logical_failed.append({"key": key, "world_id": world_id, "attempts_total": entry.get("attempts_total")})

    for key, path, episode in _all_failed_artifacts(episodes_root):
        failed_artifacts.append({
            "key": key,
            "world_id": _world_id(key, path),
            "path": str(path.parent),
            "failure": episode.get("failure"),
            "failure_group": _failure_group(episode.get("failure")),
        })

    world_reports = []
    for world_id, spec in sorted(index_by_world.items()):
        complete = [row for row in complete_reports if row["world_id"] == world_id]
        qualified = [row for row in complete if row.get("passed")]
        logical_fail = [row for row in logical_failed if row["world_id"] == world_id]
        artifact_fail = [row for row in failed_artifacts if row["world_id"] == world_id]
        issues = sorted({issue for row in complete if not row.get("passed") for issue in row.get("issues", [])})
        if not qualified and not issues:
            issues.append("没有合格 complete 架次")
        world_reports.append({
            "world_id": world_id,
            "split": spec.get("split"),
            "target_category": spec.get("target_combo", {}).get("category"),
            "paired_confusers": spec.get("paired_confusers", []),
            "target_short_side_px": spec.get("view_geometry", {}).get("target_short_side_px"),
            "target_short_side_px_estimate": spec.get("view_geometry", {}).get("target_short_side_px_estimate"),
            "complete_count": len(complete),
            "qualified_count": len(qualified),
            "logical_failed_count": len(logical_fail),
            "failed_artifact_count": len(artifact_fail),
            "failure_groups": dict(Counter(row["failure_group"] for row in artifact_fail)),
            "issues": issues,
        })

    task_counter = Counter(row.get("task_text") for row in complete_reports if row.get("passed"))
    qualified_worlds = sorted({row["world_id"] for row in complete_reports if row.get("passed")})
    failure_groups = Counter(row["failure_group"] for row in failed_artifacts)
    pickup_task = sum(1 for row in complete_reports if row.get("passed") and "皮卡" in str(row.get("task_text")))
    suv_task = sum(1 for row in complete_reports if row.get("passed") and "SUV" in str(row.get("task_text")))
    category_by_world = {row["world_id"]: row["target_category"] for row in world_reports}
    pickup_to_suv = sum(
        1 for row in complete_reports
        if row.get("passed") and category_by_world.get(row["world_id"]) == "pickup" and "SUV" in str(row.get("task_text"))
    )
    report = {
        "generated_on": date.today().isoformat(),
        "minimum_frames": minimum_frames,
        "logical_entry_count": len(round_state_data.get("episodes", {})),
        "logical_complete_count": len(complete_reports),
        "qualified_episode_count": sum(1 for row in complete_reports if row.get("passed")),
        "qualified_world_count": len(qualified_worlds),
        "qualified_worlds": qualified_worlds,
        "complete_reports": complete_reports,
        "world_reports": world_reports,
        "logical_failed_count": len(logical_failed),
        "failed_artifact_count": len(failed_artifacts),
        "failure_groups": dict(failure_groups),
        "logical_failed": logical_failed,
        "failed_artifacts": failed_artifacts,
        "qualified_task_texts": dict(task_counter),
        "pickup_task_qualified_count": pickup_task,
        "suv_task_qualified_count": suv_task,
        "pickup_to_suv_rejection_coverage": pickup_to_suv,
        "index_world_count": len(index_by_world),
        "unqualified_worlds": [row["world_id"] for row in world_reports if not row["qualified_count"]],
    }
    if sft_dir is not None:
        report["sft"] = audit_sft_additions(sft_dir)
    return report


def _readiness_line(report: dict[str, Any]) -> str:
    total = report.get("index_world_count") or 0
    qualified = report["qualified_world_count"]
    sft = report.get("sft")
    pickup_to_suv = (sft.get("pickup_to_suv_reject_count") if sft else report.get("pickup_to_suv_rejection_coverage")) or 0
    gaps = []
    if qualified < total:
        gaps.append(f"仍有 {total - qualified} 个 world 没有合格架次（{', '.join(report.get('unqualified_worlds') or [])}）")
    if not pickup_to_suv:
        gaps.append("pickup world 上没有合格的 SUV 请求架次（pickup→suv 拒绝方向未覆盖）")
    pickup_label = "SFT pickup→suv 拒绝行" if sft else "pickup→suv 合格架次"
    head = f"合格覆盖 {qualified}/{total} world、{report['qualified_episode_count']} 个 episode；{pickup_label} {pickup_to_suv} 个。"
    if gaps:
        return head + "尚未满足完整采集：" + "；".join(gaps) + "。已有合格架次可作为增量数据。"
    return head + "覆盖完整，可以进入 annotate/build_sft。"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# r21id-r4 已采集架次质量审计",
        "",
        f"审计日期：{report['generated_on']}。审计只读取 artifacts/r21id-r4c、round-state、r21id-r4 index 与场景 truth/intent；观察帧门槛为每视角至少 {report['minimum_frames']} 帧。",
        "",
        "## 结论",
        "",
        f"round-state 有 {report['logical_entry_count']} 个逻辑架次，其中 status=complete {report['logical_complete_count']} 个；逐帧审计后合格 {report['qualified_episode_count']} 个，覆盖 {report['qualified_world_count']} 个 world（{', '.join(report['qualified_worlds']) or '无'}）。",
        "",
        f"失败逻辑架次 {report['logical_failed_count']} 个；对应失败 episode artifact {report['failed_artifact_count']} 个。短边档位这里只能核对 index 的设计 estimate（采集 steps 没有 bbox/短边字段），不会把设计值冒充实测像素。",
        "",
        _readiness_line(report),
        "",
        "## 按世界",
        "",
        "| world | 目标/干扰物 | complete | 合格 | 失败逻辑/失败 artifact | index 短边档 | 问题 |",
        "|---|---|---:|---:|---:|---|---|",
    ]
    for row in report["world_reports"]:
        confusers = ", ".join(row["paired_confusers"]) or "-"
        failures = f"{row['logical_failed_count']}/{row['failed_artifact_count']}"
        issues = "；".join(row["issues"]) or "-"
        lines.append(f"| {row['world_id']} | {row['target_category']} / {confusers} | {row['complete_count']} | {row['qualified_count']} | {failures} | {row['target_short_side_px']}（设计 {row['target_short_side_px_estimate']} px） | {issues} |")
    lines += ["", "## 合格架次明细", "", "| 架次 | world | 任务 | 四视角观察帧 | 设计距离/观测均值（m） | 短边设计档 |", "|---|---|---|---|---:|---|"]
    for row in report["complete_reports"]:
        if not row.get("passed"):
            continue
        counts = ", ".join(f"{view}={row['view_counts'][view]}" for view in REQUIRED_VIEWS)
        distance = f"{row['designed_ground_distance_m']:.1f}/{row['observed_ground_distance_mean_m']:.1f}"
        lines.append(f"| {row['key']} | {row['world_id']} | {row.get('task_text', '-')} | {counts} | {distance} | {row['short_side_band']}（{row['short_side_estimate_px']} px） |")
    sft = report.get("sft")
    if sft:
        lines += ["", "## SFT 交换任务覆盖", "", f"读取目录：`{sft['dir']}`；manifest schema_version={sft['manifest'].get('schema_version')}。", "", "| split | 方向 | view | reject | positive |", "|---|---|---|---:|---:|"]
        for split in SFT_SPLITS:
            for pair in sorted(sft["counts"].get(split, {})):
                for view in REQUIRED_VIEWS:
                    values = sft["counts"][split][pair].get(view)
                    if values:
                        lines.append(f"| {split} | {pair} | {view} | {values['reject']} | {values['positive']} |")
        lines += ["", "| 方向 | 四视角 reject pair | 四视角 positive pair | acceptance（每方向 ≥30） |", "|---|---:|---:|---|"]
        for pair, target in sorted(sft["acceptance_targets"].items()):
            reject_pairs = sum(sft["four_view_pairs"].get(split, {}).get(pair, {}).get("reject", 0) for split in SFT_SPLITS)
            positive_pairs = sum(sft["four_view_pairs"].get(split, {}).get(pair, {}).get("positive", 0) for split in SFT_SPLITS)
            lines.append(f"| {pair} | {reject_pairs} | {positive_pairs} | {'满足' if target['met'] else '未满足'} |")
    lines += ["", "## 失败原因", "", "| 归一化原因 | 失败 artifact 数 |", "|---|---:|"]
    for reason, count in sorted(report["failure_groups"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {reason} | {count} |")
    lines += ["", "失败原因按 world 交叉："]
    for row in report["world_reports"]:
        if row["failure_groups"]:
            details = ", ".join(f"{key}={value}" for key, value in sorted(row["failure_groups"].items()))
            lines.append(f"- {row['world_id']}：{details}。")
    lines += ["", "## 重新生成或修改清单", ""]
    for row in report["world_reports"]:
        if not row["qualified_count"]:
            lines.append(f"- {row['world_id']}：没有合格 complete 架次（失败原因：{', '.join(sorted(row['failure_groups'])) or '无失败 artifact'}），需修复后重采。")
        elif row["issues"]:
            lines.append(f"- {row['world_id']}：有合格架次，但部分架次有问题：{'；'.join(row['issues'])}。")
    lines += ["", "## 审计口径", "", "- complete 架次按 round-state 的最终 path 审计；失败原因按对应 world/ep 下所有 status=failed 的 episode.json artifact 统计，同时保留逻辑失败架次数量。", "- 每个视角只计 phase=observe、target_in_fov=true 且 RGB 文件存在的帧；距离使用现有 smoke 检查的目标地面距离 ±25% 判定。", "- index 短边档检查的是 target_short_side_px_estimate 是否落在声明档位；steps 没有目标 bbox，因此未声称实测短边像素合格。", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes-root", type=Path, default=Path("artifacts/r21id-r4c/episodes"))
    parser.add_argument("--round-state", type=Path, default=Path("artifacts/r21id-r4c/pipeline/round-state.json"))
    parser.add_argument("--index", type=Path, default=Path("configs/scenarios/generated-r21id-r4/index.json"))
    parser.add_argument("--scenarios", type=Path, default=Path("configs/scenarios/generated-r21id-r4"))
    parser.add_argument("--minimum-frames", type=int, default=8)
    parser.add_argument("--sft-dir", type=Path, help="built SFT additions directory containing train/val/test.jsonl and manifest.json")
    parser.add_argument("--output-md", type=Path)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args(argv)
    report = audit_collection(episodes_root=args.episodes_root, round_state=args.round_state, index_path=args.index, scenarios=args.scenarios, minimum_frames=args.minimum_frames, sft_dir=args.sft_dir)
    if args.output_md:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(render_markdown(report), encoding="utf-8")
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {key: report[key] for key in ("logical_entry_count", "logical_complete_count", "qualified_episode_count", "qualified_world_count", "logical_failed_count", "failed_artifact_count", "failure_groups")}
    if report.get("sft"):
        summary["sft"] = report["sft"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
