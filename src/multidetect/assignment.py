from __future__ import annotations

import math
from collections.abc import Sequence


def rectangular_linear_assignment(costs: Sequence[Sequence[float]]) -> tuple[int, ...]:
    """Return the minimum-cost column for every row using a rectangular Hungarian solve.

    The matrix must contain at least as many columns as rows.  The pure-Python
    implementation keeps runtime tracking and offline evaluation deterministic without
    adding SciPy to the Jetson metadata path.
    """

    if not costs:
        return ()
    row_count = len(costs)
    column_count = len(costs[0])
    if column_count < row_count:
        raise ValueError("linear assignment requires at least as many columns as rows")
    if any(len(row) != column_count for row in costs):
        raise ValueError("linear assignment cost matrix must be rectangular")
    if any(not math.isfinite(cost) for row in costs for cost in row):
        raise ValueError("linear assignment costs must be finite")

    row_potential = [0.0] * (row_count + 1)
    column_potential = [0.0] * (column_count + 1)
    matched_row = [0] * (column_count + 1)
    predecessor = [0] * (column_count + 1)
    for row in range(1, row_count + 1):
        matched_row[0] = row
        minimum_reduced_cost = [math.inf] * (column_count + 1)
        used = [False] * (column_count + 1)
        column = 0
        while True:
            used[column] = True
            current_row = matched_row[column]
            delta = math.inf
            next_column = 0
            for candidate_column in range(1, column_count + 1):
                if used[candidate_column]:
                    continue
                reduced_cost = (
                    costs[current_row - 1][candidate_column - 1]
                    - row_potential[current_row]
                    - column_potential[candidate_column]
                )
                if reduced_cost < minimum_reduced_cost[candidate_column]:
                    minimum_reduced_cost[candidate_column] = reduced_cost
                    predecessor[candidate_column] = column
                if minimum_reduced_cost[candidate_column] < delta:
                    delta = minimum_reduced_cost[candidate_column]
                    next_column = candidate_column
            for candidate_column in range(column_count + 1):
                if used[candidate_column]:
                    row_potential[matched_row[candidate_column]] += delta
                    column_potential[candidate_column] -= delta
                else:
                    minimum_reduced_cost[candidate_column] -= delta
            column = next_column
            if matched_row[column] == 0:
                break
        while True:
            previous_column = predecessor[column]
            matched_row[column] = matched_row[previous_column]
            column = previous_column
            if column == 0:
                break

    assignment = [-1] * row_count
    for column in range(1, column_count + 1):
        if matched_row[column] != 0:
            assignment[matched_row[column] - 1] = column - 1
    return tuple(assignment)


__all__ = ["rectangular_linear_assignment"]
