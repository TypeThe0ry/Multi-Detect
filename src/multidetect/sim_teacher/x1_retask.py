"""Serial X1 route slot and scoped preflight checks for an exact airborne suffix."""
from __future__ import annotations

from dataclasses import dataclass

from multidetect.autonomy.x1_mission import X1AirborneStep


@dataclass(frozen=True, slots=True)
class InstalledStep:
    revision: int
    step: X1AirborneStep


@dataclass(frozen=True, slots=True)
class _Route:
    revision: int
    boundary: int
    steps: tuple[X1AirborneStep, ...]
    plan_sha256: str
    finding_step_ids: frozenset[str]
    finding_error_m: float | None


class AirborneRouteSlot:
    """Single writer. Installation changes the cursor and revision as one value."""

    def __init__(self):
        self._route = None
        self.boundary = None
        self.prepared = None

    def enter_tick(self, boundary):
        self.boundary = boundary
        self.prepared = None

    def stage(self, plan, revision, steps, *, plan_sha256, finding_error_m=None):
        if self.boundary is not None and steps:
            finding_ids = frozenset(step.id for step in plan.steps if step.params.get("target_ref") == "finding:1")
            self.prepared = plan, _Route(revision + 1, self.boundary, tuple(steps),
                                         plan_sha256, finding_ids, finding_error_m)

    def replace_remaining(self, plan, expected_revision):
        prepared = self.prepared
        self.prepared = None
        if (self.boundary is None or prepared is None or prepared[0] is not plan
                or prepared[1].revision != expected_revision + 1 or expected_revision >= 2**31 - 1
                or self._route is not None):
            return False
        self._route = prepared[1]
        return True

    @property
    def end(self):
        route = self._route
        return route.boundary + len(route.steps) if route is not None else None

    def starts_at(self, index):
        return self._route is not None and self._route.boundary == index

    def remaining_at(self, index):
        route = self.installed_at(index)
        return route.steps[index - route.boundary:] if route is not None else ()

    def installed_at(self, index):
        route = self._route
        if route is not None and type(index) is int and route.boundary <= index < self.end:
            return route

    def at(self, index):
        route = self._route
        if route is None or not route.boundary <= index < self.end:
            return None
        return InstalledStep(route.revision, route.steps[index - route.boundary])
