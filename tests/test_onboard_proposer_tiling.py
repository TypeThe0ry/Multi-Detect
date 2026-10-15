import math
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from multidetect.autonomy.onboard_loop import Detection
from scripts.onboard_policy import VOCABULARY, OnboardPolicy, YoloeProposer


class StubModel:
    names = {0: "object", 1: "different label"}

    def __init__(self, rows=None):
        self.calls = []
        self.device = None
        self.prompts = None
        self.rows = rows

    def to(self, device):
        self.device = device

    def get_text_pe(self, prompts):
        assert prompts == list(VOCABULARY)
        return "stub text embeddings"

    def set_classes(self, prompts, embeddings):
        assert embeddings == "stub text embeddings"
        self.prompts = prompts

    def predict(self, image, **kwargs):
        self.calls.append((image.copy(), kwargs))
        rows = self.rows(image) if callable(self.rows) else (self.rows or [])
        boxes = SimpleNamespace(
            xyxy=np.asarray([row[:4] for row in rows]),
            conf=np.asarray([row[4] for row in rows]),
            cls=np.asarray([row[5] for row in rows]),
        )
        return [SimpleNamespace(boxes=boxes)]


@pytest.fixture
def create_proposer(monkeypatch):
    def create(*, rows=None, **kwargs):
        model = StubModel(rows)
        paths = []

        def constructor(path):
            paths.append(path)
            return model

        monkeypatch.setitem(sys.modules, "ultralytics", SimpleNamespace(YOLOE=constructor))
        proposer = YoloeProposer(Path("local.pt"), confidence=0.05, imgsz=1280, **kwargs)
        assert paths == [str(Path("local.pt"))]
        assert model.prompts == list(VOCABULARY)
        return proposer, model

    return create


def test_default_is_one_unmodified_predict_call(create_proposer):
    proposer, model = create_proposer(rows=[[1, 2, 7, 9, 0.7, 1]])
    image = np.zeros((17, 23, 3), dtype=np.uint8)
    detections = proposer(image)
    assert proposer.tiled is False
    assert model.device == 0
    assert len(model.calls) == 1
    np.testing.assert_array_equal(model.calls[0][0], image)
    assert model.calls[0][1] == {
        "imgsz": 1280,
        "conf": 0.05,
        "device": 0,
        "verbose": False,
    }
    assert detections == [Detection((1, 2, 7, 9), 0.7, "different label")]


def test_hybrid_has_full_frame_plus_four_calls_with_same_settings(create_proposer):
    proposer, model = create_proposer(tiled=True, device="cpu")
    image = np.zeros((960, 1280, 3), dtype=np.uint8)
    assert proposer(image) == []
    assert model.device == "cpu"
    assert [call[0].shape[:2] for call in model.calls] == [
        (960, 1280),
        (534, 712),
        (534, 712),
        (534, 712),
        (534, 712),
    ]
    assert all(
        call[1]
        == {
            "imgsz": 1280,
            "conf": 0.05,
            "device": "cpu",
            "verbose": False,
        }
        for call in model.calls
    )


def test_odd_rectangular_tiles_cover_boundaries_and_restore_offsets(create_proposer):
    proposer, model = create_proposer(rows=[[1, 1, 3, 3, 0.8, 0]])
    y, x = np.indices((9, 13))
    image = np.stack((x, y, x + y), axis=-1).astype(np.uint8)
    detections = proposer.tile_proposals(image)
    assert [d.bbox for d in detections] == [
        (1, 1, 3, 3),
        (6, 1, 8, 3),
        (1, 5, 3, 7),
        (6, 5, 8, 7),
    ]
    expected = [image[:5, :8], image[:5, 5:], image[4:, :8], image[4:, 5:]]
    for (crop, _), expected_crop in zip(model.calls, expected, strict=True):
        np.testing.assert_array_equal(crop, expected_crop)
    # The duplicated columns/rows are exactly the overlap; outer edges survive.
    np.testing.assert_array_equal(model.calls[0][0][:, 5:], model.calls[1][0][:, :3])
    np.testing.assert_array_equal(model.calls[0][0][4:], model.calls[2][0][:1])
    np.testing.assert_array_equal(model.calls[3][0][-1, -1], image[-1, -1])


def test_restored_boxes_clip_to_image_and_drop_degenerate(create_proposer):
    proposer, _ = create_proposer(
        rows=lambda image: [
            [-20, -20, 100, 100, 0.9, 0],
            [-20, -20, -10, -10, 0.8, 0],
        ]
    )
    detections = proposer.tile_proposals(np.zeros((9, 13, 3), dtype=np.uint8))
    assert len(detections) == 4
    assert all(d.bbox == (0, 0, 13, 9) for d in detections)


def test_nms_is_class_agnostic_keeps_higher_score_and_disjoint(create_proposer):
    proposer, _ = create_proposer()
    low = Detection((10, 10, 20, 20), 0.4, "car")
    high = Detection((10, 10, 20, 20), 0.9, "traffic cone")
    disjoint = Detection((30, 30, 40, 40), 0.6, "car")
    assert proposer.merge([low, disjoint, high]) == [high, disjoint]


def test_hybrid_merges_duplicates_across_full_frame_and_tiles(create_proposer):
    proposer, _ = create_proposer(tiled=True, rows=[[1, 1, 3, 3, 0.8, 0]])
    detections = proposer(np.zeros((9, 13, 3), dtype=np.uint8))
    assert len(detections) == 4  # The top-left tile duplicates the full-frame box.
    assert len({d.bbox for d in detections}) == 4


@pytest.mark.parametrize("shape,expected_calls", [((1, 1), 1), ((1, 9), 2), ((9, 1), 2)])
def test_tiny_images_deduplicate_identical_tile_origins(create_proposer, shape, expected_calls):
    proposer, model = create_proposer()
    proposer.tile_proposals(np.zeros((*shape, 3), dtype=np.uint8))
    assert len(model.calls) == expected_calls
    assert all(min(image.shape[:2]) > 0 for image, _ in model.calls)


@pytest.mark.parametrize(
    "options",
    [
        {"tiled": "false"},
        {"tiled": 1},
        {"tile_overlap": -0.1},
        {"tile_overlap": 1},
        {"tile_overlap": float("nan")},
        {"merge_iou": 0},
        {"merge_iou": 1.01},
        {"merge_iou": float("nan")},
    ],
)
def test_invalid_tiling_options_fail_before_loading_weights(options):
    with pytest.raises(ValueError):
        YoloeProposer(Path("missing.pt"), confidence=0.05, imgsz=1280, **options)


@pytest.mark.parametrize("min_side,expected", [(None, 6.0), (2.0, 2.0)])
def test_policy_config_wires_tiling_and_minimum_box_filter(
    monkeypatch, tmp_path, min_side, expected
):
    import scripts.eval_slow_system as slow
    import scripts.onboard_policy as policy_module

    calls = []
    monkeypatch.setattr(policy_module, "YoloeProposer", lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(slow, "LlamaServerRunner", lambda *a, **kw: None)
    config = {"proposer_tiled": True, "proposer_tile_overlap": 0.25, "proposer_merge_iou": 0.6}
    if min_side is not None:
        config["proposal_min_side_px"] = min_side
    policy = OnboardPolicy(
        task_text="Find a traffic cone", world_path=None, altitude_m=20,
        camera_info={"fx": 540, "fy": 540, "cx": 640, "cy": 480},
        image_size=(1280, 960), work_dir=tmp_path, config=config,
    )
    assert calls[0]["tiled"] is True
    assert calls[0]["tile_overlap"] == 0.25
    assert calls[0]["merge_iou"] == 0.6
    assert policy.loop.config.min_box_side_px == expected
    tiny = Detection((10, 10, 13, 15), 0.9, "object")
    assert policy.loop.filter_detections([tiny]) == ([tiny] if min_side else [])


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_policy_rejects_invalid_min_side_before_loading_models(tmp_path, value):
    with pytest.raises(ValueError, match="proposal_min_side_px"):
        OnboardPolicy(
            task_text="Find a cone", world_path=None, altitude_m=20,
            camera_info={"fx": 540, "fy": 540, "cx": 640, "cy": 480},
            image_size=(1280, 960), work_dir=tmp_path,
            config={"proposal_min_side_px": value},
        )


def test_default_policy_stays_single_pass_and_altitude_uses_effective_task(monkeypatch, tmp_path):
    import scripts.eval_slow_system as slow
    import scripts.onboard_policy as policy_module

    calls = []
    monkeypatch.setattr(policy_module, "YoloeProposer", lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(slow, "LlamaServerRunner", lambda *a, **kw: None)
    policy = OnboardPolicy(
        task_text="Find a bus", world_path=None, altitude_m=30,
        camera_info={"fx": 540, "fy": 540, "cx": 640, "cy": 480},
        image_size=(1280, 960), work_dir=tmp_path,
        config={"task_text": "Find a traffic cone", "size_aware_search": True,
                "min_search_px": 16},
    )
    assert calls[0]["tiled"] is False
    assert policy.loop.config.min_box_side_px == 6
    assert policy.search_altitude_m == pytest.approx(
        0.5 * 540 * math.sin(0.18 + math.radians(20)) / 16
    )
