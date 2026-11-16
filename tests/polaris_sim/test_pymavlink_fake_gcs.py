import pytest

from multidetect.polaris_sim.gateway.protocol import MavlinkFrameDecoder, route_frames


def test_pymavlink_fake_gcs_frames_route_command_and_gimbal_manager_by_target():
    mavlink = pytest.importorskip("pymavlink.dialects.v20.common")
    gcs = mavlink.MAVLink(None, srcSystem=255, srcComponent=190)
    messages = [
        mavlink.MAVLink_heartbeat_message(
            mavlink.MAV_TYPE_GCS, mavlink.MAV_AUTOPILOT_INVALID, 0, 0, mavlink.MAV_STATE_ACTIVE, 3
        ),
        mavlink.MAVLink_command_long_message(
            2, 1, mavlink.MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW, 0, -25, 0, float("nan"),
            float("nan"), 44, 0, 0
        ),
    ]
    frames = []
    for message in messages:
        frames.extend(MavlinkFrameDecoder().feed(message.pack(gcs)))
    routed = route_frames(frames, {1, 2})
    assert [frame.message_id for frame in MavlinkFrameDecoder().feed(routed[1])] == [0]
    assert [frame.message_id for frame in MavlinkFrameDecoder().feed(routed[2])] == [0, 76]
