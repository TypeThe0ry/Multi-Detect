#!/usr/bin/env python3
"""Generate offline X1 planner labels; no sentence-to-plan function ships in runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from multidetect.autonomy.mission_planner import plan_json_schema, plan_prompt  # noqa: E402
from multidetect.autonomy.onboard_policy_parts.helpers import labelled_refs  # noqa: E402
from multidetect.autonomy.x1_plan import X1_CAPABILITY, x1_planner_context  # noqa: E402
from scripts.build_plan_sft import normalize_task  # noqa: E402
from scripts.x1_plan_eval import score_x1_task, x1_eval_package  # noqa: E402

# RW-inspired aliases and synthetic NE positions, not surveyed landmarks or flight routes.
SITES = {
    "train": (
        ("rw_farm_iowa__clear_noon", "粮仓北坪|谷仓外场|grain elevator apron|silo clearing"),
        ("rw_port_rotterdam__clear_noon", "集装箱外堆场|港池东侧|container yard|east basin"),
        ("rw_suburb_davis__clear_noon", "戴维斯球场|郊区绿地|Davis ballfield|suburban green"),
        ("rw_coast_santacruz__clear_noon", "圣克鲁斯海岸平台|海边步道口|coastal terrace|boardwalk entrance"),
    ),
    "validation": (("rw_canal_amsterdam__clear_noon", "运河西岸广场|驳船码头|canal plaza|barge wharf"),),
    "holdout": (
        ("rw_city_zurich__clear_noon", "苏黎世湖滨坪|湖岸平台|Zurich lakeside lawn|lakefront terrace"),
        ("rw_port_oakland__clear_noon", "奥克兰港外场|港口南坪|Oakland outer yard|port south apron"),
    ),
}
FORMS = {
    "train": {
        "zh": ("请以离地{asked}米{act}，随后回起飞点降落。", "离地{asked}米{act}，办完原地基地着陆。",
               "起飞后在离地{asked}米{act}，任务结束就回家降落。", "保持对地{asked}米{act}，再返航落地。"),
        "en": ("At {asked} m AGL, {act}, then return to launch and land.",
               "Climb to {asked} metres above ground and {act} before landing back at base.",
               "Take off, {act} at {asked} m above terrain, and land at launch.",
               "Maintain {asked} m above ground while you {act}, then land back home."),
    },
    "validation": {
        "zh": ("离地{asked}米执行：{act}；收工飞回基地着陆。",),
        "en": ("Use {asked} m of terrain clearance to {act}; recover at launch.",),
    },
    "holdout": {
        "zh": ("起飞后{act}（高度按离地{asked}米算），最后在出发处着陆。",
               "收尾时落回起飞点，之前请在距地面{asked}米的高度{act}。"),
        "en": ("After launch, {act} with {asked} m of ground clearance; finish on the launch pad.",
               "Your landing is back at launch; first {act}, keeping {asked} m above the terrain."),
    },
}
OBJECTS = {"train": (("红车", "red car"), ("交通锥", "traffic cone"), ("信箱", "mailbox")),
           "validation": (("卡车", "truck"),), "holdout": (("蓝车", "blue car"), ("消防栓", "fire hydrant"))}
FAMILIES = ("waypoint", "two_waypoints", "observe", "search", "gimbal", "clamp_low", "clamp_high",
            "reject_altitude_low", "reject_altitude_high", "reject_distance", "reject_short_leg",
            "reject_corridor", "reject_unknown", "reject_gimbal", "reject_radius")
SKILL = {"observe": "loiter_observe", "search": "find_object_then_return", "gimbal": "gimbal_stare_at"}


def _package(world, labels, point, cap, zone_label, family):
    place_name = world.removeprefix("rw_").split("__")[0]
    poi = f"poi:{place_name}"
    zone = f"zone:{place_name}_search"
    area = {"north_min_m": -1800, "north_max_m": 1800, "east_min_m": -1500, "east_max_m": 1500,
            "min_altitude_m": 15, "max_altitude_m": 145}
    corridor = dict(zip(tuple(area)[:4], (-850, 1600, -900, 900), strict=True))
    # Place/zone facts are scenario fixtures, not values inferred from an utterance.
    other = f"poi:{place_name}_marker"
    marker = [point[0] + 70, point[1] - 90]
    places = {poi: list(point)} if family not in {"reject_unknown", "search"} else {}
    places[other] = marker
    zones = {zone: {"centre_ne": list(point), "radius_m": 125}} if family == "search" else {}
    return poi, zone, other, {
        "mission_id": f"x1-offline-{place_name}", "home": [0, 0], "places": places, "zones": zones,
        "routes": {}, "ref_labels": ({poi: " / ".join(labels)} if poi in places else {})
        | ({zone: zone_label} if zones else {}) | {other: "蓝色测绘标记 / blue survey marker"},
        "operating_area": area, "budget": {"time_s": 1000, "reserve_time_s": 120,
                                          "energy_wh": 74, "reserve_energy_wh": 14.8},
        "capabilities": sorted(cap), "oversight_steps": [], "risk_level": "low",
        "link_loss_policy": {"action": "return_now", "timeout_s": 0},
        "x1_planning": {"world": world, "max_distance_from_home_m": 1200,
                        "corridor": corridor, "terrain_rel_home_m": [-5, 15]},
    }


def _target(text, family, agl, waypoints, zone, radius, duration, query):
    if family.startswith("reject_"):
        unknown = family == "reject_unknown"
        gimbal = family == "reject_gimbal"
        code = "unknown_place" if unknown else "missing_capability" if gimbal else "unsafe_request"
        reasons = {"distance": "Destination is beyond the package home radius.",
                   "short_leg": "The FW leg is shorter than the allowed minimum.",
                   "corridor": "The destination does not allow transition runout within the corridor.",
                   "unknown": "The location is absent from the operator map.",
                   "gimbal": "The package does not allow gimbal control.",
                   "radius": "Orbit radius is too small for the aircraft envelope."}
        return {"plan_version": 1, "decision": "clarify" if unknown else "reject", "reason_code": code,
                "reason": reasons.get(family.removeprefix("reject_"), "Requested altitude is outside the X1 AGL band."),
                "missing_capabilities": ["gimbal_control"] if gimbal else [], "unresolved_refs": [],
                "question": "Which declared point should I use?" if unknown else None}
    steps = [("vtol_takeoff", {"altitude_agl_m": agl}), ("fw_transition", {})]
    for ref in waypoints:
        steps.append(("fw_goto_waypoint", {"target_ref": ref, "altitude_agl_m": agl}))
    if family in SKILL:
        params = {"altitude_agl_m": agl, "radius_m": radius, "duration_s": duration}
        params.update(region=zone, query=query) if family == "search" else params.update(target_ref=waypoints[-1])
        steps.append((SKILL[family], params))
    steps.extend([("back_transition", {}), ("return_home", {})])
    return {"plan_version": 1, "goal": text[:160], "report": ["status", "findings"] if family == "search" else ["status"],
            "steps": [{"id": f"s{i}", "skill": skill, "params": params,
                       "on_success": f"s{i+1}" if i < len(steps) else None,
                       "on_fail": f"s{len(steps)}" if i < len(steps) else None}
                      for i, (skill, params) in enumerate(steps, 1)]}


def generate(split):
    for world, aliases in SITES[split]:
        labels = aliases.split("|")
        for language, forms in FORMS[split].items():
            zh = language == "zh"
            for variant, form in enumerate(forms):
                for family in FAMILIES:
                    for k, nominal in enumerate({"train": (40, 56, 77, 98, 120),
                                                 "validation": (46, 83, 115), "holdout": (43, 69, 104, 117)}[split]):
                        label = labels[(0 if zh else 2) + (k + variant) % 2]
                        point = {"reject_distance": (1320+k*15, 0), "reject_short_leg": (20+k*3, 0),
                                 "reject_corridor": (450, 545+k*10)}.get(family, (215+k*13, 20+variant*8))
                        cap = frozenset({X1_CAPABILITY} | (set() if family == "reject_gimbal" else {"gimbal_control"}))
                        radius, duration = ((20+k*2) if family == "reject_radius" else (64+k*6+variant), 18+k*7+variant)
                        obj_zh, query = OBJECTS[split][k % len(OBJECTS[split])]
                        zone_label = " / ".join([f"{x}搜索区" for x in labels[:2]] + [f"{x} search sector" for x in labels[2:]])
                        zone_alias = f"{label}搜索区" if zh else f"{label} search sector"
                        poi, zone, marker, package = _package(world, labels, point, cap, zone_label, family)
                        if family in {"reject_altitude_low", "clamp_low"}:
                            asked, agl = nominal // 4, 40
                        elif family in {"reject_altitude_high", "clamp_high"}:
                            asked, agl = 121 + nominal // 2, 120
                        else:
                            asked = agl = nominal
                        radial = family in {"observe", "gimbal", "reject_radius", "reject_gimbal"}
                        if radial:
                            gimbal = family in {"gimbal", "reject_gimbal"}
                            look = "并用云台盯住该点" if gimbal else "并观察该点"
                            watch = " with the gimbal locked on that point" if gimbal else " while observing it"
                            act = (f"到{label}以{radius}米半径绕飞{duration}秒{look}" if zh else
                                   f"fly to {label} and circle it at radius {radius} m for {duration} s{watch}")
                        elif family == "search":
                            act = (f"在{zone_alias}以{radius}米半径找{obj_zh}，找到或满{duration}秒就结束" if zh else
                                   f"search for a {query} in the {zone_alias} with a {radius} m orbit, stopping on detection or after {duration} s")
                        else:
                            act = f"飞到{label}" if zh else f"fly to {label}"
                            if family in {"reject_distance", "reject_short_leg", "reject_corridor"}:
                                if zh:
                                    act += f"（位置：北{point[0]}米、东{point[1]}米）"
                                else:
                                    act += f" ({point[0]} m north, {point[1]} m east of launch)"
                        waypoints = [] if family == "search" else [poi]
                        if family == "two_waypoints":
                            if (variant + k) % 2:
                                waypoints = [marker, poi]
                                act = (f"先飞过蓝色测绘标记，再飞过{label}" if zh else
                                       f"pass the blue survey marker first and the {label} second")
                            else:
                                waypoints = [poi, marker]
                                act = (f"先飞过{label}，再飞过蓝色测绘标记" if zh else
                                       f"pass the {label} followed by the blue survey marker")
                        if family == "reject_unknown":
                            act += "（地图未收录此点）" if zh else " (not listed on the operator map)"
                        if family == "reject_gimbal":
                            act += "，云台不可用时不要改成普通观察" if zh else ", without substituting ordinary observation"
                        text = form.format(asked=asked, act=act)
                        if family.startswith("clamp_"):
                            text = (text[:-1] + "，超出高度范围时可改用最接近的合法离地高度。" if zh else
                                    text[:-1] + "; if that altitude is outside limits, use the nearest legal AGL.")
                        # Do not let otherwise-identical requests across different context qualify as held out.
                        sample_id = f"x1-{split}-" + hashlib.sha256(text.encode()).hexdigest()[:20]
                        invalid = family.startswith("reject_")
                        target = _target(text, family, agl, waypoints, zone, radius, duration, query)
                        eval_row = {"sample_id": sample_id, "text": text, "language": language, "world": world,
                                    "category": family, "x1_package": package, "expected_target": target,
                                    "expect_invalid": invalid,
                                    "skills": [step["skill"] for step in target.get("steps", ())]}
                        loaded = x1_eval_package(eval_row)
                        refs = labelled_refs(loaded.refs, loaded.document["ref_labels"])
                        eval_row["refs"], eval_row["capabilities"] = list(refs), sorted(cap)
                        score = score_x1_task(eval_row, json.dumps(target), package=loaded)
                        if not score["exact"]:
                            raise ValueError(f"invalid fixture: {text}: {score}")
                        yield eval_row, {
                            "sample_id": sample_id, "kind": "mission_plan", "world": world, "family": f"x1_{family}",
                            "split": "val" if split == "validation" else split,
                            "source": "build_x1_plan_sft.py",
                            "language": language, "task_text": text, "refs": list(refs), "capabilities": sorted(cap),
                            "expected_valid": not invalid, "expected_findings": [target["reason_code"]] if invalid else [],
                            "prompt": plan_prompt(text, refs=refs, capabilities=cap, x1_context=x1_planner_context(loaded)),
                            "assistant_target": target,
                        }


def write_schema(path):
    schema = plan_json_schema(capabilities=frozenset({X1_CAPABILITY, "gimbal_control"}))
    doc = {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "X1 SIM plan v1 or decision", **schema}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replay-train", type=Path, help="append the X1 slice to a frozen r24 training corpus")
    parser.add_argument("--write-schema", type=Path, help="emit the full-capability schema snapshot for review")
    args = parser.parse_args(argv)
    legacy = {normalize_task(row["text"])
              for pattern in ("plan-eval-tasks*.json", "plan-probe-tasks*.json")
              for path in (ROOT / "configs/onboard").glob(pattern)
              for row in json.loads(path.read_text(encoding="utf-8"))["tasks"]}
    generated = {}; keys = set(legacy)
    for split in SITES:
        rows = []
        for ev, train in generate(split):
            key = normalize_task(ev["text"])
            if key in keys:
                raise ValueError(f"normalized task collision in {split}: {ev['text']}")
            keys.add(key)
            rows.append((ev, train))
        generated[split] = rows
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    train = [row for _, row in generated["train"]]
    replay = []
    if args.replay_train:
        replay = [json.loads(line) for line in args.replay_train.read_text(encoding="utf-8").splitlines() if line.strip()]
        x1_tasks = keys - legacy
        if not replay or any(normalize_task(row["task_text"]) in x1_tasks for row in replay):
            parser.error("empty replay corpus or replay/X1 normalized task overlap")
    if len({row["sample_id"] for row in replay + train}) != len(replay) + len(train):
        parser.error("duplicate training sample_id")
    for name, rows in (("train", replay + train), ("val", [row for _, row in generated["validation"]])):
        (output / f"{name}.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                                                     for row in rows), encoding="utf-8")
    (output / "x1-holdout.json").write_text(json.dumps({"tasks": [ev for ev, _ in generated["holdout"]]},
                                            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {"split": {split: {"worlds": sorted({ev["world"] for ev, _ in rows}), "rows": len(rows),
                                  "by_language": dict(Counter(ev["language"] for ev, _ in rows)),
                                  "by_family": dict(Counter(ev["category"] for ev, _ in rows))}
                          for split, rows in generated.items()}, "replay_rows": len(replay),
                "replay_sha256": hashlib.sha256(args.replay_train.read_bytes()).hexdigest() if replay else None,
                "sha256": {name: hashlib.sha256((output / name).read_bytes()).hexdigest()
                           for name in ("train.jsonl", "val.jsonl", "x1-holdout.json")}}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.write_schema:
        write_schema(args.write_schema)
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
