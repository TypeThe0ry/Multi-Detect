#!/usr/bin/env python3
"""Read-only evidence checker for an onboard climb-fault SITL episode.

The checker only reads JSON/JSONL artifacts.  It deliberately keeps PX4
MAVLink observations separate from onboard mission-log semantics: an onboard
``land_now`` request is not a PX4 mode acknowledgement, and a collector
summary is not substituted for a PX4 message.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable


PX4 = "provable_from_px4_mavlink"
ONBOARD = "provable_only_from_onboard_logs"
MISSING = "missing"

_POINTS = (
    ("px4_boot_time_advancing", "PX4 boot time advancing"),
    ("low_altitude_airborne_before_abort", "low-altitude airborne before abort"),
    ("two_climb_timeouts_no_altitude_gain", "two climb timeouts with no altitude gain"),
    ("no_third_climb", "no third climb"),
    ("failure_event_alert", "failure event/alert"),
    ("land_now_handoff", "land_now handoff"),
    ("land_mode_switch_ack", "LAND mode switch ACK"),
    ("land_mode", "LAND mode"),
    ("fresh_landed_state_1", "fresh landed_state=1"),
    ("disarm", "disarm"),
)


def _read_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {path}:{line_number}: {exc}") from exc
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _first(root: Path, pattern: str) -> Path | None:
    direct = root / pattern
    if direct.is_file():
        return direct
    matches = sorted(path for path in root.rglob(pattern) if path.is_file())
    return matches[0] if matches else None


def _paths(root: Path) -> dict[str, Path | None]:
    if root.is_file():
        root = root.parent
    return {
        "root": root,
        "mavlink": _first(root, "mavlink.jsonl"),
        "steps": _first(root, "steps.jsonl"),
        "mission_log": _first(root, "mission_log/mission.jsonl"),
        "summary": _first(root, "mission_log/summary.json"),
    }


def _message_type(row: dict[str, Any]) -> str | None:
    value = row.get("message_type") or row.get("mavpackettype")
    return str(value) if value is not None else None


def _number(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _event_time(row: dict[str, Any]) -> float | None:
    for key in ("receive_wall_time", "receive_monotonic_ns", "time_boot_ms"):
        value = _number(row.get(key))
        if value is not None:
            return value
    return None


def _point(point_id: str, label: str, classification: str, evidence: list[dict[str, Any]], detail: str) -> dict[str, Any]:
    return {
        "id": point_id,
        "label": label,
        "class": classification,
        "class_label": {
            PX4: "可由 PX4 MAVLink 证明",
            ONBOARD: "只能由机载日志证明",
            MISSING: "缺失",
        }[classification],
        "evidence": evidence,
        "detail": detail,
    }


def _evidence(path: Path | None, root: Path, **fields: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"file": str(path.relative_to(root)) if path and path.is_relative_to(root) else str(path) if path else None}
    item.update(fields)
    return item


def _land_mode_index(rows: list[dict[str, Any]]) -> int | None:
    for index, row in enumerate(rows):
        if _message_type(row) == "HEARTBEAT" and row.get("custom_mode") == 100925440:
            return index
    return None


def _timeout_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for row in steps:
        action = row.get("action") or {}
        outcome = row.get("outcome") or {}
        ack = row.get("ack") or {}
        if (
            action.get("kind") == "translate"
            and _number(action.get("up_m")) is not None
            and action.get("up_m", 0) > 0
            and outcome.get("status") == "timeout"
            and ack.get("sim_fault_injection") == "return_home_climb_timeout"
        ):
            before = row.get("obs_before") or {}
            after = row.get("obs_after") or {}
            before_pose = before.get("pose") or {}
            after_pose = after.get("pose") or {}
            before_ned = before_pose.get("local_ned_m") or []
            after_ned = after_pose.get("local_ned_m") or []
            gain = None
            if len(before_ned) >= 3 and len(after_ned) >= 3:
                before_z = _number(before_ned[2])
                after_z = _number(after_ned[2])
                if before_z is not None and after_z is not None:
                    gain = -(after_z - before_z)
            found.append({"row": row, "gain_m": gain})
    return found


def check_climb_fault_evidence(artifact: str | Path) -> dict[str, Any]:
    """Return a three-class report without creating or changing any file."""

    paths = _paths(Path(artifact).resolve())
    root = paths["root"]  # type: ignore[assignment]
    mavlink = _read_jsonl(paths["mavlink"])
    steps = _read_jsonl(paths["steps"])
    mission = _read_jsonl(paths["mission_log"])
    summary = _read_json(paths["summary"])
    points: list[dict[str, Any]] = []

    local = [row for row in mavlink if _message_type(row) == "LOCAL_POSITION_NED"]
    boot = [_number(row.get("time_boot_ms")) for row in local]
    boot = [value for value in boot if value is not None]
    boot_advancing = len(boot) > 1 and all(later >= earlier for earlier, later in zip(boot, boot[1:])) and max(boot) > min(boot)
    points.append(_point(
        "px4_boot_time_advancing", _POINTS[0][1], PX4 if boot_advancing else MISSING,
        [_evidence(paths["mavlink"], root, message_type="LOCAL_POSITION_NED", first_time_boot_ms=boot[0], last_time_boot_ms=boot[-1], samples=len(boot))] if boot_advancing else [],
        f"LOCAL_POSITION_NED time_boot_ms 从 {boot[0]:g} 推进到 {boot[-1]:g}。" if boot_advancing else "没有可证明单调推进的 LOCAL_POSITION_NED time_boot_ms。",
    ))

    first_timeout_boot = None
    timeout_candidates = _timeout_steps(steps)
    if timeout_candidates:
        pose = (timeout_candidates[0]["row"].get("obs_before") or {}).get("pose") or {}
        first_timeout_boot = _number(pose.get("time_boot_ms"))
    latest_landed: int | None = None
    low_sample = None
    for row in mavlink:
        if _message_type(row) == "EXTENDED_SYS_STATE" and isinstance(row.get("landed_state"), int):
            latest_landed = row["landed_state"]
        if _message_type(row) == "LOCAL_POSITION_NED":
            altitude = _number(row.get("z"))
            altitude = -altitude if altitude is not None else None
            boot_ms = _number(row.get("time_boot_ms"))
            if altitude is not None and altitude <= 2.5 and latest_landed == 2 and (first_timeout_boot is None or (boot_ms is not None and boot_ms <= first_timeout_boot)):
                low_sample = (row, altitude)
    points.append(_point(
        "low_altitude_airborne_before_abort", _POINTS[1][1], PX4 if low_sample else MISSING,
        [_evidence(paths["mavlink"], root, message_type="LOCAL_POSITION_NED", time_boot_ms=low_sample[0].get("time_boot_ms"), altitude_m=-low_sample[0].get("z", 0), landed_state=2)] if low_sample else [],
        f"在第一次 timeout 前观测到约 {low_sample[1]:.3f} m 且 landed_state=2。" if low_sample else "没有在 abort/第一次爬升 timeout 前找到低空 airborne 的 PX4 样本。",
    ))

    valid_timeouts = [item for item in timeout_candidates if item["gain_m"] is not None and item["gain_m"] < 0.5]
    two_timeouts = len(valid_timeouts) >= 2
    points.append(_point(
        "two_climb_timeouts_no_altitude_gain", _POINTS[2][1], ONBOARD if two_timeouts else MISSING,
        [_evidence(paths["steps"], root, step_index=item["row"].get("step_index"), outcome="timeout", altitude_gain_m=item["gain_m"]) for item in valid_timeouts[:2]] if two_timeouts else [],
        "steps.jsonl 记录两次纯上升 timeout，且每次高度增益小于 0.5 m；这是机载步骤/结果语义。" if two_timeouts else "没有两次带有可计算低增益的机载爬升 timeout。",
    ))

    third_climb = [row for row in steps if (row.get("action") or {}).get("kind") == "translate" and _number((row.get("action") or {}).get("up_m")) is not None and (row.get("action") or {}).get("up_m", 0) > 0]
    no_third = two_timeouts and len(third_climb) == 2
    points.append(_point(
        "no_third_climb", _POINTS[3][1], ONBOARD if no_third else MISSING,
        [_evidence(paths["steps"], root, climb_step_count=len(third_climb))] if no_third else [],
        "机载步骤中只有两次上升 primitive，第二次失败后没有第三次。" if no_third else "不能从机载步骤证明不存在第三次爬升。",
    ))

    failure_event = any(row.get("kind") == "climb_failed_suspected_loss_of_control" for row in mission)
    failure_alert = any(row.get("kind") == "alert" and row.get("value") == "climb_failed_suspected_loss_of_control" for row in mission)
    points.append(_point(
        "failure_event_alert", _POINTS[4][1], ONBOARD if failure_event and failure_alert else MISSING,
        [_evidence(paths["mission_log"], root, kind="climb_failed_suspected_loss_of_control"), _evidence(paths["mission_log"], root, kind="alert", value="climb_failed_suspected_loss_of_control")] if failure_event and failure_alert else [],
        "mission.jsonl 同时记录 failure event 和对应 alert。" if failure_event and failure_alert else "没有同时找到 failure event 与对应 alert。",
    ))

    handoff = any(row.get("kind") == "escalation" and row.get("action") == "land_now" for row in mission)
    terminal_action = ((summary.get("summary") or {}).get("terminal_action"))
    handoff = handoff or terminal_action == "land_now"
    points.append(_point(
        "land_now_handoff", _POINTS[5][1], ONBOARD if handoff else MISSING,
        [_evidence(paths["mission_log"], root, kind="escalation", action="land_now")] if handoff else [],
        "机载日志记录 supervisor 的 land_now handoff；它不是 PX4 ACK。" if handoff else "没有机载 land_now handoff。",
    ))

    land_ack_index = None
    for index, row in enumerate(mavlink):
        if not (
            _message_type(row) == "COMMAND_ACK"
            and row.get("command") == 176
            and row.get("result") == 0
        ):
            continue
        ack_time = _event_time(row)
        for later in mavlink[index + 1:]:
            if _message_type(later) != "HEARTBEAT" or later.get("custom_mode") != 100925440:
                continue
            mode_time = _event_time(later)
            if ack_time is not None and mode_time is not None and 0 <= mode_time - ack_time <= 5:
                land_ack_index = index
                break
        if land_ack_index is not None:
            break
    points.append(_point(
        "land_mode_switch_ack", _POINTS[6][1], PX4 if land_ack_index is not None else MISSING,
        [_evidence(paths["mavlink"], root, message_type="COMMAND_ACK", command=176, result=0)] if land_ack_index is not None else [],
        "MAV_CMD_DO_SET_MODE 的 PX4 COMMAND_ACK=result=0 后出现 LAND 模式 heartbeat。" if land_ack_index is not None else "没有能与 LAND 模式确认关联的 command 176 ACK。",
    ))

    land_mode_index = _land_mode_index(mavlink)
    points.append(_point(
        "land_mode", _POINTS[7][1], PX4 if land_mode_index is not None else MISSING,
        [_evidence(paths["mavlink"], root, message_type="HEARTBEAT", custom_mode=100925440)] if land_mode_index is not None else [],
        "PX4 heartbeat custom_mode=100925440，且对应 LAND 模式窗口。" if land_mode_index is not None else "没有 PX4 LAND 模式 heartbeat。",
    ))

    fresh_landed_index = None
    if land_mode_index is not None:
        fresh_landed_index = next((index for index, row in enumerate(mavlink[land_mode_index + 1:], land_mode_index + 1) if _message_type(row) == "EXTENDED_SYS_STATE" and row.get("landed_state") == 1), None)
    points.append(_point(
        "fresh_landed_state_1", _POINTS[8][1], PX4 if fresh_landed_index is not None else MISSING,
        [_evidence(paths["mavlink"], root, message_type="EXTENDED_SYS_STATE", landed_state=1, fresh_after_land_mode=True)] if fresh_landed_index is not None else [],
        "LAND 模式之后出现新的 EXTENDED_SYS_STATE landed_state=1。" if fresh_landed_index is not None else "没有 LAND 模式之后的新 landed_state=1。",
    ))

    disarm_index = None
    if fresh_landed_index is not None:
        disarm_index = next((index for index, row in enumerate(mavlink[fresh_landed_index + 1:], fresh_landed_index + 1) if _message_type(row) == "HEARTBEAT" and isinstance(row.get("base_mode"), int) and not (row["base_mode"] & 128)), None)
    points.append(_point(
        "disarm", _POINTS[9][1], PX4 if disarm_index is not None else MISSING,
        [_evidence(paths["mavlink"], root, message_type="HEARTBEAT", base_mode="armed bit clear", fresh_after_landed_state=True)] if disarm_index is not None else [],
        "fresh landed_state=1 之后 heartbeat 的 MAV_MODE_FLAG_SAFETY_ARMED 位清除。" if disarm_index is not None else "没有 fresh landed_state=1 之后的 PX4 disarm heartbeat。",
    ))

    counts = {classification: sum(point["class"] == classification for point in points) for classification in (PX4, ONBOARD, MISSING)}
    return {
        "checker": "onboard-climb-px4-evidence-checker",
        "read_only": True,
        "artifact": str(root),
        "inputs": {name: str(path.relative_to(root)) if path and path.is_relative_to(root) else str(path) if path else None for name, path in paths.items() if name != "root"},
        "counts": counts,
        "points": points,
    }


evaluate_artifact = check_climb_fault_evidence


def _print_summary(report: dict[str, Any]) -> None:
    print(f"证据目录: {report['artifact']}")
    print(f"分类计数: PX4={report['counts'][PX4]} 机载日志={report['counts'][ONBOARD]} 缺失={report['counts'][MISSING]}")
    for point in report["points"]:
        print(f"[{point['class_label']}] {point['label']}: {point['detail']}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读检查 climb-fault SITL 的 PX4/机载证据边界")
    parser.add_argument("artifact", type=Path, help="artifact 根目录、episode 目录或包含 JSONL 的目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args(argv)
    report = check_climb_fault_evidence(args.artifact)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
