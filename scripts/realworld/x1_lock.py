"""Shared X1 flock and crash-safe flight priority for the world builder."""
from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path

LOCK = Path("/run/lock/md-x1-resume.lock")
FLIGHT_WAIT = 120
POLL_SECONDS = 0.25
TEMPFAIL = 75


class LockBusy(BlockingIOError):
    """Another process holds the X1 lock."""


class Yielded(RuntimeError):
    """The world worker must leave its current replayable stage."""


def sidecar(lock, suffix):
    return lock.with_name(f"{lock.name}.{suffix}")


def try_lock(stream, mode=fcntl.LOCK_EX):
    try:
        fcntl.flock(stream, mode | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def held(path):
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        return False
    with stream:
        return not try_lock(stream, fcntl.LOCK_SH)


def flight_requested(lock):
    # The request is an exclusive flock, not the existence of the file. Process exit
    # drops the request automatically. Never unlink any of these lock files.
    return held(sidecar(lock, "flight-request"))


@contextmanager
def world_owner(lock):
    with sidecar(lock, "world-owner").open("a") as owner:
        if not try_lock(owner):
            raise LockBusy(f"X1 world worker already running: {lock}")
        yield


@contextmanager
def world_phase(lock):
    if flight_requested(lock):
        raise Yielded("flight requested the X1 lock")
    with lock.open("a") as stream:
        if not try_lock(stream) or flight_requested(lock):
            raise Yielded("X1 lock reserved for flight")
        yield


@contextmanager
def flight_lock(lock, *, wait=FLIGHT_WAIT, shared=False):
    """Wait for a cooperative world worker, with a bounded nonblocking poll."""
    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    with lock.open("a") as stream:
        if not try_lock(stream, mode):
            if not held(sidecar(lock, "world-owner")):
                # Close the race where the world worker died during contention.
                if not try_lock(stream, mode):
                    raise LockBusy(f"X1 lock held: {lock}")
            else:
                deadline = time.monotonic() + wait
                with sidecar(lock, "flight-request").open("a") as request:
                    # Concurrent observers can probe with shared locks. Only a
                    # waiting flight holds an exclusive request.
                    while True:
                        if try_lock(request) and try_lock(stream, mode):
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise LockBusy(f"timed out after {wait:g}s waiting for X1 lock: {lock}")
                        time.sleep(min(remaining, POLL_SECONDS))
        yield


@contextmanager
def flight_lane(lock, lane):
    """Share the flight lock across lanes, with one worker per lane."""
    with sidecar(lock, f"flight-{lane}").open("a") as owner:
        if not try_lock(owner):
            raise LockBusy(f"X1 flight lane {lane} already running: {lock}")
        with flight_lock(lock, shared=True):
            yield
