#!/usr/bin/env python3
"""多旋翼悬停平移方向诊断：指令方位与实际位移方位的夹角。

连接一个已经在运行的 PX4 SITL（MAVLink），OFFBOARD 起飞到固定高度后依次向
北/东/南/西各平移一段，记录 EKF 的 LOCAL_POSITION_NED、ATTITUDE 和
ESTIMATOR_STATUS，输出每一段的

* ``initial_velocity_bearing_deg``：起步 ``--initial-window`` 秒内的平均速度方位，
  不受后续位置反馈影响，最能反映控制映射；
* ``net_displacement_bearing_deg``：整段净位移方位；
* ``*_error_deg``：与指令方位的有符号夹角（顺时针为正，范围 -180..180）。

给定 ``--ulog`` 时再用 PX4 日志里的 ``*_groundtruth`` 话题对照 EKF 偏航和真值偏航，
区分「EKF 航向有偏差」和「推力方向本身被转了」。验收标准：每段夹角绝对值 < 10°。

不启动 GPU 作业；只需要一个已经起来的 SITL，所以由算力线程排期。示例::

    python scripts/diagnose_mc_translation.py --connect udpin:0.0.0.0:14540 \
        --out /tmp/mc-translation.json --ulog /path/to/log.ulg
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

TOLERANCE_DEG = 10.0
DEFAULT_LEG_BEARINGS_DEG = (0.0, 90.0, 180.0, 270.0)


def wrap_deg(angle: float) -> float:
    """折到 (-180, 180]。"""
    wrapped = (angle + 180.0) % 360.0 - 180.0
    return 180.0 if wrapped == -180.0 else wrapped


def bearing_deg(north: float, east: float) -> float:
    """NED 方位角：从正北顺时针，0..360。"""
    return math.degrees(math.atan2(east, north)) % 360.0


def signed_angle_deg(actual: float, command: float) -> float:
    """实际方位相对指令方位的夹角，顺时针为正。"""
    return wrap_deg(actual - command)


def quaternion_yaw_deg(w: float, x: float, y: float, z: float) -> float:
    """机体系 FRD→NED 四元数的偏航角（度，0..360）。"""
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return math.degrees(yaw) % 360.0


@dataclass(frozen=True)
class Sample:
    t: float
    north: float
    east: float
    down: float
    vn: float = 0.0
    ve: float = 0.0
    yaw_deg: float | None = None


@dataclass
class LegResult:
    index: int
    command_bearing_deg: float
    command_distance_m: float
    start: tuple[float, float, float]
    end: tuple[float, float, float]
    initial_velocity_bearing_deg: float | None
    net_displacement_bearing_deg: float | None
    net_displacement_m: float
    initial_error_deg: float | None
    net_error_deg: float | None
    yaw_start_deg: float | None
    yaw_end_deg: float | None
    reached: bool
    samples: int

    def worst_error_deg(self) -> float | None:
        errors = [
            abs(value)
            for value in (self.initial_error_deg, self.net_error_deg)
            if value is not None
        ]
        return max(errors) if errors else None


def analyze_leg(
    index: int,
    command_bearing: float,
    command_distance: float,
    samples: Sequence[Sample],
    *,
    initial_window_s: float = 3.0,
    min_speed_mps: float = 0.3,
    min_displacement_m: float = 1.0,
    reached_radius_m: float = 2.0,
) -> LegResult:
    if len(samples) < 2:
        raise ValueError("a leg needs at least two samples")
    first, last = samples[0], samples[-1]
    dn, de = last.north - first.north, last.east - first.east
    net_distance = math.hypot(dn, de)
    net_bearing = bearing_deg(dn, de) if net_distance >= min_displacement_m else None

    window = [s for s in samples if s.t - first.t <= initial_window_s]
    vn = sum(s.vn for s in window) / len(window)
    ve = sum(s.ve for s in window) / len(window)
    initial_bearing = bearing_deg(vn, ve) if math.hypot(vn, ve) >= min_speed_mps else None

    target_n = first.north + command_distance * math.cos(math.radians(command_bearing))
    target_e = first.east + command_distance * math.sin(math.radians(command_bearing))
    return LegResult(
        index=index,
        command_bearing_deg=command_bearing,
        command_distance_m=command_distance,
        start=(first.north, first.east, first.down),
        end=(last.north, last.east, last.down),
        initial_velocity_bearing_deg=initial_bearing,
        net_displacement_bearing_deg=net_bearing,
        net_displacement_m=net_distance,
        initial_error_deg=None
        if initial_bearing is None
        else signed_angle_deg(initial_bearing, command_bearing),
        net_error_deg=None
        if net_bearing is None
        else signed_angle_deg(net_bearing, command_bearing),
        yaw_start_deg=first.yaw_deg,
        yaw_end_deg=last.yaw_deg,
        reached=math.hypot(last.north - target_n, last.east - target_e) <= reached_radius_m,
        samples=len(samples),
    )


def summarize(legs: Sequence[LegResult], tolerance_deg: float = TOLERANCE_DEG) -> dict[str, Any]:
    """逐段结论 + 总体是否通过；没有任何可测量的段时视为未通过。"""
    worst = [leg.worst_error_deg() for leg in legs]
    measurable = [value for value in worst if value is not None]
    initial = [leg.initial_error_deg for leg in legs if leg.initial_error_deg is not None]
    # 各段误差的圆均值：同号且接近常数说明是一个固定的偏航参考偏差。
    mean_offset = None
    if initial:
        sin_sum = sum(math.sin(math.radians(v)) for v in initial)
        cos_sum = sum(math.cos(math.radians(v)) for v in initial)
        mean_offset = math.degrees(math.atan2(sin_sum, cos_sum))
    spread = None
    if mean_offset is not None:
        spread = max(abs(wrap_deg(v - mean_offset)) for v in initial)
    return {
        "tolerance_deg": tolerance_deg,
        "legs_measured": len(measurable),
        "legs_total": len(legs),
        "worst_abs_error_deg": max(measurable) if measurable else None,
        "mean_initial_offset_deg": mean_offset,
        "initial_offset_spread_deg": spread,
        "constant_offset": bool(
            mean_offset is not None and abs(mean_offset) > tolerance_deg and (spread or 0.0) <= 25.0
        ),
        "passed": bool(measurable)
        and len(measurable) == len(legs)
        and max(measurable) < tolerance_deg,
    }


class Link(Protocol):
    """诊断需要的最小 MAVLink 接口；真实实现见 :class:`MavlinkLink`。"""

    def poll(self) -> Sample | None: ...

    def send_setpoint(self, north: float, east: float, down: float, yaw_deg: float) -> None: ...

    def offboard_and_arm(self) -> None: ...

    def land(self) -> None: ...

    def enter_reposition_mode(self) -> None: ...


@dataclass
class Plan:
    altitude_m: float = 15.0
    leg_distance_m: float = 12.0
    leg_bearings_deg: Sequence[float] = DEFAULT_LEG_BEARINGS_DEG
    takeoff_timeout_s: float = 60.0
    leg_timeout_s: float = 25.0
    settle_s: float = 4.0
    initial_window_s: float = 3.0
    setpoint_hz: float = 20.0
    hold_yaw_deg: float | None = None
    use_reposition: bool = False  # 腿段改用 AUTO_LOITER + MAV_CMD_DO_REPOSITION（采集器的路径）


@dataclass
class Report:
    legs: list[LegResult] = field(default_factory=list)
    hold_yaw_deg: float | None = None
    takeoff_ok: bool = False
    summary: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "hold_yaw_deg": self.hold_yaw_deg,
            "takeoff_ok": self.takeoff_ok,
            "summary": self.summary,
            "legs": [leg.__dict__ for leg in self.legs],
        }


def run_plan(
    link: Link,
    plan: Plan,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Report:
    report = Report()
    period = 1.0 / plan.setpoint_hz
    latest: Sample | None = None

    def pump(
        setpoint: tuple[float, float, float, float],
        duration_s: float,
        *,
        stop_when: Callable[[Sample], bool] | None = None,
    ) -> list[Sample]:
        """以固定频率发送 setpoint 并收集样本，直到超时或 stop_when 为真。"""
        nonlocal latest
        collected: list[Sample] = []
        deadline = clock() + duration_s
        while clock() < deadline:
            link.send_setpoint(*setpoint)
            tick_end = clock() + period
            while clock() < tick_end:
                sample = link.poll()
                if sample is not None:
                    latest = sample
                    collected.append(sample)
                    if stop_when is not None and stop_when(sample):
                        return collected
                else:
                    sleep(min(period / 4.0, max(tick_end - clock(), 0.0)))
        return collected

    # 先用当前位置预热 setpoint 流，再切 OFFBOARD 并解锁。
    wait_deadline = clock() + 15.0
    while latest is None and clock() < wait_deadline:
        sample = link.poll()
        if sample is None:
            sleep(period / 4.0)
        else:
            latest = sample
    if latest is None:
        raise RuntimeError("no LOCAL_POSITION_NED received within 15 s")
    start = latest
    yaw = plan.hold_yaw_deg if plan.hold_yaw_deg is not None else (start.yaw_deg or 0.0)
    report.hold_yaw_deg = yaw
    hold = (start.north, start.east, start.down, yaw)
    pump(hold, 1.0)
    link.offboard_and_arm()

    takeoff = (start.north, start.east, start.down - plan.altitude_m, yaw)
    target_down = takeoff[2]
    pump(takeoff, plan.takeoff_timeout_s, stop_when=lambda s: abs(s.down - target_down) < 0.8)
    pump(takeoff, plan.settle_s)  # 到高后稳一稳
    assert latest is not None
    report.takeoff_ok = abs(latest.down - target_down) < 1.5

    if plan.use_reposition:
        link.enter_reposition_mode()
        pump(takeoff, 2.0)
    for index, bearing in enumerate(plan.leg_bearings_deg):
        origin = latest
        target_n = origin.north + plan.leg_distance_m * math.cos(math.radians(bearing))
        target_e = origin.east + plan.leg_distance_m * math.sin(math.radians(bearing))
        setpoint = (target_n, target_e, target_down, yaw)
        samples = [origin] + pump(
            setpoint,
            plan.leg_timeout_s,
            stop_when=lambda s, tn=target_n, te=target_e: (
                math.hypot(s.north - tn, s.east - te) < 1.0
            ),
        )
        samples += pump(setpoint, plan.settle_s)
        if len(samples) >= 2:
            report.legs.append(
                analyze_leg(
                    index,
                    bearing,
                    plan.leg_distance_m,
                    samples,
                    initial_window_s=plan.initial_window_s,
                )
            )
    report.summary = summarize(report.legs)
    link.land()
    return report


class MavlinkLink:  # pragma: no cover - 需要真实 PX4，离线不覆盖
    """pymavlink 实现。"""

    def __init__(self, connection_string: str) -> None:
        from pymavlink import mavutil

        self._mavutil = mavutil
        self.mav = mavutil.mavlink_connection(connection_string)
        self.mav.wait_heartbeat(timeout=30)
        self.target = (self.mav.target_system, self.mav.target_component or 1)
        self._yaw_deg: float | None = None
        self._last: Sample | None = None
        self.estimator_flags: int | None = None
        self._global: tuple[float, float, float] | None = None
        self._global_local: tuple[float, float] | None = None
        self._reposition = False
        self._sent_target: tuple[float, float] | None = None
        for message_id, hz in ((32, 20), (30, 20), (33, 5), (230, 2)):
            self.mav.mav.command_long_send(
                *self.target, 511, 0, message_id, 1e6 / hz, 0, 0, 0, 0, 0
            )

    def poll(self) -> Sample | None:
        message = self.mav.recv_match(
            type=["LOCAL_POSITION_NED", "ATTITUDE", "ESTIMATOR_STATUS", "GLOBAL_POSITION_INT"],
            blocking=False,
        )
        if message is None:
            return None
        kind = message.get_type()
        if kind == "ATTITUDE":
            self._yaw_deg = math.degrees(message.yaw) % 360.0
            return None
        if kind == "GLOBAL_POSITION_INT":
            self._global = (message.lat / 1e7, message.lon / 1e7, message.alt / 1e3)
            self._global_local = (self._last.north, self._last.east) if self._last else None
            return None
        if kind == "ESTIMATOR_STATUS":
            self.estimator_flags = int(message.flags)
            return None
        sample = Sample(
            t=time.monotonic(),
            north=message.x,
            east=message.y,
            down=message.z,
            vn=message.vx,
            ve=message.vy,
            yaw_deg=self._yaw_deg,
        )
        self._last = sample
        return sample

    def enter_reposition_mode(self) -> None:
        # PX4 自定义模式：main=4 (AUTO)，sub=3 (LOITER/HOLD)
        self.mav.mav.command_long_send(*self.target, 176, 0, 1, 4, 3, 0, 0, 0, 0)
        self._reposition = True

    def _send_reposition(self, north: float, east: float, down: float, yaw_deg: float) -> None:
        """与采集器相同的路径：局部 NED → 经纬度 → MAV_CMD_DO_REPOSITION，只在目标变化时发送。"""
        if (north, east) == self._sent_target or self._global is None or self._global_local is None:
            return
        n0, e0 = self._global_local
        lat0, lon0, alt0 = self._global
        earth = 6378137.0
        lat = lat0 + math.degrees((north - n0) / earth)
        lon = lon0 + math.degrees((east - e0) / (earth * math.cos(math.radians(lat0))))
        # param1 地速(-1 默认) param2 bit0 / param4 偏航(度，NaN 表示不控制)
        self.mav.mav.command_int_send(
            *self.target,
            5,  # MAV_FRAME_GLOBAL_INT（AMSL 高度）
            192,
            0,
            0,
            -1,
            1,
            0,
            yaw_deg,
            int(lat * 1e7),
            int(lon * 1e7),
            alt0 + 0.0,
        )
        self._sent_target = (north, east)

    def send_setpoint(self, north: float, east: float, down: float, yaw_deg: float) -> None:
        if self._last is not None and north == 0.0 and east == 0.0 and down == 0.0:
            north, east, down = self._last.north, self._last.east, self._last.down
        if self._reposition:
            self._send_reposition(north, east, down, yaw_deg)
            return
        self.mav.mav.set_position_target_local_ned_send(
            int(time.monotonic() * 1000) & 0xFFFFFFFF,
            *self.target,
            1,  # MAV_FRAME_LOCAL_NED
            0b101111111000,  # 位置 + 偏航
            north,
            east,
            down,
            0,
            0,
            0,
            0,
            0,
            0,
            math.radians(yaw_deg),
            0,
        )

    def offboard_and_arm(self) -> None:
        self.mav.mav.command_long_send(*self.target, 176, 0, 1, 6, 0, 0, 0, 0, 0)  # OFFBOARD
        self.mav.mav.command_long_send(*self.target, 400, 0, 1, 0, 0, 0, 0, 0, 0)  # arm

    def land(self) -> None:
        self.mav.mav.command_long_send(*self.target, 21, 0, 0, 0, 0, 0, 0, 0, 0)


def truth_comparison(ulog_path: Path) -> dict[str, Any]:  # pragma: no cover - 需要真实日志
    """EKF 偏航 vs 真值偏航、EKF 位移 vs 真值位移（需要日志里有 groundtruth 话题）。"""
    from pyulog import ULog

    log = ULog(
        str(ulog_path),
        message_name_filter_list=[
            "vehicle_attitude",
            "vehicle_attitude_groundtruth",
            "vehicle_local_position",
            "vehicle_local_position_groundtruth",
        ],
    )
    data = {d.name: d.data for d in log.data_list}
    result: dict[str, Any] = {"topics": sorted(data)}
    if "vehicle_attitude" not in data or "vehicle_attitude_groundtruth" not in data:
        result["error"] = (
            "日志缺少 vehicle_attitude / vehicle_attitude_groundtruth（需要 SITL 日志配置）"
        )
        return result

    def yaws(topic: str) -> tuple[list[float], list[float]]:
        d = data[topic]
        return [t / 1e6 for t in d["timestamp"]], [
            quaternion_yaw_deg(d["q[0]"][i], d["q[1]"][i], d["q[2]"][i], d["q[3]"][i])
            for i in range(len(d["timestamp"]))
        ]

    et, ey = yaws("vehicle_attitude")
    tt, ty = yaws("vehicle_attitude_groundtruth")
    diffs: list[float] = []
    j = 0
    for t, yaw in zip(et, ey, strict=True):
        while j + 1 < len(tt) and tt[j + 1] <= t:
            j += 1
        diffs.append(signed_angle_deg(yaw, ty[j]))
    n = len(diffs)

    # 圆均值 + 前/后段分开，看 EKF 偏航误差是一开始就有还是飞行中长出来的。
    def circ_mean(values: Sequence[float]) -> float | None:
        if not values:
            return None
        return math.degrees(
            math.atan2(
                sum(math.sin(math.radians(v)) for v in values),
                sum(math.cos(math.radians(v)) for v in values),
            )
        )

    result.update(
        {
            "ekf_minus_truth_yaw_deg": {
                "first_10pct": circ_mean(diffs[: max(n // 10, 1)]),
                "last_10pct": circ_mean(diffs[-max(n // 10, 1) :]),
                "overall": circ_mean(diffs),
                "max_abs": max((abs(v) for v in diffs), default=None),
            },
        }
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - 薄封装
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--connect", default="udpin:0.0.0.0:14540")
    parser.add_argument("--altitude", type=float, default=15.0)
    parser.add_argument("--distance", type=float, default=12.0)
    parser.add_argument(
        "--yaw-deg",
        type=float,
        default=None,
        help="保持的偏航（NED，度）；默认保持起飞时的 EKF 偏航",
    )
    parser.add_argument(
        "--reposition",
        action="store_true",
        help="腿段改走 AUTO_LOITER + DO_REPOSITION（采集器的路径）而不是 OFFBOARD",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ulog", type=Path, default=None)
    args = parser.parse_args(argv)

    plan = Plan(
        altitude_m=args.altitude,
        leg_distance_m=args.distance,
        hold_yaw_deg=args.yaw_deg,
        use_reposition=args.reposition,
    )
    report = run_plan(MavlinkLink(args.connect), plan)
    payload = report.to_json()
    if args.ulog is not None:
        payload["truth"] = truth_comparison(args.ulog)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False))
    return 0 if report.summary.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
