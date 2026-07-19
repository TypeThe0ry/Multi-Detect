from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "three-mode-multitarget-system.md"


def _specification() -> str:
    return SPEC.read_text(encoding="utf-8")


def test_three_modes_share_one_perception_and_tracking_contract() -> None:
    specification = _specification()

    assert "模式 1：巡检" in specification
    assert "模式 2：灭火载荷投放仿真" in specification
    assert "模式 3：高楼/消耗性平台接近仿真" in specification
    assert "至少同时维持 10 条轨迹" in specification
    assert "切换主目标不能删除其他轨迹" in specification
    assert "OCCLUDED -> REACQUIRING -> RECOVERED" in specification
    assert "不能为了连续显示而绑定到相似目标" in specification


def test_mode_specific_target_and_execution_boundaries_are_explicit() -> None:
    specification = _specification()

    assert "人员、烟雾和普通车辆必须强制禁投" in specification
    assert "TARGET_NOT_PAYLOAD_ELIGIBLE" in specification
    assert "自动检测 -> 人工选择 -> 投放资格解析 -> 滑动确认" in specification
    assert "实际火区瞄准目标 ID/修订号" in specification
    assert "车辆/建筑框本身不是落点" in specification
    assert "当前硬件传感器基线只有一台 RGB RTSP 摄像头" in specification
    assert "热像检测、RGB/热像融合" in specification
    assert "热成像热点类别" in specification
    assert "独立火情复核模型确认" in specification
    assert "包括建筑、火点、人员和车辆" in specification
    assert "模式 3 自动识别火、烟、人、车、建筑等候选" in specification
    assert "不可复用的滑动确认" in specification
    assert "ABORT_CLIMB_SIM" in specification
    assert "不得携带真实飞控或舵机命令" in specification
    assert "`MULTIDETECT_FLIGHT_CONTROL_WRITES=0`" in specification
    assert "`MULTIDETECT_PHYSICAL_RELEASE=0`" in specification


def test_monocular_avoidance_is_fail_closed_and_does_not_claim_metric_depth() -> None:
    specification = _specification()

    assert "单目模块是风险估计器，不宣称提供精确绝对深度" in specification
    assert "金字塔 LK 光流" in specification
    assert "旋转去除" in specification
    assert "碰撞时间 `TTC`" in specification
    assert "CLEAR / CAUTION / AVOID / INVALID" in specification
    assert "输出 `INVALID`" in specification
    assert "不低于 20 Hz" in specification
    assert "不超过 25 ms/帧" in specification


def test_ranging_and_acceptance_metrics_are_measurable() -> None:
    specification = _specification()

    assert "95% 置信区间" in specification
    assert "VALID / DEGRADED / INVALID" in specification
    assert "不超过 250 ms" in specification
    assert "不超过 400 ms" in specification
    assert "不超过 0.5 s" in specification
    assert "不超过 2 s" in specification
    assert "60 分钟无持续帧积压或内存增长" in specification
