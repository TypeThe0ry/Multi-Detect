"""Discrete X1 camera planning poses at guided loiters and Offboard waypoint passes."""
from __future__ import annotations

from dataclasses import dataclass
import math

from multidetect.camera_mount import _mat_transpose
from multidetect.sim_teacher.horizon_stabilization import rotation_deg, wrap_deg
from scripts.realworld.x1_scenario_class import FW_PASS_CROSS_M


@dataclass(frozen=True)
class WaypointTurn:
    switch_ned: tuple
    arriving: tuple
    departing: tuple


def _offset(point, heading, distance):
    radians = math.radians(heading)
    return (point[0] + distance * math.cos(radians), point[1] + distance * math.sin(radians), point[2])


def _turn_pose(point, course):
    # Recorded roll/pitch are MC-frame ZYX Euler angles. The horizontal wing
    # heading is the azimuth of -body Z, not Euler yaw (up to ~90° apart).
    bodies = []
    for roll, pitch in ((0., -83.), (75., -71.), (-75., -71.), (90., -53.), (-90., -53.)):
        base = rotation_deg(dict(roll=roll, pitch=pitch, yaw=0.))
        bearing = math.degrees(math.atan2(-base[5], -base[2]))
        bodies.append(_mat_transpose(rotation_deg(dict(roll=roll, pitch=pitch, yaw=course-bearing))))
    return point, tuple(bodies)


def waypoint_turn(waypoint, inbound, outbound, radius, capture_radius, *, plane_pass, short_approach):
    """Sample the old and new leg through handoff; distances in local NED metres."""
    delta = wrap_deg(outbound - inbound)
    if capture_radius is not None:
        # Bounded Offboard passages: by the *track-mode marker* (later than
        # waypoint acceptance) observed median displacement is ~22 m beyond
        # the plane. In the 31.6 m legs the bend carries the aircraft outside
        # the polygon by ~12-25 m. Mirror this offset with the turn direction.
        if plane_pass:
            along = 22.
            outside = min(1., abs(delta) / 15.) * FW_PASS_CROSS_M if short_approach else 0.
            switch = _offset(waypoint, inbound, along)
            switch = _offset(switch, inbound - math.copysign(90., delta), outside)
        else:
            # Older capture-disk passages may switch before crossing the plane.
            switch = _offset(waypoint, inbound, -capture_radius)
        # Cover lag on entering a short bend and overshoot on exit. Include
        # both headings at the switch for both legs, in addition to the plane.
        courses = (inbound - .5 * delta, inbound, outbound, outbound + .5 * delta)
        shared = tuple(_turn_pose(switch, course) for course in courses)
        arriving = (_turn_pose(waypoint, inbound), *shared)
        departing = (*shared, _turn_pose(_offset(switch, outbound, radius / 2), outbound))
        return WaypointTurn(switch, arriving, departing)

    # Guided reposition: tangent to a clockwise ring. On Iowa the wing had
    # turned 98..119° at the actual switch. It can keep turning briefly as
    # the next command takes effect, before reversing bank towards outbound.
    def ring(sweep):
        course = inbound + sweep
        return _turn_pose(_offset(waypoint, course - 90., radius), course)

    arrival = tuple(ring(sweep) for sweep in (0., 25., 50., 75., 100., 120.))
    switch = ring(100.)[0]
    course = inbound + 120.
    delta = wrap_deg(outbound - course)
    side = math.copysign(1., delta)
    # The recorded wing bank can double in the rollout (17° to ~37°).
    # At comparable airspeed this roughly halves the turn radius. Sample
    # both arcs, retaining the association between heading and position.
    departure = list(arrival[-2:])
    steps = max(1, math.ceil(abs(delta) / 15.))
    for exit_radius in (radius / 2, radius):
        center = _offset(arrival[-1][0], course + 90., side * exit_radius)
        for i in range(1, steps + 1):
            heading = course + delta * i / steps
            position = _offset(center, heading - 90., side * exit_radius)
            departure.append(_turn_pose(position, heading))
    return WaypointTurn(switch, arrival, tuple(departure))
