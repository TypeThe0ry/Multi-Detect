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
    def add(self, name, stamp=100, **kwargs): self.events.append((name, Message(**kwargs), stamp))


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
        reader.state.vehicle_time_boot_ms = 2001
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
