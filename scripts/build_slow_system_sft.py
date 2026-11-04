#!/usr/bin/env python3
"""Build truth-labelled, world-disjoint slow-system SFT rows from SIM teacher RGB.

Only task text, an RGB path, detector-like boxes, and short memory enter model
inputs. Scenario truth and teacher labels are used solely in this offline builder.
The original images are referenced in place; no image copies are written.
"""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.onboard_decision import parse_onboard_decision  # noqa: E402
from multidetect.sim_teacher.policies import load_sdf_obstacles  # noqa: E402
from multidetect.sim_teacher.segmentation import (  # noqa: E402
    boxes_from_segmentation,
    load_labels_map,
)
from multidetect.simulator.camera_projection import (  # noqa: E402
    project_target_bbox,
    visible_fraction,
)
from multidetect.simulator.task_semantics import fuel_task_texts, task_equivalent  # noqa: E402
from scripts.build_sim_detector_dataset import (  # noqa: E402
    CLASS_ALIASES,
    DEFAULT_SIZES,
    _sdf_sizes,
    _urban_target,
)
from scripts.sft_task_category_balance import (  # noqa: E402
    balance_task_categories as balance_task_categories_fn,
)
from scripts.slow_system_runtime import IDENTITY_VARIANTS, identity_image_sizes  # noqa: E402

CAMPAIGNS = ("campaign-20261011-r2", "campaign-20261011-r3", "campaign-20261011-r4")
SPLITS = ("train", "val", "test")
COLORS = ("red", "blue", "green", "yellow", "orange", "black", "white")
SLUG = re.compile(r"(?:red|blue|green|yellow|orange|black|white)_(?:vehicle|rover|cargo_cart|inspection_marker)", re.I)
TASK_TEMPLATES = (
    "Find and record the {color} {class_name}.",
    "Locate the {color} {class_name}; verify the candidate first.",
    "Please identify the {color} {class_name} in the current view.",
    "\u8bf7\u5bfb\u627e{color_zh}{class_zh}\u5e76\u786e\u8ba4\u5019\u9009\u76ee\u6807\u3002",
    "\u627e\u5230{color_zh}{class_zh}\u540e\u62a5\u544a\u4f4d\u7f6e\u3002",
)
COLOR_ZH = {"red": "\u7ea2\u8272", "blue": "\u84dd\u8272", "green": "\u7eff\u8272", "yellow": "\u9ec4\u8272", "orange": "\u6a59\u8272", "black": "\u9ed1\u8272", "white": "\u767d\u8272"}
CLASS_ZH = {"vehicle": "\u8f66\u8f86", "rover": "\u63a2\u6d4b\u8f66", "cargo_cart": "\u8d27\u8fd0\u8f66", "inspection_marker": "\u68c0\u67e5\u6807\u8bb0"}
CLASS_NAMES = {"vehicle": "vehicle", "rover": "rover", "cargo_cart": "cargo cart", "inspection_marker": "inspection marker"}
SEARCH_REGIONS = ("center", "left", "right")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_jsonl(path: Path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def _relative(path: Path) -> str:
    """Repository-relative path, as every dataset row stores it.

    A worktree run may read campaign data from another checkout; such paths are
    made relative to the repository that holds them so they resolve against that
    data root.
    """
    resolved = path.resolve(strict=True)
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        for parent in resolved.parents:
            if (parent / ".git").exists():
                return resolved.relative_to(parent).as_posix()
        raise


def _task_slug(text: str) -> tuple[str, str] | None:
    match = SLUG.search(text)
    if match is None:
        return None
    color, cls = match.group(0).lower().split("_", 1)
    return color, cls


def _target_color(target: dict) -> str | None:
    label = str(target.get("label", "")).lower()
    return next((color for color in COLORS if label.startswith(color + "_")), None)


def _target_class(target: dict) -> str:
    cls = str(target.get("class") or target.get("label", "").split("_", 1)[-1])
    if cls == "" and str(target.get("target_id", "")).startswith("multidetect_search_target"):
        return "inspection_marker"
    if cls.endswith("_inspection_marker"):
        return "inspection_marker"
    return CLASS_ALIASES.get(cls, cls)


def _target_category(target: dict) -> str:
    category = target.get("category")
    if category:
        return str(category)
    words = target.get("category_words") or []
    if words:
        return str(words[0]).replace(" ", "_")
    return _target_class(target)


def _task_text(slug: tuple[str, str], variant: int) -> str:
    color, cls = slug
    template = TASK_TEMPLATES[variant % len(TASK_TEMPLATES)]
    return template.format(
        color=color,
        class_name=CLASS_NAMES.get(cls, cls.replace("_", " ")),
        color_zh=COLOR_ZH[color],
        class_zh=CLASS_ZH.get(cls, cls),
    )


def _fuel_description(target: dict) -> str:
    words = target.get("category_words") or []
    category = " ".join(dict.fromkeys(" ".join(words).split())) if words else str(target.get("class") or "object").replace("_", " ")
    # Bound each description so the complete rejection stays within 20 words.
    return " ".join(f"{target.get('color') or ''} {category}".split()[:5])


def _fuel_reject_reason(candidate: dict, requested: dict, task_text: str) -> str:
    color = requested.get("color")
    mentions_color = color and (re.search(rf"\b{re.escape(color)}\b", task_text, re.I) or COLOR_ZH.get(color, "\0") in task_text)
    requested = requested if mentions_color else {**requested, "color": None}
    return f"Candidate is {_fuel_description(candidate)}; task asks for {_fuel_description(requested)}."


def _decision(kind: str, *, track: str | None = None, reason: str, region: str | None = None) -> dict:
    value = {
        "decision": kind,
        "args": {"track_id": track, "region": region, "duration_s": None},
        "reason": reason,
    }
    parse_onboard_decision(value)
    return value


def _dhash(image: Image.Image) -> int:
    gray = image.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
    pixels = list(gray.getdata())
    result = 0
    for y in range(8):
        for x in range(8):
            result = (result << 1) | (pixels[y * 9 + x] > pixels[y * 9 + x + 1])
    return result


def _near_duplicate(signature: int, prior: list[int]) -> bool:
    return any((signature ^ old).bit_count() <= 3 for old in prior)


def _augmentation_config(option: str | Mapping[str, object]) -> dict[str, object]:
    presets: dict[str, dict[str, object]] = {
        "mild": {"brightness": (0.82, 1.18), "contrast": (0.85, 1.15), "color_temperature": (-0.12, 0.12), "blur": (0.0, 0.8), "noise": (0.0, 5.0)},
        "weather": {"brightness": (0.62, 1.05), "contrast": (0.70, 1.05), "color_temperature": (-0.22, 0.18), "blur": (0.0, 1.8), "noise": (0.0, 10.0)},
    }
    if isinstance(option, str):
        if option not in presets:
            raise ValueError(f"unknown image augmentation preset: {option}")
        return dict(presets[option])
    result = dict(option)
    unknown = set(result) - {"brightness", "contrast", "color_temperature", "blur", "noise"}
    if unknown:
        raise ValueError(f"unknown image augmentation fields: {sorted(unknown)}")
    return result


def _sample_augmentation(value: object, rng: random.Random, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return rng.uniform(float(value[0]), float(value[1]))
    return float(value)


def _augment_image(image: Image.Image, option: str | Mapping[str, object], seed: int) -> Image.Image:
    config = _augmentation_config(option)
    rng = random.Random(seed)
    brightness = _sample_augmentation(config.get("brightness"), rng, 1.0)
    contrast = _sample_augmentation(config.get("contrast"), rng, 1.0)
    temperature = _sample_augmentation(config.get("color_temperature"), rng, 0.0)
    blur = max(0.0, _sample_augmentation(config.get("blur"), rng, 0.0))
    noise = max(0.0, _sample_augmentation(config.get("noise"), rng, 0.0))
    output = image.convert("RGB")
    if brightness != 1.0:
        output = ImageEnhance.Brightness(output).enhance(brightness)
    if contrast != 1.0:
        output = ImageEnhance.Contrast(output).enhance(contrast)
    if temperature:
        red = 1.0 + max(-0.45, min(0.45, temperature))
        blue = 1.0 - max(-0.45, min(0.45, temperature))
        channels = output.split()
        channels = (channels[0].point(lambda p: max(0, min(255, int(p * red)))), channels[1], channels[2].point(lambda p: max(0, min(255, int(p * blue)))))
        output = Image.merge("RGB", channels)
    if blur:
        output = output.filter(ImageFilter.GaussianBlur(blur))
    if noise:
        pixels = []
        for red, green, blue in output.getdata():
            pixels.append(tuple(max(0, min(255, int(channel + rng.gauss(0.0, noise)))) for channel in (red, green, blue)))
        output.putdata(pixels)
    return output


def _materialize_image_augmentation(rows: dict[str, list[dict]], output: Path, option: str | Mapping[str, object], seed: int, source_hashes: dict[str, str]) -> None:
    cache: dict[str, str] = {}
    augmented_root = output / "augmented-images"
    for split_rows in rows.values():
        for row in split_rows:
            original_name = str(row["image_path"])
            if original_name not in cache:
                source = Path(original_name)
                if not source.is_absolute():
                    source = ROOT / source
                if not source.is_file():
                    raise FileNotFoundError(f"cannot augment missing image: {source}")
                key = hashlib.sha256(f"{original_name}:{seed}:{repr(option)}".encode()).hexdigest()[:20]
                destination = augmented_root / f"{key}.png"
                destination.parent.mkdir(parents=True, exist_ok=True)
                with Image.open(source) as opened:
                    _augment_image(opened, option, int(key, 16)).save(destination, format="PNG")
                try:
                    cache[original_name] = _relative(destination)
                except ValueError:
                    cache[original_name] = destination.as_posix()
                source_hashes[cache[original_name]] = _sha(destination)
            row["image_path"] = cache[original_name]


def _normalized(box: tuple[float, float, float, float], width: int, height: int) -> list[float]:
    return [round(value / (width if index % 2 == 0 else height), 4) for index, value in enumerate(box)]


def _project(obs: dict, targets: list[dict], sizes: dict[str, tuple[float, float, float]], width: int, height: int, obstacles=(), min_visible_fraction: float = 0.3, stats: Counter | None = None) -> list[dict]:
    projected = []
    pose = {**(obs.get("pose") or {}), "attitude": obs.get("attitude") or {}}
    for target in targets:
        metric = target.get("pose") or {}
        cls = _target_class(target)
        try:
            size = tuple(target.get("size_m") or target.get("measured_size_m") or sizes.get(str(target.get("target_id"))) or DEFAULT_SIZES.get(cls, (3.0, 1.8, 1.5)))
            yaw = float(metric.get("yaw_rad", 0.0))
            bottom_offset = target.get(
                "bottom_offset_m",
                target.get("mesh_bottom_offset_m", target.get("origin_bottom_z_m")),
            )
            box = project_target_bbox(
                target_ned=(float(metric["y_north_m"]), float(metric["x_east_m"]), float(metric["z_up_m"])),
                pose=pose,
                camera_info=obs.get("camera_info") or {},
                image_size=(width, height),
                target_size_m=size,
                target_yaw_rad=yaw,
                target_bottom_offset_m=float(bottom_offset) if bottom_offset is not None else None,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if box is None or min(box[2] - box[0], box[3] - box[1]) < 8:
            continue
        fraction = visible_fraction(
            pose=pose, target_ned=(float(metric["y_north_m"]), float(metric["x_east_m"]), float(metric["z_up_m"])),
            target_size_m=size, target_yaw_rad=yaw, obstacles=obstacles,
            target_bottom_offset_m=float(bottom_offset) if bottom_offset is not None else None,
        )
        if min_visible_fraction > 0.0 and fraction < min_visible_fraction:
            if stats is not None:
                stats["dropped_occluded"] += 1
            continue
        pos = (obs.get("pose") or {}).get("local_ned_m") or (0, 0, 0)
        distance = math.hypot(float(metric["y_north_m"]) - float(pos[0]), float(metric["x_east_m"]) - float(pos[1]))
        color = target.get("color") if "true_target" in target else _target_color(target)
        if color is None and cls == "inspection_marker":
            color = "orange"
        projected.append({"target": target, "box": box, "class": cls, "color": color, "range_m": distance, "visible_fraction": fraction})
    return projected


def _background_box(rng: random.Random, projected: list[dict], width: int, height: int) -> tuple[float, float, float, float] | None:
    for _ in range(24):
        box_w, box_h = rng.randint(24, 65), rng.randint(24, 65)
        x1, y1 = rng.randint(0, width - box_w), rng.randint(0, height - box_h)
        x2, y2 = x1 + box_w, y1 + box_h
        if all(x2 < p["box"][0] - 10 or x1 > p["box"][2] + 10 or y2 < p["box"][1] - 10 or y1 > p["box"][3] + 10 for p in projected):
            return float(x1), float(y1), float(x2), float(y2)
    return None


def _row(
    *, world: str, split: str, frame: Path, task_text: str, box: tuple[float, float, float, float] | None,
    size: tuple[int, int], target: dict, kind: str, campaign: str, label_type: str,
    candidate_boxes: list[tuple[float, float, float, float]] | None = None,
    focus_index: int | None = None,
    visible_fraction: float | None = None,
    task_category: str | None = None,
    view_metadata: dict | None = None,
    target_id: str | None = None,
) -> dict:
    boxes = candidate_boxes if candidate_boxes is not None else ([] if box is None else [box])
    candidates = [
        {"track_id": f"track-{index + 1}", "box_xyxy_normalized": _normalized(candidate, *size)}
        for index, candidate in enumerate(boxes)
    ]
    if focus_index is not None:
        target = {**target, "args": {**target["args"], "track_id": f"track-{focus_index + 1}"}}
        parse_onboard_decision(target)
    sample_key = json.dumps([world, _relative(frame), task_text, candidates, target], ensure_ascii=False, sort_keys=True)
    row = {
        "sample_id": hashlib.sha256(sample_key.encode("utf-8")).hexdigest()[:24],
        "world": world,
        "split": split,
        "image_path": _relative(frame),
        "task_text": task_text,
        "candidates": candidates,
        # The track the host asks about (from the FAST event that triggered the
        # call); chosen per candidate, never from the label.
        "focus_track_id": (
            f"track-{focus_index + 1}" if focus_index is not None
            else (candidates[0]["track_id"] if len(candidates) == 1 else None)
        ),
        "memory": "No prior confirmed target; search in progress.",
        "assistant_target": target,
        "campaign": campaign,
        "label_type": label_type,
        "visible_fraction": visible_fraction,
    }
    if task_category is not None:
        row["task_category"] = task_category
    if view_metadata is not None:
        row.update(view_metadata)
        row["target_id"] = str(target_id) if target_id is not None else None
    return row


def _derive_view(obs: dict, sidecar: dict, *, target_id: str | None, fixed_wing: bool = False, tolerance_m: float = 15.0) -> dict:
    """Match an episode NED pose to its teacher-intent waypoint."""
    result = {"view": None, "view_distance_m": None, "target_id": target_id}
    if fixed_wing:
        return result
    pose = (obs.get("pose") or {}).get("local_ned_m")
    observation = sidecar.get("observation", {}) if isinstance(sidecar, dict) else {}
    views = observation.get("views", observation.get("observation_views", ())) if isinstance(observation, dict) else ()
    if not isinstance(pose, (list, tuple)) or len(pose) < 3 or not isinstance(views, list):
        return result
    try:
        north, east, down = (float(value) for value in pose[:3])
    except (TypeError, ValueError):
        return result
    altitude = float(observation.get("camera_altitude_m", 0.0) or 0.0)
    nearest: tuple[float, str] | None = None
    for item in views:
        if not isinstance(item, dict) or not item.get("view"):
            continue
        waypoint = item.get("observation_waypoint", item.get("waypoint", {}))
        if not isinstance(waypoint, dict):
            continue
        try:
            waypoint_north = float(waypoint.get("y_north_m", 0.0))
            waypoint_east = float(waypoint.get("x_east_m", 0.0))
            waypoint_up = float(waypoint.get("z_up_m", 0.0) or altitude)
            distance = math.sqrt(
                (north - waypoint_north) ** 2
                + (east - waypoint_east) ** 2
                + (down + waypoint_up) ** 2
            )
        except (TypeError, ValueError):
            continue
        if nearest is None or distance < nearest[0]:
            nearest = distance, str(item["view"])
    if nearest is not None:
        result["view_distance_m"] = round(nearest[0], 3)
        if nearest[0] <= tolerance_m:
            result["view"] = nearest[1]
    return result


def _scenario_is_fixed_wing(world: str, directories: tuple[Path, ...]) -> bool:
    for directory in directories:
        scenario_path = directory / f"{world}.scenario.json"
        if not scenario_path.is_file():
            continue
        try:
            scenario = _json(scenario_path)
            runtime_rel = scenario.get("channels", {}).get("runtime_input", {}).get("path")
            if not runtime_rel:
                continue
            runtime = _json((directory / runtime_rel).resolve())
            model = str((runtime.get("vehicle") or {}).get("model") or "").lower()
            return "fixed_wing" in model or "cessna" in model
        except (OSError, ValueError, TypeError):
            continue
    return False


def _sample_content_key(row: dict) -> str:
    """Return the model-visible identity used by ``_row``'s sample id."""
    # Balance replicas are intentional exposures.  Keep their copy index in the
    # key so a later content de-duplication pass cannot erase them.
    balance_copy = row.get("balance_copy_index")
    if balance_copy is None:
        match = re.search(r"~(?:view-balance|confusable-view)-(\d+)$", str(row.get("sample_id", "")))
        if match:
            balance_copy = int(match.group(1))
    return json.dumps(
        [row["world"], row["image_path"], row["task_text"], row["candidates"], row["assistant_target"], balance_copy],
        ensure_ascii=False,
        sort_keys=True,
    )


def _dedupe_rows(rows: list[dict], stats: Counter) -> list[dict]:
    """Drop exact duplicate model inputs and retain an auditable source count."""
    kept: list[dict] = []
    seen: dict[str, dict] = {}
    for row in rows:
        sample_id = str(row.get("sample_id", ""))
        key = _sample_content_key(row)
        previous = seen.get(key)
        if previous is None:
            seen[key] = row
            kept.append(row)
            continue
        stats["duplicate_rows"] += 1
        stats[f"duplicate_rows:{previous.get('label_type', 'unknown')}->{row.get('label_type', 'unknown')}"] += 1
    return kept


def _category_descriptor(category: str) -> dict:
    """Represent an asset/category name using its semantic parent words."""
    aliases = {
        "walking_person": ("person", ("person", "pedestrian", "walking person")),
        "standing_person": ("person", ("person", "pedestrian", "standing person")),
        "male_visitor": ("person", ("person", "man", "pedestrian")),
        "hatchback": ("car", ("car", "hatchback")),
        "hatchback_blue": ("car", ("car", "hatchback")),
        "hatchback_red": ("car", ("car", "hatchback")),
    }
    parent, words = aliases.get(category, (category, (category.replace("_", " "),)))
    return {"class": parent, "category_words": list(words), "color": None}


def _category_task_equivalent(requested_category: str, candidate: dict) -> bool:
    """Whether a candidate is a semantic subtype of the requested category."""
    return task_equivalent(_category_descriptor(str(requested_category)), candidate)


def _decision_conflict_key(row: dict) -> str:
    candidates = row.get("candidates") or []
    focus = row.get("focus_track_id")
    focused = next(
        (candidate for candidate in candidates if candidate.get("track_id") == focus),
        candidates[0] if candidates else None,
    )
    return json.dumps(
        [row.get("image_path"), row.get("task_text"), focused],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_no_conflicting_decisions(rows: Sequence[dict]) -> int:
    decisions: dict[str, str] = {}
    conflicts: list[tuple[str, str, str]] = []
    for row in rows:
        key = _decision_conflict_key(row)
        decision = str((row.get("assistant_target") or {}).get("decision", "unknown"))
        previous = decisions.get(key)
        if previous is not None and previous != decision:
            conflicts.append((key, previous, decision))
        else:
            decisions[key] = decision
    if conflicts:
        sample = conflicts[0]
        raise ValueError(f"conflicting decisions for identical model input ({len(conflicts)} groups): {sample[1]} vs {sample[2]}")
    return 0


def _append_generated_row(groups, kind: str, row: dict, stats: Counter) -> None:
    """Append a generated row unless this exact model input already exists."""
    key = _sample_content_key(row)
    for existing_rows in groups.values():
        for existing in existing_rows:
            if _sample_content_key(existing) == key:
                stats["duplicate_rows"] += 1
                stats[f"duplicate_rows:{existing.get('label_type', 'unknown')}->{row.get('label_type', 'unknown')}"] += 1
                return
    groups[kind].append(row)


def _eligible_frames(campaign_roots: list[Path], world_splits: dict[str, str], source_hashes: dict[str, str], *, seed: int, min_visible_fraction: float = 0.3, world_metadata: dict[str, dict] | None = None, truth_dirs: tuple[Path, ...] = ()):
    seen: dict[str, list[int]] = defaultdict(list)
    stats = Counter()
    for campaign in campaign_roots:
        for metadata in sorted(campaign.rglob("episode.json")):
            ep = metadata.parent
            if _json(metadata).get("status") != "complete":
                continue
            world = next((part for part in ep.parts if part.startswith("gen_")), None)
            urban = any(part.startswith("urban_inspection_multirotor_001") for part in ep.parts)
            if urban and world is None:
                world = "urban_inspection_multirotor_001"
            if world is None or (not urban and world not in world_splits):
                continue
            split = "train" if urban else world_splits[world]
            truth_path = next(
                (
                    candidate
                    for candidate in (
                        # The index's own directory first: a fresh round (e.g. generated-r19)
                        # keeps its truth beside the index it was built from.
                        *(directory / f"{world}.truth.json" for directory in truth_dirs),
                        ROOT / "configs/scenarios/generated" / f"{world}.truth.json",
                        ROOT / "configs/scenarios/generated-movers" / f"{world}.truth.json",
                        ROOT / "configs/scenarios/generated-v2" / f"{world}.truth.json",
                        ROOT / "configs/scenarios/generated-v2-appearance" / f"{world}.truth.json",
                        ROOT / "configs/scenarios/generated-confusers" / f"{world}.truth.json",
                        ROOT / "configs/scenarios/generated-confusers-2" / f"{world}.truth.json",
                        ROOT / "configs/scenarios/generated-r15" / f"{world}.truth.json",
                    )
                    if candidate.is_file()
                ),
                ROOT / "configs/scenarios/generated" / f"{world}.truth.json",
            )
            sdf_path = ROOT / "simulation/gazebo/worlds" / f"{world}.sdf"
            if not urban and not truth_path.is_file():
                continue
            truth = {} if urban else _json(truth_path)
            world_info = (world_metadata or {}).get(world, {})
            sidecar_path = next(
                (directory / f"{world}.teacher_intent.json" for directory in truth_dirs
                 if (directory / f"{world}.teacher_intent.json").is_file()),
                None,
            )
            sidecar = _json(sidecar_path) if sidecar_path is not None else None
            if sidecar_path is not None:
                source_hashes[_relative(sidecar_path)] = _sha(sidecar_path)
            fuel = truth.get("asset_mode") == "fuel_v1" or world_info.get("asset_mode") == "fuel_v1"
            targets = _urban_target(world, sdf_path) if urban else truth.get("targets", [])
            requested = None
            if fuel:
                true_targets = [target for target in targets if target.get("true_target") is True]
                if len(true_targets) != 1:
                    raise ValueError(f"Fuel world {world} must have exactly one true_target")
                requested = true_targets[0]
            if not targets:
                continue
            sizes = _sdf_sizes(sdf_path)
            obstacles = load_sdf_obstacles(sdf_path)
            steps = ep / "steps.jsonl"
            source_hashes[_relative(steps)] = _sha(steps)
            source_hashes[_relative(metadata)] = _sha(metadata)
            if truth_path.is_file():
                source_hashes[_relative(truth_path)] = _sha(truth_path)
            if sdf_path.is_file():
                source_hashes[_relative(sdf_path)] = _sha(sdf_path)
            episode_info = _json(metadata)
            episode_task = str(episode_info.get("task_text") or "")
            airframe_name = str(episode_info.get("airframe") or "").lower()
            fixed_wing = (
                "fixed_wing" in airframe_name
                or "cessna" in airframe_name
                or _scenario_is_fixed_wing(world, truth_dirs)
            )
            for step in _iter_jsonl(steps):
                if fuel:
                    # The generated index carries the curated natural-language task; earlier
                    # fuel episodes recorded an English label inside a Chinese template.
                    task_text = str(world_info.get("task_text") or step.get("task_text") or episode_task or "")
                    if not task_text:
                        raise ValueError(f"Fuel world {world} has no task text")
                    slug = {
                        "task_text": task_text,
                        "task_text_en": world_info.get("task_text_en"),
                        "task_category": (world_info.get("target_combo") or {}).get("category"),
                        "true_target": requested,
                        "task_text_variants": world_info.get("task_text_variants", []),
                    }
                else:
                    slug = _task_slug(str(step.get("task_text") or episode_task))
                if slug is None:
                    stats["missing_task_slug"] += 1
                    continue
                for side in ("before", "after"):
                    obs = step.get("obs_" + side)
                    if not isinstance(obs, dict) or abs(float(obs.get("sim_skew_ms", 0))) > 250:
                        continue
                    rgb = obs.get("rgb_path")
                    if not isinstance(rgb, str):
                        continue
                    frame = ep / Path(rgb.replace("\\", "/"))
                    if not frame.is_file():
                        continue
                    if sidecar is not None:
                        step = dict(step)
                        view_obs = _derive_view(
                            obs,
                            sidecar,
                            target_id=None,
                            fixed_wing=fixed_wing or str(world_info.get("airframe_category", "")).lower() == "fixed_wing",
                        )
                        step["_view_metadata"] = view_obs
                    with Image.open(frame) as opened:
                        if opened.mode != "RGB":
                            continue
                        width, height = opened.size
                        signature = _dhash(opened)
                    if _near_duplicate(signature, seen[world]):
                        stats["near_duplicate_frames"] += 1
                        continue
                    seen[world].append(signature)
                    source_hashes[_relative(frame)] = _sha(frame)
                    seg_candidate = ep / Path(obs["segmentation_path"].replace("\\", "/")) if isinstance(obs.get("segmentation_path"), str) else None
                    if seg_candidate is not None and seg_candidate.is_file():
                        seg_path = seg_candidate
                        seg_image = load_labels_map(seg_path)
                        if seg_image.shape[:2] != (height, width):
                            raise ValueError("segmentation geometry differs from RGB")
                        source_hashes[_relative(seg_path)] = _sha(seg_path)
                        visible = boxes_from_segmentation(seg_image, truth, min_area_px=4, min_box_px=8)
                        records = {str(t.get("target_id")): t for t in targets}
                        labels_map = truth["segmentation_labels"]
                        # Every labelled object, scenery included, for matching live proposals.
                        scene = [
                            {
                                "target_id": object_id,
                                "box": item["box"],
                                "role": (labels_map.get(str(item["segmentation_label"])) or {}).get("role"),
                                "asset_slug": (labels_map.get(str(item["segmentation_label"])) or {}).get("asset_slug"),
                                "record": records.get(object_id),
                            }
                            for object_id, item in boxes_from_segmentation(seg_image, truth, min_area_px=4, min_box_px=2).items()
                        ]
                        projected = []
                        for target in targets:
                            item = visible.get(str(target.get("target_id")))
                            if item is None:
                                continue
                            metric = target.get("pose") or {}
                            pos = (obs.get("pose") or {}).get("local_ned_m") or (0, 0, 0)
                            distance = math.hypot(float(metric["y_north_m"]) - float(pos[0]), float(metric["x_east_m"]) - float(pos[1]))
                            cls = _target_class(target)
                            color = target.get("color") if "true_target" in target else _target_color(target)
                            if color is None and cls == "inspection_marker":
                                color = "orange"
                            projected.append({"target": target, "box": item["box"], "class": cls, "color": color, "range_m": distance, "visible_fraction": None, "label_source": "segmentation", **item})
                        stats["segmentation_frames"] += 1
                    else:
                        scene = None
                        projected = _project(obs, targets, sizes, width, height, obstacles, min_visible_fraction=min_visible_fraction, stats=stats)
                    stats["eligible_frames"] += 1
                    yield campaign.name, world, split, frame, (width, height), slug, projected, step, side, stats, scene


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _coverage(inner, outer) -> float:
    ix = max(0.0, min(inner[2], outer[2]) - max(inner[0], outer[0]))
    iy = max(0.0, min(inner[3], outer[3]) - max(inner[1], outer[1]))
    area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    return ix * iy / area if area > 0 else 0.0


def _identity_match(
    box, track: str, confirm_min_side_px: float | None, *, range_m: float | None = None
) -> dict:
    min_side = min(box[2] - box[0], box[3] - box[1])
    too_small = (
        min_side < confirm_min_side_px
        if confirm_min_side_px is not None
        else (range_m is not None and range_m > 45) or min_side < 12
    )
    if not too_small:
        return _decision("confirm_target", track=track, reason="Candidate class and color match the task.")
    return _decision("approach_track", track=track, reason="Matching candidate is distant; approach for a closer view.")


def _counterfactual_task(obj: dict, others: list[dict], variant: int) -> str | None:
    """A task naming ``obj`` itself, unless another visible object fits it too."""
    if any(task_equivalent(obj, other) for other in others if other is not obj):
        return None
    chinese, english = fuel_task_texts(obj)
    return english if variant % 2 else chinese


def _counterfactual_negative_focus(named: dict, others: list[tuple[int, dict]], held_out: frozenset[str]):
    """Another visible object the counterfactual task does not describe.

    Counterfactual rows alone only ever say "yes" to a renamed task, which taught
    r15/r16 that a task naming a vehicle class accepts any vehicle (task "pickup",
    candidate SUV). The paired reject keeps identity tied to the named class.
    The true target is preferred: it is the most salient object in the frame.
    """
    eligible = [
        (index, obj) for index, obj in others
        if obj is not named and not task_equivalent(named, obj) and obj.get("asset_slug") not in held_out
    ]
    eligible.sort(key=lambda item: not str(item[1].get("target_id", "")).startswith("target_true"))
    return eligible[0] if eligible else None


# Deliberately narrow near-category confusions: these are the identity errors
# observed in r21id, rather than a broad synonym list that would weaken rejects.
CONFUSABLE_NEGATIVES = {
    "pickup": ("suv", "car"), "suv": ("pickup", "car"), "car": ("suv", "pickup", "postbox", "mailbox"),
    "person": ("jersey_barrier", "postbox", "mailbox"),
    "jersey_barrier": ("person",), "postbox": ("person", "car", "pickup"), "mailbox": ("person", "car"),
}


def _confusable_task(category: str, variant: int) -> str:
    names = {"pickup": ("pickup", "pickup truck"), "suv": ("SUV", "SUV"), "car": ("car", "car"),
             "person": ("person", "person"), "jersey_barrier": ("jersey barrier", "jersey barrier"),
             "postbox": ("postbox", "postbox"), "mailbox": ("mailbox", "mailbox")}
    name = names.get(category, (category, category))[variant % 2]
    return f"Find the {name} and record its position."


def _balance_identity_views(
    rows: list[dict], *, max_upsample: int, seed: int
) -> tuple[list[dict], dict[str, dict[str, int]]]:
    """Upsample task-category/decision strata toward view parity, deterministically."""
    groups: dict[tuple[str, str], dict[str, list[dict]]] = defaultdict(
        lambda: defaultdict(list)
    )
    positive = {"confirm_target", "approach_track"}
    for row in rows:
        view = row.get("view")
        category = row.get("task_category")
        decision = (row.get("assistant_target") or {}).get("decision")
        if view not in {"front", "oblique", "edge_truncated", "recovery"} or not category:
            continue
        if decision in positive:
            polarity = "positive"
        elif decision == "reject_track":
            polarity = "negative"
        else:
            continue
        groups[(str(category), polarity)][str(view)].append(row)

    result = list(rows)
    report: dict[str, dict[str, int]] = {}
    rng = random.Random(seed)
    for (category, polarity), by_view in sorted(groups.items()):
        largest = max(map(len, by_view.values()))
        for view, originals in sorted(by_view.items()):
            target = min(largest, len(originals) * max_upsample)
            added = target - len(originals)
            key = f"{category}|{polarity}|{view}"
            report[key] = {"original": len(originals), "added": added, "final": target}
            for index in range(added):
                duplicate = dict(originals[rng.randrange(len(originals))])
                duplicate["sample_id"] = f"{duplicate['sample_id']}~view-balance-{index + 1}"
                duplicate["balance_copy_index"] = index + 1
                result.append(duplicate)
    return result, report


def _balance_confusable_negative_views(
    rows: list[dict], *, max_upsample: int, seed: int
) -> tuple[list[dict], dict[str, dict[str, int]]]:
    """Balance existing hard-reject pair families across their available views."""
    groups: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        pair = row.get("confusable_pair_type")
        view = row.get("view")
        decision = (row.get("assistant_target") or {}).get("decision")
        if row.get("label_type") != "confusable_negative" or decision != "reject_track":
            continue
        if pair and view in {"front", "oblique", "edge_truncated", "recovery"}:
            groups[str(pair)][str(view)].append(row)

    result = list(rows)
    report: dict[str, dict[str, int]] = {}
    rng = random.Random(seed)
    for pair, by_view in sorted(groups.items()):
        largest = max(map(len, by_view.values()))
        for view, originals in sorted(by_view.items()):
            target = min(largest, len(originals) * max_upsample)
            added = target - len(originals)
            report[f"{pair}|{view}"] = {
                "original": len(originals), "added": added, "final": target,
            }
            for index in range(added):
                duplicate = dict(originals[rng.randrange(len(originals))])
                duplicate["sample_id"] = f"{duplicate['sample_id']}~confusable-view-{index + 1}"
                duplicate["balance_copy_index"] = index + 1
                result.append(duplicate)
    return result, report


def _cap_task_variant_positives(
    rows: list[dict], *, positive_cap: float,
) -> tuple[list[dict], dict[str, object]]:
    """Bound swapped-task positives against the original target positives.

    ``confusable_positive`` rows are produced only from ``task_text_variants``.
    The budget is per world, derived view, and pair direction; original target
    positives remain untouched so the cap cannot remove the requested object.
    """
    if not math.isfinite(positive_cap) or positive_cap < 0:
        raise ValueError("task_variant_positive_cap must be finite and non-negative")
    positive_decisions = {"confirm_target", "approach_track"}
    original_labels = {"true_target", "proposal_true"}
    original: Counter[tuple[str, str | None, str]] = Counter()
    variants: dict[tuple[str, str | None, str], list[dict]] = defaultdict(list)
    for row in rows:
        decision = (row.get("assistant_target") or {}).get("decision")
        key_pair = row.get("confusable_pair_type")
        if not key_pair or decision not in positive_decisions:
            continue
        key = (str(row.get("world", "")), row.get("view"), str(key_pair))
        if row.get("label_type") == "confusable_positive":
            variants[key].append(row)
        elif row.get("label_type") in original_labels:
            original[key] += 1

    # A variant pair is keyed by requested->primary. Its original positives are
    # the true/proposal positives for the same world/view, regardless of pair.
    original_by_world_view: Counter[tuple[str, str | None]] = Counter()
    for row in rows:
        decision = (row.get("assistant_target") or {}).get("decision")
        if row.get("label_type") not in original_labels or decision not in positive_decisions:
            continue
        original_by_world_view[(str(row.get("world", "")), row.get("view"))] += 1

    report_strata: dict[str, dict[str, int | float]] = {}
    kept_ids: set[str] = set()
    # view is None for rows without view metadata; sort on a None-safe key.
    for key, candidates in sorted(variants.items(), key=lambda item: tuple("" if part is None else str(part) for part in item[0])):
        world, view, pair = key
        base = original[key] or original_by_world_view[(world, view)]
        limit = math.floor(base * positive_cap)
        ordered = sorted(candidates, key=lambda row: str(row.get("sample_id", "")))
        keep = ordered[:limit]
        kept_ids.update(str(row.get("sample_id")) for row in keep)
        report_strata[f"{world}|{view or 'unknown'}|{pair}"] = {
            "original": int(base),
            "before": len(candidates),
            "after": len(keep),
            "limit": int(limit),
        }

    result = [
        row for row in rows
        if row.get("label_type") != "confusable_positive"
        or str(row.get("sample_id")) in kept_ids
        or not row.get("confusable_pair_type")
    ]
    before = sum(item["before"] for item in report_strata.values())
    after = sum(item["after"] for item in report_strata.values())
    return result, {
        "ratio": positive_cap,
        "before": before,
        "after": after,
        "dropped": before - after,
        "strata": report_strata,
    }


def _proposal_counterfactual_negative(
    groups, stats: Counter, proposals, index: int, named_obj: dict, task: str, covered_object, *,
    held_out: frozenset[str], min_side_px: float, world: str, split: str, frame: Path,
    size: tuple[int, int], campaign: str, view_metadata: dict | None = None,
) -> None:
    others = []
    for j, other_box in enumerate(proposals):
        if j == index or min(other_box[2] - other_box[0], other_box[3] - other_box[1]) < min_side_px:
            continue
        obj = covered_object(other_box)
        if obj is not None and obj.get("record") is not None and obj is not named_obj:
            others.append((j, {"asset_slug": obj.get("asset_slug"), **obj["record"]}))
    pick = _counterfactual_negative_focus(named_obj["record"], others, held_out)
    if pick is None:
        return
    j, record = pick
    box = proposals[j]
    shown = [box] + [other for k, other in enumerate(proposals) if k != j][:3]
    stats["proposal_counterfactual_negative"] += 1
    row = _row(
        world=world, split=split, frame=frame, task_text=task, box=box, size=size,
        target=_decision("reject_track", track="track-1", reason=_fuel_reject_reason(record, named_obj["record"], task)),
        kind="reject_track", campaign=campaign, label_type="proposal_counterfactual_negative",
        candidate_boxes=shown, focus_index=0, task_category=_target_category(named_obj["record"]),
        view_metadata=view_metadata, target_id=record.get("target_id"),
    )
    _append_generated_row(groups, "reject_track", row, stats)


def _proposal_rows(
    groups, stats: Counter, boxes: list[list[float]], scene: list[dict], *, slug: dict, task: str,
    world: str, split: str, frame: Path, size: tuple[int, int], campaign: str, true_id: str,
    iou_threshold: float, min_side_px: float, confirm_min_side_px: float,
    held_out: frozenset[str] = frozenset(), counterfactual: bool = False, variant: int = 0,
    task_category: str | None = None, counterfactual_negatives: bool = False,
    view_metadata: dict | None = None,
) -> None:
    """Label live FAST proposals by the segmentation object they cover.

    The prompt shows the focus proposal first and up to three others, exactly as
    the onboard policy does. Boxes that half-cover an object, and distractors the
    task text also describes, get no label.
    """
    proposals = [tuple(float(v) for v in box[:4]) for box in boxes]
    requested = slug["true_target"]

    def covered_object(box) -> dict | None:
        """The scene object a proposal clearly covers, if any."""
        best_iou, best = max(((_iou(box, obj["box"]), obj) for obj in scene), key=lambda item: item[0], default=(0.0, None))
        return best if best is not None and best_iou >= iou_threshold else None
    for index, box in enumerate(proposals):
        if min(box[2] - box[0], box[3] - box[1]) < min_side_px:
            continue
        overlaps = sorted(((_iou(box, obj["box"]), obj) for obj in scene), key=lambda item: item[0], reverse=True)
        best_iou, best = overlaps[0] if overlaps else (0.0, None)
        # Share of each object's box that lies inside the proposal.
        covered = max((_coverage(obj["box"], box) for obj in scene), default=0.0)
        if best_iou < iou_threshold and (best_iou >= 0.1 or covered >= 0.1):
            stats["proposal_ambiguous_box"] += 1
            continue
        shown = [box] + [other for j, other in enumerate(proposals) if j != index][:3]
        track = "track-1"
        if best is None or best_iou < iou_threshold:
            label_type = "proposal_background"
            target = _decision("reject_track", track=track, reason="No object is visible inside this candidate box.")
        elif best["target_id"] == true_id:
            label_type = "proposal_true"
            if min(box[2] - box[0], box[3] - box[1]) >= confirm_min_side_px:
                target = _decision("confirm_target", track=track, reason="Candidate class and color match the task.")
            else:
                target = _decision("approach_track", track=track, reason="Matching candidate is distant; approach for a closer view.")
        elif best["record"] is not None:
            if best.get("asset_slug") in held_out:
                stats["dropped_held_out_focus"] += 1
                continue
            if task_equivalent(requested, best["record"]):
                stats["dropped_task_equivalent"] += 1
                continue
            label_type = "proposal_distractor"
            target = _decision("reject_track", track=track, reason=_fuel_reject_reason(best["record"], requested, task))
            other_task = _counterfactual_task(
                best["record"], [o["record"] for o in scene if o["record"] is not None], variant
            ) if counterfactual else None
            if other_task is not None:
                stats["proposal_counterfactual"] += 1
                groups["approach_track" if min(box[2] - box[0], box[3] - box[1]) < confirm_min_side_px else "confirm_target"].append(_row(
                    world=world, split=split, frame=frame, task_text=other_task, box=box, size=size,
                    target=_identity_match(box, track, confirm_min_side_px), kind="match", campaign=campaign,
                    label_type="proposal_counterfactual", candidate_boxes=shown, focus_index=0,
                    task_category=_target_category(best["record"]),
                    view_metadata=view_metadata, target_id=best.get("target_id"),
                ))
                if counterfactual_negatives:
                    _proposal_counterfactual_negative(
                        groups, stats, proposals, index, best, other_task, covered_object,
                        held_out=held_out, min_side_px=min_side_px, world=world, split=split,
                        frame=frame, size=size, campaign=campaign,
                        view_metadata=view_metadata,
                    )
        else:
            label_type = "proposal_scenery"
            scenery = {"category_words": str(best.get("asset_slug") or "structure").split("_"), "color": None}
            target = _decision("reject_track", track=track, reason=_fuel_reject_reason(scenery, requested, task))
        kind = target["decision"]
        stats[label_type] += 1
        groups[kind].append(_row(
            world=world, split=split, frame=frame, task_text=task, box=box, size=size, target=target,
            kind=kind, campaign=campaign, label_type=label_type, candidate_boxes=shown, focus_index=0,
            task_category=task_category,
            view_metadata=view_metadata,
            target_id=best.get("target_id") if best is not None else None,
        ))


def build_dataset(
    campaigns: list[Path], output: Path, *, index: Path, seed: int = 20261011,
    negative_ratio: float = 1.5, crop_policy: str = "candidate_zoom_v2", min_visible_fraction: float = 0.3,
    confirm_min_side_px: float | None = None,
    proposals: Path | None = None, proposal_iou: float = 0.3, proposal_min_side_px: float = 10.0,
    counterfactual_tasks: bool = False, unseen_holdout: bool = False,
    holdout_from: Sequence[Path] = (), counterfactual_negatives: bool = False,
    confusable_negatives: bool = False,
    task_text_variants: bool = False,
    identity_variant: str = "default",
    balance_task_categories: float | None = None, max_upsample: int = 4,
    balance_identity_views: bool = False,
    balance_confusable_negative_views: bool = False,
    task_variant_positive_cap: float | None = None,
    image_augmentation: str | Mapping[str, object] | None = None,
) -> dict:
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    if negative_ratio <= 0 or not math.isfinite(negative_ratio):
        raise ValueError("negative_ratio must be finite and positive")
    if balance_task_categories is not None and (not math.isfinite(balance_task_categories) or balance_task_categories < 0):
        raise ValueError("balance_task_categories must be finite and non-negative")
    if task_variant_positive_cap is not None and (
        not math.isfinite(task_variant_positive_cap) or task_variant_positive_cap < 0
    ):
        raise ValueError("task_variant_positive_cap must be finite and non-negative")
    if max_upsample < 1:
        raise ValueError("max_upsample must be at least 1")
    if crop_policy not in {"none", "candidate_zoom_v1", "candidate_zoom_v2"}:
        raise ValueError("unsupported crop_policy")
    if identity_variant not in IDENTITY_VARIANTS:
        raise ValueError(f"unsupported identity variant: {identity_variant}")
    if not 0.0 <= float(min_visible_fraction) <= 1.0:
        raise ValueError("min_visible_fraction must be in [0, 1]")
    index_document = _json(index)
    world_splits = {str(w["world_id"]): str(w["split"]) for w in index_document["worlds"]}
    world_metadata = {str(w["world_id"]): {"asset_mode": index_document.get("asset_mode"), **w} for w in index_document["worlds"]}
    # A full campaign has train/val/test; a top-up round (r15) only adds training
    # worlds and keeps the frozen test set elsewhere, so train is the requirement.
    if "train" not in set(world_splits.values()) or not set(world_splits.values()) <= set(SPLITS):
        raise ValueError("World index must contain train worlds and only train/val/test splits")
    rng = random.Random(seed)
    # Held-out target classes stay fully unseen in train/val: never the focus of a label.
    held_out_slugs = frozenset(
        str(combo["slug"]) for combo in index_document.get("held_out_target_combos", [])
    ) if unseen_holdout else frozenset()
    # A top-up round's worlds have no held-out list of their own; the frozen test
    # set's classes (e.g. generated-v2) must stay unseen or its scores are inflated.
    for holdout_index in holdout_from:
        held_out_slugs |= frozenset(str(combo["slug"]) for combo in _json(holdout_index).get("held_out_target_combos", []))
    proposal_cache: dict[str, list[list[float]]] = {}
    if proposals is not None:
        for line in proposals.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                proposal_cache[entry["image"]] = entry["boxes"]
    source_hashes = {_relative(index): _sha(index), _relative(Path(__file__)): _sha(Path(__file__))}
    for holdout_index in holdout_from:
        source_hashes[_relative(holdout_index)] = _sha(holdout_index)
    buckets: dict[str, dict[str, list[dict]]] = {split: defaultdict(list) for split in SPLITS}
    view_metadata_by_image: dict[str, dict] = {}
    stats = Counter()
    if proposals is not None:
        source_hashes[_relative(proposals)] = _sha(proposals)
    for campaign, world, split, frame, size, slug, projected, step, side, frame_stats, scene in _eligible_frames(campaigns, world_splits, source_hashes, seed=seed, min_visible_fraction=min_visible_fraction, world_metadata=world_metadata, truth_dirs=(Path(index).parent,)):
        stats.update(frame_stats)
        frame_stats.clear()
        variant = int(hashlib.sha256(f"{world}:{frame.name}:{seed}".encode()).hexdigest()[:8], 16)
        fuel = isinstance(slug, dict)
        structured_target = next(
            (
                item["target"] for item in projected
                if str(item.get("target", {}).get("target_id", "")).startswith("target_true")
            ),
            None,
        )
        task_category = (
            str(slug.get("task_category") or _target_category(slug["true_target"]))
            if fuel else (_target_category(structured_target) if structured_target else str(slug[1]))
        )
        row_task_category = (
            task_category
            if split == "train" and (balance_task_categories is not None or balance_identity_views)
            else None
        )
        task = (str(slug.get("task_text_en") or slug["task_text"]) if variant % 2 else slug["task_text"]) if fuel else _task_text(slug, variant)
        labels = step.get("labels") if isinstance(step, dict) else {}
        labels = labels if isinstance(labels, dict) else {}
        view_metadata = step.get("_view_metadata") if isinstance(step, dict) else None
        view_metadata = view_metadata if isinstance(view_metadata, dict) else None
        if view_metadata is not None:
            view_metadata_by_image[_relative(frame)] = view_metadata
        expert_phase = str(labels.get("phase") or "").casefold() if side == "after" else ""
        expert_target_in_fov = labels.get("target_in_fov") if side == "after" else None
        try:
            expert_range = float(labels.get("target_range_m")) if side == "after" else math.inf
        except (TypeError, ValueError):
            expert_range = math.inf
        true_id = next((p["target"]["target_id"] for p in projected if str(p["target"].get("target_id", "")).startswith("target_true")), None)
        if fuel:
            true_id = slug["true_target"]["target_id"]
        if world.startswith("urban_"):
            true_id = "multidetect_search_target_urban_001"
        if fuel and split != "test" and slug["true_target"].get("asset_slug") in held_out_slugs:
            # A training world whose target is a held-out class: every row from it
            # would teach that class (as a target or by its look-alikes).
            stats["dropped_held_out_world_frame"] += 1
            continue
        if not projected:
            region = SEARCH_REGIONS[variant % len(SEARCH_REGIONS)]
            reason = "No visible candidate; continue searching."
            if expert_phase == "scan" and expert_target_in_fov is False:
                reason = "Expert scan reports no visible target; continue searching."
            target = _decision("search_region", region=region, reason=reason)
            buckets[split]["search_region"].append(_row(world=world, split=split, frame=frame, task_text=task, box=None, size=size, target=target, kind="search_region", campaign=campaign, label_type="no_candidate", task_category=row_task_category, view_metadata=view_metadata))
            continue
        projected_boxes = [item["box"] for item in projected]
        for p_index, p in enumerate(projected):
            color_match = True if fuel else p["color"] == slug[0]
            class_match = True if fuel else p["class"] == slug[1]
            is_true = p["target"].get("target_id") == true_id
            if is_true and color_match and class_match:
                remote_by_teacher = expert_phase == "approach" and expert_range > 45
                min_side = min(p["box"][2] - p["box"][0], p["box"][3] - p["box"][1])
                if fuel and confirm_min_side_px is not None:
                    # Observable rule: confirm once the target is large enough to verify;
                    # range is invisible to the model and made the labels contradictory.
                    too_small = min_side < confirm_min_side_px
                else:
                    too_small = remote_by_teacher or p["range_m"] > 45 or min_side < 12
                if too_small:
                    target = _decision("approach_track", track=f"track-{p_index + 1}", reason="Matching candidate is distant; approach for a closer view.")
                    kind = "approach_track"
                else:
                    target = _decision("confirm_target", track=f"track-{p_index + 1}", reason="Candidate class and color match the task.")
                    kind = "confirm_target"
            elif fuel and task_equivalent(slug["true_target"], p["target"]):
                # The task text describes this distractor as well: no consistent label.
                stats["dropped_task_equivalent"] += 1
                continue
            elif fuel and split != "test" and p["target"].get("asset_slug") in held_out_slugs:
                stats["dropped_held_out_focus"] += 1
                continue
            elif fuel:
                target = _decision("reject_track", track=f"track-{p_index + 1}", reason=_fuel_reject_reason(p["target"], slug["true_target"], task))
                kind = "reject_track"
                if confusable_negatives and split != "test":
                    distractor_category = _target_category(p["target"])
                    for near_category in CONFUSABLE_NEGATIVES.get(distractor_category, ()):
                        near_task = _confusable_task(near_category, variant)
                        same_category = [q for q in projected if _target_category(q["target"]) == near_category]
                        if _category_task_equivalent(near_category, p["target"]):
                            stats["dropped_confusable_task_equivalent"] += 1
                            continue
                        if any(
                            q["target"].get("target_id") == true_id
                            and _category_task_equivalent(near_category, q["target"])
                            for q in same_category
                        ):
                            # The renamed task already describes the primary
                            # target (for example car -> SUV).  Emitting a
                            # second positive for that same candidate would
                            # create a decision conflict with its true-target
                            # row.
                            stats["dropped_confusable_task_equivalent"] += 1
                            continue
                        pair_key = f"{near_category}->{distractor_category}"
                        # Bound each directional pair family so the new hard
                        # negatives correct coverage without swamping r18 replay.
                        if stats[f"confusable_pair:{pair_key}:reject"] < 30:
                            stats[f"confusable_pair:{pair_key}:reject"] += 1
                            hard_negative = _row(
                                world=world, split=split, frame=frame, task_text=near_task, box=p["box"], size=size,
                                target=_decision("reject_track", track=f"track-{p_index + 1}", reason=f"Task asks for {near_category}; candidate is {distractor_category}."),
                                kind="reject_track", campaign=campaign, label_type="confusable_negative",
                                candidate_boxes=projected_boxes, focus_index=p_index, visible_fraction=p.get("visible_fraction"),
                                task_category=near_category, view_metadata=view_metadata, target_id=p["target"].get("target_id"))
                            hard_negative["confusable_pair_type"] = pair_key
                            buckets[split]["reject_track"].append(hard_negative)
                        for q in same_category:
                            q_index = projected.index(q)
                            stats[f"confusable_pair:{pair_key}:positive"] += 1
                            hard_target = _identity_match(
                                q["box"], f"track-{q_index + 1}", confirm_min_side_px,
                                range_m=q.get("range_m"),
                            )
                            hard_positive = _row(
                                world=world, split=split, frame=frame, task_text=near_task, box=q["box"], size=size,
                                target=hard_target,
                                kind=hard_target["decision"], campaign=campaign, label_type="confusable_positive",
                                candidate_boxes=projected_boxes, focus_index=q_index, visible_fraction=q.get("visible_fraction"),
                                task_category=near_category, view_metadata=view_metadata, target_id=q["target"].get("target_id"))
                            hard_positive["confusable_pair_type"] = pair_key
                            buckets[split][hard_target["decision"]].append(hard_positive)
                other_task = _counterfactual_task(p["target"], [o["target"] for o in projected], variant) if counterfactual_tasks and split != "test" else None
                if other_task is not None:
                    # The same object is the answer when the task names it: identity
                    # depends on the task text, not on the object's class alone.
                    stats["counterfactual"] += 1
                    match = _identity_match(
                        p["box"], f"track-{p_index + 1}", confirm_min_side_px,
                        range_m=p.get("range_m"),
                    )
                    buckets[split][match["decision"]].append(_row(world=world, split=split, frame=frame, task_text=other_task, box=p["box"], size=size, target=match, kind=match["decision"], campaign=campaign, label_type="counterfactual", candidate_boxes=projected_boxes, focus_index=p_index, visible_fraction=p.get("visible_fraction"), task_category=row_task_category))
                    pick = _counterfactual_negative_focus(
                        p["target"], [(q_index, q["target"]) for q_index, q in enumerate(projected) if q_index != p_index], held_out_slugs,
                    ) if counterfactual_negatives else None
                    if pick is not None:
                        q_index, q_target = pick
                        stats["counterfactual_negative"] += 1
                        buckets[split]["reject_track"].append(_row(world=world, split=split, frame=frame, task_text=other_task, box=projected[q_index]["box"], size=size, target=_decision("reject_track", track=f"track-{q_index + 1}", reason=_fuel_reject_reason(q_target, p["target"], other_task)), kind="reject_track", campaign=campaign, label_type="counterfactual_negative", candidate_boxes=projected_boxes, focus_index=q_index, visible_fraction=projected[q_index].get("visible_fraction"), task_category=row_task_category))
            elif not class_match:
                target = _decision("reject_track", track=f"track-{p_index + 1}", reason=f"Wrong class: requested {slug[1]}, candidate is {p['class']}.")
                kind = "reject_track"
            elif not color_match:
                target = _decision("reject_track", track=f"track-{p_index + 1}", reason=f"Wrong color: requested {slug[0]}, candidate is {p['color'] or 'unknown'}.")
                kind = "reject_track"
            else:
                continue
            buckets[split][kind].append(_row(world=world, split=split, frame=frame, task_text=task, box=p["box"], size=size, target=target, kind=kind, campaign=campaign, label_type="true_target" if is_true else "distractor", candidate_boxes=projected_boxes, focus_index=p_index, visible_fraction=p.get("visible_fraction"), task_category=row_task_category, view_metadata=view_metadata, target_id=p["target"].get("target_id")))

        variants = slug.get("task_text_variants", []) if fuel and task_text_variants else []
        if split != "test" and isinstance(variants, list) and len(variants) > 1:
            primary_category = str(slug.get("task_category") or _target_category(slug["true_target"]))
            for variant_info in variants:
                if not isinstance(variant_info, dict):
                    continue
                requested_category = str(variant_info.get("requested_category") or "")
                if not requested_category or requested_category == primary_category:
                    continue
                requested_candidates = [
                    (index, item) for index, item in enumerate(projected)
                    if str(item["target"].get("asset_slug") or item.get("class") or _target_category(item["target"])) == requested_category
                ]
                if not requested_candidates:
                    continue
                text = str(
                    (variant_info.get("task_text_en") if variant % 2 else variant_info.get("task_text"))
                    or variant_info.get("task_text_en")
                    or variant_info.get("task_text")
                    or ""
                )
                if not text:
                    continue
                # A requested asset alias may be a subtype of the primary
                # category (walking_person -> person, hatchback -> car).  Such
                # a task describes the true target and must never receive a
                # reject label.
                true_candidate = next(
                    (item for item in projected if item["target"].get("target_id") == true_id),
                    None,
                )
                semantic_alias = true_candidate is not None and _category_task_equivalent(
                    requested_category, true_candidate["target"]
                )
                if semantic_alias:
                    stats["dropped_confusable_task_equivalent"] += 1
                pair_key = f"{requested_category}->{primary_category}"
                for true_index, true_candidate in enumerate(projected):
                    if true_candidate["target"].get("target_id") != true_id:
                        continue
                    if not semantic_alias:
                        rejected = _row(
                            world=world, split=split, frame=frame, task_text=text,
                            box=true_candidate["box"], size=size,
                            target=_decision(
                                "reject_track", track=f"track-{true_index + 1}",
                                reason=f"Task asks for {requested_category}; candidate is {primary_category}.",
                            ),
                            kind="reject_track", campaign=campaign,
                            label_type="confusable_negative", candidate_boxes=projected_boxes,
                            focus_index=true_index, visible_fraction=true_candidate.get("visible_fraction"),
                            task_category=requested_category, view_metadata=view_metadata or {},
                            target_id=true_candidate["target"].get("target_id"),
                        )
                        rejected["confusable_pair_type"] = pair_key
                        buckets[split]["reject_track"].append(rejected)
                    for positive_index, positive_candidate in requested_candidates:
                        min_side = min(
                            positive_candidate["box"][2] - positive_candidate["box"][0],
                            positive_candidate["box"][3] - positive_candidate["box"][1],
                        )
                        if confirm_min_side_px is not None:
                            decision = "approach_track" if min_side < confirm_min_side_px else "confirm_target"
                        else:
                            decision = "approach_track" if (
                                positive_candidate["range_m"] > 45 or min_side < 12
                            ) else "confirm_target"
                        positive = _row(
                            world=world, split=split, frame=frame, task_text=text,
                            box=positive_candidate["box"], size=size,
                            target=_decision(
                                decision, track=f"track-{positive_index + 1}",
                                reason=("Matching candidate is distant; approach for a closer view."
                                        if decision == "approach_track"
                                        else "Candidate class matches the task."),
                            ),
                            kind=decision, campaign=campaign,
                            label_type="confusable_positive", candidate_boxes=projected_boxes,
                            focus_index=positive_index,
                            visible_fraction=positive_candidate.get("visible_fraction"),
                            task_category=requested_category, view_metadata=view_metadata or {},
                            target_id=positive_candidate["target"].get("target_id"),
                        )
                        positive["confusable_pair_type"] = pair_key
                        buckets[split][decision].append(positive)
        if fuel and scene is not None and _relative(frame) in proposal_cache:
            _proposal_rows(
                buckets[split], stats, proposal_cache[_relative(frame)], scene, slug=slug, task=task,
                world=world, split=split, frame=frame, size=size, campaign=campaign,
                true_id=true_id, iou_threshold=proposal_iou, min_side_px=proposal_min_side_px,
                confirm_min_side_px=confirm_min_side_px if confirm_min_side_px is not None else 20.0,
                held_out=held_out_slugs if split != "test" else frozenset(),
                counterfactual=counterfactual_tasks and split != "test", variant=variant,
                task_category=row_task_category, counterfactual_negatives=counterfactual_negatives,
                view_metadata=view_metadata,
            )
        if variant % 12 == 0:
            background = _background_box(rng, projected, *size)
            if background is not None:
                target = _decision("reject_track", track="track-1", reason="No object is visible inside this candidate box.")
                buckets[split]["reject_track"].append(_row(world=world, split=split, frame=frame, task_text=task, box=background, size=size, target=target, kind="reject_track", campaign=campaign, label_type="background", task_category=row_task_category))
        if variant % 35 == 0:
            target = _decision("ask_clarify", reason="Task lacks the class and color needed to identify a target.")
            buckets[split]["ask_clarify"].append(_row(world=world, split=split, frame=frame, task_text=(("Find the target." if variant % 2 else "\u627e\u5230\u76ee\u6807\u5e76\u8bb0\u5f55\u4f4d\u7f6e") if fuel else "Find the target; class and color unspecified."), box=projected[0]["box"], size=size, target=target, kind="ask_clarify", campaign=campaign, label_type="ambiguous_task", task_category=row_task_category))
    selected: dict[str, list[dict]] = {}
    for split in SPLITS:
        groups = buckets[split]
        positive = groups["confirm_target"] + groups["approach_track"]
        negative = groups["reject_track"]
        rng.shuffle(positive)
        rng.shuffle(negative)
        # Keep negatives at roughly 1.5 per visual positive while preserving all
        # positive examples and a bounded set of search/clarification cases.
        # Live scenery/background proposals are rare hard negatives: keep them all.
        # So are the rejects paired with counterfactual tasks; without them the
        # renamed-task rows only ever teach "yes".
        rare = [row for row in negative if row["label_type"] in {
            "proposal_scenery", "proposal_background", "counterfactual_negative", "proposal_counterfactual_negative", "confusable_negative",
        }]
        negative = rare + [row for row in negative if row not in rare][: max(0, round(len(positive) * negative_ratio) - len(rare))]
        # Preserve at least one no-candidate/clarification example per world;
        # otherwise the bounded prefix can silently discard the urban train
        # world because generated worlds are visited first.
        search_limit = max(1, len(positive) // 5)
        search_by_world: dict[str, list[dict]] = defaultdict(list)
        for row in groups["search_region"]:
            search_by_world[row["world"]].append(row)
        search_representatives = [rows[0] for rows in search_by_world.values()]
        remaining_search = [row for row in groups["search_region"] if row not in search_representatives]
        extras_search = (search_representatives + remaining_search)[:max(search_limit, len(search_representatives))]
        clarify_limit = max(1, len(positive) // 12)
        clarify_by_world: dict[str, list[dict]] = defaultdict(list)
        for row in groups["ask_clarify"]:
            clarify_by_world[row["world"]].append(row)
        clarify_representatives = [rows[0] for rows in clarify_by_world.values()]
        remaining_clarify = [row for row in groups["ask_clarify"] if row not in clarify_representatives]
        extras_clarify = (clarify_representatives + remaining_clarify)[:max(clarify_limit, len(clarify_representatives))]
        extras = extras_search + extras_clarify
        rows = _dedupe_rows(positive + negative + extras, stats)
        rows.sort(key=lambda r: r["sample_id"])
        if split == "train" and balance_task_categories is not None:
            rows, train_category_report = balance_task_categories_fn(
                rows, min_ratio=balance_task_categories, max_upsample=max_upsample, seed=seed,
            )
        if split == "train" and balance_identity_views:
            rows, train_view_report = _balance_identity_views(
                rows, max_upsample=max_upsample, seed=seed,
            )
        if split == "train" and balance_confusable_negative_views:
            rows, train_confusable_view_report = _balance_confusable_negative_views(
                rows, max_upsample=max_upsample, seed=seed,
            )
        selected[split] = rows
        # Only splits the world index declares must yield samples (a top-up round
        # has no test worlds; its test file is simply empty).
        if not rows and split in set(world_splits.values()):
            raise ValueError(f"No samples for {split}")
    for rows in selected.values():
        for row in rows:
            metadata = view_metadata_by_image.get(str(row.get("image_path")))
            if metadata is not None:
                row.setdefault("view", metadata.get("view"))
                row.setdefault("view_distance_m", metadata.get("view_distance_m"))
                row.setdefault("target_id", None)
    task_variant_cap_report = None
    if task_variant_positive_cap is not None:
        task_variant_cap_report = {
            "ratio": task_variant_positive_cap,
            "before": 0,
            "after": 0,
            "dropped": 0,
            "strata": {},
        }
        for split in ("train", "val"):
            selected[split], split_report = _cap_task_variant_positives(
                selected[split], positive_cap=task_variant_positive_cap,
            )
            task_variant_cap_report["before"] += split_report["before"]
            task_variant_cap_report["after"] += split_report["after"]
            task_variant_cap_report["dropped"] += split_report["dropped"]
            task_variant_cap_report["strata"].update({
                f"{split}|{key}": value for key, value in split_report["strata"].items()
            })
    seen = set()
    for split, rows in selected.items():
        selected[split] = rows = _dedupe_rows(rows, stats)
        for row in rows:
            if identity_variant != "default":
                row["identity_variant"] = identity_variant
            if row["world"] in world_splits and world_splits[row["world"]] != split:
                raise ValueError("World crossed split boundary")
            if row["sample_id"] in seen:
                raise ValueError("Duplicate sample ID")
            seen.add(row["sample_id"])
            parse_onboard_decision(row["assistant_target"])
    conflict_count = 0
    for rows in selected.values():
        conflict_count += _validate_no_conflicting_decisions(rows)
    output.mkdir(parents=True, exist_ok=True)
    if image_augmentation is not None:
        _materialize_image_augmentation(selected, output, image_augmentation, seed, source_hashes)
    output_hashes = {}
    for split, rows in selected.items():
        path = output / f"{split}.jsonl"
        path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8", newline="\n")
        output_hashes[path.name] = _sha(path)
    report = {
        "schema_version": 1,
        "seed": seed,
        "negative_ratio_requested": negative_ratio,
        "image_policy": "reference_existing_rgb_png_no_copy; full_frame_resize_to_448_max_side_at_model_input; candidate_crop_resize_to_224x224_at_model_input",
        "crop_policy": crop_policy,
        "counterfactual_tasks": counterfactual_tasks,
        "counterfactual_negatives": counterfactual_negatives,
        "confusable_negatives": confusable_negatives,
        "holdout_from": [_relative(path) for path in holdout_from],
        "unseen_holdout": sorted(held_out_slugs),
        "proposals": (
            {"cache": _relative(proposals), "iou": proposal_iou, "min_side_px": proposal_min_side_px}
            if proposals is not None else None
        ),
        "confirm_rule": (
            {"kind": "min_side_px", "threshold": confirm_min_side_px, "applies_to": "fuel_v1"}
            if confirm_min_side_px is not None else {"kind": "range_45m_or_min_side_12px"}
        ),
        "dedupe": {
            "frames": "per_world_64_bit_dhash_hamming_distance_at_most_3; one_source_frame_can_have_multiple_candidate_labels",
            "exact_model_input_duplicates_removed": int(stats.get("duplicate_rows", 0)),
            "source_pairs": {key.split(":", 1)[1]: value for key, value in stats.items() if key.startswith("duplicate_rows:")},
        },
        "world_splits": {"urban_inspection_multirotor_001": "train", **world_splits},
        "counts": {split: {"total": len(rows), "worlds": len({r["world"] for r in rows}), "decisions": dict(Counter(r["assistant_target"]["decision"] for r in rows)), "label_types": dict(Counter(r["label_type"] for r in rows))} for split, rows in selected.items()},
        "source_counts": dict(stats),
        "conflicting_decision_groups": conflict_count,
        "visibility": {"min_visible_fraction": float(min_visible_fraction), "dropped_occluded": int(stats.get("dropped_occluded", 0))},
        "source_sha256": dict(sorted(source_hashes.items())),
        "output_sha256": output_hashes,
    }
    if image_augmentation is not None:
        report["image_augmentation"] = {"option": image_augmentation, "seed": seed}
    if task_text_variants:
        report["task_text_variants"] = True
    if task_variant_cap_report is not None:
        report["task_variant_positive_cap"] = task_variant_cap_report
    if identity_variant != "default":
        frame_side, crop_side = identity_image_sizes({}, identity_variant=identity_variant)
        report["identity_variant"] = identity_variant
        report["image_policy"] = (
            f"reference_existing_rgb_png_no_copy; full_frame_resize_to_{frame_side}_max_side_at_model_input; "
            f"candidate_crop_resize_to_{crop_side}x{crop_side}_at_model_input"
        )
    if balance_task_categories is not None:
        report["task_category_balance"] = {
            "min_ratio": balance_task_categories,
            "max_upsample": max_upsample,
            "categories": train_category_report,
        }
    if balance_identity_views:
        report["identity_view_balance"] = {
            "max_upsample": max_upsample,
            "strata": train_view_report,
            "effective_count": sum(item["final"] for item in train_view_report.values()),
        }
    if balance_confusable_negative_views:
        report["confusable_negative_view_balance"] = {
            "max_upsample": max_upsample,
            "strata": train_confusable_view_report,
            "effective_count": sum(item["final"] for item in train_confusable_view_report.values()),
        }
    (output / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", action="append", type=Path, default=[])
    parser.add_argument("--index", type=Path, default=ROOT / "configs/scenarios/generated/index.json")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/datasets/slow-system-sft-r1")
    parser.add_argument("--seed", type=int, default=20261011)
    parser.add_argument("--negative-ratio", type=float, default=1.5)
    parser.add_argument("--crop-policy", choices=("none", "candidate_zoom_v1", "candidate_zoom_v2"), default="candidate_zoom_v2")
    parser.add_argument("--identity-variant", choices=IDENTITY_VARIANTS, default="default", help="persist an opt-in short prompt/image policy for training and evaluation")
    parser.add_argument("--confirm-min-side-px", type=float, default=None, help="fuel_v1 worlds: confirm when the target box min side reaches this many pixels (default: legacy range rule)")
    parser.add_argument("--min-visible-fraction", type=float, default=0.3)
    parser.add_argument("--proposals", type=Path, default=None, help="cached onboard FAST proposals (scripts/cache_onboard_proposals.py); adds rows labelled from live boxes")
    parser.add_argument("--proposal-iou", type=float, default=0.3)
    parser.add_argument("--counterfactual-tasks", action="store_true", help="train/val: also ask for each visible distractor by name (a match), so identity follows the task text")
    parser.add_argument("--unseen-holdout", action="store_true", help="train/val: never label held-out target classes (index held_out_target_combos)")
    parser.add_argument("--holdout-from", type=Path, action="append", default=[], help="train/val: also keep this index's held_out_target_combos unseen (the frozen test set of a top-up round)")
    parser.add_argument("--counterfactual-negatives", action="store_true", help="pair each counterfactual task with a reject on another visible object it does not describe")
    parser.add_argument("--confusable-negatives", action="store_true", help="train/val only: add near-category same-frame reject/positive pairs")
    parser.add_argument("--task-text-variants", action="store_true", help="train/val only: add identity pairs for index task_text_variants")
    parser.add_argument(
        "--task-variant-positive-cap", type=float, default=None, metavar="RATIO",
        help="opt-in cap on swapped-task positives per world/view/pair, relative to original positives",
    )
    parser.add_argument("--balance-task-categories", type=float, default=None, metavar="MIN_RATIO", help="train only: upsample identity positives until each task category reaches MIN_RATIO times its rejects")
    parser.add_argument("--max-upsample", type=int, default=4, metavar="K", help="cap each category at K times its original positive rows")
    parser.add_argument("--balance-identity-views", action="store_true", help="train only: upsample task-category/decision strata to balance front, oblique, edge_truncated, and recovery views")
    parser.add_argument("--balance-confusable-negative-views", action="store_true", help="train only: upsample existing confusable-negative pair families toward view parity")
    parser.add_argument("--image-augmentation", choices=("mild", "weather"), default=None, help="opt-in seeded image-space augmentation; default keeps byte-identical source paths/output")
    args = parser.parse_args(argv)
    campaigns = args.campaign or [ROOT / "artifacts/datasets/sim-teacher" / name for name in CAMPAIGNS]
    report = build_dataset(campaigns, args.output, index=args.index, seed=args.seed, negative_ratio=args.negative_ratio, crop_policy=args.crop_policy, min_visible_fraction=args.min_visible_fraction, confirm_min_side_px=args.confirm_min_side_px, proposals=args.proposals, proposal_iou=args.proposal_iou, counterfactual_tasks=args.counterfactual_tasks, unseen_holdout=args.unseen_holdout, holdout_from=args.holdout_from, counterfactual_negatives=args.counterfactual_negatives, confusable_negatives=args.confusable_negatives, task_text_variants=args.task_text_variants, task_variant_positive_cap=args.task_variant_positive_cap, identity_variant=args.identity_variant, balance_task_categories=args.balance_task_categories, max_upsample=args.max_upsample, balance_identity_views=args.balance_identity_views, balance_confusable_negative_views=args.balance_confusable_negative_views, image_augmentation=args.image_augmentation)
    print(json.dumps({"output": str(args.output), "counts": report["counts"], "source_counts": report["source_counts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
