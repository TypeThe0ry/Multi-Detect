"""悬停平移方向诊断脚本的离线合同：用一个把推力方向转了固定角度的假飞机验证。"""

from __future__ import annotations

import math

import pytest

from scripts import diagnose_mc_translation as diag


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 1e-3)


class RotatedVehicle:
    """点质量飞机：一阶速度响应；推力方向相对指令方向转 ``rotation_deg``。"""

    def __init__(self, clock: FakeClock, rotation_deg: float, yaw_deg: float = 90.0) -> None:
        self.clock = clock
        self.rotation = math.radians(rotation_deg)
        self.yaw_deg = yaw_deg
        self.pos = [0.0, 0.0, 0.0]
        self.vel = [0.0, 0.0]
        self.setpoint = None
        self.armed = False
        self.landed = False
        self._last_t = 0.0
        self._target = None
        self._origin = (0.0, 0.0)
        self._last_emit = -1.0

    def send_setpoint(self, north, east, down, yaw_deg):
        self.setpoint = (north, east, down)

    def offboard_and_arm(self):
        self.armed = True

    def land(self):
        self.landed = True

    def _step(self):
        dt = self.clock.now - self._last_t
        self._last_t = self.clock.now
        if dt <= 0.0 or self.setpoint is None or not self.armed:
            return
        n, e, d = self.setpoint
        # 垂直方向直接跟随；水平方向：每次换 setpoint 记下起点，推力方向转 rotation，
        # 速度随「已走距离」收敛（不用位置反馈，免得转 90° 时变成发散螺旋）。
        self.pos[2] += (d - self.pos[2]) * min(dt * 1.0, 1.0)
        if (n, e) != self._target:
            self._target = (n, e)
            self._origin = (self.pos[0], self.pos[1])
        on, oe = self._origin
        total = math.hypot(n - on, e - oe)
        want_n = want_e = 0.0
        if total > 1e-6:
            travelled = math.hypot(self.pos[0] - on, self.pos[1] - oe)
            speed = max(min(0.5 * (total - travelled), 4.0), 0.0)
            ux, uy = (n - on) / total, (e - oe) / total
            c, s = math.cos(self.rotation), math.sin(self.rotation)
            want_n, want_e = speed * (c * ux - s * uy), speed * (s * ux + c * uy)
        rot_n, rot_e = want_n, want_e
        gain = min(dt * 2.0, 1.0)
        self.vel[0] += (rot_n - self.vel[0]) * gain
        self.vel[1] += (rot_e - self.vel[1]) * gain
        self.pos[0] += self.vel[0] * dt
        self.pos[1] += self.vel[1] * dt

    def poll(self):
        self._step()
        if self.clock.now - self._last_emit < 0.05:
            return None
        self._last_emit = self.clock.now
        return diag.Sample(
            t=self.clock.now,
            north=self.pos[0],
            east=self.pos[1],
            down=self.pos[2],
            vn=self.vel[0],
            ve=self.vel[1],
            yaw_deg=self.yaw_deg,
        )


def run(rotation_deg: float, **plan_kwargs):
    clock = FakeClock()
    vehicle = RotatedVehicle(clock, rotation_deg)
    plan = diag.Plan(leg_timeout_s=40.0, settle_s=2.0, initial_window_s=1.0, **plan_kwargs)
    return diag.run_plan(vehicle, plan, clock=clock, sleep=clock.sleep), vehicle


def test_angle_helpers():
    assert diag.bearing_deg(1, 0) == 0
    assert diag.bearing_deg(0, 1) == 90
    assert diag.bearing_deg(-1, 0) == 180
    assert diag.bearing_deg(0, -1) == 270
    assert diag.signed_angle_deg(10, 350) == 20
    assert diag.signed_angle_deg(350, 10) == -20
    assert diag.wrap_deg(180) == 180 and diag.wrap_deg(-180) == 180
    # 与现场数据一致：指令 +12.3N +8.6E，实际 -6.6N +10.0E，转了约 88°
    command = diag.bearing_deg(12.3, 8.6)
    actual = diag.bearing_deg(-6.6, 10.0)
    assert diag.signed_angle_deg(actual, command) == pytest.approx(88.4, abs=0.5)


def test_quaternion_yaw():
    half = math.radians(45) / 2.0  # 绕 down 轴转 45°
    assert diag.quaternion_yaw_deg(math.cos(half), 0, 0, math.sin(half)) == pytest.approx(45.0)
    assert diag.quaternion_yaw_deg(1, 0, 0, 0) == pytest.approx(0.0)


def test_healthy_vehicle_passes():
    report, vehicle = run(0.0)
    assert report.takeoff_ok
    assert vehicle.landed
    assert len(report.legs) == 4
    assert report.summary["passed"], report.summary
    assert report.summary["worst_abs_error_deg"] < 5.0
    assert report.summary["constant_offset"] is False


@pytest.mark.parametrize("rotation", [90.0, 60.0, -90.0])
def test_rotated_thrust_direction_is_detected_as_constant_offset(rotation):
    report, _ = run(rotation)
    assert not report.summary["passed"]
    assert report.summary["constant_offset"] is True
    assert report.summary["mean_initial_offset_deg"] == pytest.approx(rotation, abs=12.0)
    for leg in report.legs:
        assert abs(leg.initial_error_deg - rotation) < 25.0


def test_no_motion_is_not_a_pass():
    leg = diag.analyze_leg(
        0,
        0.0,
        12.0,
        [diag.Sample(0.0, 0, 0, -10), diag.Sample(5.0, 0.1, 0.0, -10)],
    )
    assert leg.initial_error_deg is None and leg.net_error_deg is None
    assert diag.summarize([leg])["passed"] is False
    assert diag.summarize([])["passed"] is False


def test_requires_two_samples():
    with pytest.raises(ValueError):
        diag.analyze_leg(0, 0.0, 12.0, [diag.Sample(0.0, 0, 0, -10)])
