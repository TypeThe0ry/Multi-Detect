from multidetect.polaris_sim.gateway.netem import NetemConfig, NetemShaper


def test_loss_rate_is_within_tolerance_and_seed_is_reproducible():
    config = NetemConfig(delay_ms=40, jitter_ms=10, loss_pct=20)
    left = NetemShaper(config, config, seed=1701)
    right = NetemShaper(config, config, seed=1701)
    outcomes = [left.shape("up", b"packet", now=index) for index in range(2000)]
    repeated = [right.shape("up", b"packet", now=index) for index in range(2000)]
    assert outcomes == repeated
    observed_loss = sum(result is None for result in outcomes) / len(outcomes)
    assert abs(observed_loss - 0.20) < 0.025


def test_up_down_are_independent_and_bandwidth_adds_serialization_delay():
    shaper = NetemShaper(
        up=NetemConfig(bandwidth_kbps=8),
        down=NetemConfig(delay_ms=100),
        seed=4,
    )
    assert shaper.shape("up", bytes(1000), now=1.0) == 1.0
    assert shaper.shape("down", b"x", now=1.0) == 0.1


def test_netem_validation_rejects_invalid_values():
    try:
        NetemConfig(loss_pct=100.1)
    except ValueError as exc:
        assert "at most 100" in str(exc)
    else:
        raise AssertionError("out-of-range loss was accepted")
