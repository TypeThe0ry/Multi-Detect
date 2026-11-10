"""Offline source-contract checks for the pinned PX4 Gazebo status patch.

Set PX4_SOURCE_ROOT to the pinned checkout; no compiler, simulator or process is run.
These are source checks, not firmware or flight acceptance.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'simulation/px4/patches/gz-gimbal-attitude-device-id.patch'


def test_patch_is_the_exact_one_assignment_candidate():
    text = PATCH.read_text()
    assert hashlib.sha256(PATCH.read_bytes()).hexdigest() == 'f5c5d6caca92fd04f09e4cf2323b3ba2ab4859e545542a4a163e40721c9d5d13'
    assert [line for line in text.splitlines() if line.startswith('+') and not line.startswith('+++')] == [
        '+\tgimbal_att.gimbal_device_id = _gimbal_device_id;'
    ]
    assert not [line for line in text.splitlines() if line.startswith('-') and not line.startswith('---')]


def test_pinned_px4_source_connects_device_information_status_and_manager():
    root = os.environ.get('PX4_SOURCE_ROOT')
    if not root:
        pytest.skip('set PX4_SOURCE_ROOT to the pinned PX4 checkout')
    root = Path(root)
    def read(relative, sha=None):
        path = root / relative
        if sha:
            assert hashlib.sha256(path.read_bytes()).hexdigest() == sha
        return path.read_text()
    base = 'src/modules/simulation/gz_bridge/'
    source = read(base + 'GZGimbal.cpp', '1b5ff1dfc8c5575ed9ebaf70f781dbd93571dfcabc23934714bbfa05d6534a7a')
    header = read(base + 'GZGimbal.hpp', '5ac1e9bf6463d5c8fc221df6f93480e2b47e826e898ef727a688513eb6264e7a')
    assert 'const uint8_t _gimbal_device_id = 1;' in header
    assert 'device_info.gimbal_device_id = _gimbal_device_id;' in source
    before, rest = source.split('void GZGimbal::publishDeviceAttitude()', 1)
    original, _ = rest.split('void GZGimbal::publishJointCommand', 1)
    assert 'gimbal_att.gimbal_device_id' not in original
    context = '\tgimbal_att.target_component = 0; // Broadcast\n'
    assert original.count(context) == 1
    patched = original.replace(context, context + '\tgimbal_att.gimbal_device_id = _gimbal_device_id;\n')
    assert patched.count('gimbal_att.gimbal_device_id = _gimbal_device_id;') == 1
    assert 'gimbal_device_attitude_status_s gimbal_att{};' in patched
    assert '_gimbal_device_attitude_status_pub.publish(gimbal_att);' in patched
    attitude_stream = read('src/modules/mavlink/streams/GIMBAL_DEVICE_ATTITUDE_STATUS.hpp', '17b16112dae563fd9bc40b7230f6b501e05bbe08737b844624f6cdb43242bd7e')
    manager_stream = read('src/modules/mavlink/streams/GIMBAL_MANAGER_STATUS.hpp', 'c1553e5d8fc88e3e447a3a53afcf5d948916b017b4efde0d0c3e31bc5df6ba43')
    assert 'msg.gimbal_device_id = gimbal_device_attitude_status.gimbal_device_id;' in attitude_stream
    assert 'msg.gimbal_device_id = gimbal_manager_status.gimbal_device_id;' in manager_stream
    manager_output = read('src/modules/gimbal/output_mavlink.cpp')
    assert '_gimbal_device_id = gimbal_device_information.gimbal_device_id;' in manager_output
    assert 'gimbal_device_id = _gimbal_device_found ? _gimbal_device_id : 0;' in manager_output
    manager_input = read('src/modules/gimbal/input_mavlink.cpp')
    assert 'gimbal_manager_status.gimbal_device_id = control_data.device_compid;' in manager_input
