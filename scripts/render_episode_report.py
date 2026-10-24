"""Render recorded SIM episodes as self-contained HTML review reports."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.render_flight_review import (  # noqa: E402
    _finite_pair,
    _flight_positions,
    _geometry,
    _latest_mission,
    _mission_rows,
    _onboard,
    _refs,
    _track_ground_position,
)
from scripts.score_onboard_loop import (  # noqa: E402
    _object_matches_query,
    _row_sim_time,
    truth_for_episode,
)


def _json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _rows(episode: Path) -> list[dict[str, Any]]:
    result = []
    for line_no, line in enumerate(
        (episode / "steps.jsonl").read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{episode / 'steps.jsonl'}:{line_no}: {exc}") from exc
        if isinstance(row, dict):
            result.append(row)
    return result


def _episode_dirs(root: Path) -> list[Path]:
    if (root / "steps.jsonl").is_file():
        return [root]
    return sorted({p.parent for p in root.rglob("steps.jsonl")})


def _pose(row: dict[str, Any], after: bool = False) -> tuple[float, float, float] | None:
    obs = row.get("obs_after" if after else "obs_before") or {}
    pose = obs.get("pose") if isinstance(obs, dict) else None
    local = pose.get("local_ned_m") if isinstance(pose, dict) else None
    if isinstance(local, list) and len(local) >= 3:
        try:
            n, e, down = map(float, local[:3])
            if all(math.isfinite(v) for v in (n, e, down)):
                return n, e, -down
        except (TypeError, ValueError):
            pass
    return None


def _ground_position(row: dict[str, Any], track: dict[str, Any]) -> tuple[float, float] | None:
    for key in (
        "ground_ne",
        "position_estimate_ne",
        "position_ne",
        "ground_position_ne",
        "ground_point_ne",
    ):
        point = _finite_pair(track.get(key))
        if point is not None:
            return point
    return _track_ground_position(row, track)


def _config(episode: Path, rows: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
    missions = _mission_rows(rows)
    refs, _ = _refs(None, missions)
    cfg = {}
    if isinstance(metadata.get("onboard_config"), dict):
        cfg = metadata["onboard_config"]
    elif isinstance(metadata.get("mission_config"), dict):
        cfg = metadata["mission_config"]
    if refs and not cfg.get("refs"):
        cfg = {**cfg, "refs": refs}
    return cfg


def _truth_comparison(
    rows: list[dict[str, Any]], truth: dict[str, Any] | None, radius: float
) -> list[dict[str, Any]]:
    if not truth:
        return []
    mission = {}
    for candidate in _mission_rows(rows):
        mission = candidate or mission
    positions = []
    for value in mission.get("positions") or []:
        point = _finite_pair(value.split(",") if isinstance(value, str) else value)
        if point:
            positions.append(point)
    targets = [
        t
        for t in truth.get("targets", [])
        if isinstance(t, dict) and (t.get("count_member") or t.get("true_target"))
    ]
    remaining = list(positions)
    matched_positions: dict[str, tuple[float, float]] = {}
    for target in targets:
        pose = target.get("pose") or {}
        p = (float(pose.get("y_north_m", 0)), float(pose.get("x_east_m", 0)))
        if not remaining:
            continue
        nearest = min(remaining, key=lambda q: math.dist(q, p))
        if math.dist(nearest, p) <= radius:
            matched_positions[str(target.get("target_id"))] = nearest
            remaining.remove(nearest)

    query = next((str(r.get("task_text")) for r in rows if r.get("task_text")), "")
    result = []
    for target in targets:
        ident = str(target.get("target_id", "unknown"))
        pose = target.get("pose") or {}
        point = (float(pose.get("y_north_m", 0)), float(pose.get("x_east_m", 0)))
        nearest_detection = None
        for row_i, row in enumerate(rows):
            onboard = _onboard(row)
            for track in onboard.get("tracks") or []:
                if not isinstance(track, dict):
                    continue
                estimate = _ground_position(row, track)
                if estimate is None:
                    continue
                distance = math.dist(estimate, point)
                candidate = {
                    "step": row.get("step_index", row_i),
                    "track_id": track.get("id"),
                    "distance_m": round(distance, 2),
                    "position_ne": list(estimate),
                    "verdict": track.get("verdict"),
                }
                if nearest_detection is None or distance < nearest_detection["distance_m"]:
                    nearest_detection = candidate
        evidence = "未检出候选"
        if nearest_detection:
            evidence = (
                "候选有记录但地面估计偏离"
                if nearest_detection["distance_m"] > radius
                else "候选接近目标但未进入最终计数"
            )
            for row in rows:
                if row.get("step_index") == nearest_detection["step"]:
                    onboard = _onboard(row)
                    calls = [
                        c
                        for c in onboard.get("slow_calls") or []
                        if str(c.get("track_id")) == str(nearest_detection["track_id"])
                    ]
                    if calls:
                        call = calls[-1]
                        evidence += (
                            f"；慢模型 {call.get('decision') or call.get('identity') or '未知'}"
                        )
                        if call.get("reason"):
                            evidence += f"（{call['reason']}）"
                    break
        result.append(
            {
                "id": ident,
                "category": target.get("label")
                or target.get("class")
                or "/".join(target.get("category_words") or []),
                "north": point[0],
                "east": point[1],
                "counted": ident in matched_positions,
                "match_distance_m": round(math.dist(point, matched_positions[ident]), 2)
                if ident in matched_positions
                else None,
                "nearest_detection": nearest_detection,
                "loss_hint": evidence,
                "is_query_target": bool(target.get("true_target"))
                or _object_matches_query(target, query),
            }
        )
    return result


def _events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    previous_count = None
    for i, row in enumerate(rows):
        on = _onboard(row)
        mission = on.get("mission") or {}
        counts = mission.get("counts") or {}
        count = sum(int(v) for v in counts.values() if isinstance(v, (int, float)))
        delta = None if previous_count is None else count - previous_count
        previous_count = count
        calls = [c for c in on.get("slow_calls") or [] if isinstance(c, dict)]
        safety = on.get("safety") or {}
        revisit = on.get("revisit_candidate_snapshots") or on.get("revisit") or {}
        events.append(
            {
                "step": row.get("step_index", i),
                "action": (row.get("action") or {}).get("kind", ""),
                "calls": calls,
                "count": count,
                "count_delta": delta,
                "revisit": revisit,
                "safety": safety,
            }
        )
    return events


def _obstacles(
    episode: Path, metadata: dict[str, Any], truth: dict[str, Any] | None
) -> list[dict[str, float]]:
    sdf_path = metadata.get("world_sdf")
    if isinstance(sdf_path, str) and Path(sdf_path).is_file():
        try:
            tree = ET.parse(sdf_path)
            result = []
            for model in tree.iter("model"):
                pose_node = model.find("pose")
                if pose_node is None:
                    continue
                vals = [float(x) for x in (pose_node.text or "").split()[:3]]
                if len(vals) == 3:
                    result.append({"north": vals[1], "east": vals[0], "radius": 3.0})
            if result:
                return result
        except (OSError, ET.ParseError, ValueError):
            pass
    result = []
    for target in (truth or {}).get("targets", []):
        if not isinstance(target, dict) or "obstacle" not in str(target.get("target_id", "")):
            continue
        pose = target.get("pose") or {}
        try:
            result.append(
                {"north": float(pose["y_north_m"]), "east": float(pose["x_east_m"]), "radius": 3.0}
            )
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _map_svg(
    positions: list[list[float]],
    tracks: list[dict[str, Any]],
    comparison: list[dict[str, Any]],
    events: list[dict[str, Any]],
    zones: list[dict[str, Any]],
    obstacles: list[dict[str, float]],
) -> str:
    pts = [(float(p[0]), float(p[1])) for p in positions]
    pts += [(x["north"], x["east"]) for x in comparison]
    pts += [(x["north"], x["east"]) for x in tracks]
    pts += [(x["north"], x["east"]) for x in obstacles]
    for z in zones:
        pts.append((z["north"] - z["radius"], z["east"] - z["radius"]))
        pts.append((z["north"] + z["radius"], z["east"] + z["radius"]))
    if not pts:
        pts = [(0, 0)]
    min_n, max_n = min(x[0] for x in pts), max(x[0] for x in pts)
    min_e, max_e = min(x[1] for x in pts), max(x[1] for x in pts)
    scale = min(820 / max(max_e - min_e, 1), 540 / max(max_n - min_n, 1))

    def xy(n, e):
        return 70 + (e - min_e) * scale, 580 - (n - min_n) * scale

    out = [
        '<svg id="map" viewBox="0 0 960 640" role="img" aria-label="俯视轨迹图"><rect width="960" height="640" fill="#f6f8f8"/>'
    ]
    for zone in zones:
        x, y = xy(zone["north"], zone["east"])
        out.append(
            f'<circle class="work-area" cx="{x:.1f}" cy="{y:.1f}" r="{zone["radius"] * scale:.1f}"/>'
        )
    for obstacle in obstacles:
        x, y = xy(obstacle["north"], obstacle["east"])
        r = max(4, obstacle["radius"] * scale)
        out.append(f'<circle class="obstacle" cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}"/>')
    if positions:
        coords = " ".join(f"{xy(n, e)[0]:.1f},{xy(n, e)[1]:.1f}" for n, e in positions)
        out.append(f'<polyline class="path" points="{coords}"/>')
        for i, (n, e) in enumerate(positions):
            x, y = xy(n, e)
            hue = (i / max(len(positions) - 1, 1)) * 120
            out.append(
                f'<circle class="step" cx="{x:.1f}" cy="{y:.1f}" r="4" fill="hsl({hue:.0f} 65% 42%)"><title>步 {i}</title></circle>'
            )
        x, y = xy(*positions[0])
        out.append(f'<path class="home" d="M{x - 7:.1f} {y:.1f}h14m-7-7v14"/>')
    for obj in comparison:
        x, y = xy(obj["north"], obj["east"])
        css = "counted" if obj["counted"] else "missed"
        out.append(
            f'<path class="{css}" d="M{x:.1f} {y - 8:.1f}l8 8-8 8-8-8z"><title>{html.escape(str(obj["category"]))}: {"已计数" if obj["counted"] else "漏计"}</title></path>'
        )
    for tr in tracks:
        x, y = xy(tr["north"], tr["east"])
        out.append(
            f'<circle class="estimate" cx="{x:.1f}" cy="{y:.1f}" r="3"><title>检测/航迹第 {tr["step"]} 步</title></circle>'
        )
    for event in events:
        if event["safety"].get("rejected"):
            idx = next((i for i, p in enumerate(positions) if i == event["step"]), None)
            if idx is not None:
                x, y = xy(*positions[idx])
                out.append(f'<path class="safety" d="M{x - 6:.1f} {y - 6:.1f}l12 12m0-12l-12 12"/>')
    out.append('<text x="18" y="28">北 ↑</text><text x="850" y="620">东 →</text></svg>')
    return "".join(out)


STYLE = """body{font:14px system-ui,'Microsoft YaHei',sans-serif;margin:0;background:#f2f5f5;color:#17252c}header{background:#18323a;color:white;padding:22px 4vw}main{max-width:1440px;margin:auto;padding:18px 3vw}section{margin:18px 0;padding:16px;background:white;border:1px solid #d7e0e2;border-radius:6px;overflow:auto}h1{font-size:24px;margin:0}h2{font-size:18px;margin:0 0 12px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;vertical-align:top;padding:8px;border-bottom:1px solid #e2e8e9}th{background:#edf2f3;position:sticky;top:0}svg{width:100%;max-height:680px}.path{fill:none;stroke:#315966;stroke-width:2}.step{stroke:white;stroke-width:1}.home{stroke:#202c31;stroke-width:3}.work-area{fill:#5da99a18;stroke:#378577;stroke-dasharray:6 4}.obstacle{fill:#8d999d66;stroke:#64757b}.counted{fill:#1e9b70;stroke:#12694c}.missed{fill:#e2534d;stroke:#9e2925}.estimate{fill:#357fbd}.safety{stroke:#c73532;stroke-width:3}.chart{height:220px;width:100%}.bar{fill:#3b8990}.warn{color:#bb302e;font-weight:700}.muted{color:#62757a}.summary{display:flex;gap:22px;flex-wrap:wrap}.summary b{font-size:18px}code{word-break:break-all}"""


def render_episode_report(
    episode: Path,
    output: Path,
    *,
    truth_path: Path | None = None,
    thumbnails: bool = True,
    radius_m: float = 10.0,
) -> dict[str, Any]:
    episode = episode.resolve()
    rows = _rows(episode)
    metadata = _json(episode / "episode.json")
    if truth_path:
        truth = _json(truth_path)
    else:
        _, truth = truth_for_episode(episode)
    comparison = _truth_comparison(rows, truth, radius_m)
    missions = _mission_rows(rows)
    mission = _latest_mission(missions)
    for candidate in missions:
        if candidate.get("positions") or candidate.get("counts"):
            mission = candidate
    config = _config(episode, rows, metadata)
    zones, _ = _geometry(config.get("refs") or {})
    tracks = []
    for i, row in enumerate(rows):
        for track in _onboard(row).get("tracks") or []:
            if not isinstance(track, dict):
                continue
            point = _ground_position(row, track)
            if point is not None:
                tracks.append(
                    {"step": row.get("step_index", i), "north": point[0], "east": point[1]}
                )
    events = _events(rows)
    positions = _flight_positions(rows)
    obstacles = _obstacles(episode, metadata, truth)
    svg = _map_svg(positions, tracks, comparison, events, zones, obstacles)
    altitudes = [_pose(row) for row in rows]
    altitudes = [(p[2] if p else None) for p in altitudes]
    times = [_row_sim_time(row) for row in rows]
    start = next((t for t in times if t is not None), 0.0)
    times = [(t - start if t is not None else float(i)) for i, t in enumerate(times)]
    max_alt = max([a for a in altitudes if a is not None] or [1])
    chart_points = " ".join(
        f"{40 + i * 860 / max(len(rows) - 1, 1):.1f},{190 - (a or 0) * 160 / max_alt:.1f}"
        for i, a in enumerate(altitudes)
    )
    chart = f'<svg class="chart" viewBox="0 0 940 220"><path d="M40 20V190H920" fill="none" stroke="#8da0a4"/><polyline class="path" points="{chart_points}"/></svg>'

    truth_section = "真值不可用"
    if truth:
        items = []
        for obj in comparison:
            det = obj["nearest_detection"]
            nearest = (
                f"第 {det['step']} 步 / {det['track_id']}，{det['distance_m']} m"
                if det
                else "无检测航迹"
            )
            state = "已计入" if obj["counted"] else "漏计"
            items.append(
                f"<tr><td>{html.escape(str(obj['id']))}</td><td>{html.escape(str(obj['category']))}</td><td>{state}</td><td>{nearest}</td><td>{html.escape(obj['loss_hint'])}</td></tr>"
            )
        truth_section = (
            "<table><thead><tr><th>真值 ID</th><th>类别</th><th>结果</th><th>最近检测</th><th>线索</th></tr></thead><tbody>"
            + "".join(items)
            + "</tbody></table>"
        )

    def call_text(call: dict[str, Any]) -> str:
        bbox = call.get("bbox") or call.get("focus_bbox")
        dims = (
            f"框 {bbox[2] - bbox[0]:.0f}×{bbox[3] - bbox[1]:.0f}px"
            if isinstance(bbox, list) and len(bbox) == 4
            else "框尺寸未知"
        )
        return f"{html.escape(str(call.get('decision') or call.get('identity') or '未知'))}，置信度 {call.get('confidence', '-')}，{dims}"

    step_rows = []
    for event in events:
        revisit = event["revisit"]
        if isinstance(revisit, dict):
            revisit_text = json.dumps(revisit, ensure_ascii=False)
        elif isinstance(revisit, list):
            revisit_text = json.dumps(revisit, ensure_ascii=False)
        else:
            revisit_text = str(revisit or "-")
        safety = event["safety"]
        safety_text = (
            "拒绝: " + ", ".join(map(str, safety.get("reasons") or []))
            if safety.get("rejected")
            else "-"
        )
        delta = "-" if event["count_delta"] is None else f"{event['count_delta']:+d}"
        calls = "<br>".join(call_text(c) for c in event["calls"]) or "-"
        step_rows.append(
            f'<tr><td>{event["step"]}</td><td>{html.escape(str(event["action"]))}</td><td>{calls}</td><td>{event["count"]} ({delta})</td><td>{html.escape(revisit_text)}</td><td class="warn">{html.escape(safety_text)}</td></tr>'
        )
    timeline = (
        "<table><thead><tr><th>步</th><th>动作</th><th>慢模型</th><th>计数</th><th>补看规划/跳过原因</th><th>安全拒绝</th></tr></thead><tbody>"
        + "".join(step_rows)
        + "</tbody></table>"
    )

    assets = []
    if thumbnails:
        import base64

        from scripts.render_flight_review import _frame_directories, _jpeg_bytes, _resolve_frame

        picks = sorted(set([0, max(0, len(rows) // 2), max(0, len(rows) - 1)]))
        for i in picks:
            if i >= len(rows):
                continue
            path = _resolve_frame(episode, rows[i], i, _frame_directories(episode, None))
            if path:
                try:
                    import io

                    from PIL import Image

                    data = _jpeg_bytes(path, max_size=(420, 280), quality=55)
                    with Image.open(io.BytesIO(data)) as image:
                        assets.append(
                            f'<figure><img width="{image.width}" height="{image.height}" src="data:image/jpeg;base64,{base64.b64encode(data).decode("ascii")}"><figcaption>关键帧 · 第 {rows[i].get("step_index", i)} 步</figcaption></figure>'
                        )
                except (OSError, ValueError):
                    continue

    provenance = metadata.get("provenance") or {}
    config_blob = json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    config_hash = (
        hashlib.sha256(config_blob.encode()).hexdigest()
        if config
        else metadata.get("config_sha256") or "不可用"
    )
    commit = (
        provenance.get("git_commit")
        or provenance.get("code_version")
        or metadata.get("git_commit")
        or "不可用"
    )
    summary = mission.get("summary") or _json(episode / "mission_summary.json")
    overview = f'<div class="summary"><span><b>{len(rows)}</b> 步</span><span>状态 <b>{html.escape(str(metadata.get("status") or summary.get("status") or "未知"))}</b></span><span>最终计数 <b>{sum(int(v) for v in (mission.get("counts") or {}).values() if isinstance(v, (int, float)))}</b></span><span>真值目标 <b>{len(comparison)}</b></span></div>'
    data = {"episode": episode.name, "steps": len(rows), "truth": comparison}
    payload_json = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SIM 架次复盘 · {html.escape(episode.name)}</title><style>{STYLE}figure{{display:inline-block;margin:8px}}img{{max-width:100%;height:auto}}.thumbs{{overflow:auto}}</style><body><header><h1>SIM 架次复盘 · {html.escape(episode.name)}</h1><p>{html.escape(str(metadata.get("task_text") or (rows[0].get("task_text") if rows else "")))}</p></header><main><section><h2>最终摘要</h2>{overview}<p>Provenance：代码版本 <code>{html.escape(str(commit))}</code> · 配置 SHA-256 <code>{html.escape(str(config_hash))}</code></p></section><section><h2>俯视图</h2><p>绿菱形：已计数；红菱形：漏计；蓝点：检测/航迹估计；灰圈：障碍物估计；红叉：安全拒绝。</p>{svg}</section><section><h2>高度-时间</h2><p class="muted">相对 home 的 NED 高度，横轴使用仿真时间（缺失时按步序）。</p>{chart}</section><section><h2>决策时间线</h2>{timeline}</section><section><h2>真值对比</h2>{truth_section}</section><section><h2>关键帧</h2><div class="thumbs">{"".join(assets) if assets else "缩略图关闭或无可用帧。"}</div></section><section><h2>任务摘要原文</h2><pre>{html.escape(json.dumps(summary, ensure_ascii=False, indent=2))}</pre></section></main><script id="report-data" type="application/json">{payload_json}</script></body></html>"""
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
    return {
        "output": output,
        "episode": episode,
        "steps": len(rows),
        "truth_targets": len(comparison),
        "bytes": output.stat().st_size,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="episode、campaign 或 benchmark 目录")
    parser.add_argument("--out", type=Path, required=True, help="HTML 文件或批量输出目录")
    parser.add_argument("--truth", type=Path, help="真值 JSON，单架次使用")
    parser.add_argument("--no-thumbnails", action="store_true", help="不嵌入关键帧")
    parser.add_argument("--match-radius-m", type=float, default=10.0)
    args = parser.parse_args(argv)
    episodes = _episode_dirs(args.input.resolve())
    if not episodes:
        parser.error(f"未找到 steps.jsonl: {args.input}")
    batch = len(episodes) > 1
    if batch:
        args.out.mkdir(parents=True, exist_ok=True)
    results = []
    for episode in episodes:
        output = args.out / f"{episode.name}.html" if batch or args.out.is_dir() else args.out
        results.append(
            render_episode_report(
                episode,
                output,
                truth_path=args.truth,
                thumbnails=not args.no_thumbnails,
                radius_m=args.match_radius_m,
            )
        )
    print(
        json.dumps(
            [{**r, "output": str(r["output"]), "episode": str(r["episode"])} for r in results],
            ensure_ascii=False,
            default=str,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
