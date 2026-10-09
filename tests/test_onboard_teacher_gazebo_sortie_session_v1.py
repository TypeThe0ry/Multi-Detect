"""CPU constructor/cleanup timeout seams; NO Docker/native execution or flight."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_onboard_teacher_sim_operator as helpers
from test_onboard_teacher_sortie_budget_v1 import budget as budget_fixture

from multidetect.clock import HighResolutionSystemClock
from scripts.onboard_teacher_gazebo_sortie_session_v1 import OwnedTeacherGazeboSessionV9

budget = budget_fixture
template = helpers.template
plan = helpers.plan


def make_session(plan, budget, **options):
    clock = HighResolutionSystemClock()
    budget = replace(budget, clock_id=clock.session_id, started_at_s=clock.monotonic())
    plan = replace(plan, session_timeout_s=budget.duration_s)
    session = OwnedTeacherGazeboSessionV9(plan, budget=budget, clock=clock, **options)
    return session, budget


def test_session_binding_does_not_start_or_renew(plan, budget):
    session, budget = make_session(plan, budget)
    before = session._remaining()
    assert 0 < before <= 235
    assert session.container_id is None and not session._entered
    assert session.to_mapping()["original_budget_sha256"] == budget.canonical_hash
    assert session._remaining() <= before and session.budget.started_at_s == budget.started_at_s


def test_cleanup_uses_original_remaining_not_new15secondlease(plan, budget):
    calls = []

    def runner(argv, timeout_s):
        calls.append((argv, timeout_s))
        return SimpleNamespace(returncode=0, stdout="CPU-not-native", stderr="")

    session, budget = make_session(plan, budget, runner=runner)
    # This bounded callback is a cleanup scheduling seam, not real Docker.
    session._remaining = lambda: 2.5
    session._run(["inspect", "CPU-not-container"], cleanup=True)
    assert calls[0][1] == 2.5
    assert session.budget.deadline_s == budget.deadline_s


def test_old_relative_deadline_cannot_extend_originalbudget(plan, budget):
    session, budget = make_session(plan, budget)
    session.deadline_s = budget.deadline_s + 10000
    assert session._remaining() <= budget.duration_s
    with pytest.raises(ValueError, match="queue capacity"):
        session.start_receiver(queue_capacity=256)


def test_missing_or_foreign_initialclock_refused(plan, budget):
    with pytest.raises(ValueError, match="clock"):
        OwnedTeacherGazeboSessionV9(plan, budget=budget)
    with pytest.raises(ValueError, match="duration"):
        clock = HighResolutionSystemClock()
        OwnedTeacherGazeboSessionV9(
            plan, budget=replace(budget, clock_id=clock.session_id), clock=clock
        )
