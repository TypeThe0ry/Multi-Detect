from multidetect.autonomy.policy_v2 import SearchMemoryGrid


def test_grid_repeated_pose_does_not_increase_coverage() -> None:
    grid = SearchMemoryGrid()
    assert grid.update(north_m=0.0, east_m=0.0, captured_at_s=1.0)
    first = grid.snapshot(captured_at_s=1.0).channels[8][8][1]
    assert grid.update(north_m=0.0, east_m=0.0, captured_at_s=2.0)
    second = grid.snapshot(captured_at_s=2.0).channels[8][8][1]
    assert first == second


def test_grid_rejects_backwards_and_out_of_bounds_without_mutation() -> None:
    grid = SearchMemoryGrid()
    assert grid.update(north_m=0.0, east_m=0.0, captured_at_s=2.0)
    before = grid.snapshot()
    assert grid.update(north_m=0.0, east_m=0.0, captured_at_s=1.0) is False
    assert grid.update(north_m=100.0, east_m=0.0, captured_at_s=3.0) is False
    assert grid.snapshot() == before


def test_grid_reset_clears_cells_and_sets_new_anchor() -> None:
    grid = SearchMemoryGrid()
    assert grid.update(north_m=0.0, east_m=0.0, captured_at_s=1.0, target_evidence=1.0)
    grid.reset(anchor_north_m=10.0, anchor_east_m=-5.0, captured_at_s=4.0)
    snap = grid.snapshot(captured_at_s=4.0)
    assert (snap.anchor_north_m, snap.anchor_east_m) == (10.0, -5.0)
    assert all(value == 0.0 for row in snap.channels for cell in row for value in cell)


def test_grid_updates_are_deterministic() -> None:
    kwargs = {
        "north_m": 1.0,
        "east_m": -2.0,
        "captured_at_s": 1.0,
        "obstacle_probability": 0.3,
        "target_evidence": 0.8,
        "planned_cursor": True,
    }
    first = SearchMemoryGrid()
    second = SearchMemoryGrid()
    assert first.update(**kwargs)
    assert second.update(**kwargs)
    assert first.snapshot(captured_at_s=3.0) == second.snapshot(captured_at_s=3.0)


def test_grid_rejects_invalid_parameters_without_partial_mutation() -> None:
    grid = SearchMemoryGrid()
    before = grid.snapshot()
    for kwargs in (
        {"north_m": 0.0, "east_m": 0.0, "captured_at_s": 1.0, "obstacle_probability": 1.1},
        {"north_m": 0.0, "east_m": 0.0, "captured_at_s": 1.0, "target_evidence": -0.1},
        {"north_m": 0.0, "east_m": 0.0, "captured_at_s": 1.0, "planned_cursor": 1},
    ):
        try:
            grid.update(**kwargs)
        except Exception:
            pass
    assert grid.snapshot() == before
