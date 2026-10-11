"""Model-visible input rendering for slow-system SFT and test evaluation."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def image_path_for_row(row: dict[str, Any]) -> Path:
    path = Path(row["image_path"])
    return (path if path.is_absolute() else ROOT / path).resolve(strict=True)


def prompt_for_row(row: dict[str, Any]) -> str:
    """Match the benchmark's slow planner contract without exposing truth."""
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
    return "\n".join(lines)


def image_for_row(row: dict[str, Any], max_side: int = 448) -> Image.Image:
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


def image_jpeg_bytes(row: dict[str, Any], max_side: int = 448) -> bytes:
    stream = io.BytesIO()
    image_for_row(row, max_side).save(stream, format="JPEG", quality=85)
    return stream.getvalue()


def chat_messages(row: dict[str, Any], max_side: int = 448) -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_for_row(row)},
                {"type": "image", "image": image_for_row(row, max_side)},
            ],
        }
    ]


def load_jsonl(path: Path, *, split: str | None = None) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if split is not None and any(row.get("split") != split for row in rows):
        raise ValueError(f"{path} contains rows outside {split} split")
    return rows
