"""报告、SDF、PX4 分配和启动边界的离线合同；不启动 SITL/Docker。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from multidetect.autonomy.airframe_profile import AirframeProfile
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.simulator.delta_tailsitter import cad_to_base_m, select_visual_mesh
from scripts import collect_sim_teacher_episodes as collector
from scripts import run_simulator_docker as docker
from scripts import smoke_airframe_flight as smoke
from scripts.check_delta_tailsitter_aero import (
    aerodynamic_force,
    aerodynamic_force_moment,
    pitching_moment_slope,
    surfaces_from_sdf,
    trim_scan,
)
from scripts.generate_delta_tailsitter_model import generate

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "simulation/gazebo/models/delta_tailsitter"
WRAPPER_DIR = ROOT / "simulation/gazebo/models/delta_tailsitter_mono_cam"
GIMBAL_WRAPPER_DIR = ROOT / "simulation/gazebo/models/delta_tailsitter_gimbal_cam"
OVERLAY_DIR = ROOT / "simulation/gazebo/model-overlays/delta_tailsitter_mono_cam_seg"
SCENARIO_DIR = ROOT / "configs/scenarios/airframes"
SCENARIO = SCENARIO_DIR / "delta_tailsitter_mono_cam_001.scenario.json"
AIRFRAME = ROOT / "simulation/px4/airframes/4050_gz_delta_tailsitter"


def _numbers(text):
    return tuple(float(x) for x in text.split())


def _model(directory):
    return ET.parse(directory / "model.sdf").getroot().find("model")


def _parameters():
    return {
        name: float(value)
        for name, value in re.findall(
            r"^param set-default (\S+) ([-+\de.]+)$", AIRFRAME.read_text("utf-8"), re.MULTILINE
        )
    }


def test_cad_transform_is_a_rotation_and_keeps_report_landmarks():
    assert cad_to_base_m((0, 646, -9)) == (0, 0, 0)
    assert cad_to_base_m((0, 258, -55)) == pytest.approx((0.046, 0, 0.388))
    assert cad_to_base_m((600, 629, 150)) == pytest.approx((-0.159, 0.6, 0.017))
    # x_c × y_c = z_c，在变换后仍然成立（禁止镜像 STL）。
    assert cad_to_base_m((1000, 646, -9)) == (0, 1, 0)
    assert cad_to_base_m((0, 1646, -9)) == (0, 0, -1)
    assert cad_to_base_m((0, 646, 991)) == (-1, 0, 0)


def test_complete_model_has_sensors_and_no_upstream_aircraft_dependency():
    model = _model(MODEL_DIR)
    assert model.get("name") == "delta_tailsitter"
    assert [item.findtext("uri") for item in model.findall("include")] == ["model://airspeed"]
    assert {sensor.get("type") for sensor in model.findall("link/sensor")} == {
        "imu",
        "air_pressure",
        "magnetometer",
        "navsat",
    }
    assert not [joint for joint in model.findall("joint") if str(joint.get("name", "")).startswith("c11_")]
    assert model.find("joint[@name='airspeed_joint']/parent").text == "base_link"
    assert len(model.findall("joint[@type='revolute']")) == 4
    assert all(not model.find(f"link[@name='rotor_{i}']/collision") for i in range(4))
    assert not model.findall(".//control_joint_rad_to_cl")
    for mesh in model.findall(".//mesh/uri"):
        assert (MODEL_DIR / mesh.text.removeprefix("model://delta_tailsitter/")).is_file()
    assert "model://quadtailsitter" not in (MODEL_DIR / "model.sdf").read_text("utf-8")


@pytest.mark.parametrize("wrapper_dir", [WRAPPER_DIR, GIMBAL_WRAPPER_DIR])
def test_assembly_mass_cg_and_inertia_match_report_and_are_positive_definite(wrapper_dir):
    links = _model(MODEL_DIR).findall("link") + _model(wrapper_dir).findall("link")
    mass = 0.0
    first_moment = [0.0] * 3
    inertia = [[0.0] * 3 for _ in range(3)]
    for link in links:
        inertial = link.find("inertial")
        m = float(inertial.findtext("mass"))
        position = [0.0] * 3
        if link.get("name") in {"camera_link", "depth_camera_link"}:
            position = [0.046, 0.0, 0.388]
        elif link.find("pose") is not None:
            position = list(_numbers(link.findtext("pose"))[:3])
        center = _numbers(inertial.findtext("pose", "0 0 0 0 0 0"))[:3]
        if not (wrapper_dir == GIMBAL_WRAPPER_DIR and link.get("name") == "camera_link"):
            position = [position[i] + center[i] for i in range(3)]
        matrix = inertial.find("inertia")
        local = [
            [
                float(matrix.findtext(f"i{'xyz'[i]}{'xyz'[j]}", "0")) if i <= j else 0.0
                for j in range(3)
            ]
            for i in range(3)
        ]
        for i in range(3):
            for j in range(i):
                local[i][j] = local[j][i]
        a, b, c = local[0]
        _, d, e = local[1]
        _, _, f = local[2]
        assert a > 0 and a * d - b * b > 0
        assert a * (d * f - e * e) - b * (b * f - c * e) + c * (b * e - c * d) > 0
        assert all(local[i][i] <= sum(local[j][j] for j in range(3) if j != i) for i in range(3))
        mass += m
        for i in range(3):
            first_moment[i] += m * position[i]
            for j in range(3):
                shift = (sum(x * x for x in position) if i == j else 0) - position[i] * position[j]
                inertia[i][j] += local[i][j] + m * shift
    assert mass == pytest.approx(2.929, abs=1e-10)
    assert first_moment == pytest.approx([0, 0, 0], abs=1e-10)
    for i, expected in enumerate((0.431, 0.097, 0.373)):
        for j in range(3):
            assert inertia[i][j] == pytest.approx(expected if i == j else 0, abs=1e-9)


def test_motor_geometry_directions_and_thrust_match_control_allocation():
    model, params = _model(MODEL_DIR), _parameters()
    motors = model.findall("plugin[@name='gz::sim::systems::MulticopterMotorModel']")
    assert len(motors) == params["CA_ROTOR_COUNT"] == 4
    assert params["CA_SV_CS_COUNT"] == 0
    assert params["CA_AIRFRAME"] == 4 and params["VT_TYPE"] == 0
    assert params["FW_PR_P"] == pytest.approx(0.5 * (0.097 / 0.03020833333))
    assert params["FW_PR_I"] == pytest.approx(0.3 * (0.097 / 0.03020833333))
    roll_authority_ratio = (0.373 / 0.08354166667) * (0.05 / 0.025)
    assert params["FW_RR_P"] == pytest.approx(0.2 * roll_authority_ratio)
    assert params["FW_RR_I"] == pytest.approx(0.1 * roll_authority_ratio)
    assert params["FW_RR_FF"] == pytest.approx(0.1 * roll_authority_ratio)
    yaw_authority_ratio = (0.431 / 0.1133333333) * (0.23 / 0.6)
    assert params["FW_YR_P"] == pytest.approx(0.2 * yaw_authority_ratio)
    assert params["VT_FW_DIFTHR_EN"] == 7
    assert params["VT_FW_DIFTHR_S_R"] == params["VT_FW_DIFTHR_S_P"] == params["VT_FW_DIFTHR_S_Y"] == 1
    assert params["FW_THR_TRIM"] == pytest.approx(0.32)
    assert params["FW_T_CLMB_MAX"] == pytest.approx(6.0)
    assert params["FW_AIRSPD_MAX"] == pytest.approx(18.0)
    assert params["FW_R_TC"] == pytest.approx(1.0)
    assert params["FW_PN_R_SLEW_MAX"] == pytest.approx(20.0)
    assert params["NPFG_PERIOD"] == pytest.approx(20.0)
    assert params["NPFG_ROLL_TC"] == pytest.approx(1.0)
    assert "FW_P_LIM_MIN" not in params
    assert "FW_T_SPDWEIGHT" not in params
    assert params["FW_THR_MIN"] == pytest.approx(0.0)
    assert params["VT_ARSP_TRANS"] == pytest.approx(15.0)
    assert params["VT_F_TRANS_THR"] == pytest.approx(0.25)
    assert params["VT_F_TRANS_DUR"] == pytest.approx(5.0)
    assert params["VT_B_TRANS_DUR"] == pytest.approx(10.0)
    assert params["THR_MDL_FAC"] == 1 and params["MPC_THR_HOVER"] == 0.44
    assert sorted(int(plugin.findtext("motorNumber")) for plugin in motors) == [0, 1, 2, 3]
    rotor_geometry = [
        value
        for i in range(4)
        for value in (
            params[f"CA_ROTOR{i}_PX"],
            params[f"CA_ROTOR{i}_PY"],
            params[f"CA_ROTOR{i}_PZ"],
            params[f"CA_ROTOR{i}_KM"],
        )
    ]
    assert rotor_geometry == pytest.approx(
        [
            0.141, 0.6, -0.041, 0.025,
            -0.159, -0.6, -0.041, 0.025,
            0.141, -0.6, -0.041, -0.025,
            -0.159, 0.6, -0.041, -0.025,
        ]
    )
    for motor in motors:
        i = int(motor.findtext("motorNumber"))
        position = _numbers(model.findtext(f"link[@name='rotor_{i}']/pose"))[:3]
        assert (
            params[f"CA_ROTOR{i}_PX"],
            params[f"CA_ROTOR{i}_PY"],
            params[f"CA_ROTOR{i}_PZ"],
        ) == pytest.approx((position[0], -position[1], -position[2]))
        km = params[f"CA_ROTOR{i}_KM"]
        assert motor.findtext("turningDirection") == ("ccw" if km > 0 else "cw")
        assert abs(km) == float(motor.findtext("momentConstant")) == 0.025
        speed = float(motor.findtext("maxRotVelocity"))
        assert params[f"SIM_GZ_EC_FUNC{i + 1}"] == 101 + i
        assert params[f"SIM_GZ_EC_MAX{i + 1}"] == speed == 1100
        assert params[f"SIM_GZ_EC_MIN{i + 1}"] == 0
        assert float(motor.findtext("motorConstant")) * speed**2 == pytest.approx(16.2)
        assert _numbers(model.findtext(f"joint[@name='rotor_{i}_joint']/axis/xyz")) == (0, 0, 1)
    assert 2.929 * 9.80665 / (4 * 16.2) == pytest.approx(0.44, abs=0.004)
    profile = AirframeProfile.from_json(ROOT / "configs/airframes/delta_tailsitter.json")
    assert params["FW_R_LIM"] == profile.max_bank_deg
    for param, value in (
        ("FW_AIRSPD_STALL", profile.stall_airspeed_mps),
        ("FW_AIRSPD_MIN", profile.min_airspeed_mps),
        ("FW_AIRSPD_TRIM", profile.cruise_airspeed_mps),
        ("FW_AIRSPD_MAX", profile.max_airspeed_mps),
        ("VT_ARSP_TRANS", profile.transition.minimum_airspeed_mps),
        ("VT_TRANS_TIMEOUT", profile.transition.timeout_s),
    ):
        assert params[param] == value


def test_four_feet_touch_before_body_and_support_the_cg():
    base = _model(MODEL_DIR).find("link[@name='base_link']")
    feet = [item for item in base.findall("collision") if item.find("geometry/sphere") is not None]
    assert len(base.findall("collision")) == 5 and len(feet) == 4
    points = [_numbers(foot.findtext("pose"))[:3] for foot in feet]
    bottoms = [
        point[2] - float(foot.findtext("geometry/sphere/radius"))
        for point, foot in zip(points, feet, strict=True)
    ]
    assert bottoms == pytest.approx([-0.409] * 4)
    assert min(p[0] for p in points) < 0 < max(p[0] for p in points)
    assert min(p[1] for p in points) < 0 < max(p[1] for p in points)
    body = base.find("collision[@name='body_collision']")
    body_bottom = (
        _numbers(body.findtext("pose"))[2] - _numbers(body.findtext("geometry/box/size"))[2] / 2
    )
    assert body_bottom > max(bottoms)
    for foot in feet:
        assert float(foot.findtext("surface/friction/ode/mu")) >= 0.5


def test_aerodynamic_area_lift_slope_static_margin_and_parasitic_drag():
    surfaces = _model(MODEL_DIR).findall("plugin[@name='gz::sim::systems::LiftDrag']")
    wings = [s for s in surfaces if float(s.findtext("cla")) > 0]
    assert len(wings) == 6
    assert sum(float(s.findtext("area")) for s in wings) == pytest.approx(0.607)
    weights = [
        float(s.findtext("area"))
        * float(s.findtext("cla"))
        * _numbers(s.findtext("upward"))[0] ** 2
        for s in wings
    ]
    assert sum(weights) / 0.607 == pytest.approx(2.82)
    neutral_z = sum(
        w * _numbers(s.findtext("cp"))[2] for w, s in zip(weights, wings, strict=True)
    ) / sum(weights)
    assert -neutral_z / 0.569 == pytest.approx(0.020 / 0.569, abs=1e-10)
    parasitic = [s for s in surfaces if float(s.findtext("cla")) == 0]
    airframe_drag = [s for s in parasitic if float(s.findtext("cda")) == pytest.approx(0.064)]
    propeller_drag = [s for s in parasitic if float(s.findtext("cda")) == pytest.approx(0.216)]
    assert len(airframe_drag) == 2
    assert len(propeller_drag) == 2
    for alpha in (-0.3, -0.1, 0, 0.1, 0.3):
        cd = (
            sum(
                float(s.findtext("area"))
                * abs(float(s.findtext("cda")) * (alpha + float(s.findtext("a0"))))
                for s in airframe_drag
            )
            / 0.607
        )
        assert cd == pytest.approx(0.032)


def test_offline_aero_force_is_trimmed_bounded_and_finite():
    surfaces = surfaces_from_sdf(MODEL_DIR / "model.sdf")
    alpha = math.radians(7.4314663815)
    lift, drag = aerodynamic_force(surfaces, 15.0, alpha)
    force, moment = aerodynamic_force_moment(surfaces, 15.0, alpha)
    assert lift == pytest.approx(2.929 * 9.80665, rel=0.06)
    assert 6.0 <= lift / drag <= 8.0
    assert all(math.isfinite(value) for value in (*force, *moment))
    assert abs(moment[0]) < 1.0 and abs(moment[2]) < 1.0
    assert abs(moment[1]) < 0.1
    # The LiftDrag angle sign is opposite conventional fixed-wing AoA in this
    # MC-FLU basis, so positive dMy/d(alpha_plugin) represents static stability.
    assert pitching_moment_slope(surfaces, 15.0, alpha) > 0.0
    effective_arm_m = pitching_moment_slope(surfaces, 15.0, alpha) / (
        0.5 * 1.15 * 15.0**2 * 0.607 * 2.82
    )
    assert effective_arm_m == pytest.approx(0.020, abs=0.0025)
    assert aerodynamic_force(surfaces, 0.0, 10.0) == (0.0, 0.0)
    stalled_lift, stalled_drag = aerodynamic_force(surfaces, 80.0, math.radians(89.0))
    assert math.isfinite(stalled_lift) and math.isfinite(stalled_drag)
    assert stalled_lift <= 0.5 * 1.15 * 80.0**2 * 0.607 * 0.8 * 1.01


def test_split_wings_produce_damping_sign_roll_moment_and_advance_drag_is_sized():
    surfaces = surfaces_from_sdf(MODEL_DIR / "model.sdf")
    central = [surface for surface in surfaces if surface.name.startswith("central_wing_")]
    assert len(central) == 2
    assert sum(surface.area for surface in central) == pytest.approx(0.399)
    assert sorted(surface.cp[1] for surface in central) == pytest.approx([-0.262, 0.262])
    _, moment = aerodynamic_force_moment(
        surfaces, 15.0, math.radians(7.4314663815), roll_rate_z_rad_s=1.0
    )
    assert moment[2] < 0.0
    assert 1.0 <= abs(moment[2]) <= 30.0

    clean_drag = aerodynamic_force(surfaces, 24.0, math.radians(7.4))[1]
    total_drag = aerodynamic_force(
        surfaces, 24.0, math.radians(7.4), include_advance_drag=True
    )[1]
    assert total_drag - clean_drag == pytest.approx(
        0.5 * 1.15 * 24.0**2 * 0.607 * 0.108, rel=0.02
    )


def test_aero_trim_scan_preserves_cruise_lift_and_static_stability():
    surfaces = surfaces_from_sdf(MODEL_DIR / "model.sdf")
    rows = trim_scan(surfaces)
    assert len(rows) == 4 * 26
    cruise = [row for row in rows if 3.0 <= row["alpha_deg"] <= 12.0]
    assert all(row["pitch_moment_slope_nm_per_rad"] > 0.0 for row in cruise)
    for speed in (12.0, 15.0, 19.0, 25.0):
        row = min(
            (item for item in rows if item["speed_mps"] == speed),
            key=lambda item: abs(item["lift_n"] - 2.929 * 9.80665),
        )
        assert abs(row["lift_n"] - 2.929 * 9.80665) < 5.0
    cruise_trim = aerodynamic_force_moment(
        surfaces, 15.0, math.radians(7.4314663815)
    )[1][1]
    assert abs(cruise_trim) < 0.05


def test_articulated_payload_links_have_solver_safe_mass_and_inertia():
    model = _model(GIMBAL_WRAPPER_DIR)
    for name in ("c11_mount_link", "c11_yaw_link", "c11_roll_link", "camera_link", "depth_camera_link"):
        link = model.find(f"link[@name='{name}']")
        assert link is not None
        assert float(link.findtext("inertial/mass")) >= 0.001
        for field in ("ixx", "iyy", "izz"):
            assert float(link.findtext(f"inertial/inertia/{field}")) >= 1e-8


def test_gimbal_axes_and_imu_use_px4_frame_contract():
    model = _model(GIMBAL_WRAPPER_DIR)
    assert _numbers(model.findtext("joint[@name='c11_yaw_joint']/axis/xyz")) == (0, 0, -1)
    assert _numbers(model.findtext("joint[@name='c11_roll_joint']/axis/xyz")) == (-1, 0, 0)
    assert _numbers(model.findtext("joint[@name='c11_pitch_joint']/axis/xyz")) == (0, 1, 0)
    assert float(model.findtext("joint[@name='c11_pitch_joint']/axis/initial_position")) == 0.0
    assert _numbers(model.findtext("link[@name='camera_link']/sensor[@name='camera_imu']/pose")) == pytest.approx((0, 0, 0, 0, 0, math.pi), abs=1e-9)


@pytest.mark.parametrize("directory", [WRAPPER_DIR, OVERLAY_DIR])
def test_c11_camera_depth_and_projection_matrix_use_the_same_mount(directory):
    model = _model(WRAPPER_DIR)
    wrapper = _model(directory)
    assert wrapper.findtext("include/uri") == "model://delta_tailsitter"
    rgb = model.find("link[@name='camera_link']/sensor[@name='camera']")
    depth = model.find("link[@name='depth_camera_link']/sensor[@name='depth_camera']")
    for sensor in (rgb, depth):
        assert sensor.findtext("camera/image/width") == "1280"
        assert sensor.findtext("camera/image/height") == "960"
        assert float(sensor.findtext("camera/horizontal_fov")) == 1.74
    assert depth.findtext("camera/image/format") == "R_FLOAT32"
    assert model.findtext("joint[@name='DepthCameraJoint']/parent") == "camera_link"
    profile = AirframeProfile.from_json(ROOT / "configs/airframes/delta_tailsitter.json")
    c, s = math.cos(math.radians(-20)), math.sin(math.radians(-20))
    # Gazebo FLU -> PX4 FRD，以及相机光轴/像面右/像面下约定。
    expected = (c, 0, -s, 0, 1, 0, s, 0, c)
    assert profile.camera.body_to_camera_rotation == pytest.approx(expected)
    for mode in ("mc", "fw"):
        assert profile.camera.attitude_rotations[mode] == pytest.approx(expected)
    if directory == OVERLAY_DIR:
        assert wrapper.findtext("link[@name='segmentation_camera_link']/sensor/topic") == (
            "/multidetect/teacher/segmentation"
        )


def test_model_generator_is_reproducible(tmp_path):
    shutil.copytree(MODEL_DIR, tmp_path / "delta_tailsitter")
    generate(tmp_path / "delta_tailsitter")
    assert (tmp_path / "delta_tailsitter/model.sdf").read_bytes() == (MODEL_DIR / "model.sdf").read_bytes()
    for mesh in (tmp_path / "delta_tailsitter/meshes").iterdir():
        assert mesh.read_bytes() == (MODEL_DIR / "meshes" / mesh.name).read_bytes()


def test_cad_mesh_preference_preserves_physics_and_drops_placeholders(tmp_path):
    shutil.copytree(MODEL_DIR, tmp_path / "delta_tailsitter")
    directory = tmp_path / "delta_tailsitter"
    (directory / "meshes" / "visual.stl").unlink()
    assert select_visual_mesh(directory) is None
    for ext in ("stl", "dae"):
        (directory / "meshes" / f"visual.{ext}").write_bytes(b"test mesh selection only")
        text, selected = select_visual_mesh(directory)
        assert selected.suffix == f".{ext}"
        root = ET.fromstring(text).find("model")
        base = root.find("link[@name='base_link']")
        assert base.findtext("visual[@name='cad_visual']/geometry/mesh/uri").endswith(f".{ext}")
        assert len(base.findall("visual")) == 2
        assert len(base.findall("collision")) == 5
        assert len(root.findall("plugin")) == len(_model(MODEL_DIR).findall("plugin"))


def test_fixed_and_gimbal_variants_share_base_mass_calibration_and_airframe_parameters():
    fixed = _model(WRAPPER_DIR)
    gimbal_dir = ROOT / "simulation/gazebo/models/delta_tailsitter_gimbal_cam"
    gimbal = _model(gimbal_dir)
    base = _model(MODEL_DIR)
    assert base.find("link[@name='base_link']/visual[@name='c11_shell_mesh']") is not None
    assert not [joint for joint in base.findall("joint") if "c11_" in str(joint.get("name"))]
    assert fixed.find("joint[@name='c11_pitch_joint']") is None
    assert gimbal.find("joint[@name='c11_pitch_joint']") is not None
    assert all(model.findtext("include/uri") == "model://delta_tailsitter" for model in (fixed, gimbal))
    for wrapper in (fixed, gimbal):
        wrapper_mass = sum(float(link.findtext("inertial/mass", "0")) for link in wrapper.findall("link"))
        base_mass = sum(float(link.findtext("inertial/mass", "0")) for link in base.findall("link"))
        # Camera and gimbal payload mass is carried by the wrapper links and
        # deducted from the shared dynamics body by the generator.
        assert base_mass + wrapper_mass == pytest.approx(2.929, abs=1e-10)
    fixed_profile = AirframeProfile.from_json(ROOT / "configs/airframes/delta_tailsitter.json")
    gimbal_profile = AirframeProfile.from_json(ROOT / "configs/airframes/delta_tailsitter_gimbal.json")
    assert fixed_profile.px4_autostart == 4050 and fixed_profile.camera.mount == "forward"
    assert "gimbal" not in fixed_profile.camera.to_mapping()
    assert gimbal_profile.px4_autostart == 4051 and gimbal_profile.camera.mount == "gimbal"
    assert gimbal_profile.camera.gimbal["axes"] == ["yaw", "roll", "pitch"]
    calibration = json.loads((ROOT / "simulation/gazebo/calibration/gazebo-front-camera-v1.json").read_text())
    calibration_values = calibration["calibration"]
    for directory in (WRAPPER_DIR, gimbal_dir):
        model = _model(directory)
        rgb = model.find("link[@name='camera_link']/sensor[@name='camera']")
        depth = model.find("link[@name='depth_camera_link']/sensor[@name='depth_camera']")
        assert _numbers(rgb.findtext("pose")) == pytest.approx(_numbers(depth.findtext("pose")))
        minimum_offset = 0.09 if directory == WRAPPER_DIR else 0.16
        assert _numbers(rgb.findtext("pose"))[0] >= minimum_offset
        assert _numbers(rgb.findtext("pose"))[5] == pytest.approx(0.0)
        for sensor in (rgb, depth):
            assert sensor.findtext("camera/image/width") == str(calibration_values["width_px"])
            assert sensor.findtext("camera/image/height") == str(calibration_values["height_px"])
            expected_fov = 2 * math.atan(calibration_values["width_px"] / (2 * calibration_values["fx_px"]))
            assert float(sensor.findtext("camera/horizontal_fov")) == pytest.approx(expected_fov, abs=0.005)
    fixed_params = _parameters()
    assert not any(name.startswith("MNT_") for name in fixed_params)
    gimbal_script = (ROOT / "simulation/px4/airframes/4051_gz_delta_tailsitter_gimbal").read_text()
    assert "4050_gz_delta_tailsitter" in gimbal_script
    assert "param set-default MNT_MODE_OUT 2" in gimbal_script


def test_fixed_camera_wrapper_attaches_payload_to_base_link():
    model = _model(WRAPPER_DIR)
    joint = model.find("joint[@name='CameraMountJoint']")
    assert joint is not None and joint.get("type") == "fixed"
    assert joint.findtext("parent") == "base_link"
    assert joint.findtext("child") == "camera_link"


def test_smoke_plan_adds_strict_sustain_only_for_delta_tailsitters():
    for airframe, expected in (
        ("delta_tailsitter", True),
        ("delta_tailsitter_gimbal", True),
        ("advanced_plane", False),
    ):
        profile = AirframeProfile.from_json(ROOT / f"configs/airframes/{airframe}.json")
        plan = smoke.build_plan(airframe, profile, SCENARIO, None)
        loiter = next(step for step in plan["steps"] if step["stage"] == "loiter")
        assert (loiter.get("minimum_fw_duration_s") == 120) is expected
        assert ("fw_metrics_pass" in plan["pass_fields"]) is expected


@pytest.mark.parametrize("stem", ["delta_tailsitter_mono_cam_001", "delta_tailsitter_gimbal_cam_001"])
def test_variant_scenario_manifest_hashes_match_channel_contents(stem):
    scenario_path = SCENARIO_DIR / f"{stem}.scenario.json"
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    for channel in ("runtime_input", "truth", "expectations"):
        entry = scenario["channels"][channel]
        content = json.loads((SCENARIO_DIR / entry["path"]).read_text(encoding="utf-8"))
        assert canonical_sha256(content) == entry["sha256"]


def test_step_assets_split_rotors_and_c11_into_link_local_meshes():
    meshes = MODEL_DIR / "meshes"
    assert all((meshes / f"rotor_{index}.stl").is_file() for index in range(4))
    assert (meshes / "c11.stl").is_file()
    import struct

    counts = []
    for index in range(4):
        data = (meshes / f"rotor_{index}.stl").read_bytes()
        count = struct.unpack("<I", data[80:84])[0]
        counts.append(count)
        assert len(data) == 84 + count * 50
        assert count > 1000
    assert max(counts) / min(counts) < 1.1
    # The static body mesh is intentionally smaller than the previous assembly
    # export because the four propeller parts are no longer baked into it.
    body = (meshes / "visual.stl").read_bytes()
    assert struct.unpack("<I", body[80:84])[0] < 120_000


@pytest.mark.parametrize("airframe", ["delta_tailsitter", "delta_tailsitter_mono_cam", "delta_tailsitter_gimbal_cam"])
def test_smoke_and_ai_collector_resolve_new_airframe(airframe):
    profile = smoke.profile_for(airframe)
    scenario = smoke.default_scenario(airframe)
    assert scenario is not None
    plan = smoke.build_plan(airframe, profile, scenario, None)
    stages = [step["stage"] for step in plan["steps"]]
    assert stages.index("takeoff") < stages.index("transition_fw") < stages.index("land")
    assert next(step for step in plan["steps"] if step["stage"] == "loiter")["airspeed_mps"] == [12, 18]
    assert ("gimbal_check" in stages) == ("gimbal" in airframe)
    runtime = SCENARIO_DIR / ("delta_tailsitter_gimbal_cam_001.runtime.json" if "gimbal" in airframe else "delta_tailsitter_mono_cam_001.runtime.json")
    assert collector._airframe_profile(airframe).id == ("delta_tailsitter_gimbal" if "gimbal" in airframe else "delta_tailsitter")
    vehicle = collector._validate_runtime(runtime, airframe)["vehicle"]
    assert vehicle["model"] == ("delta_tailsitter_gimbal_cam" if "gimbal" in airframe else "delta_tailsitter_mono_cam")


def test_docker_plan_mounts_custom_airframe_readonly_and_records_its_hash(tmp_path, monkeypatch):
    plan = docker.build_docker_simulator_plan(SCENARIO, job_dir=tmp_path / "job", gpu_render=False)
    assert dict(plan.environment)["PX4_SYS_AUTOSTART"] == "4050"
    assert dict(plan.environment)["PX4_SIM_MODEL"] == "gz_delta_tailsitter_mono_cam"
    assert float(dict(plan.environment)["PX4_GZ_MODEL_POSE"].split(",")[2]) == 0.411
    mount = plan.to_mapping()["mounts"]["custom_airframe"]
    assert mount["readonly"] is True
    assert mount["sha256"] == hashlib.sha256(AIRFRAME.read_bytes()).hexdigest()
    assert f"type=bind,source={AIRFRAME},target={mount['container']},readonly" in plan.command
    assert dict(plan.environment)["MULTIDETECT_PX4_LOGGER_MODE"] == "off"
    startup = plan.command[-1]
    assert "rc.logging" not in startup
    assert dict(plan.environment)["SDLOG_BACKEND"] == "0"

    gimbal = docker.build_docker_simulator_plan(
        SCENARIO_DIR / "delta_tailsitter_gimbal_cam_001.scenario.json",
        job_dir=tmp_path / "gimbal",
        gpu_render=False,
    )
    assert dict(gimbal.environment)["MULTIDETECT_PX4_LOGGER_MODE"] == "off"
    assert dict(gimbal.environment)["SDLOG_BACKEND"] == "0"
    assert "rc.logging" not in gimbal.command[-1]

    monkeypatch.setenv("MULTIDETECT_PX4_LOGGER_MODE", "default")
    explicit_default = docker.build_docker_simulator_plan(
        SCENARIO, job_dir=tmp_path / "explicit-default", gpu_render=False
    )
    assert dict(explicit_default.environment)["MULTIDETECT_PX4_LOGGER_MODE"] == "default"
    assert "rc.logging" not in explicit_default.command[-1]


def test_px4_logger_mode_override_and_default_preserve_other_airframes(tmp_path, monkeypatch):
    default = docker.build_docker_simulator_plan(
        ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json",
        job_dir=tmp_path / "default",
        gpu_render=False,
    )
    assert dict(default.environment)["MULTIDETECT_PX4_LOGGER_MODE"] == "default"
    assert "rc.logging" not in default.command[-1]

    monkeypatch.setenv("MULTIDETECT_PX4_LOGGER_MODE", "off")
    disabled = docker.build_docker_simulator_plan(
        SCENARIO, job_dir=tmp_path / "off", gpu_render=False
    )
    assert dict(disabled.environment)["MULTIDETECT_PX4_LOGGER_MODE"] == "off"
    assert dict(disabled.environment)["SDLOG_BACKEND"] == "0"
    assert "rc.logging" not in disabled.command[-1]

    monkeypatch.setenv("MULTIDETECT_PX4_LOGGER_MODE", "invalid")
    with pytest.raises(docker.DockerSimulatorPlanError, match="MULTIDETECT_PX4_LOGGER_MODE"):
        docker.build_docker_simulator_plan(
            SCENARIO, job_dir=tmp_path / "invalid", gpu_render=False
        )


@pytest.mark.parametrize(
    ("airframe", "expected"),
    [
        ("delta_tailsitter", "off"),
        ("delta_tailsitter_mono_cam", "off"),
        ("delta_tailsitter_gimbal", "off"),
        ("delta_tailsitter_gimbal_cam", "off"),
        ("advanced_plane", "default"),
    ],
)
def test_collector_logger_mode_resolves_airframe_aliases(airframe, expected):
    assert collector._default_px4_logger_mode(airframe) == expected


def test_docker_cad_override_is_staged_without_mutating_source(tmp_path, monkeypatch):
    original = (MODEL_DIR / "model.sdf").read_bytes()
    shutil.copytree(MODEL_DIR, tmp_path / "model")
    (tmp_path / "model/meshes/visual.stl").write_bytes(b"test selector only")
    selected = select_visual_mesh(tmp_path / "model")
    monkeypatch.setattr(docker, "select_visual_mesh", lambda directory: selected)
    plan = docker.build_docker_simulator_plan(SCENARIO, job_dir=tmp_path / "job", gpu_render=False)
    assert plan.staged_model_sdf.read_text("utf-8") == selected[0]
    assert plan.to_mapping()["mounts"]["cad_model_sdf"]["readonly"] is True
    assert (MODEL_DIR / "model.sdf").read_bytes() == original


def test_segmentation_collector_keeps_custom_airframe_mount_and_sensor_topics(tmp_path):
    plan = collector.CollectionPlan(
        scenario=str(SCENARIO),
        runtime=str(SCENARIO_DIR / "delta_tailsitter_mono_cam_001.runtime.json"),
        truth=str(SCENARIO_DIR / "delta_tailsitter_mono_cam_001.truth.json"),
        teacher="onboard",
        episodes=1,
        steps_per_episode=1,
        seed=0,
        out=str(tmp_path),
        airframe="delta_tailsitter_mono_cam",
        segmentation=True,
        gpu_render=False,
    )
    command = collector.docker_command(plan)
    mounts = [arg for arg in command if arg.startswith("type=bind,")]
    assert any(f"source={OVERLAY_DIR}" in mount and "readonly" in mount for mount in mounts)
    assert any(f"source={AIRFRAME}" in mount and "readonly" in mount for mount in mounts)
    assert collector._gazebo_model_name(plan, {}) == "delta_tailsitter_mono_cam_0"
    assert collector._onboard_airframe_id(plan) == "delta_tailsitter"


def test_generated_ai_scenarios_can_be_derived_for_this_vtol(tmp_path):
    import json

    from multidetect.autonomy.serialization import canonical_sha256
    from multidetect.simulator.world_gen import derive_vtol_scenario

    derive_vtol_scenario(SCENARIO, tmp_path, airframe="delta_tailsitter")
    runtime = json.loads(
        (tmp_path / "delta_tailsitter_mono_cam_001.runtime_input.json").read_text("utf-8")
    )
    assert runtime["vehicle"]["model"] == "delta_tailsitter_mono_cam"
    assert runtime["vehicle"]["spawn_pose"]["z_up_m"] == 0.411
    manifest = json.loads((tmp_path / SCENARIO.name).read_text("utf-8"))
    assert manifest["channels"]["runtime_input"]["sha256"] == canonical_sha256(runtime)


@pytest.mark.parametrize("model_name", ["delta_tailsitter_mono_cam", "delta_tailsitter_gimbal_cam"])
def test_camera_wrappers_and_overlays_publish_odometry_once(model_name: str) -> None:
    # 帧服务按 /model/<name>/odometry_with_covariance 配帧；缺了它采集器一帧都拿不到。
    root = Path(__file__).resolve().parents[1] / "simulation" / "gazebo"
    for path in (root / "models" / model_name / "model.sdf", root / "model-overlays" / f"{model_name}_seg" / "model.sdf"):
        plugins = ET.parse(path).getroot().findall(".//plugin[@name='gz::sim::systems::OdometryPublisher']")
        assert len(plugins) == 1, path


def test_gimbal_wrapper_publishes_all_three_joint_angles():
    root = Path(__file__).resolve().parents[1] / "simulation" / "gazebo"
    for path in (
        root / "models/delta_tailsitter_gimbal_cam/model.sdf",
        root / "model-overlays/delta_tailsitter_gimbal_cam_seg/model.sdf",
    ):
        plugins = ET.parse(path).getroot().findall(
            ".//plugin[@name='gz::sim::systems::JointStatePublisher']"
        )
        assert len(plugins) == 1, path
        assert {item.text for item in plugins[0].findall("joint_name")} == {
            "c11_yaw_joint",
            "c11_roll_joint",
            "c11_pitch_joint",
        }
