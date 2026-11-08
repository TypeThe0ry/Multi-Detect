from __future__ import annotations

import signal

import pytest

from scripts.collect_sim_teacher_episodes import _TerminationRequested, _raise_on_termination


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_collector_termination_signal_becomes_cleanup_exception(signum):
    with pytest.raises(_TerminationRequested, match=rf"signal {int(signum)}"):
        _raise_on_termination(signum, None)
