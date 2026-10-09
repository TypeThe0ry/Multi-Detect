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
