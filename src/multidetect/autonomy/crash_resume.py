"""Crash-resume checkpoints and fail-closed restart decisions.

The format is deliberately small and boring: one JSON document replaced atomically.
It is an audit record, not a second mission database.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

MAX_CHECKPOINT_BYTES = 64 * 1024


@dataclass(frozen=True)
class RecoveryDecision:
    action: str  # resume, new_mission, return_home, land
    reason_zh: str
    checkpoint: dict[str, Any] | None = None


def _valid_checkpoint(value: Any, *, now: float, max_age_s: float,
                      mission_id: str, package_digest: str) -> tuple[bool, str]:
    if not isinstance(value, dict):
        return False, "检查点不是对象"
    required = ("mission_id", "package_digest", "step_id", "phase", "sequence")
    if any(key not in value for key in required):
        return False, "检查点字段不完整"
    if value["mission_id"] != mission_id or value["package_digest"] != package_digest:
        return False, "检查点任务包身份不匹配"
    sequence = value["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        return False, "检查点序号无效"
    stamp = value.get("saved_at")
    if not isinstance(stamp, (int, float)) or not float(stamp) <= now:
        return False, "检查点时间戳无效"
    if now - float(stamp) > max_age_s:
        return False, "检查点已过期"
    return True, "有效检查点"


def decide_recovery(*, landed: bool | None = None, armed: bool | None = None,
                    checkpoint: Mapping[str, Any] | None, mission_id: str,
                    package_digest: str | None = None, package_sha256: str | None = None,
                    now: float | None = None, max_age_s: float = 60.0,
                    rtl_available: bool = True, recovery_available: bool | None = None,
                    landed_state: int | None = None, vehicle: Any | None = None,
                    nav_state: str | None = None, boot_time_ms: int | float | None = None,
                    resume_after_failsafe: bool = False) -> RecoveryDecision:
    """Pure restart policy. Unknown vehicle state is treated as airborne risk."""
    now = time.time() if now is None else float(now)
    if vehicle is not None:
        landed = getattr(vehicle, "landed", landed)
        armed = getattr(vehicle, "armed", armed)
        landed_state = getattr(vehicle, "landed_state", landed_state)
    if package_digest is None:
        package_digest = package_sha256
    if recovery_available is not None:
        rtl_available = recovery_available
    airborne = armed is True or landed is False or landed_state in {2, 3, 4}
    if not airborne and landed is True and armed is False:
        return RecoveryDecision("new_mission", "飞机在地面，按正常新任务流程启动")
    if airborne:
        if checkpoint is not None:
            ok, reason = _valid_checkpoint(checkpoint, now=now, max_age_s=max_age_s,
                                           mission_id=mission_id, package_digest=package_digest)
            if ok and checkpoint.get("boot_time_ms") is not None and boot_time_ms is None:
                return RecoveryDecision("monitor_failsafe", "PX4 boot time 未知，onboard 不接管，仅记录与监视", dict(checkpoint))
            if ok and boot_time_ms is not None and checkpoint.get("boot_time_ms") is not None and float(boot_time_ms) < float(checkpoint["boot_time_ms"]):
                ok, reason = False, "PX4 已重启，boot time 不匹配"
            mode = nav_state.upper() if isinstance(nav_state, str) else None
            if ok and (mode is None or mode.startswith("PX4(")):
                return RecoveryDecision("monitor_failsafe", "飞行模式未知，onboard 不接管，仅记录与监视", dict(checkpoint))
            if ok and mode in {"RTL", "LAND", "AUTO_RTL", "AUTO_LAND", "FAILSAFE"} and not resume_after_failsafe:
                return RecoveryDecision("monitor_failsafe", "PX4 失效保护已接管，onboard 只记录与监视", dict(checkpoint))
            if ok and mode not in {"HOLD", "LOITER", "AUTO_LOITER", "OFFBOARD"} and not resume_after_failsafe:
                return RecoveryDecision("monitor_failsafe", "飞行模式不在允许恢复集合，onboard 仅记录与监视", dict(checkpoint))
            if ok:
                return RecoveryDecision("resume", "飞机在空中且检查点有效，继续执行", dict(checkpoint))
        else:
            reason = "检查点缺失"
        action = "return_home" if rtl_available else "land"
        suffix = "返航" if rtl_available else "按机型 recovery policy 降落"
        return RecoveryDecision(action, f"飞机在空中但{reason}，安全{suffix}")
    action = "return_home" if rtl_available else "land"
    return RecoveryDecision(action, "飞机状态未知，无法安全恢复，执行安全动作")


class CheckpointStore:
    """Bounded atomic checkpoint file. Sequence regressions are rejected."""
    def __init__(self, path: str | Path, *, max_bytes: int = MAX_CHECKPOINT_BYTES) -> None:
        self.path = Path(path)
        self.max_bytes = min(int(max_bytes), MAX_CHECKPOINT_BYTES)

    def save(self, document: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(document)
        payload.setdefault("schema", "multidetect.crash-resume.v1")
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(encoded) > self.max_bytes:
            raise ValueError("检查点超过大小上限")
        previous = self.load()
        if previous is not None and int(payload.get("sequence", 0)) <= int(previous.get("sequence", 0)):
            raise ValueError("检查点序号必须单调递增")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        return payload

    def load(self) -> dict[str, Any] | None:
        try:
            if self.path.stat().st_size > self.max_bytes:
                return None
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None


def checkpoint_from_state(*, mission_id: str, package_digest: str, step_id: str,
                          phase: str, sequence: int, counts: Mapping[str, int] | None = None,
                          alerts: list[Mapping[str, Any]] | None = None,
                          visited: list[Any] | None = None, home: Any = None,
                          elapsed_s: float = 0.0, energy: Mapping[str, Any] | None = None,
                          provenance: Mapping[str, Any] | None = None,
                          boot_time_ms: int | float | None = None) -> dict[str, Any]:
    return {"schema": "multidetect.crash-resume.v1", "mission_id": mission_id,
            "package_digest": package_digest, "step_id": step_id, "phase": phase,
            "sequence": int(sequence), "saved_at": time.time(), "boot_time_ms": boot_time_ms,
            "counts": dict(counts or {}), "alerts": list(alerts or []),
            "visited": list(visited or []), "home": home, "elapsed_s": float(elapsed_s),
            "energy": dict(energy or {}), "provenance": dict(provenance or {})}


# Small functional aliases keep the API convenient for harnesses and adapters.
def write_checkpoint(path: str | Path, document: Mapping[str, Any]) -> dict[str, Any]:
    return CheckpointStore(path).save(document)


def load_checkpoint(path: str | Path, *, max_bytes: int = MAX_CHECKPOINT_BYTES) -> dict[str, Any] | None:
    return CheckpointStore(path, max_bytes=max_bytes).load()
