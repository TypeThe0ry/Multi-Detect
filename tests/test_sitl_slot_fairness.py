from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

import scripts.sitl_lock as lock_module
from scripts.sitl_lock import SitlLock


def make_lock(root, *, slots=3, slot=None, priority=0):
    return SitlLock(root, slots=slots, slot=slot, priority=priority, retry_s=0,
                    runner=lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout=''),
                    memory_fn=lambda: 100.0)


def test_noncompeting_general_queue_does_not_block_free_delta_slot(tmp_path):
    root = tmp_path / 'global'
    holders = [make_lock(root, slots=3, slot=i) for i in range(3)]
    earlier = make_lock(root, slots=3)
    delta = make_lock(root, slots=5, slot=4)
    try:
        for lock in holders:
            lock.acquire(wait=False)
        earlier._make_ticket()
        delta.acquire(wait=False)
        assert delta.acquired and delta.slot == 4
        assert earlier._ticket.exists()
        assert all(lock.acquired for lock in holders)
    finally:
        delta.release()
        for lock in holders:
            lock.release()
        if earlier._ticket is not None:
            earlier._ticket.unlink(missing_ok=True)


def test_overlapping_ticket_cannot_be_overtaken_even_with_two_free_slots(tmp_path):
    root = tmp_path / 'global'
    earlier = make_lock(root, slots=3, slot=0)
    later = make_lock(root, slots=3, slot=0)
    earlier._make_ticket()
    try:
        with pytest.raises(RuntimeError, match='SITL slots busy'):
            later.acquire(wait=False)
        earlier.acquire(wait=False)
        assert earlier.slot == 0
    finally:
        earlier.release()
        later.release()


def test_general_request_uses_a_slot_disjoint_from_earlier_fixed_request(tmp_path):
    root = tmp_path / 'global'
    earlier = make_lock(root, slots=3, slot=0)
    later = make_lock(root, slots=3)
    earlier._make_ticket()
    try:
        later.acquire(wait=False)
        assert later.slot in (1, 2)
        earlier.acquire(wait=False)
        assert earlier.slot == 0
    finally:
        later.release()
        earlier.release()


@pytest.mark.parametrize('legacy_text', [
    'old {pid} 2026-11-08T00:00:00+00:00\n',
    'old {pid} 2026-11-08T00:00:00+00:00\nnot-json\n',
    'old {pid} 2026-11-08T00:00:00+00:00\n{{"sitl_slots":3}}\n',
])
def test_legacy_or_malformed_scope_does_not_get_skipped(tmp_path, legacy_text):
    root = tmp_path / 'global'
    queue = tmp_path / 'global.queue'
    queue.mkdir()
    legacy = queue / 'p000000000000-00000000000000000000-legacy'
    legacy.write_text(legacy_text.format(pid=os.getpid()), encoding='utf-8')
    later = make_lock(root, slots=5, slot=4)
    with pytest.raises(RuntimeError, match='SITL slots busy'):
        later.acquire(wait=False)
    assert legacy.exists()
    assert not (tmp_path / 'global.slot-4').exists()


def test_priority_still_wins_for_overlapping_requests(tmp_path):
    root = tmp_path / 'global'
    low = make_lock(root, slots=5, slot=4, priority=0)
    high = make_lock(root, slots=5, slot=4, priority=100)
    low._make_ticket()
    high._make_ticket()
    try:
        with pytest.raises(RuntimeError, match='SITL slots busy'):
            low.acquire(wait=False)
        high.acquire(wait=False)
        assert high.slot == 4
    finally:
        low.release()
        high.release()


def test_dead_scoped_ticket_is_reclaimed_after_grace(tmp_path, monkeypatch):
    root = tmp_path / 'global'
    earlier = make_lock(root, slots=3, slot=0)
    ticket = earlier._make_ticket()
    ticket.write_text('dead 424242 2026-01-01T00:00:00+00:00\n'
                      + json.dumps({'sitl_slots': 3, 'sitl_slot': 0}) + '\n')
    old = time.time() - 121
    os.utime(ticket, (old, old))
    monkeypatch.setattr(lock_module, '_process_snapshot',
                        lambda pid: (False, None) if pid == 424242 else (True, 123.0))
    later = make_lock(root, slots=3, slot=0)
    try:
        later.acquire(wait=False)
        assert later.acquired and not ticket.exists()
    finally:
        later.release()


def test_overlapping_general_requests_keep_fifo_and_retry_after_nonblocking_failure(tmp_path):
    root = tmp_path / 'global'
    first = make_lock(root, slots=3)
    second = make_lock(root, slots=3)
    first._make_ticket()
    try:
        with pytest.raises(RuntimeError, match='SITL slots busy'):
            second.acquire(wait=False)
        assert second._ticket is None
        first.acquire(wait=False)
        second.acquire(wait=False)
        assert {first.slot, second.slot} == {0, 1}
    finally:
        second.release()
        first.release()


def test_lost_mkdir_race_preserves_winner_and_discards_loser_ticket(tmp_path, monkeypatch):
    root = tmp_path / 'global'
    lock = make_lock(root, slots=3, slot=0)
    original = lock._reclaim_stale

    def winner_claimed_slot():
        lock.path.mkdir()
        lock.owner.write_text('winner must survive\n', encoding='utf-8')

    monkeypatch.setattr(lock, '_reclaim_stale', winner_claimed_slot)
    with pytest.raises(RuntimeError, match='SITL slots busy'):
        lock.acquire(wait=False)
    assert lock.owner.read_text() == 'winner must survive\n'
    assert lock._ticket is None
    assert not list(lock._queue_path.iterdir())
    lock.owner.unlink()
    lock.path.rmdir()
    monkeypatch.setattr(lock, '_reclaim_stale', original)
    lock.acquire(wait=False)
    lock.release()
