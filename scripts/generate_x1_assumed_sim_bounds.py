#!/usr/bin/env python3
"""Generate the explicitly scoped Iowa X1 SITL acceptance configuration."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from multidetect.sim_teacher.x1_fw_native_rtl import suffix_power_w
from multidetect.sim_teacher.x1_native_handoff import IOWA_FW_AIRSPEED_PARAMETERS, IOWA_RTL_DESCEND_ALT_M

BASIS = "ASSUMED_SIM"
WORLD = "rw_farm_iowa_w6km__clear_noon"
IMAGE = "multidetect/px4-sitl-gazebo:gz-rendering-830-r1-gimbal-horizon4"
IMAGE_ID = "sha256:871d3f3ea016a585834ce82e2f55c3116b7bf904940db45cbd7b4155de39c55d"
WORLD_SHA = "4e09aa4febff6b78568201a94bc4552ac5bbc6420f2c128d0784e1845bb9999c"
DEM_SHA = "4bb6d4a0d3bf3bd6adaf512af722135bbf99448907161a328326a0d620a97cae"
PROFILE_SHA = "b3faa1b755017d6269f90ffb919c2fde407cb8d5b941d7b497094419f9aa7266"
# Exact Iowa package plus the canonical, validated launch plan.
BASE_SHA = "e8f9731e9fe511e6f45c4af5ae0402042640b7d6022285379e8f7f74806e7ce2"
SCOPES = {
    "target_found": "a0499238a83ee19872d46b592313d1883d0e2df861d8b618c7ebe3fbf44b54bd",
    "battery_drop": "11e617b8bba7fc931fb72070ab6c850d4f81d8f694868b5c7d8496ac7e2c0f61",
    "link_loss": "f825effdde8a264e2d7d005e61f62b6b913f702638b9599896911d5152e2ced1",
    "wind_step": "5500bcd114c41250745dac58ff3d21b45baf4fd53b1ae73b6e436c00c765aff3",
    # configs/simulation/x1-native-handoff-faults-v1.json: link loss inside the MC return.
    "link_loss:mc_return": "bc36ef6a6717a37d857722566464124cc4a93be667c39c4bdd1b16d677d78e95",
    # Same 17% / 12 s battery fault, at 50 s for the Iowa MC return case.
    "battery_drop:mc_return": "f09ddb15d724e92b8839cc72d8bf0bb683218664b5a2469d2703b4c871ae032a",
}


def scopes_for(kind):
    """Every admitted scenario scope for one selected SITL fault kind."""
    return {scope for key, scope in SCOPES.items() if key.split(":", 1)[0] == kind}


def assumed(value, justification):
    return {"value": value, "basis": BASIS, "justification": justification}


def generate(*, mode, world, image, profile_path):
    if (mode, world, image) != ("sim", WORLD, IMAGE):
        raise ValueError("ASSUMED_SIM requires explicit sim mode, the Iowa Clear Noon world and the pinned SITL image")
    raw = profile_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PROFILE_SHA:
        raise ValueError("ASSUMED_SIM X1 airframe profile mismatch")
    p = json.loads(raw)
    home_z = 318.23
    cruise_z = 317.404 + 85.0
    dem_error = 12.0
    capacity = 5.0 * 4 * 3.6
    ground_speed = p["max_airspeed_mps"] + 9.0  # Wind step magnitude rounded up.
    fw_power, motion_power = suffix_power_w(p["endurance"])

    def leg(phase, end, track, vertical, seconds, watts):
        return dict(phase=phase, end=end, padding_ne_m=track, padding_alt_m=vertical,
                    duration_s=seconds, peak_power_w=watts)

    runout = 485.0  # Northward pass/back-transition runout, inside the compiled corridor.
    transition = leg("transition", (runout, 0.0, cruise_z), 30, 12, p["transition"]["timeout_s"], motion_power)
    cruise = leg("fw", (320.0, 0.0, cruise_z), 19, 10, 55, fw_power)
    inbound = [leg("mc", (45.0, 0.0, cruise_z), 30, 12, 72, motion_power),
               leg("mc", (0.0, 0.0, cruise_z), 8, 10, 18, motion_power)]
    landing = leg("landing", (0.0, 0.0, home_z), 5, 8, 85, motion_power)

    def suffix(entry, legs, fixed_wing, *, failure=False):
        return dict(entry=entry, max_entry_speed_mps=ground_speed if fixed_wing else 12,
                    legs=legs, dynamics=dict(max_bank_deg=p["max_bank_deg"],
                        max_climb_mps=p["max_climb_rate_mps"], max_descent_mps=p["max_descent_rate_mps"],
                        max_acceleration_mps2=p["limits"]["max_acceleration_mps2"],
                        max_angular_rate_dps=p["limits"]["max_angular_rate_dps"]),
                    airspeed=[p["min_airspeed_mps"], p["max_airspeed_mps"]] if fixed_wing else None,
                    failure_revalidation_enforced=failure)

    entry = [45, 410, -24, 24, cruise_z - 10, cruise_z + 10]
    mc_entry = [25, 485, -24, 24, *entry[4:]]
    dynamics = suffix(entry, [], True)["dynamics"]
    xy_cruise, acceptance = 5.0, 2.0
    native_inbound = [leg("mc", (45.0, 0.0, cruise_z), 30, 12, 110, motion_power),
                      leg("mc", (0.0, 0.0, cruise_z), 8, 10, 20, motion_power)]
    orbit_radius, orbit_speed = 80.0, p["cruise_airspeed_mps"]
    return {
        "basis": BASIS,
        "scope": dict(mode=mode, world=world, image=image, image_id=IMAGE_ID, world_sha256=WORLD_SHA,
                      dem_sha256=DEM_SHA, profile_sha256=PROFILE_SHA, base_mission_sha256=BASE_SHA,
                      scenarios=SCOPES),
        "airborne_retask": assumed({"vertical_tracking_m": 10, "orbit_tracking_m": 40,
                                    "fw_tracking_m": 55, "max_ground_speed_mps": 21,
                                    "finding_error_m": 12, "fw_s": 40, "orbit_entry_s": 28,
                                    "transition_s": 12, "mc_s": 90, "landing_s": 85},
            "仅供 evidence-follow。进出盘旋与过渡按全航向扫掠计算，限时与无后续写入由 collector 检查。"),
        "dem_error_m": assumed(dem_error, "全站残差 9.14 m（走廊 5.41 m）加 2×1 m sigma 后向上取整为 12 m，作为 SIM 硬界假设。"),
        "landing_pad": assumed({"radius_m": 16, "elevation_error_m": dem_error + 1},
            "20 m 内切圆内留 4 m 径向余量，DEM 12 m 假设另加 1 m 覆盖台面与局部地形起伏。"),
        "battery_capacity_wh": assumed(capacity, "PX4 SIM airframe 4051 继承 5000 mAh，四节各按 3.6 V 下限计算。"),
        "battery": assumed({"nominal_drain_s": 1800, "nominal_floor_pct": 50,
            "nominal_soc_error_pct": 4, "fault_drain_s": 1, "fault_floor_pct": 17,
            "fault_soc_error_pct": 54, "max_sample_age_ms": 500,
            "max_voltage_v": 4 * 4.05, "consumption_error_wh": capacity},
            "1800 s/50% 或故障 1 s/17%，500 ms 内分别向上计 1/50 百分点再留 3/4 点滤波量化余量；上限 4×4.05 V，意外出现计量值时扣满 72 Wh。"),
        "suffixes": {
            "1": assumed(suffix(entry, [cruise, transition, *inbound, landing], True, failure=True),
                "固定北向 320 m 路线，FW 跟踪 19 m，15 m/s 巡航按 55 s 与两倍盘旋功率预算，失败由 runner 阻断后续写入。"),
            "2": assumed(suffix([280, 435, -24, 24, *entry[4:]], [transition, *inbound, landing], True),
                "后过渡限 20 s 且向北留出到 485 m 的延伸，水平/垂直余量 30/12 m，返航与下降按 90/85 s、机型最高功率预算。"),
            "3": assumed(suffix([25, 485, -24, 24, *entry[4:]], [*inbound, landing], False),
                "MC 回航按 8 m/s 目标保留 90 s 覆盖约 485 m，85 s 覆盖约 84 m 下降，使用机型最高功率并检查 5 m 着陆跟踪盒。"),
        },
        "px4_native_parameters": assumed({"RTL_TYPE": 0, "RTL_CONE_ANG": 0, "RTL_LAND_DELAY": 0,
                                          "RTL_DESCEND_ALT": IOWA_RTL_DESCEND_ALT_M,
                                          "MPC_XY_CRUISE": xy_cruise, "MPC_Z_V_AUTO_DN": 1.5,
                                          "MPC_LAND_SPEED": 0.7, "NAV_ACC_RAD": acceptance,
                                          **IOWA_FW_AIRSPEED_PARAMETERS},
            "预武装设置并读回。Iowa FW 盘旋目标为 home 上方 73 m，低于 85 m 巡航，给 40 m 净空和 12 m DEM 误差留名义余量。PX4 以返航高度封顶，实际轨迹仍须验证。MC 直返 home 不走锥形降高，零等待时跳过下降盘旋并直接 LAND，5 m/s 平飞、1.5/0.7 m/s 下降，2 m 到点半径。FW 读回机型校准空速 MIN/TRIM/MAX=12/15/18 m/s；18 m/s 是设定值上限参数，不替代实测真空速门禁。"),
        "native": {
            "return": {"3": assumed(suffix(mc_entry, [*native_inbound, landing], False),
                "PX4 MC RTL 以读回的 5 m/s 直飞 home：440 m 留 110 s、末段 45 m 留 20 s，保持当前高度后原地 LAND，85 s 覆盖 84 m 下降；FW RTL 绕 home 盘旋降高，未建模，拒绝。")},
            "land-now": {"3": assumed({"max_entry_speed_mps": 2, "padding_ne_m": 5, "padding_alt_m": 8,
                                       "duration_s": 85, "peak_power_w": motion_power, "dynamics": dynamics},
                "AUTO.LAND 原地下降，仅在 home 着陆圆内且留 5 m 跟踪余量时可用；1.5/0.7 m/s 下降 84 m 留 85 s，机型最高功率。")},
            "holds": {
                "hold:yard-orbit": assumed({
                    "mode": "fw", "steps": [1], "entry": entry, "max_entry_speed_mps": ground_speed,
                    "centre": [320.0, 0.0, cruise_z], "radius_m": orbit_radius, "tangential_speed_mps": orbit_speed,
                    "padding_ne_m": orbit_radius + 40, "padding_alt_m": 12, "duration_s": 180,
                    "peak_power_w": fw_power, "dynamics": dynamics,
                    "airspeed": [p["min_airspeed_mps"], p["max_airspeed_mps"]]},
                    "航点上空 80 m 半径的 DO_ORBIT，15 m/s 为机型校准配平空速；进圆不超过 60 s 加 120 s 驻留，跟踪与风漂移另加 40 m、垂向 12 m，两倍盘旋功率；到期前由 collector 强制重新请求。"),
                "hold:mc-here": assumed({
                    "mode": "mc", "steps": [3], "entry": mc_entry, "max_entry_speed_mps": 12,
                    "padding_ne_m": 20, "padding_alt_m": 5, "duration_s": 120, "peak_power_w": motion_power,
                    "dynamics": dynamics},
                    "MC 原地 AUTO.LOITER：8 m/s 按 2 m/s² 制动约 16 m，取 20 m 水平、5 m 垂向余量，驻留 120 s，机型最高功率。"),
            },
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--world", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--profile", type=Path, default=Path("configs/airframes/delta_tailsitter_gimbal.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        document = generate(mode=args.mode, world=args.world, image=args.image, profile_path=args.profile)
    except ValueError as exc:
        parser.error(str(exc))
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
