"""Model-visible input rendering for slow-system SFT and test evaluation."""
# ruff: noqa: E501

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
# candidate_zoom_v1 picks the crop by the row's assistant_target track id, which
# leaks the label on multi-candidate rows; it is kept only to reproduce LoRA r3.
# candidate_zoom_v2 crops every candidate in listed order and never reads labels.
CROP_POLICIES = frozenset({"none", "candidate_zoom_v1", "candidate_zoom_v2"})
MAX_CANDIDATE_CROPS = 4
IDENTITY_VARIANTS = ("default", "focus_only", "frame_336", "crop_160", "compact")


def identity_variant_for_row(row: dict[str, Any], override: str | None = None) -> str:
    """Resolve an opt-in budget experiment, including persisted SFT row metadata."""
    variant = override if override is not None else row.get("identity_variant", "default")
    if variant not in IDENTITY_VARIANTS:
        raise ValueError(f"unsupported identity variant: {variant}")
    return variant


def identity_image_sizes(row: dict[str, Any], max_side: int = 448, identity_variant: str | None = None) -> tuple[int, int]:
    variant = identity_variant_for_row(row, identity_variant)
    frame_side = min(max_side, 336) if variant in {"frame_336", "compact"} else max_side
    crop_side = 160 if variant in {"crop_160", "compact"} else 224
    return frame_side, crop_side


def zoom_candidates_for_row(row: dict[str, Any], identity_variant: str | None = None) -> list[dict[str, Any]]:
    """Select crops using observable focus metadata, never the training label."""
    candidates = row.get("candidates") or []
    if not candidates:
        return []
    variant = identity_variant_for_row(row, identity_variant)
    if variant in {"focus_only", "compact"}:
        focus = row.get("focus_track_id")
        selected = next((c for c in candidates if c.get("track_id") == focus), candidates[0])
        return [selected]
    return candidates[:MAX_CANDIDATE_CROPS]


def image_path_for_row(row: dict[str, Any]) -> Path:
    path = Path(row["image_path"])
    return (path if path.is_absolute() else ROOT / path).resolve(strict=True)


def prompt_for_row(row: dict[str, Any], *, identity_variant: str | None = None) -> str:
    """Match the benchmark's slow planner contract without exposing truth."""
    variant = identity_variant_for_row(row, identity_variant)
    candidates = row["candidates"]
    lines = [
        "You are the onboard slow planner. Choose one sub-goal, never motor commands. "
        "Output only JSON with decision, args, reason (at most 20 words). "
        "Confirm a candidate only when its visible identity and attributes match the task; "
        "reject mismatches, hover if uncertain.",
        f"Task: {row['task_text']}",
        f"Memory: {row['memory']}",
    ]
    if len(candidates) == 1:
        candidate = candidates[0]
        box = [round(float(value), 3) for value in candidate["box_xyxy_normalized"]]
        lines.append(f"Candidate: track_id={candidate['track_id']}, box_xyxy_normalized={box}.")
        lines.append(
            "Pick confirm_target or reject_track when visual evidence suffices. "
            f"Use args.track_id={candidate['track_id']} for either."
        )
    elif candidates:
        lines.append("Candidates:")
        for candidate in candidates:
            box = [round(float(value), 3) for value in candidate["box_xyxy_normalized"]]
            lines.append(f"track_id={candidate['track_id']}, box_xyxy_normalized={box}.")
        lines.append("Pick the matching candidate and use its track_id in args.track_id.")
    else:
        lines.append("No candidate tracks. Search a region if the task is clear; ask_clarify if ambiguous.")
    policy = crop_policy_for_row(row)
    if policy == "candidate_zoom_v1" and candidates and variant not in {"focus_only", "compact"}:
        lines.append("Image 2 is a zoomed crop around the candidate box; use it with image 1.")
    elif policy != "none" and candidates:
        shown = [c["track_id"] for c in zoom_candidates_for_row(row, identity_variant)]
        if len(shown) == 1:
            lines.append(f"Image 2 is a zoomed crop around {shown[0]}; use it with image 1.")
        else:
            order = ", ".join(f"image {i + 2}={track}" for i, track in enumerate(shown))
            lines.append(f"Zoomed candidate crops: {order}; use them with image 1.")
        focus = row.get("focus_track_id")
        if focus and len(candidates) > 1:
            lines.remove("Pick the matching candidate and use its track_id in args.track_id.")
            lines.append(f"Event: decide about focus track_id={focus}; use args.track_id={focus}.")
    return "\n".join(lines)


def crop_policy_for_row(row: dict[str, Any], override: str | None = None) -> str:
    policy = override or row.get("_crop_policy") or row.get("crop_policy") or "none"
    if policy not in CROP_POLICIES:
        raise ValueError(f"unsupported crop policy: {policy}")
    return policy


def image_for_row(row: dict[str, Any], max_side: int = 448, *, identity_variant: str | None = None) -> Image.Image:
    max_side, _ = identity_image_sizes(row, max_side, identity_variant)
    if max_side <= 0:
        raise ValueError("max_side must be positive")
    with Image.open(image_path_for_row(row)) as source:
        image = source.convert("RGB")
    width, height = image.size
    scale = min(1.0, max_side / max(width, height))
    if scale < 1.0:
        image = image.resize((round(width * scale), round(height * scale)), Image.Resampling.BILINEAR)
    draw = ImageDraw.Draw(image)
    for candidate in row["candidates"]:
        box = candidate["box_xyxy_normalized"]
        if len(box) != 4 or any(not 0 <= float(value) <= 1 for value in box):
            raise ValueError("candidate boxes must be normalized xyxy")
        x1, y1, x2, y2 = (
            float(box[0]) * image.width,
            float(box[1]) * image.height,
            float(box[2]) * image.width,
            float(box[3]) * image.height,
        )
        if x1 >= x2 or y1 >= y2:
            raise ValueError("candidate box has empty area")
        draw.rectangle((x1, y1, x2, y2), outline=(255, 255, 0), width=2)
        draw.text((max(0, x1), max(0, y1 - 12)), candidate["track_id"], fill=(255, 255, 0))
    return image


def candidate_crop(image: Image.Image, row: dict[str, Any], crop_side: int = 224) -> Image.Image | None:
    """Return a deterministic padded square crop from original pixel coordinates."""
    candidates = row.get("candidates") or []
    if not candidates:
        return None
    track_id = ((row.get("assistant_target") or {}).get("args") or {}).get("track_id")
    selected = next((candidate for candidate in candidates if candidate.get("track_id") == track_id), candidates[0])
    return _zoom_crop(image, selected, crop_side)


def candidate_crops(image: Image.Image, row: dict[str, Any], crop_side: int = 224, *, identity_variant: str | None = None) -> list[Image.Image]:
    """Label-free crops of every candidate (listed order, capped) for candidate_zoom_v2."""
    return [_zoom_crop(image, candidate, crop_side) for candidate in zoom_candidates_for_row(row, identity_variant)]


def _zoom_crop(image: Image.Image, selected: dict[str, Any], crop_side: int = 224) -> Image.Image:
    if crop_side <= 0:
        raise ValueError("crop_side must be positive")
    width, height = image.size
    box = selected["box_xyxy_normalized"]
    if len(box) != 4 or any(not 0 <= float(value) <= 1 for value in box):
        raise ValueError("candidate boxes must be normalized xyxy")
    x1, y1, x2, y2 = (float(box[0]) * width, float(box[1]) * height,
                      float(box[2]) * width, float(box[3]) * height)
    if x1 >= x2 or y1 >= y2:
        raise ValueError("candidate box has empty area")
    side = max(x2 - x1, y2 - y1) * 3.0
    side = max(64.0, side)
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    left, top = cx - side / 2.0, cy - side / 2.0
    right, bottom = left + side, top + side
    out = Image.new("RGB", (max(1, round(side)), max(1, round(side))), (128, 128, 128))
    src_left, src_top = max(0, int(left)), max(0, int(top))
    src_right, src_bottom = min(width, int(right + 0.999999)), min(height, int(bottom + 0.999999))
    if src_right > src_left and src_bottom > src_top:
        patch = image.crop((src_left, src_top, src_right, src_bottom))
        out.paste(patch, (src_left - round(left), src_top - round(top)))
    return out.resize((crop_side, crop_side), Image.Resampling.LANCZOS)


def image_list_for_row(row: dict[str, Any], max_side: int = 448, crop_policy: str | None = None, *, identity_variant: str | None = None) -> list[Image.Image]:
    variant = identity_variant_for_row(row, identity_variant)
    _, crop_side = identity_image_sizes(row, max_side, variant)
    full = image_for_row(row, max_side, identity_variant=variant)
    policy = crop_policy_for_row(row, crop_policy)
    if policy == "none" or not row.get("candidates"):
        return [full]
    with Image.open(image_path_for_row(row)) as source:
        original = source.convert("RGB")
    if policy == "candidate_zoom_v2" or variant in {"focus_only", "compact"}:
        return [full, *candidate_crops(original, row, crop_side, identity_variant=variant)]
    crop = candidate_crop(original, row, crop_side)
    return [full] if crop is None else [full, crop]


def image_jpeg_bytes(row: dict[str, Any], max_side: int = 448, *, identity_variant: str | None = None) -> bytes:
    stream = io.BytesIO()
    image_for_row(row, max_side, identity_variant=identity_variant).save(stream, format="JPEG", quality=85)
    return stream.getvalue()


def image_jpeg_bytes_list(row: dict[str, Any], max_side: int = 448, crop_policy: str | None = None, *, identity_variant: str | None = None) -> list[bytes]:
    result = []
    for image in image_list_for_row(row, max_side, crop_policy, identity_variant=identity_variant):
        stream = io.BytesIO()
        image.save(stream, format="JPEG", quality=85)
        result.append(stream.getvalue())
    return result


def chat_messages(row: dict[str, Any], max_side: int = 448, crop_policy: str | None = None, *, identity_variant: str | None = None) -> list[dict[str, Any]]:
    if row.get("kind") == "mission_plan":
        # Text-only planning rows carry the exact onboard planner prompt.
        return [{"role": "user", "content": [{"type": "text", "text": row["prompt"]}]}]
    images = image_list_for_row(row, max_side, crop_policy, identity_variant=identity_variant)
    prompt_row = {**row, "_crop_policy": crop_policy_for_row(row, crop_policy)}
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_for_row(prompt_row, identity_variant=identity_variant)},
                *({"type": "image", "image": image} for image in images),
            ],
        }
    ]


def dataset_crop_policy(dataset: Path, override: str | None = None) -> str:
    if override:
        return crop_policy_for_row({}, override)
    manifest = dataset / "manifest.json"
    if not manifest.is_file():
        return "none"
    return crop_policy_for_row({}, json.loads(manifest.read_text(encoding="utf-8")).get("crop_policy"))


def load_jsonl(path: Path, *, split: str | None = None) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if split is not None and any(row.get("split") != split for row in rows):
        raise ValueError(f"{path} contains rows outside {split} split")
    return rows


def trained_key_order_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return ``schema`` with object properties in sorted key order.

    Training targets are serialised with ``sort_keys=True``; llama.cpp's
    json_schema grammar emits properties in schema order, so constrained
    decoding must use the same order or it forces an unseen token sequence.
    llama.cpp also emits every required property before any optional one, so an
    object whose optional properties sort ahead of a required one becomes an
    anyOf with one branch per subset of optional properties, all required in
    sorted order. (Otherwise count's optional max_count cannot come first and
    the model's "max_count" turns into patrol's "loops".)
    """
    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            result[key] = {name: trained_key_order_schema(value[name]) for name in sorted(value)}
        elif key == "required" and isinstance(value, list):
            result[key] = sorted(value)
        elif isinstance(value, dict):
            result[key] = trained_key_order_schema(value)
        elif isinstance(value, list):
            result[key] = [trained_key_order_schema(item) if isinstance(item, dict) else item for item in value]
        else:
            result[key] = value
    properties = result.get("properties")
    if not isinstance(properties, dict):
        return result
    names = list(properties)
    required = set(result.get("required", []))
    if [n for n in names if n in required] + [n for n in names if n not in required] == names:
        return result
    optional = [n for n in names if n not in required]
    if len(optional) > 4:
        raise ValueError(f"too many optional properties to expand: {optional}")
    shared = {k: v for k, v in result.items() if k not in {"properties", "required"}}
    branches = []
    for mask in range(1 << len(optional)):
        chosen = required | {n for i, n in enumerate(optional) if mask >> i & 1}
        branches.append(
            {
                **shared,
                "required": [n for n in names if n in chosen],
                "properties": {n: properties[n] for n in names if n in chosen},
            }
        )
    return {"anyOf": branches}
