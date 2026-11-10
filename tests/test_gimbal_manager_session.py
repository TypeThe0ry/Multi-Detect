from types import SimpleNamespace

import pytest

from multidetect.gimbal_controller import GimbalManagerSession, Px4GimbalController
from multidetect.camera_mount import CameraMount


class State:
    def __init__(self):
        self.command_acks = {}
        self.received_monotonic_ns = {}
        self.manager = None
    def get(self, key):
        return self.manager if key == 'GIMBAL_MANAGER_STATUS' else None


@pytest.mark.parametrize('identity', [(255, 190), (245, 191)])
def test_acquire_before_command_using_link_identity_and_device_id(identity):
    state = State()
    reader = SimpleNamespace(state=state, raise_if_failed=lambda: None)
    connection = SimpleNamespace(mav=SimpleNamespace(srcSystem=identity[0], srcComponent=identity[1]))
    sent = []
    tick = [100]
    def clock():
        tick[0] += 1
        return tick[0]
    def send(command, *params):
        sent.append((command, params))
        state.command_acks[command] = ({'result': 0}, clock())
        if command == 1001:
            state.manager = {'gimbal_device_id': 1, 'primary_control_sysid': identity[0], 'primary_control_compid': identity[1]}
            state.received_monotonic_ns['GIMBAL_MANAGER_STATUS'] = clock()
    session = GimbalManagerSession(connection, reader, send, clock_ns=clock, sleep=lambda _: None)
    controller = Px4GimbalController(CameraMount.gimbal(), lambda command, params: session.command(command, *params))
    controller.command_pitch_yaw(0.5, 1.0, vehicle_yaw_rad=0.2)
    assert [item[0] for item in sent] == [1001, 1000]
    assert sent[0][1] == (*identity, -1.0, -1.0, 0.0, 0.0, 1.0)
    assert sent[1][1][4:] == (44.0, 0.0, 1.0)
    assert sent[1][1][1] == pytest.approx(0.8 * 180 / 3.141592653589793)


@pytest.mark.parametrize('result,manager', [(2, True), (None, True), (0, False)])
def test_acquisition_failure_never_sends_pitch_command(result, manager):
    state = State()
    reader = SimpleNamespace(state=state, raise_if_failed=lambda: None)
    connection = SimpleNamespace(mav=SimpleNamespace(srcSystem=255, srcComponent=190))
    sent, tick = [], [0]
    def clock():
        tick[0] += 100_000_000
        return tick[0]
    def send(command, *params):
        sent.append(command)
        if result is not None:
            state.command_acks[command] = ({'result': result}, clock())
        if manager:
            state.manager = {'gimbal_device_id': 1, 'primary_control_sysid': 255, 'primary_control_compid': 190}
            state.received_monotonic_ns['GIMBAL_MANAGER_STATUS'] = clock()
    session = GimbalManagerSession(connection, reader, send, clock_ns=clock, sleep=lambda _: None, timeout_s=0.5)
    with pytest.raises((RuntimeError, TimeoutError)):
        session.command(1000, *([0.0] * 7))
    assert sent == [1001]

@pytest.mark.parametrize('result', [2, None])
def test_pitch_command_rejection_or_timeout_is_propagated(result):
    state = State()
    reader = SimpleNamespace(state=state, raise_if_failed=lambda: None)
    connection = SimpleNamespace(mav=SimpleNamespace(srcSystem=245, srcComponent=191))
    tick = [0]
    def clock():
        tick[0] += 100_000_000
        return tick[0]
    def send(command, *params):
        if command == 1001:
            state.command_acks[command] = ({'result': 0}, clock())
            state.manager = {'gimbal_device_id': 1, 'primary_control_sysid': 245, 'primary_control_compid': 191}
            state.received_monotonic_ns['GIMBAL_MANAGER_STATUS'] = clock()
        elif result is not None:
            state.command_acks[command] = ({'result': result}, clock())
    session = GimbalManagerSession(connection, reader, send, clock_ns=clock, sleep=lambda _: None, timeout_s=0.5)
    with pytest.raises((RuntimeError, TimeoutError)):
        session.command(1000, *([0.0] * 7))
    assert not session.owned


def test_manager_status_must_identify_same_source():
    state = State()
    reader = SimpleNamespace(state=state, raise_if_failed=lambda: None)
    connection = SimpleNamespace(mav=SimpleNamespace(srcSystem=245, srcComponent=191))
    tick, sent = [0], []
    def clock():
        tick[0] += 100_000_000
        return tick[0]
    def send(command, *params):
        sent.append(command)
        state.command_acks[command] = ({'result': 0}, clock())
        state.manager = {'gimbal_device_id': 1, 'primary_control_sysid': 255, 'primary_control_compid': 190}
        state.received_monotonic_ns['GIMBAL_MANAGER_STATUS'] = clock()
    session = GimbalManagerSession(connection, reader, send, clock_ns=clock, sleep=lambda _: None, timeout_s=0.5)
    with pytest.raises(TimeoutError, match='ownership'):
        session.command(1000, *([0.0] * 7))
    assert sent == [1001]

@pytest.mark.parametrize('case', ['stale', 'wrong_device'])
def test_ownership_requires_fresh_status_for_selected_device(case):
    state = State()
    reader = SimpleNamespace(state=state, raise_if_failed=lambda: None)
    connection = SimpleNamespace(mav=SimpleNamespace(srcSystem=255, srcComponent=190))
    tick, sent = [0], []
    def clock():
        tick[0] += 100_000_000
        return tick[0]
    def send(command, *params):
        sent.append(command)
        state.command_acks[command] = ({'result': 0}, clock())
        state.manager = {'gimbal_device_id': 2 if case == 'wrong_device' else 1,
                         'primary_control_sysid': 255, 'primary_control_compid': 190}
        state.received_monotonic_ns['GIMBAL_MANAGER_STATUS'] = 0 if case == 'stale' else clock()
    session = GimbalManagerSession(connection, reader, send, clock_ns=clock, sleep=lambda _: None, timeout_s=0.5)
    with pytest.raises(TimeoutError, match='ownership'):
        session.command(1000, *([0.0] * 7))
    assert sent == [1001]
