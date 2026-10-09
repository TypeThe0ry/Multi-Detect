"""Small CPU fixtures only: not actual290 pixels or training admission."""

import copy
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.autonomy import onboard_navigation_supervision as nav
from scripts import audit_onboard_navigation_dataset_overlap as audit
from scripts import extract_openfly_parquet_images as extractor

H = "a" * 64


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    (tmp_path / "artifacts/diagnostics").mkdir(parents=True)
    return tmp_path


def binding(path, raw=b"control"):
    return {"path": path, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def png(size=(3, 2), color=(21, 42, 63), compression=6):
    from PIL import Image

    stream = io.BytesIO()
    with Image.new("RGB", size, color) as image:
        image.save(stream, format="PNG", compress_level=compression)
    return stream.getvalue()


def save_png(root, name, **kwargs):
    raw = png(**kwargs)
    (root / name).write_bytes(raw)
    return binding(name, raw)


def samples(count=3, partition="train", episode="episode", route="route", index="0"):
    source = {
        "repository_id": "IPEC-COMMUNITY/OpenFly",
        "revision": "a12316d56a4e35a32ad626fb725ed7089937a1c4",
        "original_split": "train",
        "annotation_record_id": index,
        **{
            name: {"path": f"fixture/{name}.json", "bytes": 3, "sha256": H}
            for name in (
                "annotation",
                "conversion",
                "license",
                "unit_verification",
                "alignment_verification",
            )
        },
        "pose": {
            "path": f"fixture/Image/env_airsim_26/astar_data/low_short/{route}/pose.jsonl",
            "bytes": 3,
            "sha256": H,
        },
        "review_receipt": None,
    }
    prompt = {
        "text_original": "Go to the building.",
        "utf8_sha256": hashlib.sha256(b"Go to the building.").hexdigest(),
        "source_record_sha256": H,
        "source_pointer": {
            "format": "json",
            "json_pointer": f"/{index}/gpt_instruction",
            "record_index": None,
        },
    }
    result, prior = [], None
    for step in range(count):
        terminal = step == count - 1
        frame = {
            "frame_id": f"{episode}-frame-{step}",
            "episode_id": episode,
            "scene_id": "env_airsim_26",
            "step_index": step,
            "path": f"fixture/{episode}-{step}.png",
            "bytes": 3,
            "sha256": H,
            "width": 3,
            "height": 2,
            "encoding": "png",
            "source_clock_id": None,
            "source_stamp": None,
            "stamp_unit": None,
        }
        raw = {
            "imageid": step,
            "pos": [float(3 * step), 0.0, 0.0],
            "yaw": 0.0,
            "type": "stop" if terminal else "go straight",
            "value": 0 if terminal else 3,
        }
        row = {
            "schema_version": 1,
            "contract": "multidetect.onboard-navigation-supervision-v1",
            "sample_id": f"{episode}-sample-{step}",
            "episode_id": episode,
            "scene_id": "env_airsim_26",
            "step_index": step,
            "source": copy.deepcopy(source),
            "prompt": copy.deepcopy(prompt),
            "frames": [copy.deepcopy(prior), frame] if prior else [frame],
            "partition": partition,
            "label_provenance": "external_planned_navigation_supervision",
            "alignment": {
                "observation_step": step,
                "action_step": step,
                "next_step": None if terminal else step + 1,
                "dt_s": None,
                "evidence_sha256": H,
            },
            "pose": {
                "coordinate_frame": "openfly-world-xy-z-up-not-geographic",
                "length_unit": "m",
                "origin": "planned_pose",
                "position_xyz": raw["pos"],
                "source_record_sha256": H,
                "yaw": 0.0,
                "yaw_unit": "rad",
            },
            "planned_action": {
                "kind": "stop" if terminal else "translate",
                "raw": raw,
                "raw_record_sha256": H,
                "raw_source_pointer": {
                    "format": "json_sequence",
                    "json_pointer": "/action",
                    "record_index": step,
                },
                "translation_m": None if terminal else [3.0, 0.0, 0.0],
                "yaw_delta_rad": None,
                "unit_verification_sha256": None if terminal else H,
            },
        }
        result.append(row)
        prior = frame
    return result


def groups(partitions=("train", "dev-test"), same_identity=False):
    descriptions, occurrences = [], []
    for number, partition in enumerate(partitions):
        rows = samples(
            partition=partition,
            episode=f"e{number}",
            route="same" if same_identity else f"route{number}",
            index="0" if same_identity else str(number),
        )
        description, current = audit.collect_dataset(f"d{number}", rows, nav)
        descriptions.append(description)
        occurrences.extend(current)
    return descriptions, occurrences


def fake_decoded(occurrences, shared=False):
    result = []
    for number, row in enumerate(occurrences):
        result.append(
            {
                "image": row["image"],
                "width": row["width"],
                "height": row["height"],
                "rgb_payload_sha256": H
                if shared
                else hashlib.sha256(str(number).encode()).hexdigest(),
            }
        )
    return result


def test_complete_typed_episodes_count_only_current_frames(sandbox):
    rows = samples()
    description, current = audit.collect_dataset("fixture", rows, nav)
    assert description["samples"] == 3
    assert len(description["episodes"]) == 1
    assert len(current) == 3
    assert description["actor_frame_reference_occurrences"] == 5
    assert description["actor_current_past_frame_closure_verified"] is True
    assert sum(len(row["frames"]) for row in rows) == 5
    assert description["episodes"][0]["sample_ids"] == [row["sample_id"] for row in rows]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_stop",
        "duplicate_sample",
        "mixed_partition",
        "past_sha",
        "cross_episode",
        "nonconsecutive",
        "boolean_bytes",
        "pixel_bomb",
    ],
)
def test_bad_complete_episode_or_history_rejected(sandbox, mutation):
    rows = samples()
    if mutation == "missing_stop":
        rows = rows[:-1]
    elif mutation == "duplicate_sample":
        rows[1]["sample_id"] = rows[0]["sample_id"]
    elif mutation == "mixed_partition":
        rows[1]["partition"] = "dev-test"
    elif mutation == "past_sha":
        rows[1]["frames"][0]["sha256"] = "b" * 64
    elif mutation == "cross_episode":
        rows[1]["frames"][0]["episode_id"] = "other"
    elif mutation == "nonconsecutive":
        rows[1]["step_index"] = 9
    elif mutation == "boolean_bytes":
        rows[0]["frames"][0]["bytes"] = True
    else:
        rows[0]["frames"][0]["width"] = audit.BUDGET["max_decoded_pixels"]
    with pytest.raises((ValueError, TypeError)):
        audit.collect_dataset("fixture", rows, nav)


def test_same_scene_different_episodes_not_merged(sandbox):
    descriptions, current = groups()
    report = audit.analyze(descriptions, current, fake_decoded(current))
    assert not report["episode_identity_overlap_groups"]
    assert not report["cross_train_development_conflicts"]
    assert report["candidate_exact_overlap_gate_passed"] is True
    assert report["absence_of_exact_overlap_proves_generalization_or_independence"] is False


def test_cross_role_pixel_conflict_preserves_entire_episodes(sandbox):
    descriptions, current = groups()
    report = audit.analyze(descriptions, current, fake_decoded(current, shared=True))
    assert len(report["cross_train_development_conflicts"]) == 1
    assert len(report["cross_train_development_conflicts"][0]["episodes"]) == 2
    assert (
        sum(
            len(row["sample_ids"])
            for row in report["cross_train_development_conflicts"][0]["episodes"]
        )
        == 6
    )
    assert len(current) == 6
    assert report["candidate_exact_overlap_gate_passed"] is False


def test_original_route_and_annotation_alias_conflict(sandbox):
    descriptions, current = groups(same_identity=True)
    report = audit.analyze(descriptions, current, fake_decoded(current))
    assert {row["identity_kind"] for row in report["episode_identity_overlap_groups"]} == {
        "route_key",
        "annotation_key",
    }
    assert len(report["cross_train_development_conflicts"]) == 1


def test_cross_dataset_train_repeats_are_separate_from_development_conflicts(sandbox):
    descriptions, current = groups(("train", "train"), True)
    report = audit.analyze(descriptions, current, fake_decoded(current))
    assert not report["cross_train_development_conflicts"]
    assert len(report["cross_dataset_train_repeated_exposure_components"]) == 1


def test_pixel_dimensions_are_part_of_exact_identity(sandbox):
    descriptions, current = groups()
    decoded = fake_decoded(current, True)
    for row, image in zip(current[3:], decoded[3:], strict=True):
        row["width"], row["height"] = 2, 3
        image["width"], image["height"] = 2, 3
    report = audit.analyze(descriptions, current, decoded)
    assert not report["cross_train_development_conflicts"]


def test_encoded_different_pngs_same_rgb_and_no_transcoding(sandbox, monkeypatch):
    from PIL import Image

    first = save_png(sandbox, "first.png", compression=0)
    second = save_png(sandbox, "second.png", compression=9)
    assert first["sha256"] != second["sha256"]
    monkeypatch.setattr(Image.Image, "save", lambda *a, **k: pytest.fail("No transcoding allowed"))
    a = audit.decode_image(first, extractor)
    b = audit.decode_image(second, extractor)
    assert a["rgb_payload_sha256"] == b["rgb_payload_sha256"]
    assert a["image"] != b["image"]
    assert a["rgb_payload_sha256"] == hashlib.sha256(bytes((21, 42, 63)) * 6).hexdigest()


@pytest.mark.parametrize("mutation", ["crc", "trailing", "truncated", "sha", "path_only"])
def test_invalid_original_png_or_source_rejected(sandbox, mutation):
    item = save_png(sandbox, "image.png")
    raw = (sandbox / "image.png").read_bytes()
    if mutation == "crc":
        raw = raw[:-1] + bytes([raw[-1] ^ 1])
    elif mutation == "trailing":
        raw += b"garbage"
    elif mutation == "truncated":
        raw = raw[:-8]
    elif mutation == "sha":
        item["sha256"] = H
    else:
        item = {"path": item["path"]}
    if mutation in {"crc", "trailing", "truncated"}:
        (sandbox / "image.png").write_bytes(raw)
        item = binding("image.png", raw)
    with pytest.raises((ValueError, KeyError)):
        audit.decode_image(item, extractor)


@pytest.mark.parametrize("value", ["../outside.json", "artifacts/../outside.json", ""])
def test_path_escape_rejected(sandbox, value):
    with pytest.raises(ValueError):
        audit.repo_path(value)


@pytest.mark.parametrize(
    "value",
    [
        "artifacts/training/onboard-navigation-dataset-overlap-x",
        "artifacts/diagnostics/not-owned",
        "artifacts/diagnostics/onboard-navigation-dataset-overlap-x/nested",
    ],
)
def test_output_scope_rejected(sandbox, value):
    with pytest.raises(ValueError):
        audit.output_path(value)


def test_create_only_and_duplicate_binding_conflict(sandbox):
    first = binding("source.json")
    assert audit.merge_bindings([first, dict(first)]) == [first]
    with pytest.raises(ValueError, match="Conflicting"):
        audit.merge_bindings([first, {**first, "sha256": H}])
    target = sandbox / "output.json"
    audit.write_json(target, {"claims": dict.fromkeys(audit.FALSE_CLAIMS, False)})
    with pytest.raises(FileExistsError):
        audit.write_json(target, {"overwrite": True})


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'])
def test_strict_json_rejects_ambiguity(raw):
    with pytest.raises(ValueError):
        audit.parse_json(raw)


def test_caps_checked_before_file_read(sandbox):
    (sandbox / "source").write_bytes(b"oversize")
    with pytest.raises(ValueError, match="bounded"):
        audit.hash_file(sandbox / "source", 2)
    with pytest.raises(ValueError):
        audit.read_bound(binding("missing", b"x" * 20), 10)


def test_bad_plan_sha_fails_before_output_or_pillow(sandbox, monkeypatch):
    target = sandbox / "plan.json"
    target.write_bytes(b"{}")
    monkeypatch.setattr(
        audit, "build_plan", lambda *a: pytest.fail("Must not rebuild unbound plan")
    )
    with pytest.raises(ValueError, match="Plan SHA"):
        audit.execute("plan.json", H)
    assert not list((sandbox / "artifacts/diagnostics").iterdir())


@pytest.mark.parametrize(
    "mutation", ["none", "source_drift", "image_drift", "decode_failure", "runtime_origin"]
)
def test_execute_fixture_closes_sources_and_never_claims_training(sandbox, monkeypatch, mutation):
    descriptions, current = groups()
    decoded = fake_decoded(current, True)
    image_sources = []
    for row in current:
        item = row["image"]
        target = sandbox / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"png")
        item.update(binding(item["path"], b"png"))
        image_sources.append(dict(item))
    decoded = fake_decoded(current, True)
    control = binding("control.json")
    (sandbox / "control.json").write_bytes(b"control")
    plan = {
        "output_dir": "artifacts/diagnostics/onboard-navigation-dataset-overlap-fixture",
        "sources": [control],
        "runtime": {"version": "fixture", "files": []},
        "datasets": descriptions,
        "images": image_sources,
        "current_frame_occurrences": current,
        "source_scope": "fixture",
    }
    target = sandbox / "plan.json"
    target.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(audit, "build_plan", lambda value: plan)
    monkeypatch.setattr(audit, "pillow_bindings", lambda: plan["runtime"])

    def origins(value):
        if mutation == "runtime_origin":
            raise ValueError("fixture runtime origin mismatch")

    monkeypatch.setattr(audit, "validate_pillow_origins", origins)
    monkeypatch.setattr(audit, "png_api", lambda: None)

    def decode(item, api):
        if mutation == "source_drift":
            (sandbox / "control.json").write_bytes(b"changed")
        elif mutation == "image_drift":
            (sandbox / item["path"]).write_bytes(b"new")
        elif mutation == "decode_failure":
            raise ValueError("fixture bad PNG")
        return next(row for row in decoded if row["image"] == item)

    monkeypatch.setattr(audit, "decode_image", decode)
    result = audit.execute("plan.json", hashlib.sha256(target.read_bytes()).hexdigest())
    assert result["native_exit_code"] == (0 if mutation == "none" else 1)
    assert result["candidate_exact_overlap_gate_passed"] is False
    assert all(value is False for value in result["claims"].values())
    assert result["sources_unchanged"] is (mutation == "none")
    assert result["verified_current_frame_occurrences"] == (6 if mutation == "none" else 0)
    assert result["all_original_images_byte_and_pixel_verified"] is (mutation == "none")
    assert result["actor_frame_reference_occurrences"] == 10
    assert result["past_frame_reference_occurrences"] == 4
    assert (sandbox / plan["output_dir"] / "report.json").exists()


def test_actual_pillow_runtime_origin_and_wrong_source_rejected():
    runtime = audit.pillow_bindings()
    audit.validate_pillow_origins(runtime)
    wrong = copy.deepcopy(runtime)
    wrong["files"][0]["path"] += ".not-actual"
    with pytest.raises(ValueError, match="origin"):
        audit.validate_pillow_origins(wrong)


def test_preserved_prior_whole_group_not_split_after_pixel_conflict(sandbox):
    descriptions, current = groups()
    extra_rows = samples(episode="third", route="third-route", index="3")
    extra, third = audit.collect_dataset("d0", extra_rows, nav)
    descriptions[0]["episodes"].extend(extra["episodes"])
    descriptions[0]["prior_partition_grouping"] = {"groups": [["e0", "third"]]}
    current.extend(third)
    decoded = fake_decoded(current)
    decoded[3]["rgb_payload_sha256"] = decoded[0]["rgb_payload_sha256"]
    report = audit.analyze(descriptions, current, decoded)
    assert {
        row["episode_id"] for row in report["cross_train_development_conflicts"][0]["episodes"]
    } == {"e0", "e1", "third"}


def test_stdlib_import_has_no_pillow_or_heavy_modules():
    entry = Path(audit.__file__).resolve()
    code = (
        f"import runpy,sys;runpy.run_path({str(entry)!r},run_name='not_main');"
        "assert not any(n.split('.')[0] in "
        "('PIL','torch','transformers','pyarrow','datasets','peft') for n in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_publisher_identity_is_not_opaque_episode_hash_or_scene_only(sandbox):
    row = samples()[0]
    key = audit.publisher_episode(row)
    changed = copy.deepcopy(row)
    changed["episode_id"] = "different-opaque-id"
    assert audit.publisher_episode(changed) == key
    changed["source"]["pose"]["path"] = (
        "another-root/Image/env_airsim_26/astar_data/low_short/route/pose.jsonl"
    )
    assert audit.publisher_episode(changed) == key
    changed["source"]["pose"]["path"] = changed["source"]["pose"]["path"].replace(
        "/route/", "/another-route/"
    )
    assert audit.publisher_episode(changed)["route_key"] != key["route_key"]


@pytest.mark.parametrize(
    "mutation", ["wrong_scene", "repeated_Image", "noncanonical_index", "reserved_split"]
)
def test_publisher_identity_rejects_ambiguous_or_reserved_source(sandbox, mutation):
    row = samples()[0]
    if mutation == "wrong_scene":
        row["scene_id"] = "env_airsim_16"
    elif mutation == "repeated_Image":
        row["source"]["pose"]["path"] = "Image/" + row["source"]["pose"]["path"]
    elif mutation == "noncanonical_index":
        row["source"]["annotation_record_id"] = "00"
    else:
        row["source"]["original_split"] = "unseen"
    with pytest.raises(ValueError):
        audit.publisher_episode(row)


def test_heavy_import_sentinel_fails(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch.forbidden_fixture", SimpleNamespace())
    with pytest.raises(ValueError, match="Forbidden"):
        audit.check_no_heavy()
