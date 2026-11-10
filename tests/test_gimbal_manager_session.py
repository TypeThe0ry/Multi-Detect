from types import SimpleNamespace

import pytest

from multidetect.gimbal_controller import GimbalManagerSession, Px4GimbalController
from multidetect.camera_mount import CameraMount


class Message:
    def __init__(self, system=1, component=1, **values):
        self.system, self.component = system, component
        self.__dict__.update(values)
    def get_srcSystem(self): return self.system
    def get_srcComponent(self): return self.component


class Reader:
    def __init__(self):
        self.state = SimpleNamespace(vehicle_time_boot_ms=0)
        self.events = []
    def raise_if_failed(self): pass
    def gimbal_events_cursor(self): return len(self.events)
    def gimbal_events_since(self, cursor): return len(self.events), self.events[cursor:]
    def add(self, name, stamp=100, **kwargs): self.events.append((name, Message(**kwargs), stamp, self.state.vehicle_time_boot_ms))


def setup(identity=(255, 190), wall_timeout_s=30):
    reader = Reader()
    connection = SimpleNamespace(mav=SimpleNamespace(srcSystem=identity[0], srcComponent=identity[1]), target_system=1, target_component=1)
    sent, clock = [], [0]
    session = GimbalManagerSession(connection, reader, lambda cmd, *p: sent.append((cmd, p)),
                                   wall_timeout_s=wall_timeout_s, clock_ns=lambda: clock[0])
    return session, reader, sent, clock


def ack(reader, identity=(255, 190), command=1001, result=0, **source):
    reader.add('COMMAND_ACK', command=command, result=result,
               target_system=identity[0], target_component=identity[1], **source)


def owner(reader, identity=(255, 190), device=1, **source):
    reader.add('GIMBAL_MANAGER_STATUS', gimbal_device_id=device,
               primary_control_sysid=identity[0], primary_control_compid=identity[1], **source)


@pytest.mark.parametrize('identity', [(255, 190), (245, 191)])
def test_nonblocking_handshake_and_source_identity(identity):
    session, reader, sent, _ = setup(identity)
    controller = Px4GimbalController(CameraMount.gimbal(), lambda cmd, params: session.command(cmd, *params))
    controller.command_pitch_yaw(0.5, 1.0, vehicle_yaw_rad=0.2)
    assert session.phase == 'configure_ack'
    assert sent[0] == (1001, (*identity, -1.0, -1.0, 0.0, 0.0, 1.0))
    ack(reader, identity)
    owner(reader, identity)
    assert session.poll() == 'command_ack'
    assert sent[1][0] == 1000
    assert sent[1][1][4:] == (44.0, 0.0, 1.0)
    assert sent[1][1][1] == pytest.approx(0.8 * 180 / 3.141592653589793)
    ack(reader, identity, command=1000)
    assert session.poll() == 'ready'


def test_wrong_source_target_and_device_cannot_establish_ownership():
    session, reader, sent, _ = setup()
    session.command(1000, *([0.0] * 7))
    ack(reader, system=2)
    ack(reader, identity=(245, 191))
    session.poll()
    assert session.phase == 'configure_ack'
    ack(reader)
    owner(reader, device=2)
    owner(reader, system=2)
    session.poll()
    assert session.phase == 'owner' and len(sent) == 1
    owner(reader)
    session.poll()
    assert sent[-1][0] == 1000


def test_interleaved_other_manager_status_and_coalesced_commands():
    session, reader, sent, _ = setup()
    session.command(1000, 1)
    session.command(1000, 2)
    ack(reader)
    owner(reader)
    owner(reader, device=2)
    session.poll()
    assert sent[-1] == (1000, (2,))
    session.command(1000, 3)
    session.command(1000, 4)
    assert len(sent) == 2
    ack(reader, command=1000)
    session.poll()
    assert sent[-1] == (1000, (4,))


@pytest.mark.parametrize('phase', ['configure_ack', 'owner', 'command_ack'])
def test_rejection_fails_without_retry(phase):
    session, reader, sent, _ = setup()
    session.command(1000, 1)
    if phase != 'configure_ack':
        ack(reader); session.poll()
    if phase == 'command_ack':
        owner(reader); session.poll()
    if phase == 'owner':
        owner(reader, device=2)
        reader.state.vehicle_time_boot_ms = 4001
    else:
        ack(reader, command=1000 if phase == 'command_ack' else 1001, result=2)
    with pytest.raises((RuntimeError, TimeoutError)):
        session.poll()
    count = len(sent)
    with pytest.raises(RuntimeError): session.command(1000, 2)
    assert len(sent) == count


def test_slow_sim_vehicle_and_wall_timeout_are_bounded_without_waiting():
    session, reader, sent, clock = setup()
    session.command(1000, 1)
    clock[0] = 10_000_000_000
    reader.state.vehicle_time_boot_ms = 1000
    assert session.poll() == 'configure_ack'
    clock[0] = 30_000_000_001
    with pytest.raises(TimeoutError): session.poll()
    ack(reader)
    with pytest.raises(RuntimeError): session.poll()
    assert len(sent) == 1


def test_close_drops_pending_without_io():
    session, _, sent, _ = setup()
    session.command(1000, 1)
    session.close()
    assert session.poll() == 'closed'
    with pytest.raises(RuntimeError): session.command(1000, 2)
    assert len(sent) == 1


def test_ownership_loss_stops_future_commands():
    session, reader, sent, _ = setup()
    session.command(1000, 1)
    ack(reader); owner(reader); session.poll()
    ack(reader, command=1000); session.poll()
    owner(reader, identity=(245, 191))
    with pytest.raises(RuntimeError, match='ownership lost'):
        session.poll()
    with pytest.raises(RuntimeError): session.command(1000, 2)
    assert len(sent) == 2


def test_simultaneous_direct_and_onboard_gimbal_control_is_rejected():
    from multidetect.onboard_runtime.vehicle import PymavlinkVehicle
    vehicle = PymavlinkVehicle('udp://127.0.0.1:14550')
    vehicle.connection = object()
    vehicle._reader = object()
    vehicle._gimbal_control_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match='direct gimbal control'):
            vehicle.send_gimbal_pitch_yaw(0.5, 0.0)
    finally:
        vehicle._gimbal_control_lock.release()
    vehicle._gimbal_session = object()
    result = vehicle.gimbal_control(pitch_deg=-10)
    assert not result.ok and 'already active' in result.detail


def test_reader_retains_interleaved_manager_events_with_source_identity():
    import queue
    import time
    from multidetect.sim_teacher.streams import MavlinkReader

    class Receiver:
        def __init__(self): self.messages = queue.Queue()
        def recv_match(self, **kwargs):
            try: return self.messages.get(timeout=0.01)
            except queue.Empty: return None
    class WireMessage(Message):
        def __init__(self, kind, **fields):
            super().__init__(**fields)
            self.kind = kind
        def get_type(self): return self.kind
        def to_dict(self): return {}

    receiver = Receiver()
    reader = MavlinkReader(receiver).start()
    try:
        cursor = reader.gimbal_events_cursor()
        receiver.messages.put(WireMessage('GIMBAL_MANAGER_STATUS', gimbal_device_id=1))
        receiver.messages.put(WireMessage('GIMBAL_MANAGER_STATUS', gimbal_device_id=2))
        receiver.messages.put(WireMessage('COMMAND_ACK', command=1001, result=0))
        deadline = time.monotonic() + 1
        while reader.gimbal_events_cursor() < cursor + 3 and time.monotonic() < deadline:
            time.sleep(0.005)
        _, events = reader.gimbal_events_since(cursor)
        assert [item[0] for item in events] == ['GIMBAL_MANAGER_STATUS', 'GIMBAL_MANAGER_STATUS', 'COMMAND_ACK']
        assert [item[1].gimbal_device_id for item in events[:2]] == [1, 2]
        assert all(item[1].get_srcSystem() == 1 for item in events)
    finally:
        reader.stop()


def test_manager_status_gets_full_period_after_delayed_ack():
    session, reader, sent, clock = setup()
    session.command(1000, 1)
    clock[0] = 1_500_000_000
    reader.state.vehicle_time_boot_ms = 1500
    reader.add('COMMAND_ACK', stamp=clock[0], command=1001, result=0,
               target_system=255, target_component=190)
    assert session.poll() == 'owner'
    # The periodic status arrives more than two seconds after configure was
    # sent, but within one full status period after ACK.
    clock[0] = 3_400_000_000
    reader.state.vehicle_time_boot_ms = 3400
    reader.add('GIMBAL_MANAGER_STATUS', stamp=clock[0], gimbal_device_id=1,
               primary_control_sysid=255, primary_control_compid=190)
    assert session.poll() == 'command_ack'
    assert sent[-1][0] == 1000


def test_collector_advances_gimbal_during_action_and_surfaces_final_rejection():
    from scripts.collect_sim_teacher_episodes import _join_action_with_gimbal
    session, reader, sent, _ = setup()
    session.command(1000, 1)
    class Thread:
        count = 0
        def is_alive(self): return self.count < 4
        def join(self, timeout=None): self.count += 1
    thread = Thread()
    def tick():
        if thread.count == 0:
            ack(reader); owner(reader)
        if thread.count == 2:
            assert sent[-1][0] == 1000
        if thread.count == 3:
            ack(reader, command=1000, result=2)
    with pytest.raises(RuntimeError, match='rejected'):
        _join_action_with_gimbal(thread, session, tick)
    assert thread.count >= 4
    assert sent[-1][0] == 1000


def test_collector_drains_final_queued_command_and_propagates_failure():
    from scripts.collect_sim_teacher_episodes import _finish_teacher_gimbal
    session, reader, sent, _ = setup()
    session.command(1000, 1)
    ticks = [0]
    def sleep(_):
        ticks[0] += 1
        if ticks[0] == 1:
            ack(reader); owner(reader)
        elif ticks[0] == 2:
            ack(reader, command=1000, result=2)
    with pytest.raises(RuntimeError, match='rejected'):
        _finish_teacher_gimbal(session, lambda: None, sleep=sleep)
    assert [x[0] for x in sent] == [1001, 1000]


@pytest.mark.parametrize('wall_timeout', [5.0, 30.0])
def test_manager_wait_has_fresh_bounded_wall_deadline(wall_timeout):
    session, reader, sent, clock = setup(wall_timeout_s=wall_timeout)
    session.command(1000, 1)
    clock[0] = 1_000_000_000
    reader.state.vehicle_time_boot_ms = 100
    reader.add('COMMAND_ACK', stamp=clock[0], command=1001, result=0,
               target_system=255, target_component=190)
    assert session.poll() == 'owner'
    clock[0] = int((1 + wall_timeout - 0.1) * 1e9)
    assert session.poll() == 'owner'
    clock[0] = int((1 + wall_timeout) * 1e9)
    with pytest.raises(TimeoutError, match='owner'):
        session.poll()
    assert len(sent) == 1


def test_collector_final_command_success_waits_for_ack():
    from scripts.collect_sim_teacher_episodes import _finish_teacher_gimbal
    session, reader, sent, _ = setup()
    session.command(1000, 1)
    ticks = [0]
    def sleep(_):
        ticks[0] += 1
        if ticks[0] == 1:
            ack(reader); owner(reader)
        elif ticks[0] == 2:
            ack(reader, command=1000)
    _finish_teacher_gimbal(session, lambda: None, sleep=sleep)
    assert session.phase == 'ready' and ticks[0] == 2
    assert [x[0] for x in sent] == [1001, 1000]


@pytest.mark.parametrize('late_by', ['wall', 'vehicle'])
def test_late_ack_cannot_transition_session(late_by):
    session, reader, sent, clock = setup(wall_timeout_s=5)
    session.command(1000, 1)
    clock[0] = 6_000_000_000 if late_by == 'wall' else 1_000_000_000
    reader.state.vehicle_time_boot_ms = 2001 if late_by == 'vehicle' else 100
    reader.add('COMMAND_ACK', stamp=clock[0], command=1001, result=0,
               target_system=255, target_component=190)
    with pytest.raises(TimeoutError, match='configure_ack'):
        session.poll()
    assert [x[0] for x in sent] == [1001]


def test_late_status_cannot_transition_session():
    session, reader, sent, clock = setup(wall_timeout_s=5)
    session.command(1000, 1)
    clock[0] = 1_000_000_000
    reader.state.vehicle_time_boot_ms = 100
    reader.add('COMMAND_ACK', stamp=clock[0], command=1001, result=0,
               target_system=255, target_component=190)
    session.poll()
    clock[0] = 6_000_000_000
    reader.state.vehicle_time_boot_ms = 200
    reader.add('GIMBAL_MANAGER_STATUS', stamp=clock[0], gimbal_device_id=1,
               primary_control_sysid=255, primary_control_compid=190)
    with pytest.raises(TimeoutError, match='owner'):
        session.poll()
    assert [x[0] for x in sent] == [1001]


def test_timely_ack_processed_after_deadline_is_not_discarded():
    session, reader, _, clock = setup(wall_timeout_s=5)
    session.command(1000, 1)
    reader.state.vehicle_time_boot_ms = 100
    reader.add('COMMAND_ACK', stamp=4_000_000_000, command=1001, result=0,
               target_system=255, target_component=190)
    clock[0] = 6_000_000_000
    assert session.poll() == 'owner'


def test_managed_primitive_serializes_previous_session_and_requires_fresh_pose():
    from scripts.collect_sim_teacher_episodes import _execute_managed_gimbal_action
    from multidetect.sim_teacher.primitives import ActionPrimitive
    session, reader, sent, clock = setup()
    messages = {}
    reader.state.get = lambda key: messages.get(key)
    reader.state.received_monotonic_ns = {}
    profile = SimpleNamespace(camera=SimpleNamespace(gimbal={
        'enabled': True, 'angle_range_deg': {'pitch': [-135, 45], 'yaw': [-180, 180], 'roll': [-45, 45]}}))
    session.command(1000, 99)  # An earlier automatic aim is still in flight.
    ticks = [0]
    def sleep(_):
        ticks[0] += 1
        clock[0] += 100_000_000
        stamp = clock[0]
        if ticks[0] == 1:
            reader.add('COMMAND_ACK', stamp=stamp, command=1001, result=0,
                       target_system=255, target_component=190)
            reader.add('GIMBAL_MANAGER_STATUS', stamp=stamp, gimbal_device_id=1,
                       primary_control_sysid=255, primary_control_compid=190)
        elif ticks[0] == 2:
            reader.add('COMMAND_ACK', stamp=stamp, command=1000, result=0,
                       target_system=255, target_component=190)
        elif ticks[0] == 3:
            assert [x[0] for x in sent] == [1001, 1000, 1000]
            reader.add('COMMAND_ACK', stamp=stamp, command=1000, result=0,
                       target_system=255, target_component=190)
            reader.add('GIMBAL_DEVICE_ATTITUDE_STATUS', stamp=session.started_ns - 1,
                       gimbal_device_id=1, q=[1, 0, 0, 0], flags=0)
        elif ticks[0] == 4:
            reader.add('GIMBAL_DEVICE_ATTITUDE_STATUS', stamp=stamp,
                       gimbal_device_id=1, q=[1, 0, 0, 0], flags=0)
    result = _execute_managed_gimbal_action(ActionPrimitive.gimbal(pitch_deg=0),
        session=session, profile=profile, reader=reader, check=lambda: None,
        clock=lambda: clock[0] / 1e9, sleep=sleep)
    assert result.status == 'success'
    assert ticks[0] == 4  # ACK and stale feedback were insufficient.
    assert sent[1][1] == (99,) and sent[2][1][0:2] == (0.0, 0.0)


def test_managed_primitive_enforces_profile_limits_before_sending():
    from scripts.collect_sim_teacher_episodes import _execute_managed_gimbal_action
    from multidetect.sim_teacher.primitives import ActionPrimitive
    session, reader, sent, clock = setup()
    profile = SimpleNamespace(camera=SimpleNamespace(gimbal={
        'enabled': True, 'angle_range_deg': {'pitch': [-45, 45], 'yaw': [-180, 180], 'roll': [-45, 45]}}))
    result = _execute_managed_gimbal_action(ActionPrimitive.gimbal(pitch_deg=-90),
        session=session, profile=profile, reader=reader, check=lambda: None,
        clock=lambda: clock[0] / 1e9, sleep=lambda _: None)
    assert result.status == 'rejected' and result.ack['reason'] == 'gimbal_angle_out_of_range'
    assert not sent


def test_late_pitch_ack_is_rejected_even_when_already_buffered():
    session, reader, _, clock = setup(wall_timeout_s=5)
    session.command(1000, 1)
    ack(reader); owner(reader); session.poll()
    assert session.phase == 'command_ack'
    clock[0] = 6_000_000_000
    reader.state.vehicle_time_boot_ms = 100
    reader.add('COMMAND_ACK', stamp=clock[0], command=1000, result=0,
               target_system=255, target_component=190)
    with pytest.raises(TimeoutError, match='command_ack'):
        session.poll()


def test_managed_primitive_from_idle_acquires_once_and_waits_for_feedback():
    from scripts.collect_sim_teacher_episodes import _execute_managed_gimbal_action
    from multidetect.sim_teacher.primitives import ActionPrimitive
    session, reader, sent, clock = setup()
    messages = {}
    reader.state.get = lambda key: messages.get(key)
    reader.state.received_monotonic_ns = {}
    profile = SimpleNamespace(camera=SimpleNamespace(gimbal={
        'enabled': True, 'angle_range_deg': {'pitch': [-135, 45], 'yaw': [-180, 180], 'roll': [-45, 45]}}))
    ticks = [0]
    def sleep(_):
        ticks[0] += 1
        clock[0] += 100_000_000
        stamp = clock[0]
        if ticks[0] == 1:
            reader.add('COMMAND_ACK', stamp=stamp, command=1001, result=0,
                       target_system=255, target_component=190)
            reader.add('GIMBAL_MANAGER_STATUS', stamp=stamp, gimbal_device_id=1,
                       primary_control_sysid=255, primary_control_compid=190)
        elif ticks[0] == 2:
            reader.add('COMMAND_ACK', stamp=stamp, command=1000, result=0,
                       target_system=255, target_component=190)
            reader.add('GIMBAL_DEVICE_ATTITUDE_STATUS', stamp=stamp,
                       gimbal_device_id=1, q=[1, 0, 0, 0], flags=0)
    result = _execute_managed_gimbal_action(ActionPrimitive.gimbal(pitch_deg=0),
        session=session, profile=profile, reader=reader, check=lambda: None,
        clock=lambda: clock[0] / 1e9, sleep=sleep)
    assert result.status == 'success' and [x[0] for x in sent] == [1001, 1000]
    assert result.ack['actual_deg']['pitch'] == 0


def test_attitude_sample_is_atomic_and_filters_other_devices_and_sources():
    session, reader, _, _ = setup()
    session.command(1000, 1)
    ack(reader); owner(reader); session.poll()
    ack(reader, command=1000)
    reader.add('GIMBAL_DEVICE_ATTITUDE_STATUS', stamp=100, gimbal_device_id=2,
               q=[1, 0, 0, 0], system=1, component=1)
    reader.add('GIMBAL_DEVICE_ATTITUDE_STATUS', stamp=101, gimbal_device_id=1,
               q=[1, 0, 0, 0], system=9, component=1)
    session.poll()
    assert session.latest_attitude is None
    reader.add('GIMBAL_DEVICE_ATTITUDE_STATUS', stamp=102, gimbal_device_id=1,
               q=[1, 0, 0, 0], system=1, component=1)
    session.poll()
    sample, stamp = session.latest_attitude
    assert stamp == 102 and sample.gimbal_device_id == 1 and sample.get_srcSystem() == 1
    reader.add('GIMBAL_DEVICE_ATTITUDE_STATUS', stamp=103, gimbal_device_id=2,
               q=[0, 1, 0, 0], system=1, component=1)
    session.poll()
    assert session.latest_attitude == (sample, 102)


def test_managed_readback_ignores_disjoint_latest_state_and_untrusted_events():
    from scripts.collect_sim_teacher_episodes import _execute_managed_gimbal_action
    from multidetect.sim_teacher.primitives import ActionPrimitive
    session, reader, _, clock = setup()
    # This intentionally inconsistent latest-state pair used to fabricate success.
    reader.state.get = lambda key: {'q': [1, 0, 0, 0], 'flags': 0} if key == 'GIMBAL_DEVICE_ATTITUDE_STATUS' else None
    reader.state.received_monotonic_ns = {'GIMBAL_DEVICE_ATTITUDE_STATUS': 9999999999}
    profile = SimpleNamespace(camera=SimpleNamespace(gimbal={
        'enabled': True, 'angle_range_deg': {'pitch': [-135, 45], 'yaw': [-180, 180], 'roll': [-45, 45]}}))
    ticks = [0]
    def sleep(_):
        ticks[0] += 1
        clock[0] += 100_000_000
        stamp = clock[0]
        if ticks[0] == 1:
            reader.add('COMMAND_ACK', stamp=stamp, command=1001, result=0, target_system=255, target_component=190)
            reader.add('GIMBAL_MANAGER_STATUS', stamp=stamp, gimbal_device_id=1, primary_control_sysid=255, primary_control_compid=190)
        elif ticks[0] == 2:
            reader.add('COMMAND_ACK', stamp=stamp, command=1000, result=0, target_system=255, target_component=190)
            reader.add('GIMBAL_DEVICE_ATTITUDE_STATUS', stamp=stamp, gimbal_device_id=2, q=[1, 0, 0, 0], flags=0)
            reader.add('GIMBAL_DEVICE_ATTITUDE_STATUS', stamp=stamp, gimbal_device_id=1, q=[1, 0, 0, 0], flags=0, system=2)
        elif ticks[0] == 3:
            reader.add('GIMBAL_DEVICE_ATTITUDE_STATUS', stamp=stamp, gimbal_device_id=1, q=[1, 0, 0, 0], flags=0)
    result = _execute_managed_gimbal_action(ActionPrimitive.gimbal(pitch_deg=0), session=session,
        profile=profile, reader=reader, check=lambda: None, clock=lambda: clock[0] / 1e9, sleep=sleep)
    assert result.status == 'success' and ticks[0] == 3
