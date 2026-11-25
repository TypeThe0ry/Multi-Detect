#!/usr/bin/env python3
"""Generate the explicitly scoped Iowa X1 SITL acceptance configuration."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

BASIS = "ASSUMED_SIM"
WORLD = "rw_farm_iowa_w6km__clear_noon"
IMAGE = "multidetect/px4-sitl-gazebo:gz-rendering-830-r1-gimbal-horizon4"
IMAGE_ID = "sha256:871d3f3ea016a585834ce82e2f55c3116b7bf904940db45cbd7b4155de39c55d"
WORLD_SHA = "4e09aa4febff6b78568201a94bc4552ac5bbc6420f2c128d0784e1845bb9999c"
DEM_SHA = "4bb6d4a0d3bf3bd6adaf512af722135bbf99448907161a328326a0d620a97cae"
PROFILE_SHA = "b3faa1b755017d6269f90ffb919c2fde407cb8d5b941d7b497094419f9aa7266"
BASE_SHA = "9bb15aa3de6a3937a723430be6e968c9ac38564003616e0ede5630b3a49f4f56"
SCOPES = {
    "battery_drop": "11e617b8bba7fc931fb72070ab6c850d4f81d8f694868b5c7d8496ac7e2c0f61",
    "link_loss": "f825effdde8a264e2d7d005e61f62b6b913f702638b9599896911d5152e2ced1",
    "wind_step": "5500bcd114c41250745dac58ff3d21b45baf4fd53b1ae73b6e436c00c765aff3",
}


def assumed(value, justification):
    return {"value": value, "basis": BASIS, "justification": justification}


def generate(*, mode, world, image, profile_path):
    if (mode, world, image) != ("sim", WORLD, IMAGE):
        raise ValueError("ASSUMED_SIM requires explicit sim mode, the Iowa Clear Noon world and the pinned SITL image")
    raw = profile_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PROFILE_SHA:
        raise ValueError("ASSUMED_SIM X1 airframe profile mismatch")
    p = json.loads(raw)
    power = p["endurance"]
    home_z = 318.23
    cruise_z = 317.404 + 85.0
    dem_error = 12.0
    capacity = 5.0 * 4 * 3.6
    ground_speed = p["max_airspeed_mps"] + 9.0  # Wind step magnitude rounded up.
    motion_power = max(power[name] for name in ("climb_power_w", "hover_power_w", "descent_power_w"))

    def leg(phase, end, track, vertical, seconds, watts):
        return dict(phase=phase, end=end, padding_ne_m=track, padding_alt_m=vertical,
                    duration_s=seconds, peak_power_w=watts)

    runout = 485.0  # Northward pass/back-transition runout, inside the compiled corridor.
    transition = leg("transition", (runout, 0.0, cruise_z), 30, 12, p["transition"]["timeout_s"], motion_power)
    cruise = leg("fw", (320.0, 0.0, cruise_z), 19, 10, 55, 2 * power["loiter_power_w"])
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
    return {
        "basis": BASIS,
        "scope": dict(mode=mode, world=world, image=image, image_id=IMAGE_ID, world_sha256=WORLD_SHA,
                      dem_sha256=DEM_SHA, profile_sha256=PROFILE_SHA, base_mission_sha256=BASE_SHA,
                      scenarios=SCOPES),
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
