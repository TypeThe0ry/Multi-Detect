"""Render one recorded onboard flight as a self-contained Chinese HTML review."""

from __future__ import annotations

import argparse
import base64
import html
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.replay_alert_votes import (  # noqa: E402
    _nearest_object,
    _object_matches_query,
    _query_from_rows,
    _vote_kind,
)
from scripts.replay_alert_votes import (  # noqa: E402
    _track_position as _project_track_position,
)
from scripts.score_onboard_loop import (  # noqa: E402
    _mission_metrics,
    episode_world_id,
    score_episode,
    truth_for_episode,
)

DEFAULT_IMAGE_BUDGET = 9 * 1024 * 1024
FRAME_SIZE = (800, 600)
CROP_SIZE = (400, 320)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 JSON 文件 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"预期 JSON 对象: {path}")
    return value


def _episode_path(path: Path) -> Path:
    path = path.resolve()
    if (path / "steps.jsonl").is_file():
        return path
    matches = sorted({item.parent for item in path.rglob("steps.jsonl")}) if path.is_dir() else []
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"找不到 steps.jsonl: {path}")
    choices = "\n".join(f"  {item}" for item in matches[:12])
    raise ValueError(f"路径内有多个 episode，请传入其中一个 episode 目录:\n{choices}")


def _read_rows(episode: Path) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(
        (episode / "steps.jsonl").read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"steps.jsonl 第 {number} 行不是有效 JSON: {exc}") from exc
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _onboard(row: dict[str, Any]) -> dict[str, Any]:
    labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
    value = labels.get("onboard") if isinstance(labels, dict) else {}
    return value if isinstance(value, dict) else {}


def _mission_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    missions = []
    for row in rows:
        value = _onboard(row).get("mission")
        if isinstance(value, dict):
            missions.append(value)
    return missions


def _finite_pair(value: Any) -> tuple[float, float] | None:
    if isinstance(value, dict):
        first = value.get("north", value.get("north_m", value.get("y_north_m")))
        second = value.get("east", value.get("east_m", value.get("x_east_m")))
        value = (first, second)
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        point = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    return point if all(math.isfinite(item) for item in point) else None


def _track_ground_position(
    row: dict[str, Any], track: dict[str, Any]
) -> tuple[float, float] | None:
    for key in (
        "position_estimate_ne",
        "position_ne",
        "ground_position_ne",
        "ground_point_ne",
    ):
        point = _finite_pair(track.get(key))
        if point is not None:
            return point
    point = _project_track_position(row, track)
    if point is not None:
        return point
    return _finite_pair(track.get("world_position"))


def _call_verdict(call: dict[str, Any]) -> str:
    kind = _vote_kind(call)
    return kind or "unknown"


def _frame_directories(episode: Path, requested: Path | None) -> list[Path]:
    candidates = []
    if requested is not None:
        candidates.append(requested.resolve())
    candidates.extend(
        (
            episode.parent.parent / f"{episode.name}-onboard-frames",
            episode.parent / f"{episode.name}-onboard-frames",
            episode.with_name(f"{episode.name}-onboard-frames"),
        )
    )
    return list(dict.fromkeys(path for path in candidates if path.is_dir()))


def _resolve_frame(
    episode: Path,
    row: dict[str, Any],
    row_index: int,
    frame_dirs: list[Path],
) -> Path | None:
    obs = row.get("obs_before") if isinstance(row.get("obs_before"), dict) else {}
    raw = obs.get("rgb_path")
    if isinstance(raw, str) and raw.strip():
        raw_path = Path(raw.replace("\\", "/"))
        candidates = (
            [raw_path]
            if raw_path.is_absolute()
            else [episode / raw_path, episode.parent / raw_path]
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
    step_index = row.get("step_index", row_index)
    try:
        index = int(step_index)
    except (TypeError, ValueError):
        index = row_index
    names = [f"frame_{index:04d}{suffix}" for suffix in (".jpg", ".jpeg", ".png")]
    names.extend(f"frame_{index:05d}{suffix}" for suffix in (".jpg", ".jpeg", ".png"))
    for directory in frame_dirs:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return candidate.resolve()
    return None


def _jpeg_bytes(
    path: Path, *, max_size: tuple[int, int], quality: int, box: list[float] | None = None
) -> bytes:
    from PIL import Image

    with Image.open(path) as source:
        image = source.convert("RGB")
    if box is not None:
        left, top, right, bottom = box
        width, height = image.size
        box_width, box_height = max(1.0, right - left), max(1.0, bottom - top)
        pad_x, pad_y = max(6.0, box_width * 0.45), max(6.0, box_height * 0.45)
        bounds = (
            max(0, int(math.floor(left - pad_x))),
            max(0, int(math.floor(top - pad_y))),
            min(width, int(math.ceil(right + pad_x))),
            min(height, int(math.ceil(bottom + pad_y))),
        )
        image = image.crop(bounds)
    image.thumbnail(max_size, Image.Resampling.LANCZOS)
    from io import BytesIO

    output = BytesIO()
    image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
    return output.getvalue()


def _bbox_for_call(row: dict[str, Any], call: dict[str, Any]) -> list[float] | None:
    for value in (call.get("focus_bbox"), call.get("bbox")):
        if isinstance(value, list) and len(value) == 4:
            try:
                box = [float(item) for item in value]
            except (TypeError, ValueError):
                continue
            if all(math.isfinite(item) for item in box) and box[2] > box[0] and box[3] > box[1]:
                return box
    track_id = str(call.get("track_id", ""))
    for track in _onboard(row).get("tracks") or []:
        if isinstance(track, dict) and str(track.get("id", "")) == track_id:
            value = track.get("bbox")
            if isinstance(value, list) and len(value) == 4:
                try:
                    box = [float(item) for item in value]
                except (TypeError, ValueError):
                    return None
                if all(math.isfinite(item) for item in box) and box[2] > box[0] and box[3] > box[1]:
                    return box
    return None


def _load_config(
    episode: Path,
    rows: list[dict[str, Any]],
    explicit: Path | None,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    if explicit is not None:
        return _load_json(explicit.resolve())
    for mission in [*reversed(_mission_rows(rows)), metadata]:
        for key in ("config", "onboard_config"):
            config = mission.get(key)
            if isinstance(config, dict):
                return config
        for key in ("config_path", "onboard_config_path", "mission_config_path"):
            value = mission.get(key) or metadata.get(key)
            if not isinstance(value, str) or not value:
                continue
            raw_path = Path(value.replace("\\", "/"))
            candidates = (
                [raw_path] if raw_path.is_absolute() else [episode / raw_path, ROOT / raw_path]
            )
            for candidate in candidates:
                if candidate.is_file():
                    return _load_json(candidate.resolve())
    embedded = metadata.get("onboard_config") or metadata.get("mission_config")
    return embedded if isinstance(embedded, dict) else None


def _refs(
    config: dict[str, Any] | None, missions: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, str]]:
    labels: dict[str, str] = {}
    for mission in reversed(missions):
        mission_config = mission.get("config") or mission.get("onboard_config")
        if isinstance(mission_config, dict):
            refs = mission.get("refs") or mission_config.get("refs")
            labels.update(mission_config.get("ref_labels") or {})
            if isinstance(refs, dict):
                return refs, labels
        refs = mission.get("refs") or (mission.get("plan") or {}).get("refs")
        if isinstance(refs, dict):
            return refs, labels
    if isinstance(config, dict):
        refs = config.get("refs")
        labels.update(config.get("ref_labels") or {})
        if isinstance(refs, dict):
            return refs, labels
    return {}, labels


def _geometry(refs: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    zones, routes = [], []
    for name, value in refs.items():
        if not isinstance(name, str) or not isinstance(value, (list, tuple)):
            continue
        if name.startswith("zone:") and len(value) >= 3:
            center = _finite_pair(value)
            try:
                radius = float(value[2])
            except (TypeError, ValueError):
                continue
            if center is not None and math.isfinite(radius) and radius > 0:
                zones.append(
                    {"name": name, "north": center[0], "east": center[1], "radius": radius}
                )
        elif name.startswith("route:"):
            points = [_finite_pair(point) for point in value]
            points = [point for point in points if point is not None]
            if len(points) >= 2:
                routes.append({"name": name, "points": [[point[0], point[1]] for point in points]})
    return zones, routes


def _truth_data(
    truth: dict[str, Any] | None,
    rows: list[dict[str, Any]],
    radius_m: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    if not isinstance(truth, dict):
        return [], [], None
    query = _query_from_rows(rows)
    targets = [item for item in truth.get("targets", []) if isinstance(item, dict)]
    relevant_ids = {
        str(item.get("target_id", index))
        for index, item in enumerate(targets)
        if item.get("true_target") or item.get("count_member") or _object_matches_query(item, query)
    }
    states = {
        str(item.get("target_id", index)): (
            "missed" if str(item.get("target_id", index)) in relevant_ids else "distractor"
        )
        for index, item in enumerate(targets)
    }
    false_calls: list[dict[str, Any]] = []
    for row in rows:
        track_positions = {
            str(track.get("id")): _track_ground_position(row, track)
            for track in (_onboard(row).get("tracks") or [])
            if isinstance(track, dict) and track.get("id")
        }
        for call in _onboard(row).get("slow_calls") or []:
            if not isinstance(call, dict) or _call_verdict(call) != "positive":
                continue
            point = track_positions.get(str(call.get("track_id")))
            if point is None:
                continue
            nearest = _nearest_object(truth, point, radius_m)
            if nearest is None:
                false_calls.append({"north": point[0], "east": point[1], "label": "未匹配告警"})
                continue
            target_id = str(nearest.get("target_id", ""))
            matched = _object_matches_query(nearest, query) or (
                query == "object" and bool(nearest.get("true_target"))
            )
            if matched:
                states[target_id] = "matched"
            elif states.get(target_id) != "matched":
                states[target_id] = "false"
                false_calls.append({"north": point[0], "east": point[1], "label": "类别不符"})
    objects = []
    for index, target in enumerate(targets):
        pose = target.get("pose") if isinstance(target.get("pose"), dict) else {}
        point = _finite_pair([pose.get("y_north_m"), pose.get("x_east_m")])
        if point is None:
            continue
        words = target.get("category_words") or target.get("semantic_words") or []
        category = (
            " / ".join(str(word) for word in words)
            if words
            else str(
                target.get("class") or target.get("category") or target.get("label") or "未知类别"
            )
        )
        target_id = str(target.get("target_id", index))
        objects.append(
            {
                "id": target_id,
                "north": point[0],
                "east": point[1],
                "category": category,
                "status": states.get(target_id, "missed"),
                "true_target": bool(target.get("true_target")),
            }
        )
    return objects, false_calls, truth


def _collect_tracks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    verdict_by_id: dict[str, str] = {}
    executor_context = None
    result = []
    for row_index, row in enumerate(rows):
        onboard = _onboard(row)
        context = (onboard.get("step"), onboard.get("skill")) if onboard.get("skill") else None
        if context is not None and context != executor_context:
            verdict_by_id.clear()
            executor_context = context
        tracks = onboard.get("tracks") or []
        calls = {
            str(call.get("track_id")): call
            for call in (onboard.get("slow_calls") or [])
            if isinstance(call, dict) and call.get("track_id")
        }
        for track in tracks:
            if not isinstance(track, dict):
                continue
            track_id = str(track.get("id", "?"))
            call = calls.get(track_id)
            raw_verdict = track.get("verdict")
            verdict = (
                _call_verdict(call)
                if call
                else str(raw_verdict or verdict_by_id.get(track_id, "unknown"))
            )
            if verdict not in {"positive", "negative", "unknown"}:
                verdict = _call_verdict({"identity": verdict})
            if verdict != "unknown":
                verdict_by_id[track_id] = verdict
            point = _track_ground_position(row, track)
            if point is None:
                continue
            result.append(
                {
                    "track_id": track_id,
                    "step": int(row.get("step_index", row_index))
                    if str(row.get("step_index", row_index)).lstrip("-").isdigit()
                    else row_index,
                    "north": point[0],
                    "east": point[1],
                    "verdict": verdict,
                }
            )
    return result


def _map_svg(
    positions: list[list[float]],
    tracks: list[dict[str, Any]],
    zones: list[dict[str, Any]],
    routes: list[dict[str, Any]],
    truth_objects: list[dict[str, Any]],
    false_calls: list[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> str:
    width, height, margin = 920.0, 620.0, 54.0
    points: list[tuple[float, float]] = [(float(p[0]), float(p[1])) for p in positions]
    points.extend((item["north"], item["east"]) for item in tracks)
    points.extend((item["north"], item["east"]) for item in truth_objects)
    points.extend((item["north"], item["east"]) for item in false_calls)
    points.extend((item["north"], item["east"]) for item in findings)
    for zone in zones:
        points.extend(
            (
                (zone["north"] - zone["radius"], zone["east"] - zone["radius"]),
                (zone["north"] + zone["radius"], zone["east"] + zone["radius"]),
            )
        )
    for route in routes:
        points.extend((float(p[0]), float(p[1])) for p in route["points"])
    if not points:
        points = [(0.0, 0.0)]
    min_n, max_n = min(p[0] for p in points), max(p[0] for p in points)
    min_e, max_e = min(p[1] for p in points), max(p[1] for p in points)
    span_n, span_e = max(1.0, max_n - min_n), max(1.0, max_e - min_e)
    scale = min((width - 2 * margin) / span_e, (height - 2 * margin) / span_n)
    offset_x = (width - 2 * margin - span_e * scale) / 2
    offset_y = (height - 2 * margin - span_n * scale) / 2

    def xy(north: float, east: float) -> tuple[float, float]:
        return margin + offset_x + (east - min_e) * scale, height - margin - offset_y - (
            north - min_n
        ) * scale

    fragments = [
        f'<svg class="map-svg" viewBox="0 0 {width:g} {height:g}" '
        'role="img" aria-label="飞行轨迹俯视图">',
        '<rect x="0" y="0" width="100%" height="100%" fill="#101d28" rx="14"/>',
    ]
    # A light metric grid makes the arbitrary local-N/E frame legible without a basemap.
    for fraction in (0.25, 0.5, 0.75):
        gx = margin + fraction * (width - 2 * margin)
        gy = margin + fraction * (height - 2 * margin)
        fragments.append(
            f'<path d="M {gx:.1f} {margin} V {height - margin} '
            f'M {margin} {gy:.1f} H {width - margin}" class="map-grid"/>'
        )
    for zone in zones:
        cx, cy = xy(zone["north"], zone["east"])
        radius = zone["radius"] * scale
        fragments.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" class="zone"/>'
            f'<text x="{cx + 6:.1f}" y="{cy - radius + 16:.1f}" class="map-label">'
            f"{html.escape(zone['name'])}</text>"
        )
    for route in routes:
        coords = " ".join(
            f"{xy(float(p[0]), float(p[1]))[0]:.1f},{xy(float(p[0]), float(p[1]))[1]:.1f}"
            for p in route["points"]
        )
        fragments.append(f'<polyline points="{coords}" class="route"/>')
        for north, east in route["points"]:
            x, y = xy(float(north), float(east))
            fragments.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" class="route-point"/>')
        x, y = xy(float(route["points"][0][0]), float(route["points"][0][1]))
        fragments.append(
            f'<text x="{x + 6:.1f}" y="{y - 7:.1f}" class="map-label">'
            f"{html.escape(route['name'])}</text>"
        )
    if len(positions) > 1:
        coords = " ".join(
            f"{xy(float(p[0]), float(p[1]))[0]:.1f},{xy(float(p[0]), float(p[1]))[1]:.1f}"
            for p in positions
        )
        fragments.append(f'<polyline points="{coords}" class="trajectory"/>')
    home = tuple(positions[0]) if positions else (0.0, 0.0)
    hx, hy = xy(float(home[0]), float(home[1]))
    fragments.append(
        f'<circle cx="{hx:.1f}" cy="{hy:.1f}" r="9" class="home"/>'
        f'<path d="M {hx - 5:.1f} {hy:.1f}h10 M {hx:.1f} {hy - 5:.1f}v10" '
        f'class="home-cross"/><text x="{hx + 12:.1f}" y="{hy + 4:.1f}" '
        'class="map-label">返航点</text>'
    )
    for item in tracks:
        x, y = xy(item["north"], item["east"])
        css = {
            "positive": "track-positive",
            "negative": "track-negative",
            "unknown": "track-unknown",
        }[item["verdict"]]
        fragments.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.2" class="{css}">'
            f"<title>轨迹 {html.escape(item['track_id'])} · 第 {item['step']} 步 · "
            f"{item['verdict']}</title></circle>"
        )
    for obj in truth_objects:
        x, y = xy(obj["north"], obj["east"])
        css = {
            "matched": "truth-matched",
            "false": "truth-false",
            "missed": "truth-missed",
            "distractor": "truth-distractor",
        }[obj["status"]]
        fragments.append(
            f'<path d="M {x:.1f} {y - 7:.1f} L {x + 7:.1f} {y:.1f} '
            f'L {x:.1f} {y + 7:.1f} L {x - 7:.1f} {y:.1f} Z" class="{css}">'
            f"<title>{html.escape(obj['category'])} · {obj['status']}</title></path>"
        )
        fragments.append(
            f'<text x="{x + 9:.1f}" y="{y - 7:.1f}" class="map-label">'
            f"{html.escape(obj['category'][:24])}</text>"
        )
    for item in false_calls:
        x, y = xy(item["north"], item["east"])
        fragments.append(
            f'<path d="M {x - 5:.1f} {y - 5:.1f}l10 10m0-10l-10 10" '
            f'class="false-call"><title>{html.escape(item["label"])}</title></path>'
        )
    for item in findings:
        x, y = xy(item["north"], item["east"])
        fragments.append(
            f'<rect x="{x - 6:.1f}" y="{y - 6:.1f}" width="12" height="12" '
            f'class="finding"><title>{html.escape(item["label"])}</title></rect>'
        )
    if positions:
        fx, fy = xy(float(positions[-1][0]), float(positions[-1][1]))
        fragments.append(
            f'<circle cx="{fx:.1f}" cy="{fy:.1f}" r="7" class="flight-end"/>'
            f'<text x="{fx + 10:.1f}" y="{fy + 4:.1f}" class="map-label">终点</text>'
        )
    fragments.append(
        '<text x="18" y="24" class="axis-label">北 ↑</text>'
        '<text x="830" y="602" class="axis-label">东 →</text></svg>'
    )
    return "".join(fragments)


def _flight_positions(rows: list[dict[str, Any]]) -> list[list[float]]:
    result = []
    observations = [row.get("obs_before") for row in rows]
    if rows and isinstance(rows[-1].get("obs_after"), dict):
        observations.append(rows[-1]["obs_after"])
    for obs in observations:
        obs = obs if isinstance(obs, dict) else {}
        pose = obs.get("pose") if isinstance(obs.get("pose"), dict) else {}
        local = pose.get("local_ned_m")
        if isinstance(local, (list, tuple)) and len(local) >= 2:
            try:
                north, east = float(local[0]), float(local[1])
            except (TypeError, ValueError):
                continue
            if math.isfinite(north) and math.isfinite(east):
                result.append([north, east])
    return result


def _findings(rows: list[dict[str, Any]], mission: dict[str, Any]) -> list[dict[str, Any]]:
    """Preserve reported positions independently of projected track observations."""
    result = []
    for alert in mission.get("alerts") or []:
        if not isinstance(alert, str) or ":alert:" not in alert:
            continue
        _, separator, coords = alert.rpartition("@")
        point = _finite_pair(coords.split(",")) if separator else None
        if point:
            result.append({"north": point[0], "east": point[1], "label": alert})
    for position in mission.get("positions") or []:
        point = _finite_pair(position.split(",") if isinstance(position, str) else position)
        if point:
            result.append({"north": point[0], "east": point[1], "label": "任务发现位置"})
    for row in rows:
        confirmed = _onboard(row).get("confirmed") or {}
        point = _finite_pair(confirmed.get("position_estimate_ne"))
        if point and not any((item["north"], item["east"]) == point for item in result):
            result.append({"north": point[0], "east": point[1], "label": "确认目标位置"})
    return result


def _distance_flown(positions: list[list[float]]) -> float:
    return sum(math.dist(a, b) for a, b in zip(positions, positions[1:], strict=False))


def _assets_for_calls(
    episode: Path,
    rows: list[dict[str, Any]],
    frame_dirs: list[Path],
    max_raw_bytes: int,
) -> tuple[list[dict[str, Any]], dict[str, str], int, int]:
    from PIL import Image

    used = 0
    skipped = 0
    cache: dict[Path, tuple[str, tuple[int, int]]] = {}
    assets: dict[str, str] = {}
    calls: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        onboard = _onboard(row)
        for call_index, call in enumerate(onboard.get("slow_calls") or []):
            if not isinstance(call, dict):
                continue
            track_id = str(call.get("track_id", "?"))
            tracks = onboard.get("tracks") or []
            track = next(
                (
                    item
                    for item in tracks
                    if isinstance(item, dict) and str(item.get("id", "")) == track_id
                ),
                {},
            )
            point = _track_ground_position(row, track) if track else None
            bbox = _bbox_for_call(row, call)
            source = _resolve_frame(episode, row, row_index, frame_dirs)
            frame_asset = crop_asset = None
            frame_dimensions = None
            if source is not None:
                if source in cache:
                    frame_asset, frame_dimensions = cache[source]
                else:
                    try:
                        payload = _jpeg_bytes(source, max_size=FRAME_SIZE, quality=58)
                    except (OSError, ValueError):
                        payload = b""
                    if payload and used + len(payload) <= max_raw_bytes:
                        with Image.open(source) as image:
                            frame_dimensions = image.size
                        used += len(payload)
                        frame_asset = f"image-{len(assets)}"
                        assets[frame_asset] = "data:image/jpeg;base64," + base64.b64encode(
                            payload
                        ).decode("ascii")
                        cache[source] = (frame_asset, frame_dimensions)
                if bbox is not None:
                    try:
                        crop_payload = _jpeg_bytes(source, max_size=CROP_SIZE, quality=62, box=bbox)
                    except (OSError, ValueError):
                        crop_payload = b""
                    if crop_payload and used + len(crop_payload) <= max_raw_bytes:
                        used += len(crop_payload)
                        crop_asset = f"image-{len(assets)}"
                        assets[crop_asset] = "data:image/jpeg;base64," + base64.b64encode(
                            crop_payload
                        ).decode("ascii")
            if source is None or (frame_asset is None and crop_asset is None):
                skipped += 1
            calls.append(
                {
                    "id": f"call-{row_index}-{call_index}",
                    "step": row.get("step_index", row_index),
                    "track_id": track_id,
                    "identity": str(call.get("identity") or "unknown"),
                    "decision": str(call.get("decision") or ""),
                    "verdict": _call_verdict(call),
                    "reason": str(call.get("reason") or ""),
                    "confidence": call.get("confidence"),
                    "latency_s": call.get("latency_s"),
                    "bbox": bbox,
                    "position_ne": list(point) if point else None,
                    "frame_asset": frame_asset,
                    "crop_asset": crop_asset,
                    "frame_width": frame_dimensions[0] if frame_dimensions else None,
                    "frame_height": frame_dimensions[1] if frame_dimensions else None,
                    "source_name": source.name if source else None,
                }
            )
    return calls, assets, used, skipped


def _latest_mission(missions: list[dict[str, Any]]) -> dict[str, Any]:
    return missions[-1] if missions else {}


def _summary(
    episode: Path,
    metadata: dict[str, Any],
    rows: list[dict[str, Any]],
    positions: list[list[float]],
    tracks: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    mission: dict[str, Any],
    truth: dict[str, Any] | None,
    mission_metrics: dict[str, Any] | None,
) -> dict[str, Any]:
    onboard_confirmed = [
        _onboard(row).get("confirmed")
        for row in rows
        if isinstance(_onboard(row).get("confirmed"), dict)
    ]
    final_confirm = onboard_confirmed[-1] if onboard_confirmed else None
    return {
        "episode": episode.name,
        "task_text": metadata.get("task_text") or (rows[0].get("task_text") if rows else ""),
        "flight_status": metadata.get("status") or "未知",
        "step_count": len(rows),
        "distance_flown_m": round(_distance_flown(positions), 1),
        "track_observations": len(tracks),
        "unique_tracks": len({item["track_id"] for item in tracks}),
        "slow_calls": len(calls),
        "positive_calls": sum(call["verdict"] == "positive" for call in calls),
        "negative_calls": sum(call["verdict"] == "negative" for call in calls),
        "confirmed": final_confirm,
        "truth_available": truth is not None,
        "mission_completed": bool(mission_metrics.get("mission_completed"))
        if mission_metrics
        else None,
    }


def _html_page(data: dict[str, Any]) -> str:
    template = (ROOT / "scripts/flight_review.html").read_text(encoding="utf-8")
    summary = data["summary"]
    values = {
        "TITLE": html.escape(str(summary.get("task_text") or summary["episode"])),
        "EPISODE": html.escape(str(summary["episode"])),
        "STATUS": html.escape(str(summary["flight_status"])),
        "MAP": data["map_svg"],
        "NOTE": html.escape(str(data["asset_note"])),
        "DATA": data["json_payload"],
    }
    # Substitute once so untrusted episode text cannot introduce template tokens.
    return re.sub(r"@@(TITLE|EPISODE|STATUS|MAP|NOTE|DATA)@@", lambda m: values[m[1]], template)


def render_flight_review(
    episode: Path,
    output: Path,
    *,
    truth: dict[str, Any] | None = None,
    truth_dir: Path | None = None,
    truth_path: Path | None = None,
    mission_config_path: Path | None = None,
    frames_dir: Path | None = None,
    match_radius_m: float = 10.0,
    max_html_mb: float = 15.0,
) -> dict[str, Any]:
    """Build and write the review page; exposed for small deterministic tests."""
    if match_radius_m <= 0 or not math.isfinite(match_radius_m):
        raise ValueError("match_radius_m 必须是正数")
    if max_html_mb <= 0 or not math.isfinite(max_html_mb):
        raise ValueError("max_html_mb 必须是正数")
    episode = _episode_path(episode)
    rows = _read_rows(episode)
    metadata = _load_json(episode / "episode.json") if (episode / "episode.json").is_file() else {}
    if truth_path is not None:
        truth = _load_json(truth_path.resolve())
    elif truth is None:
        _, truth = truth_for_episode(episode, truth_dir.resolve() if truth_dir else None)
    config = _load_config(episode, rows, mission_config_path, metadata)
    missions = _mission_rows(rows)
    mission = _latest_mission(missions)
    refs, _ = _refs(config, missions)
    zones, routes = _geometry(refs)
    positions = _flight_positions(rows)
    tracks = _collect_tracks(rows)
    truth_objects, false_calls, truth = _truth_data(truth, rows, match_radius_m)
    findings = _findings(rows, mission)
    max_bytes = int(max_html_mb * 1024 * 1024)
    # Base64 adds one third; reserve the rest for layout, text, SVG and script.
    image_budget = min(DEFAULT_IMAGE_BUDGET, max(0, int((max_bytes - 768 * 1024) * 0.62)))
    calls, assets, embedded_bytes, skipped_images = _assets_for_calls(
        episode, rows, _frame_directories(episode, frames_dir), image_budget
    )
    mission_config = config if isinstance(config, dict) else None
    metrics = _mission_metrics(rows, truth, match_radius_m, mission_config) if truth else None
    scoring_note = None
    if truth and any(target.get("true_target") for target in truth.get("targets", [])):
        try:
            metrics = score_episode(
                episode, truth, radius_m=match_radius_m, mission_config=mission_config
            )
            metrics.pop("episode", None)
        except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
            scoring_note = f"完整评分不可用，保留位置关联和任务告警评分：{exc}"
    summary = _summary(episode, metadata, rows, positions, tracks, calls, mission, truth, metrics)
    previous_executor_step = None
    previous_history: list[dict[str, Any]] = []
    steps = []
    call_cursor = 0
    for row_index, row in enumerate(rows):
        onboard = _onboard(row)
        row_calls = len(
            [call for call in (onboard.get("slow_calls") or []) if isinstance(call, dict)]
        )
        step_index = row.get("step_index", row_index)
        executor_step = onboard.get("step")
        transition = None
        if executor_step and previous_executor_step and executor_step != previous_executor_step:
            transition = f"{previous_executor_step} → {executor_step}"
        if executor_step:
            previous_executor_step = executor_step
        history = (onboard.get("mission") or {}).get("history") or []
        new_history = [entry for entry in history if entry not in previous_history]
        previous_history = history
        action = row.get("action") if isinstance(row.get("action"), dict) else {}
        detail = ", ".join(
            f"{key}={value}" for key, value in action.items() if key != "kind" and value is not None
        )
        timestamp = row.get("receive_wall_time")
        time_label = f"t={timestamp}" if isinstance(timestamp, (int, float)) else ""
        steps.append(
            {
                "index": step_index,
                "time": time_label,
                "state_before": onboard.get("state_before"),
                "state_after": onboard.get("state"),
                "executor_step": executor_step,
                "executor_skill": onboard.get("skill"),
                "executor_state": onboard.get("step_state"),
                "transition": transition,
                "history": new_history,
                "action": action.get("kind", "未知动作"),
                "action_detail": detail,
                "calls": [call["id"] for call in calls[call_cursor : call_cursor + row_calls]],
            }
        )
        call_cursor += row_calls
    mission_view = {
        "history": mission.get("history") or [],
        "alerts": mission.get("alerts") or [],
        "positions": mission.get("positions") or [],
        "counts": mission.get("counts") or {},
        "answers": mission.get("answers") or [],
        "evidence": mission.get("evidence") or [],
    }
    if not missions and summary["confirmed"]:
        position = _finite_pair(summary["confirmed"].get("position_estimate_ne"))
        if position:
            mission_view["positions"] = [f"{position[0]:.2f},{position[1]:.2f}"]
    plan = mission.get("plan") if isinstance(mission.get("plan"), dict) else None
    if plan is None and isinstance(config, dict) and isinstance(config.get("plan"), dict):
        plan = config["plan"]
    if plan is None:
        plan = next(
            (entry["plan"] for entry in missions if isinstance(entry.get("plan"), dict)), None
        )
    payload = {
        "summary": summary,
        "steps": steps,
        "calls": calls,
        "assets": assets,
        "mission": mission_view,
        "plan": plan,
        "mission_metrics": metrics,
        "truth_objects": truth_objects,
    }
    map_svg = _map_svg(positions, tracks, zones, routes, truth_objects, false_calls, findings)

    def page_content() -> bytes:
        json_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        json_payload = (
            json_payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        )
        missing = sum(not assets.get(call["frame_asset"]) for call in calls)
        asset_note = f"图像资源 {len(assets)} 个；缺图或达到上限 {missing} 条。"
        if scoring_note:
            asset_note += scoring_note
        return _html_page(
            {
                "summary": summary,
                "map_svg": map_svg,
                "asset_note": asset_note,
                "json_payload": json_payload,
            }
        ).encode("utf-8")

    page_bytes = page_content()
    while len(page_bytes) > max_bytes and assets:
        assets.popitem()
        page_bytes = page_content()
    if len(page_bytes) > max_bytes:
        raise ValueError(
            f"页面大小 {len(page_bytes) / (1024 * 1024):.2f} MiB 超过限制 {max_html_mb:.2f} MiB；"
            "文本记录本身过大，请提高 --max-html-mb 或缩小输入 episode。"
        )
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(page_bytes)
    embedded_bytes = sum(len(base64.b64decode(uri.split(",", 1)[1])) for uri in assets.values())
    return {
        "output": output,
        "episode": episode,
        "bytes": len(page_bytes),
        "steps": len(rows),
        "slow_calls": len(calls),
        "truth_available": truth is not None,
        "embedded_image_bytes": embedded_bytes,
        "max_html_bytes": max_bytes,
        "world": episode_world_id(episode),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "episode", type=Path, help="episode 目录，或只含一个 episode 的 campaign 目录"
    )
    parser.add_argument("--out", type=Path, default=None, help="输出 HTML（默认写到工作树 tmp/）")
    parser.add_argument("--truth", type=Path, help="直接指定场景 truth JSON")
    parser.add_argument("--truth-dir", type=Path, help="truth JSON 所在目录")
    parser.add_argument(
        "--config",
        "--mission-config",
        dest="mission_config",
        type=Path,
        help="可选任务配置，用于绘制 zone/route",
    )
    parser.add_argument("--frames-dir", type=Path, help="可选 *-onboard-frames 图片目录")
    parser.add_argument("--match-radius-m", type=float, default=10.0, help="真值目标匹配半径")
    parser.add_argument(
        "--max-html-mb", type=float, default=15.0, help="HTML 最大字节数上限（MiB）"
    )
    args = parser.parse_args(argv)
    try:
        episode = _episode_path(args.episode)
        output = args.out or ROOT / "tmp" / f"{episode.name}-flight-review.html"
        result = render_flight_review(
            episode,
            output,
            truth_dir=args.truth_dir,
            truth_path=args.truth,
            mission_config_path=args.mission_config,
            frames_dir=args.frames_dir,
            match_radius_m=args.match_radius_m,
            max_html_mb=args.max_html_mb,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                **result,
                "output": str(result["output"]),
                "episode": str(result["episode"]),
                "size_mib": round(result["bytes"] / (1024 * 1024), 2),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
