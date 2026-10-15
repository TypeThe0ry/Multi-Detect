from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from scripts import render_flight_review as review


@pytest.fixture
def flight(tmp_path_factory):
    # All fixture images and generated pages stay inside this worktree.
    root = Path.cwd() / "tmp"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="flight-review-test-", dir=root) as directory:
        root = Path(directory)
        episode = root / "gen_fixture" / "episodes" / "episode-test"
        episode.mkdir(parents=True)
        frames = root / "gen_fixture" / "episode-test-onboard-frames"
        frames.mkdir()
        image = Image.new("RGB", (1600, 1200), "#204060")
        ImageDraw.Draw(image).rectangle((400, 300, 800, 700), fill="red")
        image.save(frames / "frame_0000.png")
        task = "Monitor for backpack"
        metadata = {"task_text": task, "status": "complete"}
        (episode / "episode.json").write_text(json.dumps(metadata), encoding="utf-8")
        plan = {"skills": ["monitor", "return_home"]}
        tracks = [
            {"id": "t1", "bbox": [400, 300, 800, 700], "position_ne": [20, 0]},
            {"id": "t2", "bbox": [900, 300, 1100, 500], "position_ne": [60, 20]},
        ]
        rows = [
            {
                "step_index": 0,
                "task_text": task,
                "obs_before": {
                    "pose": {"local_ned_m": [0, 0, -10]},
                    "rgb_path": "rgb\\missing.png",
                },
                "action": {"kind": "hold", "seconds": 2},
                "labels": {
                    "onboard": {
                        "state_before": "searching",
                        "state": "monitor",
                        "step": "s1",
                        "skill": "monitor",
                        "step_state": "running",
                        "tracks": tracks,
                        "slow_calls": [
                            {"track_id": "t1", "identity": "match", "confidence": 0.9},
                            {"track_id": "t2", "identity": "match", "confidence": 0.8},
                        ],
                        "mission": {"plan": plan, "history": []},
                    }
                },
            },
            {
                "step_index": 1,
                "task_text": task,
                "obs_before": {"pose": {"local_ned_m": [3, 4, -10]}},
                "action": {"kind": "stop"},
                "labels": {
                    "onboard": {
                        "state_before": "returning",
                        "state": "done",
                        "step": "s2",
                        "skill": "return_home",
                        "step_state": "succeeded",
                        "tracks": [],
                        "slow_calls": [],
                        "mission": {
                            "history": [
                                {"step": "s2", "skill": "return_home", "state": "succeeded"}
                            ],
                            "alerts": ["s1:alert:backpack@20,0", "s1:alert:backpack@60,20"],
                            "positions": ["20,0"],
                            "counts": {"bags": 1},
                        },
                    }
                },
            },
        ]
        (episode / "steps.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
        )
        truth = {
            "targets": [
                {
                    "target_id": "bag",
                    "true_target": True,
                    "category_words": ["backpack"],
                    "pose": {"y_north_m": 20, "x_east_m": 0},
                },
                {
                    "target_id": "car",
                    "category_words": ["car"],
                    "pose": {"y_north_m": 60, "x_east_m": 20},
                },
                {
                    "target_id": "missed-bag",
                    "category_words": ["backpack"],
                    "pose": {"y_north_m": 40, "x_east_m": 10},
                },
            ]
        }
        config = {"refs": {"zone:site": [0, 0, 80], "route:fence": [[0, 0], [10, 20]]}}
        (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        yield root, episode, truth, rows


def _payload(path: Path) -> tuple[str, dict]:
    page = path.read_text(encoding="utf-8")
    match = re.search(
        r'<script id="review-data" type="application/json">(.*?)</script>', page, re.S
    )
    assert match
    return page, json.loads(match[1])


def test_offline_review_geometry_truth_report_and_original_bbox_coordinates(flight):
    root, episode, truth, _ = flight
    output = root / "review.html"
    result = review.render_flight_review(
        episode, output, truth=truth, mission_config_path=root / "config.json"
    )
    page, data = _payload(output)
    assert result["bytes"] == output.stat().st_size < 15 * 1024 * 1024
    assert not re.search(r"https?://|(?:src|href)=[\"'](?://|https?:)", page)
    assert 'class="zone"' in page and 'class="route"' in page
    assert 'class="finding"' in page and 'class="trajectory"' in page
    assert data["summary"]["distance_flown_m"] == 5.0
    assert data["summary"]["slow_calls"] == 2
    assert data["plan"]["skills"] == ["monitor", "return_home"]
    assert data["steps"][1]["transition"] == "s1 → s2"
    assert data["steps"][1]["history"][0]["state"] == "succeeded"
    assert {obj["id"]: obj["status"] for obj in data["truth_objects"]} == {
        "bag": "matched",
        "car": "false",
        "missed-bag": "missed",
    }
    assert data["mission_metrics"]["mission_alert_precision"] == 0.5
    assert data["mission"]["counts"] == {"bags": 1}
    first, second = data["calls"]
    assert first["frame_asset"] == second["frame_asset"]  # Shared frame is embedded once.
    assert len(data["assets"]) == 3
    assert (first["frame_width"], first["frame_height"]) == (1600, 1200)
    assert first["bbox"] == [400, 300, 800, 700]
    frame = Image.open(
        BytesIO(base64.b64decode(data["assets"][first["frame_asset"]].split(",")[1]))
    )
    assert frame.format == "JPEG" and frame.size == (800, 600)
    crop = Image.open(BytesIO(base64.b64decode(data["assets"][first["crop_asset"]].split(",")[1])))
    assert crop.width <= 400 and crop.height <= 320
    red, green, blue = crop.getpixel((crop.width // 2, crop.height // 2))
    assert red > 200 and green < 50 and blue < 50


def test_unknown_truth_missing_frames_and_small_size_budget(flight):
    root, episode, _, rows = flight
    review.render_flight_review(episode, root / "limited.html", max_html_mb=0.08)
    _, data = _payload(root / "limited.html")
    assert not data["summary"]["truth_available"]
    assert data["truth_objects"] == []
    assert (root / "limited.html").stat().st_size <= int(0.08 * 1024 * 1024)
    assert not data["assets"] and len(data["calls"]) == 2
    rows[0]["step_index"] = 99
    (episode / "steps.jsonl").write_text(json.dumps(rows[0]), encoding="utf-8")
    review.render_flight_review(episode, root / "missing.html")
    _, data = _payload(root / "missing.html")
    assert not data["assets"] and data["calls"][0]["frame_asset"] is None


def test_truth_auto_resolution_and_html_script_injection_safety(flight):
    root, episode, truth, _ = flight
    truth_dir = root / "truth"
    truth_dir.mkdir()
    (truth_dir / "gen_fixture.truth.json").write_text(json.dumps(truth), encoding="utf-8")
    metadata = {"task_text": '</script><img src=x onerror="bad()">@@MAP@@'}
    (episode / "episode.json").write_text(json.dumps(metadata), encoding="utf-8")
    review.render_flight_review(episode, root / "safe.html", truth_dir=truth_dir)
    page, data = _payload(root / "safe.html")
    assert data["summary"]["truth_available"]
    assert data["summary"]["task_text"] == metadata["task_text"]
    assert '<img src=x onerror="bad()">' not in page
    assert page.count("<script>") == 1 and page.count("</script>") == 2


def test_javascript_renders_timeline_and_clicks_call_without_network(flight):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional; needed to exercise the inline browser script")
    root, episode, truth, _ = flight
    review.render_flight_review(episode, root / "ui.html", truth=truth)
    page, data = _payload(root / "ui.html")
    script = page.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    # A small DOM double exercises the actual script and click handler, including SVG bbox units.
    harness = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children = []; }
  setAttribute(key, value) { this.attributes[key] = value; }
}
const elements = new Map();
let clickHandler;
global.document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id);
  },
  querySelector() { return {namespaceURI: 'svg-namespace'}; },
  querySelectorAll() { return []; },
  createElement(tag) { return new Element(tag); },
  createElementNS(ns, tag) { return new Element(tag); },
  addEventListener(type, handler) { clickHandler = handler; }
};
document.getElementById('review-data').textContent = JSON.stringify(DATA);
SCRIPT
assert.match(elements.get('timeline').innerHTML, /data-call-id="call-0-0"/);
assert.match(elements.get('timeline').innerHTML, /90\.0%/);
const button = new Element('button');
button.dataset = {callId: 'call-0-0'};
clickHandler({target: {closest() { return button; }}});
const grid = elements.get('call-detail').children.at(-1);
const frameBox = grid.children[0].children[0];
assert.ok(frameBox.children[0].src.startsWith('data:image/jpeg;base64,'));
assert.equal(frameBox.children[1].attributes.viewBox, '0 0 1600 1200');
assert.equal(frameBox.children[1].children[0].attributes.x, 400);
assert.equal(frameBox.children[1].children[0].attributes.width, 400);
assert.ok(grid.children[1].children[1].src.startsWith('data:image/jpeg;base64,'));
"""
    harness = harness.replace("DATA", json.dumps(data, ensure_ascii=True), 1)
    harness = harness.replace("SCRIPT", script, 1)
    js_path = root / "ui-check.cjs"
    js_path.write_text(harness, encoding="utf-8")
    checked = subprocess.run([node, str(js_path)], capture_output=True, text=True, check=False)
    assert checked.returncode == 0, checked.stderr
