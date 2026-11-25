"""Audit the archived Iowa X1 SITL battery evidence without starting PX4.

Output is an audit record. The pinned simulator has no usable consumption
meter, so this tool deliberately cannot emit live-admission battery bounds.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import hashlib
import json
import lzma
from pathlib import Path
import re


SITE = "rw_farm_iowa_w6km"
IMAGE = "sha256:871d3f3ea016a585834ce82e2f55c3116b7bf904940db45cbd7b4155de39c55d"
PX4_COMMIT = "83c4f4e580558816f0e398918f66358988004696"
PX4_ELF_SHA256 = "0bdc62d6292c2e64bd0529a77c3934050b4a8b79660d16936da451387cbb8162"
METADATA_SHA256 = "1016f682dc5f136de2b6740a9e24ee11274fe36a3a5714723fef7dfc38d85640"
DEM_SHA256 = "4bb6d4a0d3bf3bd6adaf512af722135bbf99448907161a328326a0d620a97cae"
SCENE_SHA256 = "2fdb173e77e99744922de99f8d906351f291454951fa6a728a749b991eab952e"
RUNS = ("out-windplug", "out-link2", "out-batt2")
PEER = (1, 1)
BATTERY_ID = 0
PARAMETERS = (
    "BAT1_CAPACITY", "BAT1_N_CELLS", "BAT1_V_EMPTY", "BAT1_V_CHARGED",
    "BAT1_SOURCE", "SIM_BAT_DRAIN", "SIM_BAT_MIN_PCT",
    "BAT_LOW_THR", "BAT_CRIT_THR", "BAT_EMERGEN_THR",
)


def provenance(path: Path) -> dict:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path.resolve()), "sha256": digest}


def verified_input(path: Path, expected: str, label: str) -> dict:
    record = provenance(path)
    if record["sha256"] != expected:
        raise ValueError(f"{label} does not match the archived Iowa inputs: {path}")
    return record


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def configured_parameters(metadata: Path, airframes: tuple[Path, Path]) -> tuple[dict, dict]:
    with lzma.open(metadata, "rt", encoding="utf-8") as stream:
        defaults = {row["name"]: row["default"] for row in json.load(stream)["parameters"]
                    if row["name"] in PARAMETERS}
    if set(defaults) != set(PARAMETERS):
        raise ValueError("missing pinned PX4 battery parameter defaults")
    # The pinned 4051 entry sources 4050 before applying its own settings.
    if not re.search(r"^\. .*?/4050_gz_delta_tailsitter\s*$", airframes[1].read_text(), re.MULTILINE):
        raise ValueError("4051 no longer sources the pinned 4050 airframe")
    overrides = {}
    for path in airframes:
        for line in path.read_text().splitlines():
            tokens = line.split("#", 1)[0].split()
            if len(tokens) >= 3 and tokens[0] == "param" and tokens[2] in defaults:
                if len(tokens) != 4 or tokens[1] != "set-default":
                    raise ValueError(f"unexpected airframe battery override: {line}")
                value = Decimal(tokens[3])
                if not value.is_finite():
                    raise ValueError(f"invalid battery default: {line}")
                overrides[tokens[2]] = float(value)
    return defaults, overrides


def valid_consumption(hj: int, mah: int) -> bool:
    """Match the existing consumer's definition of usable counter values."""
    return (type(hj) is int and 0 <= hj < 2**31) or (type(mah) is int and 0 < mah < 2**31)


def audit_telemetry(path: Path, *, empty_mv: int, full_mv: int) -> dict:
    samples = Counter()
    readbacks = defaultdict(set)
    pct_values, voltage_values = set(), set()
    usable = 0
    max_residual_centi_mv = 0
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if (row.get("_source_system"), row.get("_source_component")) != PEER:
                continue
            if row.get("message_type") == "PARAM_VALUE" and row.get("param_id") in PARAMETERS:
                readbacks[row["param_id"]].add(row["param_value"])
            if row.get("message_type") != "BATTERY_STATUS" or row.get("id") != BATTERY_ID:
                continue
            current, mah, hj, pct, mv = (
                row["current_battery"], row["current_consumed"], row["energy_consumed"],
                row["battery_remaining"], row["voltages"][0],
            )
            if any(type(value) is not int for value in (current, mah, hj, pct, mv)):
                raise ValueError(f"noninteger MAVLink battery measurement in {path}")
            samples[current, mah, hj] += 1
            usable += valid_consumption(hj, mah)
            if 0 <= pct <= 100 and 0 < mv < 65535:
                pct_values.add(pct)
                voltage_values.add(mv)
                # Work in centi-millivolts to keep the residual calculation exact.
                residual_centi_mv = abs(100 * (mv - empty_mv) - (full_mv - empty_mv) * pct)
                max_residual_centi_mv = max(max_residual_centi_mv, residual_centi_mv)
    if not samples or not pct_values:
        raise ValueError(f"no target pack samples with valid percentage and voltage in {path}")
    return {
        "mavlink": provenance(path), "sample_count": sum(samples.values()),
        "samples_with_counter": usable,
        "counter_groups": [{"current_battery_ca": current, "current_consumed_mah": mah,
                            "energy_consumed_hj": hj, "samples": count}
                           for (current, mah, hj), count in sorted(samples.items())],
        "percent_range": [min(pct_values), max(pct_values)],
        "pack_voltage_mv_range": [min(voltage_values), max(voltage_values)],
        "observed_max_voltage_residual_mv": max_residual_centi_mv / 100,
        "param_value_readbacks": {key: sorted(values) for key, values in sorted(readbacks.items())},
    }


def audit_run(run: Path, *, world: dict, empty_mv: int, full_mv: int) -> dict:
    result_path = run / "result.json"
    result = read_json(result_path)
    if result["world"] != SITE or result["prepared"]["collector"]["extra_sim_params"]:
        raise ValueError(f"unexpected site or collector overrides in {run}")
    episodes = list((run / "flight/episodes").glob("*/episode.json"))
    if len(episodes) != 1:
        raise ValueError(f"expected one episode in {run}")
    episode_path = episodes[0]
    episode = read_json(episode_path)
    recorded_world = episode["provenance"]["worlds"]
    if (episode["simulator_image"] != IMAGE or episode["px4_profile"] != "pinned"
            or len(recorded_world) != 1 or recorded_world[0]["sha256"] != world["sha256"]):
        raise ValueError(f"episode image, profile or world binding mismatch in {run}")
    fault = [row for row in result["contingency_scenario"]["markers"]
             if row.get("fault") == "battery_drop" and row["message_type"] in
             {"scenario_injected", "scenario_observed", "scenario_restored"}]
    receipts = [{key: row[key] for key in ("action", "status", "reason")}
                for row in result["contingency_scenario"]["markers"]
                if row.get("message_type") == "scenario_decision_receipt"]
    return {
        "result": provenance(result_path), "episode": provenance(episode_path),
        "scope_sha256": result["contingency_scenario"].get("motion_scope_sha256"),
        "base_mission_candidate_sha256": result["compiled_mission_sha256"],
        "battery_fault": fault, "decision_receipts": receipts,
        **audit_telemetry(episode_path.with_name("mavlink.jsonl"), empty_mv=empty_mv, full_mv=full_mv),
    }


def build_audit(args: argparse.Namespace) -> dict:
    deployment = {
        "world_sdf": provenance(args.world),
        "dem": verified_input(args.dem, DEM_SHA256, "Iowa DEM"),
        "scene": verified_input(args.scene, SCENE_SHA256, "Iowa scene source"),
    }
    executable = verified_input(args.px4_elf, PX4_ELF_SHA256, "PX4 ELF")
    metadata = verified_input(args.px4_metadata, METADATA_SHA256, "PX4 parameter metadata")
    airframes = tuple(args.reference / "simulation/px4/airframes" / name for name in
                      ("4050_gz_delta_tailsitter", "4051_gz_delta_tailsitter_gimbal"))
    defaults, overrides = configured_parameters(args.px4_metadata, airframes)
    static = {**defaults, **overrides}
    cells = Decimal(str(static["BAT1_N_CELLS"]))
    empty_v = cells * Decimal(str(static["BAT1_V_EMPTY"]))
    full_v = cells * Decimal(str(static["BAT1_V_CHARGED"]))
    runs = {name: audit_run(args.archive / name, world=deployment["world_sdf"],
                            empty_mv=int(1000 * empty_v), full_mv=int(1000 * full_v)) for name in RUNS}
    blockers = {
        "budget.battery_capacity_wh": "configured mAh has no simulator load/energy/cutoff semantics",
        "battery.soc_error_pct": "no energy SOC mapping or source-to-decision simulated-time age bound",
        "battery.consumption_error_wh": "no calibrated cumulative energy/charge meter",
    }
    if not any(run["samples_with_counter"] for run in runs.values()):
        blockers["BATTERY_STATUS"] = "no accepted hJ or positive mAh counter in any target pack sample"
    return {
        "kind": "x1_iowa_sim_battery_audit", "sim_only": True, "status": "unsupported",
        "eligible_bounds_fields": [], "blockers": blockers, "site": SITE,
        "site_inputs": deployment,
        "image": IMAGE, "px4_commit": PX4_COMMIT, "px4_metadata": metadata, "px4_elf": executable,
        "airframes": [provenance(path) for path in airframes], "px4_defaults": defaults,
        "airframe_overrides": overrides, "configured_defaults": static,
        "synthetic_voltage_endpoints_v": [float(empty_v), float(full_v)], "runs": runs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("reference", "archive", "px4-metadata", "px4-elf", "world", "dem", "scene"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_audit(args), indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
