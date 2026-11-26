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


class AirborneRouteSlot:
    """Single writer. Installation changes the cursor and revision as one value."""

    def __init__(self):
        self._route = None
        self.boundary = None
        self.prepared = None

    def enter_tick(self, boundary):
        self.boundary = boundary
        self.prepared = None

    def stage(self, plan, revision, steps):
        if self.boundary is not None and steps:
            self.prepared = plan, revision, tuple(steps)

    def replace_remaining(self, plan, expected_revision):
        prepared = self.prepared
        self.prepared = None
        if (self.boundary is None or prepared is None or prepared[0] is not plan
                or prepared[1] != expected_revision or expected_revision >= 2**31 - 1
                or self._route is not None):
            return False
        self._route = _Route(expected_revision + 1, self.boundary, prepared[2])
        return True

    @property
    def end(self):
        route = self._route
        return route.boundary + len(route.steps) if route is not None else None

    def starts_at(self, index):
        return self._route is not None and self._route.boundary == index

    def remaining_at(self, index):
        route = self._route
        if route is None or not route.boundary <= index < self.end:
            return ()
        return route.steps[index - route.boundary:]

    def at(self, index):
        route = self._route
        if route is None or not route.boundary <= index < self.end:
            return None
        return InstalledStep(route.revision, route.steps[index - route.boundary])
