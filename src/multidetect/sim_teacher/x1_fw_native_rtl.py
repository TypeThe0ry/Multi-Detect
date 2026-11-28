"""Host-only sweep for the direct-to-home PX4 tailsitter RTL branch."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from types import MappingProxyType

from multidetect.autonomy.x1_motion_proof import ExecutionSweep, LandingPad, SweptLeg, _number
from .x1_native_handoff import IOWA_FW_AIRSPEED_PARAMETERS, IOWA_RTL_DESCEND_ALT_M

# Source reviewed in px4-main, not the 83c4f4e image used by archived case C.
PX4_RTL_COMMIT = "53afcc82b2a51a90424ff7e2c35583e491587eb5"
_REQUIRED = {
    "SYS_AUTOSTART": 4051, "VT_TYPE": 0, "RTL_TYPE": 0, "RTL_CONE_ANG": 0,
    "RTL_LAND_DELAY": 0, "RTL_APPR_FORCE": 0, "RTL_PLD_MD": 0,
    "RTL_DESCEND_ALT": IOWA_RTL_DESCEND_ALT_M,
    "MPC_XY_CRUISE": 5, "MPC_Z_V_AUTO_DN": 1.5, "MPC_LAND_SPEED": 0.7,
    "NAV_ACC_RAD": 2,
    **IOWA_FW_AIRSPEED_PARAMETERS,
}
_POSITIVE = ("RTL_RETURN_ALT", "RTL_LOITER_RAD", "NAV_LOITER_RAD",
             "VT_B_TRANS_DUR", "VT_B_DEC_MSS", "FW_R_LIM")
_PHASES = ("fw_climb", "fw_connector", "fw_descent", "fw_to_land", "transition", "mc", "landing")


def suffix_power_w(endurance):
    """Power assumptions shared by Iowa suffixes and the FW RTL analysis."""
    return (2 * endurance["loiter_power_w"],
            max(endurance[name] for name in ("climb_power_w", "hover_power_w", "descent_power_w")))


@dataclass(frozen=True, slots=True)
class FwRtlBounds:
    """Reviewed bounds for one immutable SIM image, site and mission scope.

    The referenced host assessment must establish home-only (no rally/approach
    or avoidance detour), locked parameters, and the bounds over all admitted
    headings, sample/command latency and scoped winds. Phase times are completion
    ceilings, not PX4 ETAs. Landing tracking must include residual velocity at
    the NAV_ACC_RAD handoff, not just settled hover error.
    """

    scope: str
    px4_commit: str
    image_id: str
    validation_sha256: str
    params: Mapping[str, float]
    fw_tracking_m: float
    fw_vertical_m: float
    fw_acceptance_m: float
    move_to_land_vertical_m: float
    transition_vertical_m: float
    mc_tracking_m: float
    mc_vertical_m: float
    landing_tracking_m: float
    landing_vertical_m: float
    max_ground_speed_mps: float
    phase_time_s: tuple[float, ...]
    fw_peak_power_w: float
    hover_peak_power_w: float
    max_bank_deg: float
    max_climb_mps: float
    max_descent_mps: float
    max_acceleration_mps2: float
    max_angular_rate_dps: float
    home_only_verified: bool
    configuration_locked: bool
    # Corridor manifests (version 2) must publish the ground-speed floor used
    # for the distance-scaled connector and the manifest revision. Version 1
    # objects are the reviewed single-waypoint scope only; the default keeps
    # those in-code hypotheses constructible while ``from_host_manifest``
    # rejects any old JSON document. A missing/zero floor never enables a
    # corridor return.
    connector_ground_speed_min_mps: float | None = None
    manifest_version: int = 1

    def __post_init__(self):
        if self.connector_ground_speed_min_mps is None:
            trim = self.params.get("FW_AIRSPD_TRIM") if isinstance(self.params, Mapping) else None
            if _number(trim, 0.001):
                # The reviewed single-waypoint manifest used a 3 m/s wind margin.
                object.__setattr__(self, "connector_ground_speed_min_mps", float(trim) - 3.0)

    @classmethod
    def from_host_manifest(cls, raw):
        if (not isinstance(raw, dict) or set(raw) != {f.name for f in fields(cls)}
                or not isinstance(raw["params"], dict) or not isinstance(raw["phase_time_s"], list)):
            raise ValueError("invalid FW RTL host manifest")
        if raw.get("manifest_version") != 2:
            raise ValueError("FW RTL host manifest without the corridor v2 fields")
        if not _number(raw.get("connector_ground_speed_min_mps"), 0.001):
            raise ValueError("FW RTL host manifest is missing the connector ground-speed floor")
        return cls(**{**raw, "params": MappingProxyType(dict(raw["params"])),
                      "phase_time_s": tuple(raw["phase_time_s"])})

    def connector_time_s(self, chord_m):
        """Distance-scaled FW connector ceiling: chord / min ground speed + capture."""
        floor = self.connector_ground_speed_min_mps
        if not _number(chord_m, 0.0) or not _number(floor, 0.001):
            return None
        return chord_m / floor + self.phase_time_s[1]

    def corridor_ready(self, *, min_cruise_rel_home_m):
        """True only for a v2 manifest that cannot climb above the corridor."""
        return (self.manifest_version == 2
                and _number(self.connector_ground_speed_min_mps, 0.001)
                and _number(min_cruise_rel_home_m, 0.0)
                and _number(self.params.get("RTL_RETURN_ALT"), 0.0)
                and self.params["RTL_RETURN_ALT"] <= min_cruise_rel_home_m + 1e-9)

    def matches_validation(self, assessment):
        """Bind a reviewed sidecar to this manifest's scope and verified speed pins."""
        expected = {"basis": "ASSUMED_SIM", "status": "reviewed", "scope": self.scope,
                    "image_id": self.image_id, "px4_commit": self.px4_commit}
        if not isinstance(assessment, dict) or any(assessment.get(k) != v for k, v in expected.items()):
            return False
        pins = assessment.get("fw_airspeed_parameters")
        return (isinstance(pins, dict) and set(pins) == set(IOWA_FW_AIRSPEED_PARAMETERS)
                and all(_number(pins[k], v, v) and self.params.get(k) == v
                        for k, v in IOWA_FW_AIRSPEED_PARAMETERS.items()))

    def valid_for(self, scope, profile):
        p = self.params
        return (
            self.scope == scope and self.px4_commit == PX4_RTL_COMMIT
            and isinstance(self.image_id, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", self.image_id) is not None
            and isinstance(self.validation_sha256, str) and re.fullmatch(r"[a-f0-9]{64}", self.validation_sha256) is not None
            and self.home_only_verified is True and self.configuration_locked is True
            and (profile.subtype, profile.px4_autostart) == ("tailsitter", 4051)
            and isinstance(p, Mapping) and set(p) == {*_REQUIRED, *_POSITIVE}
            and all(_number(p[k], v, v) for k, v in _REQUIRED.items())
            and (p["FW_AIRSPD_MIN"], p["FW_AIRSPD_TRIM"], p["FW_AIRSPD_MAX"])
            == (profile.min_airspeed_mps, profile.cruise_airspeed_mps, profile.max_airspeed_mps)
            and all(_number(p[k], 0.001) for k in _POSITIVE)
            and p["VT_B_TRANS_DUR"] <= profile.transition.timeout_s
            and len(self.phase_time_s) == len(_PHASES)
            and all(_number(t, 0.001, 900) for t in self.phase_time_s)
            and self.phase_time_s[4] > p["VT_B_TRANS_DUR"]
            and all(_number(b, 0.001) for b in (
                self.fw_tracking_m, self.fw_vertical_m, self.fw_acceptance_m, self.move_to_land_vertical_m,
                self.transition_vertical_m,
                self.mc_tracking_m, self.mc_vertical_m, self.landing_tracking_m, self.landing_vertical_m,
                self.fw_peak_power_w, self.hover_peak_power_w,
            ))
            and self.manifest_version in (1, 2)
            and _number(self.connector_ground_speed_min_mps, 0.001, profile.cruise_airspeed_mps + 1e-9)
            and abs(self.connector_ground_speed_min_mps
                    - (p["FW_AIRSPD_TRIM"] - profile.endurance.wind_margin_mps)) <= 0.01
            and _number(self.max_ground_speed_mps, profile.max_airspeed_mps, 100)
            and _number(self.max_bank_deg, p["FW_R_LIM"], profile.max_bank_deg)
            and all(_number(v, 0.001, limit) for v, limit in (
                (self.max_climb_mps, profile.max_climb_rate_mps),
                (self.max_descent_mps, profile.max_descent_rate_mps),
                (self.max_acceleration_mps2, profile.limits.max_acceleration_mps2),
                (self.max_angular_rate_dps, profile.limits.max_angular_rate_dps),
            ))
        )

    def sweep(self, decision, frame, hold, observed_home, pad: LandingPad, profile):
        p = self.params
        start, orbit = frame.position_ne_amsl, hold.centre
        home = pad.point
        home_offset = math.dist(home[:2], observed_home[:2])
        home_alt_error = abs(home[2] - observed_home[2])
        # Keep the proof anchored to the surveyed pad, not wherever PX4 calls home.
        landing_width = home_offset + p["NAV_ACC_RAD"] + self.landing_tracking_m
        if math.sqrt(2) * landing_width > pad.radius_m or home_alt_error > pad.elevation_error_m:
            return None
        # The bank limit gives a feasibility floor for the radius, NOT a bound
        # on drift or turn acquisition. The latter comes from tracking evidence.
        turn = profile.max_airspeed_mps**2 / (9.80665 * math.tan(math.radians(p["FW_R_LIM"])))
        hold_r, fallback_r, rtl_r = hold.site.radius_m, p["NAV_LOITER_RAD"], p["RTL_LOITER_RAD"]
        if min(hold_r, fallback_r, rtl_r) < turn:
            return None
        if (math.dist(start[:2], orbit[:2]) > hold.padding_ne_m
                or abs(start[2] - orbit[2]) > hold.padding_alt_m):
            return None

        return_z = max(start[2], observed_home[2] + p["RTL_RETURN_ALT"])
        descend_z = min(return_z, observed_home[2] + p["RTL_DESCEND_ALT"])
        orbit_top, home_top, home_low = (*orbit[:2], return_z), (*home[:2], return_z), (*home[:2], descend_z)
        # Cover the entire prior orbit's vertical band and sampling/departure
        # uncertainty in the activation and connector; don't propagate that
        # extra hold band into the separately bounded home descent.
        fw_z = self.fw_vertical_m
        hold_z = max(hold.padding_alt_m + abs(start[2] - orbit[2]), fw_z)
        # Cover CLIMBING whether PX4 retains the established Hold or uses its
        # fallback orbit around the activation position. Include both tangent
        # acquisition and the full circles, not just the nominal connectors.
        hold_width = max(hold.padding_ne_m, fallback_r, hold_r) + turn + self.fw_tracking_m
        home_width = home_offset + rtl_r + turn + self.fw_tracking_m
        transition_time = self.phase_time_s[4]
        brake_entry = max(self.fw_acceptance_m, self.max_ground_speed_mps**2 / (2 * p["VT_B_DEC_MSS"]))
        transition_width = home_width + brake_entry + self.max_ground_speed_mps * transition_time
        # MOVE_TO_LAND has infinite altitude acceptance in Navigator. The
        # corresponding undershoot/overshoot needs its own validated bound.
        move_z = fw_z + self.move_to_land_vertical_m
        transition_z = move_z + self.transition_vertical_m
        times = dict(zip(_PHASES, self.phase_time_s, strict=True))

        def leg(phase, a, b, width, vertical, time_key):
            power = self.fw_peak_power_w if phase == "fw" else self.hover_peak_power_w
            return SweptLeg(phase, a, b, width, vertical, times[time_key], power)

        return ExecutionSweep(
            frame.frame_id, decision.digest,
            (leg("fw", start, orbit_top, hold_width, hold_z, "fw_climb"),
             leg("fw", orbit_top, home_top, max(hold_width, home_width), hold_z, "fw_connector"),
             leg("fw", home_top, home_low, home_width, fw_z, "fw_descent"),
             leg("fw", home_low, home_low, home_width + brake_entry, move_z, "fw_to_land"),
             leg("transition", home_low, home_low, transition_width, transition_z, "transition"),
             leg("mc", home_low, home_low, transition_width + self.mc_tracking_m,
                 transition_z + self.mc_vertical_m, "mc"),
             leg("landing", home_low, home, landing_width, home_alt_error + self.landing_vertical_m, "landing")),
            self.max_bank_deg, self.max_climb_mps, self.max_descent_mps,
            self.max_acceleration_mps2, self.max_angular_rate_dps,
            min_airspeed_mps=profile.min_airspeed_mps, max_airspeed_mps=profile.max_airspeed_mps,
            landing_pad=pad,
        )

    def corridor_sweep(self, decision, frame, *, start, orbit, observed_home, pad: LandingPad, profile):
        """Native RTL from a corridor position using the distance-scaled connector.

        This is deliberately separate from :meth:`sweep`: the single-waypoint
        path keeps its reviewed fixed connector ceiling byte-for-byte. The
        corridor form covers the live fix (or Hold centre) to the home descent
        with ``chord / connector_ground_speed_min_mps + capture`` and never
        assumes a climb above the corridor.
        """
        if not _number(self.connector_ground_speed_min_mps, 0.001):
            return None
        if (not isinstance(start, tuple) or len(start) != 3
                or not isinstance(orbit, tuple) or len(orbit) != 3
                or not all(_number(value) for value in (*start, *orbit))):
            return None
        p = self.params
        home = pad.point
        home_offset = math.dist(home[:2], observed_home[:2])
        home_alt_error = abs(home[2] - observed_home[2])
        landing_width = home_offset + p["NAV_ACC_RAD"] + self.landing_tracking_m
        if math.sqrt(2) * landing_width > pad.radius_m or home_alt_error > pad.elevation_error_m:
            return None
        turn = profile.max_airspeed_mps**2 / (9.80665 * math.tan(math.radians(p["FW_R_LIM"])))
        hold_r, rtl_r = p["NAV_LOITER_RAD"], p["RTL_LOITER_RAD"]
        if min(hold_r, rtl_r) < turn:
            return None

        return_z = max(start[2], observed_home[2] + p["RTL_RETURN_ALT"])
        descend_z = min(return_z, observed_home[2] + p["RTL_DESCEND_ALT"])
        orbit_top = (*orbit[:2], return_z)
        home_top, home_low = (*home[:2], return_z), (*home[:2], descend_z)
        fw_z = self.fw_vertical_m
        # The live/centre vertical band is part of the departure uncertainty.
        hold_z = max(abs(start[2] - orbit[2]), fw_z)
        hold_width = max(hold_r, rtl_r) + turn + self.fw_tracking_m
        home_width = home_offset + rtl_r + turn + self.fw_tracking_m
        transition_time = self.phase_time_s[4]
        brake_entry = max(self.fw_acceptance_m, self.max_ground_speed_mps**2 / (2 * p["VT_B_DEC_MSS"]))
        transition_width = home_width + brake_entry + self.max_ground_speed_mps * transition_time
        move_z = fw_z + self.move_to_land_vertical_m
        transition_z = move_z + self.transition_vertical_m
        times = dict(zip(_PHASES, self.phase_time_s, strict=True))
        chord = math.dist(orbit[:2], home[:2])
        connector_time = self.connector_time_s(chord)
        if connector_time is None:
            return None

        def leg(phase, a, b, width, vertical, time_s):
            power = self.fw_peak_power_w if phase == "fw" else self.hover_peak_power_w
            return SweptLeg(phase, a, b, width, vertical, time_s, power)

        legs = []
        # PX4 retains the established Hold or acquires its fallback orbit. This
        # acquisition is only present when the live fix is not already the centre.
        if math.dist(start[:2], orbit[:2]) > 1e-6 or abs(start[2] - orbit[2]) > 1e-6:
            legs.append(leg("fw", start, orbit_top, hold_width, hold_z, times["fw_climb"]))
        legs.extend((
            leg("fw", orbit_top, home_top, max(hold_width, home_width), hold_z, connector_time),
            leg("fw", home_top, home_low, home_width, fw_z, times["fw_descent"]),
            leg("fw", home_low, home_low, home_width + brake_entry, move_z, times["fw_to_land"]),
            leg("transition", home_low, home_low, transition_width, transition_z, times["transition"]),
            leg("mc", home_low, home_low, transition_width + self.mc_tracking_m,
                transition_z + self.mc_vertical_m, times["mc"]),
            leg("landing", home_low, home, landing_width, home_alt_error + self.landing_vertical_m,
                times["landing"]),
        ))
        return ExecutionSweep(
            frame.frame_id, decision.digest, tuple(legs),
            self.max_bank_deg, self.max_climb_mps, self.max_descent_mps,
            self.max_acceleration_mps2, self.max_angular_rate_dps,
            min_airspeed_mps=profile.min_airspeed_mps, max_airspeed_mps=profile.max_airspeed_mps,
            landing_pad=pad,
        )
