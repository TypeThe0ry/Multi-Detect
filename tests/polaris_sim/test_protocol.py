from multidetect.polaris_sim.gateway.protocol import (
    MavlinkFrameDecoder,
    build_mavlink2_frame,
    route_frames,
)


def test_decoder_handles_fragmented_mavlink2_frames():
    heartbeat = build_mavlink2_frame(0, bytes(9), system_id=255)
    decoder = MavlinkFrameDecoder()
    assert decoder.feed(heartbeat[:5]) == []
    frames = decoder.feed(heartbeat[5:])
    assert len(frames) == 1
    assert frames[0].system_id == 255
    assert frames[0].is_heartbeat


def test_mixed_target_batch_and_gimbal_manager_messages_route_by_sysid():
    command_long = bytearray(33)
    command_long[30] = 2
    pitch_yaw = bytearray(23)
    pitch_yaw[20] = 2  # GIMBAL_MANAGER_SET_PITCHYAW target_system in wire order
    heartbeat = build_mavlink2_frame(0, bytes(9), system_id=255)
    frames = MavlinkFrameDecoder().feed(
        heartbeat + build_mavlink2_frame(76, command_long) + build_mavlink2_frame(287, pitch_yaw)
    )
    routed = route_frames(frames, {1, 2, 3})
    assert len(MavlinkFrameDecoder().feed(routed[1])) == 1
    sysid_two = MavlinkFrameDecoder().feed(routed[2])
    assert [frame.message_id for frame in sysid_two] == [0, 76, 287]
    sysid_three = MavlinkFrameDecoder().feed(routed[3])
    assert [frame.message_id for frame in sysid_three] == [0]


def test_mission_upload_target_offset_does_not_broadcast_to_other_vehicles():
    mission_count = bytearray(5)
    mission_count[2] = 2
    frames = MavlinkFrameDecoder().feed(build_mavlink2_frame(44, mission_count))
    routed = route_frames(frames, {1, 2})
    assert set(routed) == {2}


def test_vehicle_sysid_is_fixed_from_slot():
    from multidetect.polaris_sim.gateway.server import vehicle_for_slot

    assert vehicle_for_slot(5, "rw_demo", "x500_gimbal").sysid == 1
    assert vehicle_for_slot(7, "rw_demo", "x500_gimbal").sysid == 3
