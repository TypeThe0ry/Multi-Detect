from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.gazebo_annotation_stream import (
    _box_row,
    _header_data,
    _segmentation_observation,
)


def _header(*items: tuple[str, list[str]], sec: int = 7, nsec: int = 11):
    return SimpleNamespace(
        stamp=SimpleNamespace(sec=sec, nsec=nsec),
        data=[SimpleNamespace(key=key, value=values) for key, values in items],
    )


def test_header_data_reads_repeated_proto_values() -> None:
    message = SimpleNamespace(header=_header(("seq", ["12"]), ("frame_id", ["camera"])))
    assert _header_data(message) == {"seq": "12", "frame_id": "camera"}


def test_header_data_rejects_ambiguous_repeated_value() -> None:
    message = SimpleNamespace(header=_header(("seq", ["12", "13"])))
    with pytest.raises(ValueError, match="ambiguous"):
        _header_data(message)


def test_box_row_preserves_sensor_stamp_and_rendering_view_geometry() -> None:
    center = SimpleNamespace(x=0.0, y=1.0, z=-5.0)
    size = SimpleNamespace(x=2.0, y=3.0, z=4.0)
    orientation = SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0)
    box = SimpleNamespace(center=center, boxSize=size, orientation=orientation)
    annotated = SimpleNamespace(label=42, box=box)
    message = SimpleNamespace(
        header=_header(("seq", ["3"]), ("frame_id", ["bbox_camera"])),
        annotated_box=[annotated],
    )
    row = _box_row(message)
    assert row["sensor_stamp_ns"] == 7_000_000_011
    assert row["sensor_time_s"] == pytest.approx(7.000000011)
    assert row["sequence"] == 3
    assert row["annotation_sensor_frame_id"] == "bbox_camera"
    assert row["bounding_boxes_3d"][0]["center_xyz_m"] == [0.0, 1.0, -5.0]


def test_segmentation_uses_rgb_int8_panoptic_channels_and_visible_bbox() -> None:
    # 3x2 RGB_INT8 labels map.  Label 42 is channel 2; instance is [1]*256+[0].
    payload = bytes(
        [0, 1, 41, 0, 0, 0, 0, 1, 42, 0, 2, 42, 0, 0, 0, 0, 0, 0]
    )
    message = SimpleNamespace(
        header=_header(), width=3, height=2, step=9, pixel_format=3, data=payload
    )
    observation, returned = _segmentation_observation(message)
    assert returned == payload
    assert observation["payload_sha256"]
    assert observation["visible_pixels"] == 2
    assert observation["instance_ids"] == [256, 512]
    assert observation["visible_bbox_normalized"] == pytest.approx([0.0, 0.0, 1.0, 1.0])


def test_segmentation_rejects_non_rgb8_or_bad_geometry() -> None:
    message = SimpleNamespace(
        header=_header(), width=2, height=1, step=6, pixel_format=7, data=b"\x00" * 6
    )
    with pytest.raises(ValueError, match="RGB_INT8"):
        _segmentation_observation(message)
