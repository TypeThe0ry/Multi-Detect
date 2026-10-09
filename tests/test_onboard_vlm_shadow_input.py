from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.onboard_vlm_shadow_input import prepare_shadow_case


def _case(root: Path) -> dict[str, object]:
    image = pytest.importorskip("PIL.Image")
    frames = []
    for index in range(2):
        path = root / f"frame-{index}.png"
        image.new("RGB", (8, 6), (100 + index, 80, 60)).save(path)
        frames.append(
            {
                "frame_id": f"frame-{index}",
                "path": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "captured_at_s": index * 0.5,
                "clock_domain": "explicit-offline-clip-clock",
                "source_clip_id": "fixture-clip-1",
            }
        )
    return {
        "case_id": "actual-rgb-fixture",
        "prompt": "\u89c2\u5bdf\u5f53\u524d\u753b\u9762\uff0c\u4e0d\u8981\u7f16\u9020GPS\u3002",
        "frames": frames,
        "evaluation_metadata": {"secret_label": "never feed this"},
    }


def test_real_bytes_prompt_missing_context_and_clock_are_bound(tmp_path: Path) -> None:
    case = _case(tmp_path)
    prepared = prepare_shadow_case(case, repository_root=tmp_path)
    document = prepared.to_mapping()
    assert prepared.prompt.text == case["prompt"]
    assert prepared.prompt.language == "zh"
    assert prepared.requested_at_s == 0.5
    assert prepared.deadline_at_s == 30.5
    assert prepared.max_frame_age_s == 30.5
    assert prepared.scope.home_sha256 is None
    assert prepared.scope.airframe_profile_sha256 is None
    assert prepared.ledger.receipts == ()
    assert all(frame.context is None for frame in prepared.frames)
    assert prepared.frames[0].image_bytes == (tmp_path / "frame-0.png").read_bytes()
    assert "offline-replay-not-live-sensor" in prepared.scope.clock_epoch
    assert document["execution_authority"] is False
    assert "never feed this" not in str(document)
    changed = deepcopy(case)
    changed["evaluation_metadata"] = {"secret_label": "different"}
    assert (
        prepare_shadow_case(changed, repository_root=tmp_path).canonical_hash
        == prepared.canonical_hash
    )


def test_prompt_change_resets_scope_and_hash(tmp_path: Path) -> None:
    case = _case(tmp_path)
    first = prepare_shadow_case(case, repository_root=tmp_path)
    case["prompt"] = (
        "\u53ea\u89c2\u5bdf\u9053\u8def\uff0c\u4e0d\u8981\u641c\u7d22\u8f66\u8f86\u3002"
    )
    second = prepare_shadow_case(case, repository_root=tmp_path)
    assert first.scope != second.scope
    assert first.canonical_hash != second.canonical_hash


def test_missing_clip_identity_is_rejected(tmp_path: Path) -> None:
    case = _case(tmp_path)
    del case["frames"][1]["source_clip_id"]
    with pytest.raises(ValueError, match="source_clip_id"):
        prepare_shadow_case(case, repository_root=tmp_path)


def test_clip_identity_is_bound_without_semantic_ids_visible(tmp_path: Path) -> None:
    case = _case(tmp_path)
    case["case_id"] = "near-hidden-current-only"
    for frame in case["frames"]:
        frame["frame_id"] = f"near-hidden-{frame['captured_at_s']}"
        frame["source_clip_id"] = "near-hidden-recording"
    first = prepare_shadow_case(case, repository_root=tmp_path)
    for frame in case["frames"]:
        frame["source_clip_id"] = "near-reappeared-recording"
    second = prepare_shadow_case(case, repository_root=tmp_path)
    assert first.scope != second.scope
    assert first.canonical_hash != second.canonical_hash
    serialized = str(first.to_mapping())
    assert "near-hidden" not in serialized
    assert "near-reappeared" not in str(second.to_mapping())
    assert all(frame.frame_id.startswith("rgb-") for frame in first.frames)


@pytest.mark.parametrize("bad_path", ["../escape.png", "D:/elsewhere/image.png", "C:/image.png"])
def test_paths_cannot_escape(tmp_path: Path, bad_path: str) -> None:
    case = _case(tmp_path)
    case["frames"][0]["path"] = bad_path
    with pytest.raises(ValueError, match="relative"):
        prepare_shadow_case(case, repository_root=tmp_path)


@pytest.mark.parametrize(
    "mutation", ["hash", "future", "gap", "clock", "duplicate", "width", "encoding", "clip"]
)
def test_hashes_time_and_identity_are_checked(tmp_path: Path, mutation: str) -> None:
    case = _case(tmp_path)
    first, second = case["frames"]
    if mutation == "hash":
        first["sha256"] = "0" * 64
    elif mutation == "future":
        second["captured_at_s"] = 0.0
    elif mutation == "gap":
        second["captured_at_s"] = 6.0
    elif mutation == "clock":
        second["clock_domain"] = "other-clock"
    elif mutation == "duplicate":
        second["frame_id"] = first["frame_id"]
    elif mutation == "width":
        second["width"] = 9
    elif mutation == "encoding":
        second["encoding"] = "jpeg"
    elif mutation == "clip":
        first["source_clip_id"] = "recording-1"
        second["source_clip_id"] = "recording-2"
    with pytest.raises(ValueError):
        prepare_shadow_case(case, repository_root=tmp_path)


@pytest.mark.parametrize("budget", [False, 0, -1, float("nan"), 301])
def test_deadline_budget_is_bounded(tmp_path: Path, budget: float) -> None:
    with pytest.raises(ValueError):
        prepare_shadow_case(_case(tmp_path), repository_root=tmp_path, deadline_s=budget)
