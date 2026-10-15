from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts import build_slow_system_sft as builder
from scripts import eval_slow_system as evaluator
from scripts import slow_system_runtime as runtime


def _row(tmp_path: Path) -> dict:
    path = tmp_path / "frame.png"
    image = Image.new("RGB", (640, 480))
    image.paste((255, 0, 0), (64, 96, 128, 192))
    image.paste((0, 0, 255), (480, 240, 544, 336))
    image.save(path)
    return {
        "sample_id": "fixture",
        "world": "fixture",
        "image_path": str(path),
        "task_text": "Find the red vehicle.",
        "memory": "none",
        "candidates": [
            {"track_id": "track-1", "box_xyxy_normalized": [0.1, 0.2, 0.2, 0.4]},
            {"track_id": "track-2", "box_xyxy_normalized": [0.75, 0.5, 0.85, 0.7]},
        ],
        "focus_track_id": "track-2",
        "crop_policy": "candidate_zoom_v2",
        "assistant_target": {
            "decision": "reject_track",
            "args": {"track_id": "track-2"},
            "reason": "Wrong color.",
        },
    }


def test_default_prompt_frozen_and_explicit_default_images_identical(tmp_path):
    row = _row(tmp_path)
    # Recorded from HEAD before introducing the budget flags.
    assert hashlib.sha256(runtime.prompt_for_row(row).encode()).hexdigest() == (
        "9c1a8c94f1512917426289bc0d1dcc233c1ba9961db3607a82749726e7677044"
    )
    assert runtime.prompt_for_row(row) == runtime.prompt_for_row(row, identity_variant="default")
    assert runtime.image_jpeg_bytes_list(row) == runtime.image_jpeg_bytes_list(
        row, identity_variant="default"
    )
    assert [im.size for im in runtime.image_list_for_row(row)] == [
        (448, 336),
        (224, 224),
        (224, 224),
    ]


@pytest.mark.parametrize(
    "variant,sizes",
    [
        ("focus_only", [(448, 336), (224, 224)]),
        ("frame_336", [(336, 252), (224, 224), (224, 224)]),
        ("crop_160", [(448, 336), (160, 160), (160, 160)]),
        ("compact", [(336, 252), (160, 160)]),
    ],
)
def test_variant_sizes_and_persisted_training_metadata(tmp_path, variant, sizes):
    row = _row(tmp_path)
    explicit = runtime.chat_messages(row, identity_variant=variant)[0]["content"]
    persisted = runtime.chat_messages({**row, "identity_variant": variant})[0]["content"]
    assert explicit[0] == persisted[0]
    assert [item["image"].size for item in explicit[1:]] == sizes
    assert [item["image"].tobytes() for item in explicit[1:]] == [
        item["image"].tobytes() for item in persisted[1:]
    ]
    assert row["assistant_target"]["reason"] not in explicit[0]["text"]


@pytest.mark.parametrize("policy", ["candidate_zoom_v1", "candidate_zoom_v2"])
def test_focus_crop_selection_never_reads_target_and_falls_back_to_first(tmp_path, policy):
    row = _row(tmp_path)
    other = {
        **row,
        "assistant_target": {"decision": "confirm_target", "args": {"track_id": "track-1"}},
    }
    first = runtime.image_list_for_row(row, crop_policy=policy, identity_variant="focus_only")
    second = runtime.image_list_for_row(other, crop_policy=policy, identity_variant="focus_only")
    assert [im.tobytes() for im in first] == [im.tobytes() for im in second]
    assert first[1].getpixel((112, 112))[2] > 200
    prompt = runtime.prompt_for_row({**row, "crop_policy": policy}, identity_variant="focus_only")
    assert "Image 2 is a zoomed crop around track-2" in prompt
    assert "image 3=" not in prompt
    for focus in (None, "absent"):
        fallback = {**row, "focus_track_id": focus}
        assert runtime.zoom_candidates_for_row(fallback, "focus_only") == [row["candidates"][0]]
        assert (
            runtime.image_list_for_row(fallback, identity_variant="focus_only")[1].getpixel(
                (112, 112)
            )[0]
            > 200
        )


def test_mixed_planning_rows_keep_exact_text_and_no_images():
    row = {"kind": "mission_plan", "prompt": "Exact planning prompt", "identity_variant": "compact"}
    assert runtime.chat_messages(row) == [
        {"role": "user", "content": [{"type": "text", "text": row["prompt"]}]}
    ]


def test_invalid_variant_rejected_without_candidates():
    with pytest.raises(ValueError, match="unsupported identity variant"):
        runtime.prompt_for_row(
            {"candidates": [], "task_text": "task", "memory": "none"}, identity_variant="wrong"
        )


def test_evaluation_token_usage_is_actual_and_unknown_stays_null(tmp_path):
    row = _row(tmp_path)

    class Predictor:
        def __init__(self):
            self.calls = 0
            self.last_usage = None

        def __call__(self, row):
            self.calls += 1
            self.last_usage = (
                {"prompt_tokens": 100 + self.calls, "completion_tokens": 20}
                if self.calls < 3
                else None
            )
            return json.dumps(row["assistant_target"]), 0.02

    result = evaluator.evaluate_predictor([row] * 3, Predictor())
    assert [r["prompt_tokens"] for r in result["rows"]] == [101, 102, None]
    assert result["metrics"]["prompt_tokens"] == {"count": 2, "mean": 101.5, "p50": 102, "p95": 102}
    fake = evaluator.evaluate_predictor(
        [row], lambda row: (json.dumps(row["assistant_target"]), 0.001)
    )
    assert fake["rows"][0]["prompt_tokens"] is None
    assert fake["metrics"]["prompt_tokens"]["mean"] is None


def test_llama_request_uses_variant_and_cold_prefill_cache_setting(tmp_path, monkeypatch):
    bodies = []
    runner = evaluator.LlamaServerRunner.__new__(evaluator.LlamaServerRunner)
    runner.port, runner.max_new_tokens, runner.crop_policy, runner.max_side = (
        9933,
        128,
        "candidate_zoom_v2",
        448,
    )
    runner.identity_variant, runner.identity_only, runner.logprobs, runner.cache_prompt = (
        "compact",
        False,
        False,
        False,
    )

    def reply(url, body):
        bodies.append(body)
        return {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 231, "completion_tokens": 19},
        }

    monkeypatch.setattr(evaluator, "_http_json", reply)
    runner(_row(tmp_path))
    assert bodies[0]["cache_prompt"] is False
    assert len(bodies[0]["messages"][0]["content"]) == 3
    assert runner.last_usage == {"prompt_tokens": 231, "completion_tokens": 19}
    runner.cache_prompt = True
    runner(_row(tmp_path))
    assert "cache_prompt" not in bodies[1]


def test_hf_usage_counts_processor_input_and_generated_sequence(tmp_path):
    class Inputs(dict):
        def to(self, device):
            return self

    class Output:
        shape = (1, 252)

        def __getitem__(self, key):
            return SimpleNamespace(tolist=lambda: [1] * 21)

    runner = evaluator.HFRunner.__new__(evaluator.HFRunner)
    runner.max_side, runner.crop_policy, runner.identity_variant = (
        448,
        "candidate_zoom_v2",
        "compact",
    )
    runner.max_new_tokens = 128
    runner.torch = SimpleNamespace(inference_mode=nullcontext)
    runner.model = SimpleNamespace(
        parameters=lambda: iter([SimpleNamespace(device="cpu")]),
        generate=lambda **kwargs: Output(),
    )
    runner.processor = SimpleNamespace(
        apply_chat_template=lambda *a, **kw: Inputs(input_ids=SimpleNamespace(shape=(1, 231))),
        tokenizer=SimpleNamespace(decode=lambda *a, **kw: "{}"),
    )
    output, latency = runner(_row(tmp_path))
    assert output == "{}" and latency >= 0
    assert runner.last_usage == {"prompt_tokens": 231, "completion_tokens": 21}


def test_eval_cli_flags_and_external_data_root(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        evaluator, "run", lambda args: captured.update(vars(args)) or {"metrics": {}}
    )
    assert (
        evaluator.main(
            [
                "--backend",
                "fake",
                "--identity-variant",
                "compact",
                "--data-root",
                str(tmp_path),
                "--no-prompt-cache",
            ]
        )
        == 0
    )
    assert captured["identity_variant"] == "compact" and captured["no_prompt_cache"]
    assert captured["data_root"] == tmp_path

    row = _row(tmp_path)
    row.update(image_path="frame.png", split="test")
    (tmp_path / "test.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    seen = []
    args = SimpleNamespace(
        dataset=tmp_path,
        data_root=tmp_path,
        crop_policy=None,
        backend="hf",
        model=tmp_path,
        adapter=None,
        device="cpu",
        max_side=448,
        max_new_tokens=128,
        identity_variant="compact",
    )
    monkeypatch.undo()
    monkeypatch.setattr(
        evaluator,
        "HFRunner",
        lambda *a, **kw: (
            lambda row: seen.append(row) or (json.dumps(row["assistant_target"]), 0.001)
        ),
    )
    evaluator.run(args)
    assert Path(seen[0]["image_path"]) == tmp_path / "frame.png"


def test_builder_persists_variant_only_for_short_dataset(tmp_path, monkeypatch):
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps({"worlds": [{"world_id": f"w-{s}", "split": s} for s in builder.SPLITS]}),
        encoding="utf-8",
    )
    frame = tmp_path / "frame.png"
    Image.new("RGB", (100, 100)).save(frame)
    frames = [
        (
            "campaign",
            f"w-{split}",
            split,
            frame,
            (100, 100),
            ("red", "vehicle"),
            [
                {
                    "target": {"target_id": "target_true_0"},
                    "box": (10.0, 10.0, 30.0, 30.0),
                    "class": "vehicle",
                    "color": "red",
                    "range_m": 10.0,
                },
                {
                    "target": {"target_id": "distractor_0"},
                    "box": (50.0, 50.0, 80.0, 80.0),
                    "class": "rover",
                    "color": "blue",
                    "range_m": 10.0,
                },
            ],
            {},
            "before",
            {},
            None,
        )
        for split in builder.SPLITS
    ]
    monkeypatch.setattr(builder, "_relative", lambda path: Path(path).name)
    monkeypatch.setattr(builder, "_eligible_frames", lambda *a, **kw: iter(frames))
    baseline = builder.build_dataset([], tmp_path / "baseline", index=index)
    report = builder.build_dataset([], tmp_path / "short", index=index, identity_variant="compact")
    assert "identity_variant" not in baseline
    assert report["identity_variant"] == "compact"
    assert "336_max_side" in report["image_policy"] and "160x160" in report["image_policy"]
    for split in builder.SPLITS:
        before = runtime.load_jsonl(tmp_path / "baseline" / f"{split}.jsonl")
        after = runtime.load_jsonl(tmp_path / "short" / f"{split}.jsonl")
        assert [
            {k: v for k, v in row.items() if k != "identity_variant"} for row in after
        ] == before
        assert all(row["identity_variant"] == "compact" for row in after)
