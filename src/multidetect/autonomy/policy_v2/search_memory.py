"""Deterministic 5 m search memory used by policy v2."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .contracts import PolicyContractError


@dataclass(frozen=True, slots=True)
class SearchMemorySnapshot:
    """Serializable grid snapshot.

    Channel order is ``age_s, coverage_count, obstacle_probability,
    target_evidence, planned_cursor``.  The grid is centred on the reset
    anchor; callers may use it as an egocentric local-NED grid or retain the
    anchor as a world origin.
    """

    captured_at_s: float
    anchor_north_m: float
    anchor_east_m: float
    channels: tuple[tuple[tuple[float, ...], ...], ...]

    @property
    def grid(self) -> tuple[tuple[tuple[float, ...], ...], ...]:
        return self.channels


class SearchMemoryGrid:
    CELL_SIZE_M = 5.0
    SIZE = 16
    CHANNELS = 5

    def __init__(self, *, cell_size_m: float = CELL_SIZE_M, size: int = SIZE) -> None:
        if cell_size_m != self.CELL_SIZE_M or size != self.SIZE:
            raise PolicyContractError("policy v2 memory is frozen at a 5 m 16x16 grid")
        self.cell_size_m = float(cell_size_m)
        self.size = int(size)
        self._anchor: tuple[float, float] | None = None
        self._last_time: float | None = None
        self._cells = self._new_cells()
        self._cell_last_seen: list[list[float | None]] = [
            [None] * self.SIZE for _ in range(self.SIZE)
        ]

    @staticmethod
    def _new_cells() -> list[list[list[float]]]:
        return [
            [[0.0] * SearchMemoryGrid.CHANNELS for _ in range(SearchMemoryGrid.SIZE)]
            for _ in range(SearchMemoryGrid.SIZE)
        ]

    @property
    def anchor(self) -> tuple[float, float] | None:
        return self._anchor

    @property
    def last_time_s(self) -> float | None:
        return self._last_time

    def reset(
        self,
        *,
        anchor_north_m: float = 0.0,
        anchor_east_m: float = 0.0,
        captured_at_s: float | None = None,
    ) -> None:
        self._finite(anchor_north_m, "anchor_north_m")
        self._finite(anchor_east_m, "anchor_east_m")
        if captured_at_s is not None:
            self._finite(captured_at_s, "captured_at_s", minimum=0.0)
        self._anchor = (float(anchor_north_m), float(anchor_east_m))
        self._last_time = captured_at_s
        self._cells = self._new_cells()
        self._cell_last_seen = [[None] * self.SIZE for _ in range(self.SIZE)]

    def update(
        self,
        *,
        north_m: float,
        east_m: float,
        captured_at_s: float,
        obstacle_probability: float | None = None,
        target_evidence: float | None = None,
        planned_cursor: bool = False,
    ) -> bool:
        """Record one pose.  Invalid updates are rejected without mutation."""
        north = self._finite(north_m, "north_m")
        east = self._finite(east_m, "east_m")
        now = self._finite(captured_at_s, "captured_at_s", minimum=0.0)
        if obstacle_probability is not None:
            obstacle = self._finite(
                obstacle_probability, "obstacle_probability", minimum=0.0, maximum=1.0
            )
        else:
            obstacle = None
        evidence = (
            None
            if target_evidence is None
            else self._finite(target_evidence, "target_evidence", minimum=0.0, maximum=1.0)
        )
        if type(planned_cursor) is not bool:
            raise PolicyContractError("planned_cursor must be a boolean")
        if self._anchor is None:
            anchor = (north, east)
        else:
            anchor = self._anchor
        row, col = self._index(north, east, anchor)
        if row is None or col is None:
            return False
        if self._last_time is not None and now < self._last_time:
            return False
        # All validation is complete. Mutate only after the update is known to
        # be admissible, so a rejected pose cannot partially corrupt memory.
        previous_cell_time = self._cell_last_seen[row][col]
        self._anchor = anchor
        self._last_time = now
        cell = self._cells[row][col]
        age = 0.0 if previous_cell_time is None else max(0.0, now - previous_cell_time)
        cell[0] = age
        if cell[1] == 0.0:
            cell[1] = 1.0
        if obstacle is not None:
            cell[2] = obstacle
        if evidence is not None:
            cell[3] = evidence
        if planned_cursor:
            for cells_row in self._cells:
                for candidate in cells_row:
                    candidate[4] = 0.0
            cell[4] = 1.0
        self._cell_last_seen[row][col] = now
        return True

    def mark(self, **kwargs: object) -> bool:
        return self.update(**kwargs)  # type: ignore[arg-type]

    def snapshot(self, *, captured_at_s: float | None = None) -> SearchMemorySnapshot:
        now = (
            self._last_time
            if captured_at_s is None
            else self._finite(captured_at_s, "captured_at_s", minimum=0.0)
        )
        if now is None:
            now = 0.0
        channels_list: list[list[tuple[float, ...]]] = []
        for row_index, row in enumerate(self._cells):
            output_row: list[tuple[float, ...]] = []
            for col_index, cell in enumerate(row):
                last_seen = self._cell_last_seen[row_index][col_index]
                values = list(cell)
                values[0] = 0.0 if last_seen is None else min(60.0, max(0.0, now - last_seen))
                values[1] = math.log1p(max(0.0, values[1])) / 8.0
                output_row.append(tuple(float(value) for value in values))
            channels_list.append(output_row)
        channels = tuple(tuple(row) for row in channels_list)
        return SearchMemorySnapshot(now, *(self._anchor or (0.0, 0.0)), channels)

    def as_channels(
        self, *, captured_at_s: float | None = None
    ) -> tuple[tuple[tuple[float, ...], ...], ...]:
        return self.snapshot(captured_at_s=captured_at_s).channels

    def _index(
        self, north: float, east: float, anchor: tuple[float, float]
    ) -> tuple[int, int] | tuple[None, None]:
        half = self.size * self.cell_size_m / 2.0
        dn, de = north - anchor[0], east - anchor[1]
        if not (-half <= dn < half and -half <= de < half):
            return None, None
        row = int(math.floor((dn + half) / self.cell_size_m))
        col = int(math.floor((de + half) / self.cell_size_m))
        return max(0, min(self.size - 1, row)), max(0, min(self.size - 1, col))

    @staticmethod
    def _finite(
        value: object, name: str, *, minimum: float | None = None, maximum: float | None = None
    ) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise PolicyContractError(f"{name} must be finite")
        result = float(value)
        if minimum is not None and result < minimum or maximum is not None and result > maximum:
            raise PolicyContractError(f"{name} is outside bounds")
        return result


__all__ = ["SearchMemoryGrid", "SearchMemorySnapshot"]
