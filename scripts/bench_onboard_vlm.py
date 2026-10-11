#!/usr/bin/env python3
# ruff: noqa: E501
"""Benchmark local GGUF VLM decisions on aligned Gazebo camera frames.

Truth projection is used only to construct and score this offline benchmark.
The server receives task, image, memory, and a candidate box, never SIM truth.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from multidetect.autonomy.onboard_decision import (  # noqa: E402
    DECISION_JSON_SCHEMA,
    parse_onboard_decision,
)
from multidetect.simulator.camera_projection import project_target_bbox  # noqa: E402

try:
    from scripts.build_sim_detector_dataset import CLASS_ALIASES, DEFAULT_SIZES
except ModuleNotFoundError:
    from build_sim_detector_dataset import CLASS_ALIASES, DEFAULT_SIZES


CAMPAIGNS = ("campaign-20261011-r2", "campaign-20261011-r3", "campaign-20261011-r4")
MODELS = {
    "qwen3-vl-2b": (
        "models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/Qwen3VL-2B-Instruct-Q4_K_M.gguf",
        "models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf",
    ),
    "qwen2.5-vl-3b": (
        "models--ggml-org--Qwen2.5-VL-3B-Instruct-GGUF/snapshots/5037fcf163dd95d1e41d1974465f0898ed108ca2/Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf",
        "models--ggml-org--Qwen2.5-VL-3B-Instruct-GGUF/snapshots/5037fcf163dd95d1e41d1974465f0898ed108ca2/mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf",
    ),
}
COLORS = ("red", "blue", "green", "yellow", "orange", "black", "white")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _color(target: dict[str, Any]) -> str | None:
    label = str(target.get("label", "")).casefold()
    return next((color for color in COLORS if label.startswith(color + "_")), None)


def _candidate_rows(roots: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for root in roots:
        for metadata in sorted(root.rglob("episode.json")):
            episode = metadata.parent
            episode_metadata = _read_json(metadata)
            if episode_metadata.get("status") != "complete":
                continue
            world = next((part for part in episode.parts if part.startswith("gen_w")), None)
            truth_path = ROOT / "configs/scenarios/generated" / f"{world}.truth.json"
            steps_path = episode / "steps.jsonl"
            if not world or not truth_path.is_file() or not steps_path.is_file():
                continue
            targets = _read_json(truth_path).get("targets", [])
            true_target = next((t for t in targets if str(t.get("target_id", "")).startswith("target_true")), None)
            if not true_target:
                continue
            true_class = CLASS_ALIASES.get(str(true_target.get("class", "vehicle")), str(true_target.get("class", "vehicle")))
            true_color = _color(true_target)
            if not true_color:
                continue
            for line in steps_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                step = json.loads(line)
                for side in ("before", "after"):
                    obs = step.get("obs_" + side)
                    if not isinstance(obs, dict) or abs(float(obs.get("sim_skew_ms", 0))) > 250:
                        continue
                    info = obs.get("camera_info") or {}
                    width, height = int(info.get("width", 0)), int(info.get("height", 0))
                    if not width or not height or not obs.get("rgb_path"):
                        continue
                    frame = episode / Path(obs["rgb_path"].replace("\\", "/"))
                    if not frame.is_file():
                        continue
                    pose = {**(obs.get("pose") or {}), "attitude": obs.get("attitude") or {}}
                    for target in targets:
                        if not isinstance(target, dict):
                            continue
                        target_id = str(target.get("target_id", ""))
                        is_true = target_id == str(true_target.get("target_id"))
                        # A distractor must actually contradict the requested colour.
                        if not is_true and (_color(target) is None or _color(target) == true_color):
                            continue
                        metric_pose = target.get("pose") or {}
                        target_class = CLASS_ALIASES.get(str(target.get("class", "vehicle")), str(target.get("class", "vehicle")))
                        try:
                            metric = (float(metric_pose["y_north_m"]), float(metric_pose["x_east_m"]), float(metric_pose["z_up_m"]))
                            size = tuple(target.get("size_m") or DEFAULT_SIZES.get(target_class, (3.0, 1.8, 1.5)))
                            box = project_target_bbox(
                                target_ned=metric, pose=pose, camera_info=info,
                                image_size=(width, height), target_size_m=size,
                                target_yaw_rad=float(metric_pose.get("yaw_rad", 0)),
                            )
                        except (KeyError, TypeError, ValueError):
                            continue
                        if box is None or box[2] - box[0] < 8 or box[3] - box[1] < 8:
                            continue
                        raw_task_text = str(step.get("task_text") or episode_metadata.get("task_text") or "")
                        slug = re.search(r"(?:red|blue|green|yellow|orange|black|white)_(?:vehicle|rover|cargo_cart)", raw_task_text, re.IGNORECASE)
                        # Some source Chinese bytes were irreversibly replaced by U+FFFD.
                        # Retain the mission's ASCII object slug; never synthesize from truth.
                        if slug:
                            task_text = f"Find and record the {slug.group(0).replace('_', ' ')}"
                            task_source = "mission_slug_normalized"
                        elif "\ufffd" not in raw_task_text and raw_task_text.strip():
                            task_text = raw_task_text
                            task_source = "mission_raw"
                        else:
                            continue
                        local_ned = (obs.get("pose") or {}).get("local_ned_m") or [0, 0, 0]
                        rows.append({
                            "frame": str(frame), "campaign": root.name, "world": world,
                            "altitude_m": round(-float(local_ned[2]), 2),
                            "box": [round(float(x), 1) for x in box], "image_size": [width, height],
                            "candidate_class": target_class, "candidate_color": _color(target),
                            "task_text": task_text, "task_source": task_source,
                            "true_target_class": true_class, "true_target_color": true_color,
                            "expected": "confirm_target" if is_true else "reject_track",
                            "target_id": target_id,
                        })
    return rows


def _select_rows(rows: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    # Round robin over altitude, campaign and class, with one candidate per frame.
    groups: dict[tuple[str, str, str], deque[dict[str, Any]]] = {}
    for row in rows:
        key = (row["expected"], row["campaign"], row["true_target_class"])
        groups.setdefault(key, deque()).append(row)
    for key, queue in list(groups.items()):
        ordered = sorted(queue, key=lambda row: (row["box"][2] - row["box"][0]) * (row["box"][3] - row["box"][1]))
        # Alternate large, medium, small candidates in each group.
        sampled: list[dict[str, Any]] = []
        left, right = 0, len(ordered) - 1
        while left <= right:
            sampled.append(ordered[right])
            right -= 1
            if left <= right:
                sampled.append(ordered[left])
                left += 1
        groups[key] = deque(sampled)
    selected: list[dict[str, Any]] = []
    used_frames: set[str] = set()
    keys = sorted(groups, key=lambda x: (x[0], x[1], x[2]))
    next_key = {"confirm_target": 0, "reject_track": 0}
    while len(selected) < count:
        progress = False
        need = "confirm_target" if sum(r["expected"] == "confirm_target" for r in selected) <= sum(r["expected"] == "reject_track" for r in selected) else "reject_track"
        matching_keys = [key for key in keys if key[0] == need]
        for offset in range(len(matching_keys)):
            position = (next_key[need] + offset) % len(matching_keys)
            key = matching_keys[position]
            queue = groups[key]
            while queue and queue[0]["frame"] in used_frames:
                queue.popleft()
            if queue:
                row = queue.popleft()
                selected.append(row)
                used_frames.add(row["frame"])
                next_key[need] = (position + 1) % len(matching_keys)
                progress = True
                break
        if not progress:
            break
    return selected


def _prompt(row: dict[str, Any], *, short: bool) -> str:
    w, h = row["image_size"]
    box = [round(row["box"][i] / (w if i % 2 == 0 else h), 3) for i in range(4)]
    if short:
        return (
            f"Task: {row['task_text']}\nCandidate track-1 box xyxy normalized: {box}. "
            "Memory: none. Decide if this candidate matches the task. "
            "Return JSON decision, args, reason only. Use confirm_target or reject_track when visible."
        )
    return (
        "You are the onboard slow planner. Choose one sub-goal, never motor commands. "
        "Output only JSON with decision, args, reason (at most 20 words). "
        "Confirm a candidate only when its visible identity and attributes match the task; "
        "reject mismatches, hover if uncertain.\n"
        f"Task: {row['task_text']}\n"
        "Memory: no prior confirmed target; search in progress.\n"
        f"Candidate: track_id=track-1, box_xyxy_normalized={box}.\n"
        "Pick confirm_target or reject_track when visual evidence suffices. "
        "Use args.track_id=track-1 for either."
    )


def _encode_image(frame: Path, max_side: int, box: list[float], image_size: list[int]) -> str:
    bgr = cv2.imread(str(frame), cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError(f"Cannot decode frame: {frame}")
    h, w = bgr.shape[:2]
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        bgr = cv2.resize(bgr, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    image_scale_x = bgr.shape[1] / image_size[0]
    image_scale_y = bgr.shape[0] / image_size[1]
    x1, y1, x2, y2 = (round(box[0] * image_scale_x), round(box[1] * image_scale_y), round(box[2] * image_scale_x), round(box[3] * image_scale_y))
    cv2.rectangle(bgr, (x1, y1), (x2, y2), (0, 255, 255), 2)
    cv2.putText(bgr, "track-1", (max(0, x1), max(15, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1, cv2.LINE_AA)
    ok, encoded = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise ValueError(f"Cannot JPEG encode: {frame}")
    return "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")


def _http_json(url: str, body: dict[str, Any] | None = None, timeout: float = 180) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


class ResourceSampler:
    def __init__(self, pid: int):
        self.pid = pid
        self.stop = threading.Event()
        self.peak_rss_bytes: int | None = None
        self.peak_gpu_used_mib: int | None = None
        self.samples = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=3)

    def _run(self) -> None:
        try:
            import psutil

            process = psutil.Process(self.pid)
        except (ImportError, OSError):
            process = None
        while not self.stop.is_set():
            if process:
                try:
                    value = int(process.memory_info().rss)
                    self.peak_rss_bytes = max(self.peak_rss_bytes or 0, value)
                except (OSError, Exception):  # process may have exited
                    pass
            try:
                raw = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                    stderr=subprocess.DEVNULL, timeout=3, text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                value = int(raw.splitlines()[0].strip())
                self.peak_gpu_used_mib = max(self.peak_gpu_used_mib or 0, value)
            except (OSError, ValueError, subprocess.SubprocessError, IndexError):
                pass
            self.samples += 1
            self.stop.wait(0.5)


def _percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None}
    ordered = sorted(values)
    return {"p50": round(statistics.median(ordered), 4), "p95": round(ordered[int((len(ordered) - 1) * 0.95 + 0.999999)], 4)}


def _group_summary(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, int]]:
    keys = sorted({str(r[field]) for r in rows})
    return {key: {
        "count": sum(str(r[field]) == key for r in rows),
        "valid": sum(str(r[field]) == key and r["valid"] for r in rows),
        "sane": sum(str(r[field]) == key and r["sane"] for r in rows),
    } for key in keys}


def _run_variant(rows: list[dict[str, Any]], *, base_url: str, size: int, short: bool, cap: int, timeout: float) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for row in rows:
        started = time.perf_counter()
        item = {key: row[key] for key in ("campaign", "world", "altitude_m", "frame", "box", "image_size", "task_text", "task_source", "expected", "candidate_class", "candidate_color", "true_target_class", "true_target_color")}
        try:
            image_url = _encode_image(Path(row["frame"]), size, row["box"], row["image_size"])
            encoded_s = time.perf_counter() - started
            body = {
                "model": "local", "temperature": 0, "max_tokens": cap, "stream": False,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": _prompt(row, short=short)},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "onboard_decision", "strict": True, "schema": DECISION_JSON_SCHEMA}},
            }
            response = _http_json(base_url + "/v1/chat/completions", body, timeout)
            total_s = time.perf_counter() - started
            raw = response["choices"][0]["message"].get("content", "")
            item.update({
                "encode_s": round(encoded_s, 4), "total_s": round(total_s, 4),
                "completion_tokens": response.get("usage", {}).get("completion_tokens"),
                "prompt_tokens": response.get("usage", {}).get("prompt_tokens"),
                "timings": response.get("timings"), "raw_response": raw,
            })
            decision = parse_onboard_decision(raw)
            actual = decision.decision
            item.update({
                "valid": True, "decision": actual, "sane": actual == row["expected"],
                "abstained": actual not in ("confirm_target", "reject_track"),
            })
        except Exception as exc:
            item.update({"valid": False, "sane": False, "error": f"{type(exc).__name__}: {exc}"})
            item.setdefault("total_s", round(time.perf_counter() - started, 4))
        results.append(item)
        print(f"  {len(results)}/{len(rows)} {item['expected']} {item.get('decision', item.get('error'))} {item['total_s']}s", flush=True)
    valid = [r for r in results if r["valid"]]
    for result in results:
        short_side = min(result["box"][2] - result["box"][0], result["box"][3] - result["box"][1])
        result["box_short_side_bin"] = "8-20px" if short_side < 20 else "20-40px" if short_side < 40 else "40px+"
    return {
        "sample_count": len(results), "unique_frames": len({r["frame"] for r in results}),
        "target_count": sum(r["expected"] == "confirm_target" for r in results),
        "distractor_count": sum(r["expected"] == "reject_track" for r in results),
        "valid_count": len(valid), "valid_rate": len(valid) / len(results) if results else None,
        "sane_count": sum(r["sane"] for r in results),
        "sane_rate": sum(r["sane"] for r in results) / len(results) if results else None,
        "abstention_count": sum(r.get("abstained", False) for r in valid),
        "latency_s": _percentiles([r["total_s"] for r in results]),
        "encode_s": _percentiles([r["encode_s"] for r in results if "encode_s" in r]),
        "completion_tokens": _percentiles([float(r["completion_tokens"]) for r in results if isinstance(r.get("completion_tokens"), int)]),
        "cache_tokens": _percentiles([float(r["timings"]["cache_n"]) for r in results if isinstance(r.get("timings"), dict) and isinstance(r["timings"].get("cache_n"), int)]),
        "server_prompt_ms": _percentiles([float(r["timings"]["prompt_ms"]) for r in results if isinstance(r.get("timings"), dict) and isinstance(r["timings"].get("prompt_ms"), (int, float))]),
        "server_decode_ms": _percentiles([float(r["timings"]["predicted_ms"]) for r in results if isinstance(r.get("timings"), dict) and isinstance(r["timings"].get("predicted_ms"), (int, float))]),
        "by_expected": _group_summary(results, "expected"),
        "by_campaign": _group_summary(results, "campaign"),
        "by_box_short_side": _group_summary(results, "box_short_side_bin"),
        "rows": results,
    }


def _launch_server(model: Path, mmproj: Path, *, port: int, context: int, cap: int, log_path: Path) -> tuple[subprocess.Popen[bytes], list[str], Any]:
    exe = ROOT / "artifacts/cache/llama-cpp/b11552/llama-server.exe"
    command = [str(exe), "-m", str(model), "-mm", str(mmproj), "-ngl", "all", "-c", str(context), "-n", str(cap), "-np", "1", "--host", "127.0.0.1", "--port", str(port), "--no-webui", "--cache-prompt", "--cache-reuse", "64"]
    log_file = log_path.open("wb")
    process = subprocess.Popen(command, cwd=str(exe.parent), stdout=log_file, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"llama-server exited during model load: {process.returncode}")
        try:
            health = _http_json(f"http://127.0.0.1:{port}/health", timeout=2)
            if health.get("status") == "ok":
                return process, command, log_file
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(1)
    process.terminate()
    log_file.close()
    raise TimeoutError("llama-server did not become healthy within 180 seconds")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", type=Path)
    parser.add_argument("--models", nargs="+", choices=tuple(MODELS), default=list(MODELS))
    parser.add_argument("--frames", type=int, default=40)
    parser.add_argument("--lever-frames", type=int, default=8)
    parser.add_argument("--port", type=int, default=9931)
    parser.add_argument("--context", type=int, default=4096)
    parser.add_argument("--max-tokens", type=int, default=96)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/benchmarks/onboard-vlm-20261011.json")
    args = parser.parse_args(argv)
    roots = args.root or [ROOT / "artifacts/datasets/sim-teacher" / name for name in CAMPAIGNS]
    selected = _select_rows(_candidate_rows(roots), args.frames)
    if len(selected) < args.frames:
        raise RuntimeError(f"Only {len(selected)} eligible unique frames found, requested {args.frames}")
    if sum(r["expected"] == "confirm_target" for r in selected) < args.frames // 2:
        raise RuntimeError("Insufficient true target samples for balanced evaluation")
    print(f"Selected {len(selected)} unique frames, {sum(r['expected'] == 'confirm_target' for r in selected)} true targets", flush=True)
    report: dict[str, Any] = {
        "contract": "multidetect.onboard-vlm-benchmark-v1",
        "scope": "Isolated candidate judgement. Candidate boxes and expected labels are projected from SIM truth offline; the model sees only image with the oracle candidate rectangle, task text and normalized box. This overestimates an end-to-end detector+VLM pipeline.",
        "task_text_rule": "Normalize the ASCII colour_class slug found in the recorded mission task text; skip tasks with undecodable text and no slug. No target class, colour or ID is supplied from truth.",
        "roots": [str(r) for r in roots], "frame_count": len(selected), "models": {},
        "selection": {
            "unique_frames": len({r["frame"] for r in selected}),
            "unique_campaign_worlds": len({(r["campaign"], r["world"]) for r in selected}),
            "target_count": sum(r["expected"] == "confirm_target" for r in selected),
            "distractor_count": sum(r["expected"] == "reject_track" for r in selected),
            "by_campaign": {name: {
                "count": sum(r["campaign"] == name for r in selected),
                "target_count": sum(r["campaign"] == name and r["expected"] == "confirm_target" for r in selected),
                "distractor_count": sum(r["campaign"] == name and r["expected"] == "reject_track" for r in selected),
            } for name in CAMPAIGNS},
        },
    }
    base_url = f"http://127.0.0.1:{args.port}"
    cache = ROOT / "artifacts/cache/hf-vlm-gguf"
    for name in args.models:
        model, mmproj = (cache / p for p in MODELS[name])
        if not model.is_file() or not mmproj.is_file():
            raise FileNotFoundError(f"Missing local GGUF pair: {model}, {mmproj}")
        print(f"Loading {name}", flush=True)
        load_start = time.perf_counter()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        log_path = args.output.with_name(args.output.stem + "." + name + ".server.log")
        process, command, log_file = _launch_server(model, mmproj, port=args.port, context=args.context, cap=args.max_tokens, log_path=log_path)
        sampler = ResourceSampler(process.pid)
        sampler.start()
        try:
            model_report: dict[str, Any] = {
                "server_command": command, "model_bytes": model.stat().st_size,
                "mmproj_bytes": mmproj.stat().st_size,
                "server_log": str(log_path),
                "load_s": round(time.perf_counter() - load_start, 4),
                "resource_note": "GPU value is total device used MiB, includes concurrent Gazebo/campaign; not process VRAM. RSS is llama-server process only.",
            }
            # One untimed warm request stabilizes process/page cache and tests the API.
            print("Warmup", flush=True)
            model_report["warmup"] = _run_variant(selected[:1], base_url=base_url, size=448, short=False, cap=args.max_tokens, timeout=args.timeout)
            print("Baseline 448 px", flush=True)
            model_report["baseline_448"] = _run_variant(selected, base_url=base_url, size=448, short=False, cap=args.max_tokens, timeout=args.timeout)
            if args.lever_frames:
                lever = selected[:args.lever_frames]
                print("Lever 320 px, full prompt, 96 tokens", flush=True)
                model_report["lever_320_full_96"] = _run_variant(lever, base_url=base_url, size=320, short=False, cap=96, timeout=args.timeout)
                print("Lever 448 px, short prompt, 96 tokens", flush=True)
                model_report["lever_448_short_96"] = _run_variant(lever, base_url=base_url, size=448, short=True, cap=96, timeout=args.timeout)
                print("Lever 320 px, short prompt, 64 tokens", flush=True)
                model_report["lever_320_short_64"] = _run_variant(lever, base_url=base_url, size=320, short=True, cap=64, timeout=args.timeout)
                print("Lever 320 px, short prompt, 96 tokens", flush=True)
                model_report["lever_320_short_96"] = _run_variant(lever, base_url=base_url, size=320, short=True, cap=96, timeout=args.timeout)
            report["models"][name] = model_report
        finally:
            sampler.close()
            if name in report["models"]:
                report["models"][name]["peak_server_rss_bytes"] = sampler.peak_rss_bytes
                report["models"][name]["peak_total_gpu_used_mib"] = sampler.peak_gpu_used_mib
                report["models"][name]["resource_samples"] = sampler.samples
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            log_file.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({name: {variant: {k: value for k, value in data.items() if k != "rows"} for variant, data in model.items() if isinstance(data, dict) and "sample_count" in data} for name, model in report["models"].items()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
