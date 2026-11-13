from __future__ import annotations

import copy
import struct
import zlib
from types import SimpleNamespace

import pytest

pytest.importorskip("pyarrow", reason="OpenFly parquet image tests require optional pyarrow")
pytest.importorskip(
    "huggingface_hub", reason="OpenFly parquet source binding requires optional huggingface_hub"
)
from test_bind_openfly_parquet_route_metadata import exact_source, shard_row  # noqa: F401
from test_fetch_and_audit_openfly_parquet import parquet_bytes, parquet_source  # noqa: F401
from test_fetch_openfly_multiscene_navigation import fixture_source, write  # noqa: F401

from scripts import bind_openfly_parquet_route_metadata as exact
from scripts import extract_openfly_parquet_images as images
from scripts import fetch_and_audit_openfly_parquet as parquet
from scripts import fetch_openfly_multiscene_navigation as acquisition
from scripts import plan_openfly_multiscene_acquisition as planner


def png_chunk(kind, data):
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(data, zlib.crc32(kind)) & 0xFFFFFFFF)
    )


def original_png(width=8, height=8, pixel=(15, 30, 60)):
    # Transparent stand-in publisher bytes, never used as real image evidence.
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = b"".join(b"\0" + bytes(pixel) * width for _ in range(height))
    return (
        images.PNG_SIGNATURE
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(pixels))
        + png_chunk(b"IEND", b"")
    )


@pytest.fixture
def embedded_source(parquet_source, monkeypatch):  # noqa: F811
    value = parquet_source
    monkeypatch.setattr(images, "ROOT", value.root)
    for name in images.SOURCE_FILES:
        write(value.root / name, b"new original embedded PNG evidence implementation\n")
    document = []
    for offset, route in enumerate(value.plan["routes"]):
        rows = value.rows[route["annotation_index"]]
        for index, row in enumerate(rows):
            row["image"]["bytes"] = original_png(pixel=(offset * 70 + index, 30, 60))
        raw = parquet_bytes(rows)
        payload = next(
            row
            for row in value.plan["payloads"]
            if row["annotation_index"] == route["annotation_index"] and row["role"] == "shard"
        )
        value.client.payloads[payload["url"]] = raw
        document.append(shard_row(route["parquet_path"], raw))
    # Update stand-in metadata fixtures before Stage1 is executed, never real frozen files.
    metadata = write(value.root / exact.OUTPUT / "paths-info.json", document)
    previous = planner.strict_json((value.root / exact.OUTPUT / "report.json").read_bytes())
    previous["metadata"] = metadata
    previous["routes"] = exact.exact_rows(document, value.source.plan["routes"])
    previous["shard_bytes_sum"] = sum(row["size"] for row in document)
    write(value.root / exact.OUTPUT / "report.json", previous)
    value.plan = parquet.build_plan()
    value.sha = write(value.path, value.plan)["sha256"]
    result, _receipt = parquet.acquire(value.path, value.sha, client=value.client)
    assert result["native_exit_code"] == 0
    plan = images.build_plan()
    path = value.root / "configs/training/original-png-plan.json"
    record = write(path, plan)
    return SimpleNamespace(
        root=value.root, source=value, plan=plan, path=path, sha=record["sha256"]
    )


def unit_route(value, mutate=None):
    route = copy.deepcopy(value.plan["routes"][0])
    rows = copy.deepcopy(value.source.rows[route["annotation_index"]])
    if mutate is not None:
        mutate(rows)
    file = write(value.root / "unit-source/fixture.parquet", parquet_bytes(rows))
    route["shard_file"] = file
    reader, summary = parquet.checked_parquet(
        planner.local_path(file["path"]), route["required_frame_count"]
    )
    reader.close()
    route["schema_metadata"] = summary
    out = value.root / "unit-images"
    return route, out


def test_bounded_crc_and_actual_PNG_decode_keep_original_encoding():
    raw = original_png()
    result = images.check_png(raw)
    assert result["all_chunk_CRCs_verified"] is True
    assert result["actual_Pillow_decode_succeeded"] is True
    assert result["transcoded"] is False
    assert (result["width"], result["height"], result["mode"]) == (8, 8, "RGB")
    assert result["png_chunk_count"] == 3
    assert result["complete_single_zlib_stream_verified"] is True


@pytest.mark.parametrize(
    "kind", ["missing_adler", "trailing_garbage", "two_streams", "extra_pixels"]
)
def test_correct_png_CRCS_cannot_hide_incomplete_or_extra_zlib_data(kind):
    pixels = b"\0\x10\x20\x30"
    stream = zlib.compress(pixels)
    if kind == "missing_adler":
        stream = stream[:-4]
    elif kind == "trailing_garbage":
        stream += b"trailing zlib garbage"
    elif kind == "two_streams":
        stream += zlib.compress(pixels)
    else:
        stream = zlib.compress(pixels + b"extra pixels")
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = (
        images.PNG_SIGNATURE
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", stream)
        + png_chunk(b"IEND", b"")
    )
    with pytest.raises(ValueError, match="zlib|decoded pixels"):
        images.check_png(raw)


def test_bounded_deflate_count_and_adam7_scanline_identity():
    large = images.check_png(original_png(256, 256))
    assert large["original_scanline_bytes_verified"] == (256 * 3 + 1) * 256
    width, height = 9, 9
    pixels = b""
    for x, y, dx, dy in (
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    ):
        columns = (width - x + dx - 1) // dx if width > x else 0
        rows = (height - y + dy - 1) // dy if height > y else 0
        if columns and rows:
            pixels += (b"\0" + b"\x10\x20\x30" * columns) * rows
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 1)
    raw = (
        images.PNG_SIGNATURE
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(pixels))
        + png_chunk(b"IEND", b"")
    )
    result = images.check_png(raw)
    assert result["interlace"] == 1
    assert result["original_scanline_bytes_verified"] == len(pixels)
    assert result["actual_Pillow_decode_succeeded"] is True


def test_truncated_decoder_override_is_not_allowed(monkeypatch):
    from PIL import ImageFile

    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", True)
    with pytest.raises(ValueError, match="truncated"):
        images.check_png(original_png())


@pytest.mark.parametrize(
    "raw",
    [
        None,
        b"",
        b"not a PNG",
        original_png()[:-1],
        original_png() + b"trailing bytes",
        original_png()[:40] + bytes([original_png()[40] ^ 1]) + original_png()[41:],
        images.PNG_SIGNATURE
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 65536, 65536, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", b"x")
        + png_chunk(b"IEND", b""),
        images.PNG_SIGNATURE
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 6, 0, 0, 0))
        + png_chunk(b"IDAT", b"x")
        + png_chunk(b"IEND", b""),
        images.PNG_SIGNATURE
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", b"invalid zlib but correct PNG CRC")
        + png_chunk(b"IEND", b""),
    ],
)
def test_original_png_crc_truncation_pixelbomb_encoding_and_decode_errors_rejected(raw):
    with pytest.raises((ValueError, OSError)):
        images.check_png(raw)


def test_plan_binds_only_original82_and_no_training_label_or_holdout_authority(embedded_source):
    plan = embedded_source.plan
    assert len(plan["routes"]) == 3 and plan["expected_images"] == 82
    assert all(route["original_annotation_split"] == "train" for route in plan["routes"])
    assert plan["network_authorized"] is False
    assert plan["transcode_authorized"] is False
    assert plan["merge_old_dataset_authorized"] is False
    assert plan["flight_label_derivation_authorized"] is False
    assert plan["independent_holdout_claim_allowed"] is False
    assert all(value is False for value in plan["claims"].values())


def test_82_original_encoded_bytes_sha_row_provenance_and_create_only(embedded_source, monkeypatch):
    value = embedded_source
    from PIL import Image

    def forbidden_save(*_args, **_kwargs):
        raise AssertionError("Extractor must never transcode original embedded bytes")

    monkeypatch.setattr(Image.Image, "save", forbidden_save)
    report, record = images.extract(value.path, value.sha)
    assert report["native_exit_code"] == 0
    assert report["all_82_original_images_verified"] is True
    assert report["all_source_shard_row_to_encoded_byte_bindings_verified"] is True
    assert report["PNG_decode_validated_count_at_extraction"] == 82
    assert report["current_output_files_reverified_count"] == 82
    for row in report["images"]:
        raw = value.source.rows[row["annotation_index"]][row["parquet_row_index"]]["image"]["bytes"]
        actual = planner.verify_binding(row["file"]).read_bytes()
        assert raw == actual
        assert row["embedded_encoded_sha256"] == row["file"]["sha256"]
        assert row["frame_index"] == row["parquet_row_index"]
        assert row["publisher_shard"]["digest"] == row["source_shard"]["sha256"]
        assert row["opaque_embedded_path_used_as_source"] is False
        assert row["png"]["actual_Pillow_decode_succeeded"] is True
    assert report["sources_before"] == report["sources_after"]
    assert report["runtime_sources_before"] == report["runtime_sources_after"]
    assert report["network_io_in_phase"] is False
    assert report["transcoded_images"] == 0
    assert report["optimizer_updates"] == 0 and report["GPU_used"] is False
    assert report["forbidden_model_imports"] == []
    assert report["independent_holdout_verified"] is False
    assert report["FRD_flight_labels_verified"] is False
    assert all(report[key] is False for key in acquisition.GATES)
    assert record == planner.binding(value.root / images.OUTPUT / "receipt.json")
    with pytest.raises(FileExistsError):
        images.extract(value.path, value.sha)


@pytest.mark.parametrize(
    "change",
    [
        lambda plan: plan.update(network_authorized=True),
        lambda plan: plan.update(transcode_authorized=True),
        lambda plan: plan.update(independent_holdout_claim_allowed=True),
        lambda plan: plan.update(output_directory="artifacts/training/old-source"),
        lambda plan: plan["budget"].update(max_images=83),
        lambda plan: plan["routes"][0].update(original_annotation_split="unseen"),
    ],
)
def test_plan_scope_changes_fail_before_image_writes(embedded_source, change):
    value = embedded_source
    plan = copy.deepcopy(value.plan)
    change(plan)
    record = write(value.path, plan)
    with pytest.raises(ValueError, match="sources"):
        images.extract(value.path, record["sha256"])
    assert not (value.root / images.OUTPUT).exists()


@pytest.mark.parametrize(
    "image",
    [
        None,
        {"bytes": None, "path": "https://example.invalid/image.png"},
        {"bytes": None, "path": "C:/private/other.png"},
    ],
)
def test_null_or_path_only_never_falls_back_to_any_path(embedded_source, image, monkeypatch):
    from PIL import Image

    calls = []

    def forbidden_open(*args, **_kwargs):
        calls.append(args)
        raise AssertionError("No path-only fallback or image decoder expected")

    monkeypatch.setattr(Image, "open", forbidden_open)
    route, out = unit_route(embedded_source, lambda rows: rows[0].update(image=image))
    with pytest.raises(ValueError, match="fallback"):
        images.extract_route(route, out, written=[])
    assert calls == []
    assert not out.exists()


def test_image_row_metadata_drift_or_duplicate_rejected_before_write(embedded_source):
    route, out = unit_route(embedded_source, lambda rows: rows[0].update(frame_index=1))
    with pytest.raises(ValueError, match="metadata"):
        images.extract_route(route, out, written=[])
    assert not out.exists()


@pytest.mark.parametrize("kind", ["source_only", "output_only", "both"])
def test_source_and_output_postwrite_drift_have_real_native_failure(
    embedded_source, monkeypatch, kind
):
    value = embedded_source
    original = images.extract_route
    changed = False

    def changed_route(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            changed = True
            if kind != "output_only":
                (value.root / images.SOURCE_FILES[0]).write_bytes(b"changed decoder entry")
            if kind != "source_only":
                planner.local_path(kwargs["written"][0]["file"]["path"]).write_bytes(
                    b"changed output"
                )
        return result

    monkeypatch.setattr(images, "extract_route", changed_route)
    report, _ = images.extract(value.path, value.sha)
    assert report["native_exit_code"] == 1
    assert report["sources_unchanged"] is (kind == "output_only")
    assert report["all_82_original_images_verified"] is False
    assert report["all_source_shard_row_to_encoded_byte_bindings_verified"] is False
    assert report["current_output_files_reverified_count"] == (82 if kind == "source_only" else 81)


def test_partial_png_failure_preserves_verified_images_without_false82_claim(
    embedded_source, monkeypatch
):
    value = embedded_source
    original = images.check_png
    calls = 0

    def fail_second(raw):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("original CRC stand-in fails on second image")
        return original(raw)

    monkeypatch.setattr(images, "check_png", fail_second)
    report, _ = images.extract(value.path, value.sha)
    assert report["native_exit_code"] == 1
    assert report["PNG_decode_validated_count_at_extraction"] == 60
    assert report["current_output_files_reverified_count"] == 60
    assert len(report["completed_routes"]) == 2
    assert report["all_82_original_images_verified"] is False
    assert len(report["failures"]) == 1
    assert len(list((value.root / images.OUTPUT).rglob("*.png"))) == 60


def test_wrong_explicit_sha_and_original_shard_drift_fail_before_outputs(embedded_source):
    value = embedded_source
    with pytest.raises(ValueError, match="SHA"):
        images.extract(value.path, "0" * 64)
    path = planner.local_path(value.plan["routes"][0]["shard_file"]["path"])
    path.write_bytes(b"changed original shard")
    with pytest.raises(ValueError):
        images.extract(value.path, value.sha)
    assert not (value.root / images.OUTPUT).exists()


@pytest.mark.parametrize("kind", ["route", "payload"])
def test_stage1_receipt_cannot_relabel_routes_or_payload_authority_on_emission(
    embedded_source, kind
):
    value = embedded_source
    path = value.root / images.ACQUISITION_RECEIPT
    receipt = planner.strict_json(path.read_bytes())
    if kind == "route":
        receipt["routes"][0]["image_path"] = "env_airsim_18/astar_data/low_short/changed"
        receipt["routes"][0]["scene_id"] = "env_airsim_18"
    else:
        receipt["verified_payloads"][0]["repository_path"] = "Annotation/unseen.json"
    write(path, receipt)
    with pytest.raises(ValueError, match="admitted"):
        images.build_plan()
    assert not (value.root / images.OUTPUT).exists()
