from multidetect.onboard_runtime.vehicle import PymavlinkVehicle


def test_heartbeat_mode_and_local_position_boot_time_are_real_sources():
    vehicle = PymavlinkVehicle("test", clock=lambda: 1.0)
    vehicle.ingest({"message_type": "HEARTBEAT", "autopilot": 1, "_source_system": 1,
                            "_source_component": 1, "base_mode": 128,
                            "custom_mode": (5 << 24) | (4 << 16)})
    state = vehicle.snapshot()
    assert state.mode == "RTL"
    assert state.nav_state is None
    vehicle.ingest({"message_type": "LOCAL_POSITION_NED", "_source_system": 1,
                            "_source_component": 1, "x": 0, "y": 0, "z": -1,
                            "vx": 0, "vy": 0, "vz": 0, "time_boot_ms": 1234})
    assert vehicle.snapshot().time_boot_ms == 1234
