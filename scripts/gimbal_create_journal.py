"""Candidate-only durable Docker request evidence; uncertainty is never rejection.

A request file is fsynced before invoking Docker; a response file is written only
for a successful response containing a full ID. A missing response requires HOLD.
The journal directory must be created and protected by the supervising root unit.
"""
import json
import os
import re
import stat
import uuid
from pathlib import Path


def _directory():
    path = Path(os.environ['MD_GIMBAL_JOURNAL'])
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    st = os.fstat(fd)
    if os.geteuid() != 0 or st.st_uid != 0 or st.st_mode & 0o077:
        os.close(fd)
        raise ValueError('unsafe candidate journal')
    return fd


def _record(dfd, name, payload):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
    try:
        data = (json.dumps(payload, sort_keys=True) + '\n').encode()
        with os.fdopen(fd, 'wb', closefd=False) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    finally:
        os.close(fd)
    os.fsync(dfd)


def run_recorded(command, *, name, run):
    invocation = os.environ['MD_GIMBAL_INVOCATION']
    if not re.fullmatch(r'[0-9a-f]{32}', invocation) or not name.startswith('md-sim-teacher-slot-0-'):
        raise ValueError('invalid candidate identity')
    if len(command) < 3 or command[1] != 'run' or command.count('-d') != 1 or '--rm' in command:
        raise ValueError('unexpected Docker command')
    if command.count('--name') != 1 or command[command.index('--name') + 1] != name:
        raise ValueError('container name mismatch')
    if command.count('--memory=64g') != 1 or command.count('--memory-swap=64g') != 1:
        raise ValueError('missing candidate Docker memory limit')
    if command.count('md.gimbal.invocation=' + invocation) != 1:
        raise ValueError('missing invocation label')
    request = uuid.uuid4().hex
    dfd = _directory()
    try:
        _record(dfd, request + '.request.json', {'request': request, 'invocation': invocation, 'name': name, 'pid': os.getpid()})
        result = run(command, capture_output=True, text=True, timeout=30, check=False)
        cid = result.stdout.strip()
        if result.returncode != 0 or re.fullmatch(r'[0-9a-f]{64}', cid) is None:
            raise RuntimeError('Docker response uncertain; retain locks')
        _record(dfd, request + '.response.json', {'request': request, 'invocation': invocation, 'id': cid})
        return cid
    finally:
        os.close(dfd)


def verify_owned(cid, *, run, executable):
    invocation = os.environ['MD_GIMBAL_INVOCATION']
    if not re.fullmatch(r'[0-9a-f]{64}', cid):
        raise ValueError('invalid full container ID')
    result = run([executable, 'inspect', cid], capture_output=True, text=True, timeout=15, check=False)
    if result.returncode != 0:
        raise ValueError('container identity unknown')
    rows = json.loads(result.stdout)
    if len(rows) != 1 or rows[0].get('Id') != cid or (rows[0].get('Config', {}).get('Labels') or {}).get('md.gimbal.invocation') != invocation or not rows[0].get('Name', '').lstrip('/').startswith('md-sim-teacher-slot-0-'):
        raise ValueError('container ownership mismatch')
    return cid


def record_removed(cid):
    invocation = os.environ['MD_GIMBAL_INVOCATION']
    if not re.fullmatch(r'[0-9a-f]{64}', cid) or not re.fullmatch(r'[0-9a-f]{32}', invocation):
        raise ValueError('invalid removal identity')
    dfd = _directory()
    try:
        _record(dfd, cid + '.removed.json', {'invocation': invocation, 'id': cid, 'response_received': True})
    finally:
        os.close(dfd)
