"""统一的多旋翼、固定翼和 VTOL 机型能力合同。

这个模块只描述能力和保守限制，不连接 MAVLink、PX4 或传感器。所有派生
计算都是纯函数，档案可以安全地在地面站、规划器和机载进程之间往返传输。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any


class AirframeProfileError(ValueError):
    """档案不符合能力合同。"""


_CATEGORIES = frozenset({"multirotor", "fixed_wing", "vtol"})
_SUBTYPES = frozenset(
    {"quad", "quadplane", "tailsitter", "tiltrotor", "conventional", "flying_wing", "other"}
)
_TAKEOFF_METHODS = frozenset({"hand", "runway", "catapult", "vtol", "multirotor"})
_LANDING_METHODS = frozenset({"runway", "belly", "parachute", "vtol", "multirotor"})
_MOUNTS = frozenset({"forward", "downward", "gimbal"})
_CAMERA_MOUNT_TYPES = frozenset({"fixed", "gimbal"})
_CAMERA_AXES = frozenset({"pitch", "yaw", "roll"})
_KNOWN_PX4_PARAMETERS = frozenset(
    {
        "FW_AIRSPD_MIN",
        "FW_AIRSPD_MAX",
        "FW_AIRSPD_STALL",
        "FW_AIRSPD_TRIM",
        "FW_R_LIM",
        "FW_T_CLMB_MAX",
        "FW_T_SINK_MAX",
        "FW_T_SINK_MIN",
        "FW_TKO_AIRSPD",
        "FW_LAUN_AC_THLD",
        "FW_LAUN_AC_T",
        "FW_LND_ABORT",
        "FW_LND_USETER",
        "MIS_LND_ABRT_ALT",
        "RWTO_ROT_AIRSPD",
        "RWTO_TKOFF",
        "NAV_LOITER_RAD",
        "VT_ARSP_TRANS",
        "VT_F_TRANS_DUR",
        "VT_TRANS_TIMEOUT",
        "VT_B_TRANS_DUR",
        "VT_TRANS_MIN_TM",
        "VT_TYPE",
        "VT_TILT_TRANS",
        "VT_QC_ALT_LOSS",
        "VT_QC_T_ALT_LOSS",
        "RTL_RETURN_ALT",
        "RTL_DESCEND_ALT",
        "MIS_TAKEOFF_ALT",
        "NAV_ACC_RAD",
        "CA_AIRFRAME",
        "CA_ROTOR_COUNT",
    }
)
_G = 9.80665


def _number(
    value: Any, name: str, *, minimum: float | None = None, positive: bool = False
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise AirframeProfileError(f"{name} 必须是有限数字")
    result = float(value)
    if positive and result <= 0:
        raise AirframeProfileError(f"{name} 必须大于 0")
    if minimum is not None and result < minimum:
        raise AirframeProfileError(f"{name} 不能小于 {minimum}")
    return result


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AirframeProfileError(f"{name} 必须是非空字符串")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise AirframeProfileError(f"{name} 必须是布尔值")
    return value


def _tuple_strings(value: Any, name: str, allowed: frozenset[str]) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise AirframeProfileError(f"{name} 必须是非空字符串数组")
    result = tuple(_text(item, f"{name}[]") for item in value)
    if any(item not in allowed for item in result):
        raise AirframeProfileError(f"{name} 含有不支持的值")
    if len(set(result)) != len(result):
        raise AirframeProfileError(f"{name} 不得重复")
    return result


def _strict_mapping(raw: Any, name: str, required: set[str], optional: set[str]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AirframeProfileError(f"{name} 必须是对象")
    keys = set(raw)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise AirframeProfileError(f"{name} 缺少字段: {', '.join(sorted(missing))}")
    if unknown:
        raise AirframeProfileError(f"{name} 含有未知字段: {', '.join(sorted(unknown))}")
    return dict(raw)


def _rotation(value: Any, name: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != 9:
        raise AirframeProfileError(f"{name} 必须是长度为 9 的行优先旋转矩阵")
    result = tuple(_number(item, f"{name}[]") for item in value)
    # 允许小量数值误差，但拒绝明显不是旋转矩阵的输入。
    rows = (result[0:3], result[3:6], result[6:9])
    for row in rows:
        if abs(math.sqrt(sum(x * x for x in row)) - 1.0) > 1e-3:
            raise AirframeProfileError(f"{name} 每一行必须是单位向量")
    for i in range(3):
        for j in range(i + 1, 3):
            if abs(sum(rows[i][k] * rows[j][k] for k in range(3))) > 1e-3:
                raise AirframeProfileError(f"{name} 必须正交")
    determinant = (
        rows[0][0] * (rows[1][1] * rows[2][2] - rows[1][2] * rows[2][1])
        - rows[0][1] * (rows[1][0] * rows[2][2] - rows[1][2] * rows[2][0])
        + rows[0][2] * (rows[1][0] * rows[2][1] - rows[1][1] * rows[2][0])
    )
    if abs(determinant - 1.0) > 1e-3:
        raise AirframeProfileError(f"{name} 的行列式必须为 +1")
    return result


@dataclass(frozen=True, slots=True)
class ParameterRef:
    """一个能力参数与 PX4 参数名的可追溯关联。"""

    value: float
    px4_name: str | None = None
    unverified: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _number(self.value, "parameter.value"))
        if self.px4_name is not None:
            _text(self.px4_name, "parameter.px4_name")
            if self.px4_name not in _KNOWN_PX4_PARAMETERS and not self.unverified:
                raise AirframeProfileError(
                    f"未知 PX4 参数必须标记 unverified=true: {self.px4_name}"
                )
        elif not self.unverified:
            raise AirframeProfileError("没有 PX4 参数名的参数必须标记 unverified=true")
        _bool(self.unverified, "parameter.unverified")

    @classmethod
    def from_mapping(cls, raw: Any, name: str = "parameter") -> ParameterRef:
        value = _strict_mapping(raw, name, {"value", "unverified"}, {"px4_name"})
        return cls(
            value=value["value"], px4_name=value.get("px4_name"), unverified=value["unverified"]
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {"value": self.value, "unverified": self.unverified}
        if self.px4_name is not None:
            result["px4_name"] = self.px4_name
        return result


@dataclass(frozen=True, slots=True)
class LaunchDetection:
    accel_threshold_mps2: float
    duration_s: float
    accel_parameter: ParameterRef
    duration_parameter: ParameterRef

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "accel_threshold_mps2",
            _number(
                self.accel_threshold_mps2, "launch_detection.accel_threshold_mps2", positive=True
            ),
        )
        object.__setattr__(
            self, "duration_s", _number(self.duration_s, "launch_detection.duration_s", minimum=0.0)
        )
        if self.accel_parameter.px4_name != "FW_LAUN_AC_THLD":
            raise AirframeProfileError("发射加速度阈值必须引用 FW_LAUN_AC_THLD")
        if self.duration_parameter.px4_name != "FW_LAUN_AC_T":
            raise AirframeProfileError("发射持续时间必须引用 FW_LAUN_AC_T")

    @classmethod
    def from_mapping(cls, raw: Any) -> LaunchDetection:
        value = _strict_mapping(
            raw,
            "launch_detection",
            {"accel_threshold_mps2", "duration_s"},
            {"accel_parameter", "duration_parameter", "px4_parameters"},
        )
        refs = value.get("px4_parameters", {})
        if refs:
            refs = _strict_mapping(
                refs, "launch_detection.px4_parameters", {"accel", "duration"}, set()
            )
            accel_ref = ParameterRef.from_mapping(
                refs["accel"], "launch_detection.px4_parameters.accel"
            )
            duration_ref = ParameterRef.from_mapping(
                refs["duration"], "launch_detection.px4_parameters.duration"
            )
        else:
            accel_ref = ParameterRef.from_mapping(
                value["accel_parameter"], "launch_detection.accel_parameter"
            )
            duration_ref = ParameterRef.from_mapping(
                value["duration_parameter"], "launch_detection.duration_parameter"
            )
        return cls(value["accel_threshold_mps2"], value["duration_s"], accel_ref, duration_ref)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "accel_threshold_mps2": self.accel_threshold_mps2,
            "duration_s": self.duration_s,
            "px4_parameters": {
                "accel": self.accel_parameter.to_mapping(),
                "duration": self.duration_parameter.to_mapping(),
            },
        }


@dataclass(frozen=True, slots=True)
class TakeoffProfile:
    methods: tuple[str, ...]
    launch_detection: LaunchDetection | None = None
    rotation_airspeed_mps: float | None = None
    rotation_parameter: ParameterRef | None = None
    parameters: Mapping[str, ParameterRef] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "methods", _tuple_strings(self.methods, "takeoff.methods", _TAKEOFF_METHODS)
        )
        if (
            any(method in {"hand", "catapult"} for method in self.methods)
            and self.launch_detection is None
        ):
            raise AirframeProfileError("手抛或弹射起飞必须提供 launch_detection")
        if "runway" in self.methods:
            if self.rotation_airspeed_mps is None or self.rotation_parameter is None:
                raise AirframeProfileError(
                    "滑跑起飞必须提供 rotation_airspeed_mps 和 rotation_parameter"
                )
        if self.rotation_airspeed_mps is not None:
            object.__setattr__(
                self,
                "rotation_airspeed_mps",
                _number(self.rotation_airspeed_mps, "takeoff.rotation_airspeed_mps", positive=True),
            )
        if (
            self.rotation_parameter is not None
            and self.rotation_parameter.px4_name != "RWTO_ROT_AIRSPD"
        ):
            raise AirframeProfileError("rotation_parameter 必须引用 RWTO_ROT_AIRSPD")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    @classmethod
    def from_mapping(cls, raw: Any) -> TakeoffProfile:
        value = _strict_mapping(
            raw,
            "takeoff",
            {"methods"},
            {"launch_detection", "rotation_airspeed_mps", "rotation_parameter", "parameters"},
        )
        params = {
            str(k): ParameterRef.from_mapping(v, f"takeoff.parameters.{k}")
            for k, v in dict(value.get("parameters", {})).items()
        }
        return cls(
            tuple(value["methods"]),
            LaunchDetection.from_mapping(value["launch_detection"])
            if value.get("launch_detection") is not None
            else None,
            value.get("rotation_airspeed_mps"),
            ParameterRef.from_mapping(value["rotation_parameter"], "takeoff.rotation_parameter")
            if value.get("rotation_parameter") is not None
            else None,
            params,
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {"methods": list(self.methods)}
        if self.launch_detection is not None:
            result["launch_detection"] = self.launch_detection.to_mapping()
        if self.rotation_airspeed_mps is not None:
            result["rotation_airspeed_mps"] = self.rotation_airspeed_mps
        if self.rotation_parameter is not None:
            result["rotation_parameter"] = self.rotation_parameter.to_mapping()
        if self.parameters:
            result["parameters"] = {k: v.to_mapping() for k, v in self.parameters.items()}
        return result


@dataclass(frozen=True, slots=True)
class LandingProfile:
    methods: tuple[str, ...]
    parameters: Mapping[str, ParameterRef] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "methods", _tuple_strings(self.methods, "landing.methods", _LANDING_METHODS)
        )
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    @classmethod
    def from_mapping(cls, raw: Any) -> LandingProfile:
        value = _strict_mapping(raw, "landing", {"methods"}, {"parameters"})
        params = {
            str(k): ParameterRef.from_mapping(v, f"landing.parameters.{k}")
            for k, v in dict(value.get("parameters", {})).items()
        }
        return cls(tuple(value["methods"]), params)

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {"methods": list(self.methods)}
        if self.parameters:
            result["parameters"] = {k: v.to_mapping() for k, v in self.parameters.items()}
        return result


@dataclass(frozen=True, slots=True)
class TransitionProfile:
    mc_to_fw_param1: float = 4.0
    fw_to_mc_param1: float = 3.0
    minimum_height_m: float = 10.0
    minimum_airspeed_mps: float = 10.0
    timeout_s: float = 15.0
    parameters: Mapping[str, ParameterRef] = MappingProxyType({})

    def __post_init__(self) -> None:
        if self.mc_to_fw_param1 != 4.0 or self.fw_to_mc_param1 != 3.0:
            raise AirframeProfileError("VTOL 转换命令必须使用 MC=3、FW=4")
        for name in ("minimum_height_m", "minimum_airspeed_mps", "timeout_s"):
            object.__setattr__(
                self,
                name,
                _number(
                    getattr(self, name),
                    f"transition.{name}",
                    minimum=0.0,
                    positive=name == "timeout_s",
                ),
            )
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    @classmethod
    def from_mapping(cls, raw: Any) -> TransitionProfile:
        value = _strict_mapping(
            raw,
            "transition",
            set(),
            {
                "mc_to_fw_param1",
                "fw_to_mc_param1",
                "minimum_height_m",
                "minimum_airspeed_mps",
                "timeout_s",
                "parameters",
            },
        )
        params = {
            str(k): ParameterRef.from_mapping(v, f"transition.parameters.{k}")
            for k, v in dict(value.get("parameters", {})).items()
        }
        return cls(
            value.get("mc_to_fw_param1", 4.0),
            value.get("fw_to_mc_param1", 3.0),
            value.get("minimum_height_m", 10.0),
            value.get("minimum_airspeed_mps", 10.0),
            value.get("timeout_s", 15.0),
            params,
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "mc_to_fw_param1": self.mc_to_fw_param1,
            "fw_to_mc_param1": self.fw_to_mc_param1,
            "minimum_height_m": self.minimum_height_m,
            "minimum_airspeed_mps": self.minimum_airspeed_mps,
            "timeout_s": self.timeout_s,
        }
        if self.parameters:
            result["parameters"] = {k: v.to_mapping() for k, v in self.parameters.items()}
        return result


@dataclass(frozen=True, slots=True)
class CameraProfile:
    mount: str
    body_to_camera_rotation: tuple[float, ...]
    attitude_rotations: Mapping[str, tuple[float, ...]] = MappingProxyType({})

    def __post_init__(self) -> None:
        if self.mount not in _MOUNTS:
            raise AirframeProfileError("camera.mount 不受支持")
        object.__setattr__(
            self,
            "body_to_camera_rotation",
            _rotation(self.body_to_camera_rotation, "camera.body_to_camera_rotation"),
        )
        rotations = {
            str(k): _rotation(v, f"camera.attitude_rotations.{k}")
            for k, v in dict(self.attitude_rotations).items()
        }
        object.__setattr__(self, "attitude_rotations", MappingProxyType(rotations))

    @classmethod
    def from_mapping(cls, raw: Any) -> CameraProfile:
        value = _strict_mapping(
            raw,
            "camera",
            {"mount", "body_to_camera_rotation"},
            {"attitude_rotations", "body_to_camera_by_mode"},
        )
        rotations = value.get("attitude_rotations", value.get("body_to_camera_by_mode", {}))
        if not isinstance(rotations, Mapping):
            raise AirframeProfileError("camera.attitude_rotations 必须是对象")
        return cls(value["mount"], tuple(value["body_to_camera_rotation"]), dict(rotations))

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "mount": self.mount,
            "body_to_camera_rotation": list(self.body_to_camera_rotation),
        }
        if self.attitude_rotations:
            result["attitude_rotations"] = {k: list(v) for k, v in self.attitude_rotations.items()}
        return result


@dataclass(frozen=True, slots=True)
class CameraMountProfile:
    """Selectable fixed/gimbal capability declared by an airframe profile."""

    type: str = "fixed"
    axes: tuple[str, ...] = ()
    limits: Mapping[str, tuple[float, float]] = MappingProxyType({})
    rates: Mapping[str, float] = MappingProxyType({})
    default_mode: str = "follow-body"

    def __post_init__(self) -> None:
        if self.type not in _CAMERA_MOUNT_TYPES:
            raise AirframeProfileError("camera_mount.type 必须是 fixed 或 gimbal")
        axes = tuple(self.axes)
        if len(set(axes)) != len(axes) or any(axis not in _CAMERA_AXES for axis in axes):
            raise AirframeProfileError("camera_mount.axes 含有不支持的轴")
        if self.type == "gimbal" and not axes:
            raise AirframeProfileError("gimbal camera_mount.axes 不能为空")
        normalized_limits: dict[str, tuple[float, float]] = {}
        for axis, bounds in dict(self.limits).items():
            if axis not in _CAMERA_AXES or not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                raise AirframeProfileError("camera_mount.limits 必须按轴提供 [min,max]")
            lower, upper = (_number(value, f"camera_mount.limits.{axis}[]") for value in bounds)
            if lower > upper:
                raise AirframeProfileError(f"camera_mount.limits.{axis} 反向")
            normalized_limits[axis] = (lower, upper)
        normalized_rates: dict[str, float] = {}
        for axis, rate in dict(self.rates).items():
            if axis not in _CAMERA_AXES:
                raise AirframeProfileError("camera_mount.rates 含有不支持的轴")
            normalized_rates[axis] = _number(rate, f"camera_mount.rates.{axis}", positive=True)
        if not isinstance(self.default_mode, str) or not self.default_mode.strip():
            raise AirframeProfileError("camera_mount.default_mode 必须是非空字符串")
        allowed_modes = {"follow-body", "lock-horizon", "follow-yaw"}
        if self.default_mode not in allowed_modes:
            raise AirframeProfileError("camera_mount.default_mode 不受支持")
        if self.type == "fixed" and self.default_mode != "follow-body":
            raise AirframeProfileError("fixed camera_mount 必须使用 follow-body")
        object.__setattr__(self, "axes", axes)
        object.__setattr__(self, "limits", MappingProxyType(normalized_limits))
        object.__setattr__(self, "rates", MappingProxyType(normalized_rates))

    @classmethod
    def from_mapping(cls, raw: Any) -> CameraMountProfile:
        value = _strict_mapping(raw, "camera_mount", {"type", "axes", "limits", "rates", "default_mode"}, set())
        if not isinstance(value["axes"], (list, tuple)):
            raise AirframeProfileError("camera_mount.axes 必须是数组")
        return cls(value["type"], tuple(value["axes"]), value["limits"], value["rates"], value["default_mode"])

    def to_mapping(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "axes": list(self.axes),
            "limits": {axis: list(bounds) for axis, bounds in self.limits.items()},
            "rates": dict(self.rates),
            "default_mode": self.default_mode,
        }

    def to_camera_mount(self) -> Any:
        from multidetect.camera_mount import CameraMount, GimbalLimits

        if self.type == "fixed":
            return CameraMount.fixed()
        limits = self.limits
        return CameraMount.gimbal(
            limits=GimbalLimits(
                pitch_min_rad=limits.get("pitch", (-math.pi, math.pi))[0],
                pitch_max_rad=limits.get("pitch", (-math.pi, math.pi))[1],
                yaw_min_rad=limits.get("yaw", (-math.pi, math.pi))[0],
                yaw_max_rad=limits.get("yaw", (-math.pi, math.pi))[1],
                roll_min_rad=limits.get("roll", (-math.pi, math.pi))[0],
                roll_max_rad=limits.get("roll", (-math.pi, math.pi))[1],
            ),
            rate_rad_s=self.rates.get("pitch") or self.rates.get("yaw"),
            stabilization=self.default_mode,
        )


@dataclass(frozen=True, slots=True)
class EnduranceProfile:
    battery_wh: float
    reserve_fraction: float
    hover_power_w: float
    cruise_power_w: float
    loiter_power_w: float
    climb_power_w: float
    descent_power_w: float
    transition_energy_wh: float
    glide_sink_rate_mps: float
    wind_margin_mps: float

    def __post_init__(self) -> None:
        for name in (
            "battery_wh",
            "hover_power_w",
            "cruise_power_w",
            "loiter_power_w",
            "climb_power_w",
            "descent_power_w",
        ):
            object.__setattr__(
                self, name, _number(getattr(self, name), f"endurance.{name}", positive=True)
            )
        for name in ("transition_energy_wh", "glide_sink_rate_mps", "wind_margin_mps"):
            object.__setattr__(
                self, name, _number(getattr(self, name), f"endurance.{name}", minimum=0.0)
            )
        object.__setattr__(
            self,
            "reserve_fraction",
            _number(self.reserve_fraction, "endurance.reserve_fraction", minimum=0.0),
        )
        if self.reserve_fraction >= 1.0:
            raise AirframeProfileError("endurance.reserve_fraction 必须小于 1")

    @classmethod
    def from_mapping(cls, raw: Any) -> EnduranceProfile:
        value = _strict_mapping(
            raw,
            "endurance",
            {
                "battery_wh",
                "reserve_fraction",
                "hover_power_w",
                "cruise_power_w",
                "loiter_power_w",
                "climb_power_w",
                "descent_power_w",
                "transition_energy_wh",
                "glide_sink_rate_mps",
                "wind_margin_mps",
            },
            set(),
        )
        return cls(**value)

    def to_mapping(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class LimitsProfile:
    max_step_distance_m: float
    max_acceleration_mps2: float
    max_angular_rate_dps: float

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(
                self, name, _number(getattr(self, name), f"limits.{name}", positive=True)
            )

    @classmethod
    def from_mapping(cls, raw: Any) -> LimitsProfile:
        value = _strict_mapping(
            raw,
            "limits",
            {"max_step_distance_m", "max_acceleration_mps2", "max_angular_rate_dps"},
            set(),
        )
        return cls(**value)

    def to_mapping(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class AirframeProfile:
    id: str
    category: str
    subtype: str
    px4_autostart: int
    min_airspeed_mps: float
    cruise_airspeed_mps: float
    max_airspeed_mps: float
    stall_airspeed_mps: float
    max_bank_deg: float
    max_climb_rate_mps: float
    max_descent_rate_mps: float
    can_hover: bool
    hover_modes: tuple[str, ...]
    takeoff: TakeoffProfile
    landing: LandingProfile
    camera: CameraProfile
    endurance: EnduranceProfile
    limits: LimitsProfile
    camera_mount: CameraMountProfile | None = None
    min_turn_radius_m: float | None = None
    transition: TransitionProfile | None = None
    loiter_radius_override_m: float | None = None
    notes: tuple[str, ...] = ()
    provenance: Mapping[str, str] = MappingProxyType({})
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "id"))
        object.__setattr__(self, "category", _text(self.category, "category"))
        object.__setattr__(self, "subtype", _text(self.subtype, "subtype"))
        if self.category not in _CATEGORIES:
            raise AirframeProfileError("category 必须是 multirotor/fixed_wing/vtol")
        if self.subtype not in _SUBTYPES:
            raise AirframeProfileError("subtype 不受支持")
        if self.category == "multirotor" and self.subtype not in {"quad", "other"}:
            raise AirframeProfileError("multirotor 只支持 quad 或 other subtype")
        if self.category == "fixed_wing" and self.subtype not in {
            "conventional",
            "flying_wing",
            "other",
        }:
            raise AirframeProfileError("fixed_wing 只支持 conventional/flying_wing/other subtype")
        if self.category == "vtol" and self.subtype not in {
            "quadplane",
            "tailsitter",
            "tiltrotor",
            "other",
        }:
            raise AirframeProfileError("vtol 只支持 quadplane/tailsitter/tiltrotor/other subtype")
        if (
            isinstance(self.px4_autostart, bool)
            or not isinstance(self.px4_autostart, int)
            or self.px4_autostart <= 0
        ):
            raise AirframeProfileError("px4_autostart 必须是正整数")
        speeds = (
            "min_airspeed_mps",
            "cruise_airspeed_mps",
            "max_airspeed_mps",
            "stall_airspeed_mps",
        )
        for name in speeds:
            object.__setattr__(self, name, _number(getattr(self, name), name, minimum=0.0))
        if not (
            self.stall_airspeed_mps
            <= self.min_airspeed_mps
            <= self.cruise_airspeed_mps
            <= self.max_airspeed_mps
        ):
            raise AirframeProfileError("空速必须满足 stall <= min <= cruise <= max")
        object.__setattr__(
            self, "max_bank_deg", _number(self.max_bank_deg, "max_bank_deg", minimum=1e-3)
        )
        if self.max_bank_deg >= 89.0:
            raise AirframeProfileError("max_bank_deg 必须小于 89 度")
        for name in ("max_climb_rate_mps", "max_descent_rate_mps"):
            object.__setattr__(self, name, _number(getattr(self, name), name, positive=True))
        object.__setattr__(self, "can_hover", _bool(self.can_hover, "can_hover"))
        if self.hover_modes:
            object.__setattr__(
                self,
                "hover_modes",
                _tuple_strings(
                    self.hover_modes, "hover_modes", frozenset({"multirotor", "vtol", "hover"})
                ),
            )
        elif not isinstance(self.hover_modes, (tuple, list)):
            raise AirframeProfileError("hover_modes 必须是字符串数组")
        else:
            object.__setattr__(self, "hover_modes", ())
        if self.category == "fixed_wing" and self.can_hover:
            raise AirframeProfileError("fixed_wing 不能 can_hover=true")
        if self.subtype == "tailsitter" and not {"mc", "fw"}.issubset(
            self.camera.attitude_rotations
        ):
            raise AirframeProfileError("tailsitter camera 必须同时提供 mc 和 fw 两种姿态旋转")
        if self.can_hover and not self.hover_modes:
            raise AirframeProfileError("can_hover=true 必须有 hover_modes")
        if not self.can_hover and self.hover_modes:
            raise AirframeProfileError("can_hover=false 不得声明 hover_modes")
        if self.category == "vtol" and self.transition is None:
            raise AirframeProfileError("vtol 必须提供 transition")
        if self.category != "vtol" and self.transition is not None:
            raise AirframeProfileError("只有 vtol 可以提供 transition")
        physical = self.cruise_airspeed_mps**2 / (_G * math.tan(math.radians(self.max_bank_deg)))
        if self.min_turn_radius_m is None:
            object.__setattr__(self, "min_turn_radius_m", physical * 1.10)
        else:
            object.__setattr__(
                self,
                "min_turn_radius_m",
                _number(self.min_turn_radius_m, "min_turn_radius_m", positive=True),
            )
            if self.min_turn_radius_m + 1e-9 < physical:
                raise AirframeProfileError("min_turn_radius_m 小于最大坡度和巡航空速决定的物理下限")
        if self.loiter_radius_override_m is not None:
            object.__setattr__(
                self,
                "loiter_radius_override_m",
                _number(self.loiter_radius_override_m, "loiter_radius_override_m", positive=True),
            )
        object.__setattr__(self, "notes", tuple(_text(x, "notes[]") for x in self.notes))
        object.__setattr__(
            self,
            "provenance",
            MappingProxyType(
                {str(k): _text(v, f"provenance.{k}") for k, v in dict(self.provenance).items()}
            ),
        )
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise AirframeProfileError("schema_version 必须是整数")
        if self.schema_version != 1:
            raise AirframeProfileError("不支持的 schema_version")

    @classmethod
    def from_mapping(cls, raw: Any) -> AirframeProfile:
        required = {
            "schema_version",
            "id",
            "category",
            "subtype",
            "px4_autostart",
            "min_airspeed_mps",
            "cruise_airspeed_mps",
            "max_airspeed_mps",
            "stall_airspeed_mps",
            "max_bank_deg",
            "max_climb_rate_mps",
            "max_descent_rate_mps",
            "can_hover",
            "hover_modes",
            "takeoff",
            "landing",
            "camera",
            "endurance",
            "limits",
        }
        optional = {
            "min_turn_radius_m",
            "transition",
            "loiter_radius_override_m",
            "notes",
            "provenance",
            "camera_mount",
        }
        value = _strict_mapping(raw, "AirframeProfile", required, optional)
        raw_hover_modes = value["hover_modes"]
        if not isinstance(raw_hover_modes, (list, tuple)):
            raise AirframeProfileError("hover_modes 必须是字符串数组")
        raw_notes = value.get("notes", ())
        if not isinstance(raw_notes, (list, tuple)):
            raise AirframeProfileError("notes 必须是字符串数组")
        return cls(
            id=value["id"],
            category=value["category"],
            subtype=value["subtype"],
            px4_autostart=value["px4_autostart"],
            min_airspeed_mps=value["min_airspeed_mps"],
            cruise_airspeed_mps=value["cruise_airspeed_mps"],
            max_airspeed_mps=value["max_airspeed_mps"],
            stall_airspeed_mps=value["stall_airspeed_mps"],
            max_bank_deg=value["max_bank_deg"],
            max_climb_rate_mps=value["max_climb_rate_mps"],
            max_descent_rate_mps=value["max_descent_rate_mps"],
            can_hover=value["can_hover"],
            hover_modes=tuple(raw_hover_modes),
            takeoff=TakeoffProfile.from_mapping(value["takeoff"]),
            landing=LandingProfile.from_mapping(value["landing"]),
            camera=CameraProfile.from_mapping(value["camera"]),
            camera_mount=(
                CameraMountProfile.from_mapping(value["camera_mount"])
                if "camera_mount" in value
                else None
            ),
            endurance=EnduranceProfile.from_mapping(value["endurance"]),
            limits=LimitsProfile.from_mapping(value["limits"]),
            min_turn_radius_m=value.get("min_turn_radius_m"),
            transition=TransitionProfile.from_mapping(value["transition"])
            if value.get("transition") is not None
            else None,
            loiter_radius_override_m=value.get("loiter_radius_override_m"),
            notes=tuple(raw_notes),
            provenance=value.get("provenance", {}),
            schema_version=value["schema_version"],
        )

    @classmethod
    def from_json(cls, path: str | Path) -> AirframeProfile:
        return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "id": self.id,
            "category": self.category,
            "subtype": self.subtype,
            "px4_autostart": self.px4_autostart,
            "min_airspeed_mps": self.min_airspeed_mps,
            "cruise_airspeed_mps": self.cruise_airspeed_mps,
            "max_airspeed_mps": self.max_airspeed_mps,
            "stall_airspeed_mps": self.stall_airspeed_mps,
            "min_turn_radius_m": self.min_turn_radius_m,
            "max_bank_deg": self.max_bank_deg,
            "max_climb_rate_mps": self.max_climb_rate_mps,
            "max_descent_rate_mps": self.max_descent_rate_mps,
            "can_hover": self.can_hover,
            "hover_modes": list(self.hover_modes),
            "takeoff": self.takeoff.to_mapping(),
            "landing": self.landing.to_mapping(),
            "camera": self.camera.to_mapping(),
            "endurance": self.endurance.to_mapping(),
            "limits": self.limits.to_mapping(),
        }
        if self.transition is not None:
            result["transition"] = self.transition.to_mapping()
        if self.camera_mount is not None:
            result["camera_mount"] = self.camera_mount.to_mapping()
        if self.loiter_radius_override_m is not None:
            result["loiter_radius_override_m"] = self.loiter_radius_override_m
        if self.notes:
            result["notes"] = list(self.notes)
        if self.provenance:
            result["provenance"] = dict(self.provenance)
        return result

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_mapping(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    load_json = from_json

    def save_json(self, path: str | Path) -> None:
        self.to_json(path)

    def physical_turn_radius_m(self) -> float:
        return self.cruise_airspeed_mps**2 / (_G * math.tan(math.radians(self.max_bank_deg)))

    def loiter_radius_m(self) -> float:
        return max(self.min_turn_radius_m, self.loiter_radius_override_m or 0.0)

    def orbit_radius(self, requested: float | None = None) -> float:
        if requested is not None:
            requested = _number(requested, "requested orbit radius", positive=True)
        return max(self.loiter_radius_m(), requested or 0.0)

    def can_stop_near_target(self) -> bool:
        return (
            self.can_hover
            and bool(self.hover_modes)
            and any(method in self.landing.methods for method in ("vtol", "multirotor"))
        )

    def thinking_behavior(self) -> dict[str, Any]:
        if self.category == "multirotor":
            mode = "hover" if self.can_hover else "loiter"
        elif self.category == "fixed_wing":
            mode = "loiter"
        else:
            mode = "hover" if self.can_hover else "fixed_wing_loiter"
        return {
            "hover": self.can_hover,
            "loiter": self.loiter_radius_m(),
            "mode_choice": mode,
            "stop_near_target": self.can_stop_near_target(),
        }


def load_airframe_profile(path: str | Path) -> AirframeProfile:
    return AirframeProfile.from_json(path)


def resolve_airframe_profile(
    value: str | Path | AirframeProfile | None,
    takeoff_method: str | None = None,
    *,
    base_dir: str | Path | None = None,
) -> AirframeProfile | None:
    """Resolve the collector airframe id using its canonical aliases.

    ``None`` is intentional for legacy family aliases whose collector branch
    predates an explicit profile (``x500``, ``multirotor``, ``vtol`` and the
    fixed-wing aliases).  Category and executor selection both build on this
    resolver rather than maintaining another profile alias table.
    """
    if isinstance(value, AirframeProfile):
        return value
    normalized = "" if value is None else str(value).strip().lower().replace("-", "_")
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3]
    profiles = root / "configs" / "airframes"
    if normalized == "x500_gimbal":
        return load_airframe_profile(profiles / "x500_gimbal.json")
    if normalized in {"x500", "x500_mono_cam", "multirotor", "fixed_wing", "fw", "vtol"}:
        return None

    selected = normalized
    if normalized == "rc_cessna_mono_cam" and takeoff_method in {"hand", "catapult"}:
        selected = f"rc_cessna_{takeoff_method}"
    aliases = {
        "rc_cessna_mono_cam": "rc_cessna_runway",
        "advanced_plane_mono_cam": "advanced_plane",
        "quadtailsitter_mono_cam": "quadtailsitter",
        "tiltrotor_mono_cam": "tiltrotor",
        "standard_vtol_mono_cam": "standard_vtol",
    }
    candidates = [profiles / f"{selected}.json"]
    if selected == normalized and normalized in aliases:
        candidates.insert(0, profiles / f"{aliases[normalized]}.json")
    for path in candidates:
        if path.is_file():
            return load_airframe_profile(path)
    raise ValueError(f"unknown airframe profile: {value}")


def airframe_profile_category(
    value: str | Path | AirframeProfile | None,
    takeoff_method: str | None = None,
    *,
    base_dir: str | Path | None = None,
) -> str:
    """Resolve an airframe id/path to its capability category."""
    if isinstance(value, AirframeProfile):
        return value.category
    normalized = "" if value is None else str(value).strip().lower().replace("-", "_")
    profile = resolve_airframe_profile(value, takeoff_method, base_dir=base_dir)
    if profile is not None:
        return profile.category
    if normalized in {"x500", "x500_mono_cam", "multirotor"}:
        return "multirotor"
    if normalized == "vtol":
        return "vtol"
    return "fixed_wing"


def airframe_uses_multirotor_executor(
    value: str | Path | AirframeProfile | None,
    takeoff_method: str | None = None,
    *,
    base_dir: str | Path | None = None,
) -> bool:
    """Return the collector's historical multirotor executor selection."""
    if isinstance(value, AirframeProfile):
        return value.id in {"x500_mono_cam", "x500_gimbal"}
    normalized = "" if value is None else str(value).strip().lower().replace("-", "_")
    profile = resolve_airframe_profile(value, takeoff_method, base_dir=base_dir)
    return normalized == "x500_mono_cam" or bool(profile and profile.id == "x500_gimbal")


def save_airframe_profile(profile: AirframeProfile, path: str | Path) -> None:
    if not isinstance(profile, AirframeProfile):
        raise TypeError("profile 必须是 AirframeProfile")
    profile.to_json(path)


__all__ = [
    "AirframeProfile",
    "AirframeProfileError",
    "CameraMountProfile",
    "CameraProfile",
    "EnduranceProfile",
    "LandingProfile",
    "LaunchDetection",
    "LimitsProfile",
    "ParameterRef",
    "TakeoffProfile",
    "TransitionProfile",
    "airframe_profile_category",
    "airframe_uses_multirotor_executor",
    "load_airframe_profile",
    "resolve_airframe_profile",
    "save_airframe_profile",
]
