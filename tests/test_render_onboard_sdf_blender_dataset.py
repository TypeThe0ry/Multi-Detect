from __future__ import annotations

import json
import math
import shutil
import struct
import sys
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from types import SimpleNamespace
from types import SimpleNamespace

import pytest

from scripts import render_onboard_sdf_blender_dataset as renderer

ROOT = Path(__file__).resolve().parents[1]


def test_repository_worlds_load_actual_3d_target() -> None:
    worlds = [renderer.load_world(ROOT, relative) for relative in renderer.WORLD_PATHS.values()]
    assert len({dict(world.sources)[world.relative_path] for world in worlds}) == 3
    for world in worlds:
        assert {primitive.geometry for primitive in world.primitives} == {
            "box",
            "cylinder",
            "sphere",
            "plane",
        }
        targets = [primitive for primitive in world.primitives if primitive.target]
        assert len(targets) == 11
        assert sum(not primitive.target for primitive in world.primitives) > 10
        assert dict(world.sources)[renderer.TARGET_PATH] == renderer.sha256(
            ROOT / renderer.TARGET_PATH
        )
        assert 170 < world.target_center[0] < 220
        assert world.target_center[2] > 1


def test_model_link_visual_pose_composition_is_rigid_not_translation_sum() -> None:
    model = ET.fromstring("""<model name="sample"><pose>10 20 30 0 0 1.5707963267948966</pose>
      <link name="a"><pose>2 0 3 0 1.5707963267948966 0</pose>
        <visual name="b"><pose>1 0 0 0 0 0</pose>
          <geometry><box><size>2 4 6</size></box></geometry>
        </visual>
      </link></model>""")
    primitive = renderer._model_primitives(model, renderer.IDENTITY, True, "sample")[0]
    assert renderer.transform_point(primitive.matrix, (0, 0, 0)) == pytest.approx((10, 22, 32))
    assert renderer.transform_point(primitive.matrix, (0, 0, 1)) == pytest.approx((10, 23, 32))


@pytest.mark.parametrize(
    "pose",
    [
        '<pose relative_to="another">0 0 0 0 0 0</pose>',
        '<pose rotation_format="quat_xyzw">0 0 0 0 0 0 1</pose>',
        '<pose degrees="true">0 0 0 0 0 90</pose>',
        "<pose>nan 0 0 0 0 0</pose>",
        "<pose>0 0 0</pose>",
    ],
)
def test_unsupported_or_invalid_pose_rejected(pose: str) -> None:
    with pytest.raises(renderer.SdfRenderError):
        renderer.pose_matrix(ET.fromstring(f"<link>{pose}</link>"))


@pytest.mark.parametrize(
    "geometry",
    [
        "<mesh><uri>some.obj</uri></mesh>",
        "<box><size>1 -2 3</size></box>",
        "<plane><size>2 3</size><normal>0 0 0</normal></plane>",
        "<sphere><radius>inf</radius></sphere>",
        "<box><size>1 2 3</size></box><sphere><radius>1</radius></sphere>",
    ],
)
def test_unsupported_geometry_rejected(geometry: str) -> None:
    with pytest.raises(renderer.SdfRenderError):
        renderer._geometry(ET.fromstring(f"<visual><geometry>{geometry}</geometry></visual>"))


def test_plane_normal_orientation() -> None:
    _, dimensions, orientation = renderer._geometry(
        ET.fromstring(
            "<visual><geometry><plane><size>4 5</size><normal>0 1 0</normal>"
            "</plane></geometry></visual>"
        )
    )
    assert dimensions == (4, 5)
    assert renderer.transform_point(orientation, (0, 0, 1)) == pytest.approx((0, 1, 0))


def test_camera_sampling_is_seeded_independently_by_world_and_split() -> None:
    world = renderer.load_world(ROOT, renderer.WORLD_PATHS["train"])
    samples = renderer.camera_samples(world, "train", 11, 12)
    assert samples == renderer.camera_samples(world, "train", 11, 12)
    assert samples != renderer.camera_samples(world, "val", 11, 12)
    assert samples != renderer.camera_samples(world, "train", 12, 12)
    assert len({sample["azimuth_rad"] for sample in samples}) == 12
    for sample in samples:
        assert sample["target_center"] == list(world.target_center)
        assert abs(sample["look_at_horizontal_jitter_m"]) <= 0.3 * sample["horizontal_distance_m"]
        assert abs(sample["look_at_vertical_jitter_m"]) <= 0.15 * sample["horizontal_distance_m"]
        assert 7 <= sample["horizontal_distance_m"] <= 38
        assert 3 <= sample["height_above_target_center_m"] <= 18
        assert sample["horizontal_fov_rad"] == pytest.approx(math.pi / 3)
    assert samples[0]["look_at"] == list(world.target_center)
    assert samples[0]["target_present"] is True
    assert all(sample["look_at"] != list(world.target_center) for sample in samples[1:])
    assert [sample["index"] for sample in samples if not sample["target_present"]] == [4, 9]


def test_recorded_composed_pose_is_replayed_exactly_in_blender_world() -> None:
    world = renderer.load_world(ROOT, renderer.WORLD_PATHS["train"])
    pose = {
        "center_ned": [10.0, 20.0, -3.0],
        # A 90-degree yaw in the optical-to-NED matrix.
        "optical_to_world": [0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0],
    }
    samples = renderer.camera_samples(world, "train", 11, 2, camera_pose=pose)
    assert samples[0]["location"] == [20.0, 10.0, 3.0]
    assert samples[1]["location"] == [20.0, 10.0, 3.0]
    expected = [
        [0.0, 0.0, -1.0, 20.0],
        [-1.0, 0.0, 0.0, 10.0],
        [0.0, 1.0, 0.0, 3.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    for actual_row, expected_row in zip(samples[0]["camera_world_matrix"], expected):
        assert actual_row == pytest.approx(expected_row)
    assert samples[0]["camera_pose_source"] == "recorded_composed_pose"


def test_vehicle_pose_records_gimbal_angles_in_pitch_yaw_roll_order() -> None:
    from multidetect.camera_mount import CameraMount

    world = renderer.load_world(ROOT, renderer.WORLD_PATHS["train"])
    mount = CameraMount.gimbal(pitch_rad=0.1, yaw_rad=0.2, roll_rad=0.3)
    samples = renderer.camera_samples(
        world,
        "train",
        11,
        1,
        vehicle_pose={
            "position_ned": (1.0, 2.0, -5.0),
            "attitude_rad": {"roll": 0.0, "pitch": 0.0, "yaw": 0.0},
        },
        camera_mount=mount,
    )
    assert samples[0]["gimbal_angle_rad"] == [0.1, 0.2, 0.3]


def test_look_at_render_composes_tracking_with_gimbal(monkeypatch, tmp_path: Path) -> None:
    euler_calls = []

    class Quaternion:
        def __init__(self, label: str) -> None:
            self.label = label

        def __matmul__(self, other):
            return Quaternion(f"{self.label}@{other.label}")

        def to_euler(self):
            return (self.label,)

    class Direction:
        def to_track_quat(self, track, up):
            assert (track, up) == ("-Z", "Y")
            return Quaternion("tracking")

    class Location:
        def __sub__(self, other):
            return Direction()

    class Euler:
        def __init__(self, angles, order):
            self.angles = angles
            self.order = order
            euler_calls.append((angles, order))

        def to_quaternion(self):
            assert self.order == "XYZ"
            return Quaternion("gimbal")

    class Pixels:
        def foreach_get(self, target):
            pass

    image = SimpleNamespace(size=(1, 1), pixels=Pixels())
    background = SimpleNamespace(inputs={"Color": SimpleNamespace(default_value=(0, 0, 0, 1))})
    camera_object = SimpleNamespace(location=None, matrix_world=[[1.0] * 4 for _ in range(4)])
    scene = SimpleNamespace(
        camera=camera_object,
        render=SimpleNamespace(image_settings=SimpleNamespace(color_mode=None), filepath=""),
        world=SimpleNamespace(node_tree=SimpleNamespace(nodes=SimpleNamespace(get=lambda name: background))),
    )
    bpy = SimpleNamespace(
        ops=SimpleNamespace(render=SimpleNamespace(render=lambda **kwargs: None)),
        data=SimpleNamespace(
            images=SimpleNamespace(load=lambda *args, **kwargs: image, remove=lambda value: None),
            materials=SimpleNamespace(remove=lambda value: None),
        ),
    )
    mathutils = SimpleNamespace(
        Vector=lambda value: Location(),
        Euler=Euler,
    )
    monkeypatch.setattr(renderer, "apply_propeller_state", lambda objects, camera: True)
    monkeypatch.setattr(renderer, "_material", lambda *args, **kwargs: object())

    camera = {
        "location": [1.0, 2.0, 3.0],
        "look_at": [4.0, 5.0, 6.0],
        "gimbal_angle_rad": [0.1, 0.2, 0.3],
        "camera_world_matrix": None,
        "target_present": True,
    }
    renderer._render_frame(bpy, mathutils, scene, [], camera, tmp_path / "rgb.png", tmp_path / "mask.png", 1, 1)
    assert camera_object.rotation_euler == ("tracking@gimbal",)
    assert euler_calls == [((0.1, 0.2, 0.3), "XYZ")]


def test_composed_pose_orientation_changes_with_vehicle_body_for_fixed_mount() -> None:
    from multidetect.camera_mount import CameraMount, compose_camera_pose

    mount = CameraMount.fixed(pitch_rad=0.2)
    first = compose_camera_pose(
        (0.0, 0.0, -5.0), vehicle_roll_rad=0.0, vehicle_pitch_rad=0.0,
        vehicle_yaw_rad=0.0, mount=mount,
    )
    turned = compose_camera_pose(
        (0.0, 0.0, -5.0), vehicle_roll_rad=0.0, vehicle_pitch_rad=0.0,
        vehicle_yaw_rad=math.pi / 2.0, mount=mount,
    )
    world = renderer.load_world(ROOT, renderer.WORLD_PATHS["train"])
    first_sample = renderer.camera_samples(
        world, "train", 11, 1,
        camera_pose={"center_ned": first.center_ned, "optical_to_world": first.optical_to_world},
    )[0]
    turned_sample = renderer.camera_samples(
        world, "train", 11, 1,
        camera_pose={"center_ned": turned.center_ned, "optical_to_world": turned.optical_to_world},
    )[0]
    assert first_sample["camera_world_matrix"] != turned_sample["camera_world_matrix"]


@pytest.mark.parametrize(
    ("stabilization", "changes_with_yaw"),
    [("lock-horizon", False), ("follow-yaw", True)],
)
def test_gimbal_stabilization_changes_composed_world_orientation(
    stabilization: str, changes_with_yaw: bool
) -> None:
    from multidetect.camera_mount import CameraMount, compose_camera_pose

    mount = CameraMount.gimbal(stabilization=stabilization)
    kwargs = {"vehicle_roll_rad": 0.0, "vehicle_pitch_rad": 0.0, "mount": mount}
    initial = compose_camera_pose((0.0, 0.0, -5.0), vehicle_yaw_rad=0.0, **kwargs)
    rotated = compose_camera_pose((0.0, 0.0, -5.0), vehicle_yaw_rad=math.pi / 2.0, **kwargs)
    assert (initial.optical_to_world != rotated.optical_to_world) is changes_with_yaw


def test_rendered_mask_row_order_and_occluded_holes_define_bbox() -> None:
    # Blender rows begin at the bottom. Only actual white pixels count; a
    # projected target can be larger, but no projection enters this function.
    pixels = []
    for row in ((0, 0, 0, 0), (0, 1, 0, 1), (0, 0, 0.49, 0)):
        for value in row:
            pixels.extend((value, value, value, 1))
    mask = renderer.mask_from_rgba(pixels, 4, 3)
    assert mask == bytes((0, 0, 0, 0, 0, 255, 0, 255, 0, 0, 0, 0))
    assert renderer.visible_bbox(mask, 4, 3) == (1, 1, 4, 2)
    assert renderer.yolo_label(renderer.visible_bbox(mask, 4, 3), 4, 3) == (
        "0 0.625000000 0.500000000 0.750000000 0.333333333\n"
    )


def test_fully_occluded_target_is_negative_not_projected_positive() -> None:
    mask = bytes(20)
    assert renderer.visible_bbox(mask, 5, 4) is None
    assert renderer.yolo_label(None, 5, 4) == ""


def test_mask_png_is_lossless_top_down_and_binary() -> None:
    mask = bytes((0, 255, 255, 0))
    png = renderer._mask_png(mask, 2, 2)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    offset, compressed = 8, b""
    while offset < len(png):
        length = struct.unpack(">I", png[offset : offset + 4])[0]
        kind, payload = png[offset + 4 : offset + 8], png[offset + 8 : offset + 8 + length]
        assert struct.unpack(">I", png[offset + 8 + length : offset + 12 + length])[
            0
        ] == zlib.crc32(kind + payload)
        if kind == b"IDAT":
            compressed += payload
        offset += length + 12
    assert zlib.decompress(compressed) == b"\0\0\xff\0\xff\0"


def test_output_cannot_be_overwritten_without_importing_bpy(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    original = existing / "user.txt"
    original.write_text("preserve")
    args = renderer.parse_arguments(["--output-dir", str(existing), "--samples-per-split", "1"])
    with pytest.raises(renderer.SdfRenderError, match="refusing overwrite"):
        renderer.render_dataset(args)
    assert original.read_text() == "preserve"


@pytest.mark.parametrize(
    "extra",
    [
        ["--samples-per-split", "0"],
        ["--width", "0"],
        ["--height", "99999"],
        ["--seed", "-1"],
    ],
)
def test_invalid_cli_counts_and_dimensions_rejected(extra: list[str]) -> None:
    with pytest.raises(SystemExit):
        renderer.parse_arguments(["--output-dir", "unused", *extra])


def test_smoke_and_default_counts() -> None:
    args = renderer.parse_arguments(["--output-dir", "unused"])
    assert (args.train_count, args.val_count, args.test_count) == (36, 12, 12)
    args = renderer.parse_arguments(["--output-dir", "unused", "--samples-per-split", "1"])
    assert (args.train_count, args.val_count, args.test_count) == (1, 1, 1)


def test_unknown_model_include_is_rejected(tmp_path: Path) -> None:
    world = tmp_path / "world.sdf"
    world.write_text("<sdf><world><include><uri>model://remote_asset</uri></include></world></sdf>")
    with pytest.raises(renderer.SdfRenderError, match="neutral-target include"):
        renderer.load_world(tmp_path, "world.sdf")


def test_source_path_cannot_escape_repo(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "outside.sdf").write_text("<sdf/>")
    with pytest.raises(renderer.SdfRenderError, match="escapes repository"):
        renderer._source(root, "../outside.sdf")


def _mock_render_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract-only fixture: explicitly not Blender render acceptance."""
    monkeypatch.setitem(
        sys.modules,
        "bpy",
        SimpleNamespace(
            app=SimpleNamespace(version=(4, 5, 3), version_string="4.5.3-test-fixture")
        ),
    )
    monkeypatch.setitem(sys.modules, "mathutils", SimpleNamespace())
    monkeypatch.setattr(renderer, "_build_blender_scene", lambda *_: (None, []))

    def render_fixture(_bpy, _mathutils, _scene, _objects, _camera, image, raw_mask, width, height):
        mask = bytes([255]) + bytes(width * height - 1)
        png = renderer._mask_png(mask, width, height)
        image.write_bytes(png)
        raw_mask.write_bytes(png)
        return mask, [list(row) for row in renderer.IDENTITY]

    monkeypatch.setattr(renderer, "_render_frame", render_fixture)


def test_export_manifest_contract_hashes_and_claim_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_render_io(monkeypatch)
    output = tmp_path / "new-dataset"
    args = renderer.parse_arguments(
        [
            "--output-dir",
            str(output),
            "--samples-per-split",
            "1",
            "--width",
            "16",
            "--height",
            "16",
        ]
    )
    receipt = renderer.render_dataset(args)
    assert receipt["contract"] == "multidetect.onboard-sdf-blender-visual-dataset-v1"
    assert receipt["names"] == ["search_target"]
    assert receipt["frame_count"] == 3
    assert receipt["dataset_yaml"] == "dataset.yaml"
    assert renderer.sha256(output / "dataset.yaml") == receipt["dataset_yaml_sha256"]
    assert json.loads((output / "manifest.json").read_text()) == json.loads(json.dumps(receipt))
    for claim in (
        "training_ready",
        "gazebo_capture",
        "flight",
        "promotion",
        "final_release_allowed",
    ):
        assert receipt[claim] is False
    assert receipt["split_unit"] == "source_world"
    for frame in receipt["frames"]:
        assert frame["world_path"] == renderer.WORLD_PATHS[frame["split"]]
        assert frame["world_sha256"] == renderer.sha256(ROOT / frame["world_path"])
        assert renderer.TARGET_PATH in frame["source_hashes"]
        for kind in ("image", "label", "mask", "mask_render"):
            path = output / frame[f"{kind}_path"]
            assert renderer.sha256(path) == frame[f"{kind}_sha256"]
        assert frame["visible_bbox_xyxy_px"] == (0, 0, 1, 1)
        assert frame["visible_target_pixels"] == 1
    for split in renderer.WORLD_PATHS:
        assert len(list((output / "images" / split).iterdir())) == 1
        assert len(list((output / "labels" / split).iterdir())) == 1


def test_changed_source_does_not_seal_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_render_io(monkeypatch)
    copied_root = tmp_path / "repo"
    for relative in [*renderer.WORLD_PATHS.values(), renderer.TARGET_PATH]:
        destination = copied_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    fixture_render = renderer._render_frame

    def mutate_fixture_source(*arguments):
        result = fixture_render(*arguments)
        source = copied_root / renderer.TARGET_PATH
        source.write_bytes(source.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(renderer, "_render_frame", mutate_fixture_source)
    output = tmp_path / "incomplete-dataset"
    args = renderer.parse_arguments(
        [
            "--repo-root",
            str(copied_root),
            "--output-dir",
            str(output),
            "--samples-per-split",
            "1",
            "--width",
            "16",
            "--height",
            "16",
        ]
    )
    with pytest.raises(renderer.SdfRenderError, match="source changed"):
        renderer.render_dataset(args)
    assert not (output / "manifest.json").exists()
    assert not (output / "dataset.yaml").exists()
