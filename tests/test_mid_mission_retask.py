import copy
import json
import time

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_plan import validate_mission_plan

from test_mission_supervisor import build, document, event


def alternate_plan():
    return validate_mission_plan({
        "plan_version": 1,
        "goal": "count parking lot cars",
        "report": ["counts"],
        "steps": [
            {"id": "cars", "skill": "count", "params": {"region": "zone:all", "query": "cars"}, "on_success": "return", "on_fail": "return"},
            {"id": "return", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
        ],
    })


def candidate_supervisor(tmp_path, plan=None):
    doc = document()
    doc["plan"] = {
        "plan_version": 1,
        "goal": "count parking lot cars",
        "report": ["counts"],
        "steps": [
            {"id": "cars", "skill": "count", "params": {"region": "zone:all", "query": "cars"}, "on_success": "return", "on_fail": "return"},
            {"id": "return", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
        ],
    }
    pkg = load_mission_package(doc)
    sup, _, _ = build(tmp_path, doc=copy.deepcopy(document()))
    from multidetect.autonomy.mission_executor import MissionExecutor
    from multidetect.autonomy.mission_supervisor import MissionSupervisor
    executor = MissionExecutor(plan or pkg.plan, loop_factory=sup.executor.loop_factory, refs=pkg.refs, config=sup.executor.config)
    return MissionSupervisor(executor, pkg, log=sup.log, clock=sup.clock, requires_approval=False)


def test_replace_is_queued_until_current_step_finishes_and_preserves_results(tmp_path):
    sup, _, log = build(tmp_path)
    sup.executor.counts["gate"] = 3
    sup.executor.findings.append({"step": "gate", "query": "cone", "position_ne": (1.0, 2.0)})
    sup.executor.alerts.append("gate:alert:cone@1.0,2.0")
    candidate = candidate_supervisor(tmp_path / "candidate")
    assert sup.request_retask(candidate, mode="replace")
    assert sup.executor.current_step.id == "gate"
    assert sup.executor.counts["gate"] == 3
    assert len(sup.executor.findings) == 1
    assert sup.executor.alerts == ["gate:alert:cone@1.0,2.0"]
    assert sup.executor.plan_version == 1
    assert sup.plan_history[-1]["version"] == 2


def test_append_is_queued_after_current_graph(tmp_path):
    sup, _, _ = build(tmp_path)
    assert sup.request_retask(candidate_supervisor(tmp_path / "candidate"), mode="append")
    assert sup.executor._pending_retask[1] == "append"


def test_planning_timeout_keeps_original_plan(tmp_path):
    sup, _, log = build(tmp_path)
    original = sup.executor.plan
    assert not sup.request_retask(None, planner=lambda: time.sleep(0.05), timeout_s=0.001)
    assert sup.executor.plan is original
    assert "改派规划超时" in log.path.read_text(encoding="utf-8")


def test_budget_shortfall_and_envelope_expansion_reject(tmp_path):
    sup, _, log = build(tmp_path)
    sup.update_estimates(remaining_energy_wh=1)
    assert not sup.request_retask(candidate_supervisor(tmp_path / "low"))
    expanded = document()
    expanded["operating_area"]["north_max_m"] += 20
    candidate = candidate_supervisor(tmp_path / "wide")
    candidate.package = load_mission_package(expanded)
    assert not sup.request_retask(candidate)
    assert "剩余能量不足" in log.path.read_text(encoding="utf-8")
    assert "包络" in log.path.read_text(encoding="utf-8")


def test_abort_and_pause_remain_effective_during_retask(tmp_path):
    sup, _, _ = build(tmp_path)
    assert sup.request_retask(candidate_supervisor(tmp_path / "candidate"))
    assert event(sup, "pause")
    assert sup.evaluate() == "hold"
    assert event(sup, "abort")
    assert sup.evaluate() == "return_now"


def test_retask_approval_gate_and_final_summary_history(tmp_path):
    sup, _, log = build(tmp_path)
    candidate = candidate_supervisor(tmp_path / "candidate")
    assert sup.request_retask(candidate, requires_approval=True)
    pending = sup._pending_retask_approval
    assert pending and pending[4]
    assert sup.executor.plan_version == 1
    sup.finish("stopped")
    summary = json.loads(log.summary_path.read_text(encoding="utf-8"))["summary"]
    assert summary["plan_history"][0]["version"] == 1


def test_retask_approval_timeout_rejects_without_switching(tmp_path):
    sup, _, log = build(tmp_path)
    assert sup.request_retask(
        candidate_supervisor(tmp_path / "candidate"),
        requires_approval=True,
        approval_timeout_s=0.01,
    )
    time.sleep(0.03)
    assert sup._pending_retask_approval is None
    assert sup.executor.plan_version == 1
    assert "改派审批超时" in log.path.read_text(encoding="utf-8")
