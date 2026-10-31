"""Host checks that a compiled plan agrees with the task text (any planning model)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.mission_planner import compile_mission_plan
from multidetect.autonomy.plan_intent import (
    check_plan_intent,
    landing_intent,
    place_grounded,
    query_grounding,
    return_home_intent,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plan_intent_replay.json"
GATE_REFS = (
    "poi:gate (大门 / gate)",
    "zone:people (人员区域 / people area)",
    "route:patrol (巡逻路线 / patrol route)",
    "zone:site (场地 / the site)",
)
V3_REFS = (
    "poi:gate (大门 / the gate)",
    "poi:pad (起降坪 / the landing pad)",
    "poi:tower (水塔 / the water tower)",
    "poi:shed (仓库 / the shed)",
    "poi:bridge (桥 / the bridge)",
    "zone:yard (院子 / the yard)",
)


def _plan(*steps):
    """Linear plan document from (skill, params) pairs."""
    out = []
    for index, (skill, params) in enumerate(steps):
        nxt = f"s{index + 2}" if index + 1 < len(steps) else None
        out.append(
            {
                "id": f"s{index + 1}",
                "skill": skill,
                "params": params,
                "on_success": nxt,
                "on_fail": nxt,
            }
        )
    return {"plan_version": 1, "goal": "task", "steps": out, "report": []}


def _codes(plan, text, refs=GATE_REFS, blocking=True):
    return [f.code for f in check_plan_intent(plan, text, refs) if f.blocking or not blocking]


@pytest.mark.parametrize(
    "text",
    [
        "到大门看一下，然后返航。",
        "到大门检查位置，然后返回起飞前指定的 home。",
        "去大门，然后回到起飞点。",
        "飞到大门上空，然后回 home。",
        "围栏那条路线去溜一圈，走完返场。",
        "别接着干了，直接回咱们放飞的位置。",
        "先往大门去，转一圈查3个角度，然后回来。",
        "Fly to the gate and come back home.",
        "Go to the gate, check the position, then return to the home set before takeoff.",
        "Reposition to the gate, hold that position, and bring it back.",
        "Pick out the excavator and wrap up back at launch.",
        "We are finished; bring the aircraft straight back to where it took off.",
        "Count the cones, then return.",
    ],
)
def test_return_home_intent_is_recognised(text):
    assert return_home_intent(text)


@pytest.mark.parametrize(
    "text",
    [
        "到大门，然后在大门降落。",  # landing requested, no return phrase
        "不用返航，到大门悬停。",  # negated
        "Do not come back, hover over the gate.",
        "Return to the gate and hover there.",  # a named place, not home
        "Head back to the shed.",
        "飞到起降坪上方悬停。",  # 起降坪 is a named pad, not home
        "找回来那辆车的位置。",
    ],
)
def test_return_home_intent_is_conservative(text):
    assert not return_home_intent(text)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("到大门，然后在大门降落。", True),
        ("找到后在原地降落。", True),
        ("收工落起降坪。", True),
        ("Finish with a landing.", True),
        ("Put the aircraft down at its current position.", True),
        ("Move over the pad, then touch down on the pad.", True),
        ("Fly to the landing pad and come back.", False),
        ("不要降落，返航。", False),
        ("到大门看一下，然后返航。", False),
    ],
)
def test_landing_intent(text, expected):
    assert bool(landing_intent(text)) is expected


def test_return_request_rejects_land_away_from_home():
    goto_land = _plan(("goto", {"target_ref": "poi:gate"}), ("land", {}))
    assert _codes(goto_land, "到大门看一下，然后返航。") == ["return_intent_land"]
    # The same plan is right when the task asks to land there.
    assert _codes(goto_land, "到大门，然后在大门降落。") == []
    # Return and land both requested: ambiguous, not judged.
    assert _codes(goto_land, "到大门看一下，返航后降落。") == []
    # Landing at home is a return.
    home_land = _plan(("goto", {"target_ref": "poi:gate"}), ("land", {"target_ref": "home"}))
    assert _codes(home_land, "到大门看一下，然后返航。") == []


def test_return_request_checks_failure_branches_too():
    plan = {
        "plan_version": 1,
        "goal": "g",
        "steps": [
            {
                "id": "s1",
                "skill": "goto",
                "params": {"target_ref": "poi:gate"},
                "on_success": "s2",
                "on_fail": "s3",
            },
            {"id": "s2", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
            {"id": "s3", "skill": "land", "params": {}, "on_success": None, "on_fail": None},
        ],
        "report": [],
    }
    assert _codes(plan, "Fly to the gate and come back home.") == ["return_intent_land"]


@pytest.mark.parametrize(
    "query, text, verdict",
    [
        ("traffic cone", "数一数场地里的交通锥，然后返航。", "grounded"),
        (
            "orange traffic cone",
            "The orange cone is already visible: preserve its image.",
            "grounded",
        ),
        ("electric bike", "场地里的电瓶车给我报个总数。", "grounded"),
        ("person", "沿巡逻路线巡一圈找人。", "grounded"),
        ("person", "Count the people at the gate.", "grounded"),
        ("bicycle", "Patrol the road looking for cyclists.", "grounded"),
        ("suv", "Find the SUV, record its position, and land.", "grounded"),
        ("green garbage bag", "搜搜有没有绿色垃圾桶。", "grounded"),
        ("truck", "到大门检查位置，然后返回起飞前指定的 home。", "ungrounded"),
        ("red car", "到大门检查一下，再返回 home 点。", "ungrounded"),
        ("car", "Go to the gate, check the position, then return home.", "ungrounded"),
        ("none", "Go to the gate, check the position, then return home.", "ungrounded"),
        ("red car", "飞到仓库后数一数里面的木托盘，再回到起飞点", "ungrounded"),
        ("ambulance", "检查大门，不要凭空检查救护车。", "ungrounded"),
        ("fire truck", "Inspect the gate; do not invent a fire truck target.", "ungrounded"),
        ("blue car", "找红车，不是蓝车。", "ungrounded"),
        ("red car", "找红车，不是蓝车。", "grounded"),
        ("blue car", "Find the red car, not a blue car.", "ungrounded"),
        ("red car", "Find the red car, not a blue car.", "grounded"),
        ("ambulance", "检查大门，并检查救护车。", "grounded"),
        ("fire truck", "Inspect the gate and inspect a fire truck.", "grounded"),
        # An object outside the tables is never blocked in a Chinese task.
        ("goose", "院里散着几只鹅？给个准数。", "undecided"),
        ("cracked flowerpot", "院内有个裂开的花盆，帮我定位。", "undecided"),
    ],
)
def test_query_grounding(query, text, verdict):
    assert query_grounding(query, text) == verdict


def test_invented_object_step_blocks_but_requested_watch_only_warns():
    invented = _plan(
        ("goto", {"target_ref": "poi:gate"}),
        ("area_search", {"region": "zone:all", "query": "truck"}),
        ("return_home", {}),
    )
    text = "到大门检查位置，然后返回起飞前指定的 home。"
    assert _codes(invented, text) == ["ungrounded_query"]
    # patrol must carry a query; a requested patrol with nothing named only warns.
    patrol = _plan(
        ("patrol", {"route_ref": "route:patrol", "query": "person", "loops": 1}),
        ("return_home", {}),
    )
    assert _codes(patrol, "沿巡逻路线巡一圈，然后返航。") == []
    assert _codes(patrol, "沿巡逻路线巡一圈，然后返航。", blocking=False) == ["ungrounded_query"]
    # ... but a monitor step the task never asked for blocks.
    monitor = _plan(
        ("monitor", {"region": "zone:site", "query": "dog", "duration_s": 60}), ("return_home", {})
    )
    assert _codes(monitor, "别接着干了，直接回咱们放飞的位置。") == ["ungrounded_query"]


def test_negated_query_step_is_blocked_in_chinese_and_english():
    for query, text in (
        ("ambulance", "检查大门，不要凭空检查救护车。"),
        ("fire truck", "Inspect the gate; do not invent a fire truck target."),
        ("fire truck", "Inspect the gate, don't inspect a fire truck."),
        ("fire truck", "Inspect the gate; never invent a fire truck target."),
        ("fire truck", "Inspect the gate, not a fire truck."),
    ):
        plan = _plan(
            (
                "inspect",
                {"target_ref": "poi:gate", "query": query, "views": 2, "standoff_m": 8},
            ),
            ("return_home", {}),
        )
        assert _codes(plan, text) == ["ungrounded_query"]


@pytest.mark.parametrize(
    "text, ref",
    [
        (
            "Touch down at Raven Pier, which is absent from the known places. "
            "Do not substitute the bridge or pad.",
            "poi:bridge",
        ),
        ("去未登记的旧气象站落地，不是水塔也不是仓库。", "poi:tower"),
        ("飞到地图上没标出的青石码头，到了再回来；不要拿别的地点顶替。", "poi:bridge"),
        ("Take the aircraft to the unregistered East Pump House, then bring it back.", "poi:shed"),
    ],
)
def test_unknown_place_mapped_onto_a_known_ref_is_rejected(text, ref):
    for plan in (
        _plan(("goto", {"target_ref": ref}), ("return_home", {})),
        _plan(
            ("land", {"target_ref": ref}),
        ),
    ):
        assert "ungrounded_place" in _codes(plan, text, V3_REFS)


@pytest.mark.parametrize(
    "text, ref",
    [
        ("飞到门口悬停。", "poi:gate"),  # head noun 门 of 大门
        ("先到桥头就位。", "poi:bridge"),
        ("到桥那儿稳住别动，待四十五秒。", "poi:bridge"),  # 别 negates 动, not the place
        ("Fly to the tower.", "poi:tower"),
        ("Touch down on the pad.", "poi:pad"),
    ],
)
def test_named_places_are_grounded(text, ref):
    labels = {r.split(" ", 1)[0]: r.split(" ", 1)[1].strip("()").split(" / ") for r in V3_REFS}
    assert place_grounded(ref, labels[ref], text)


@pytest.mark.parametrize(
    "text", ["飞到入口悬停。", "到门口看一下。", "Fly to the entrance and hold."]
)
def test_builtin_gate_aliases_ground_place(text):
    plan = _plan(("goto", {"target_ref": "poi:gate"}))
    assert not [
        f for f in check_plan_intent(plan, text, ("poi:gate (大门 / gate)",)) if f.blocking
    ]


def test_builtin_alias_conflict_is_nonblocking_and_disabled():
    plan = _plan(("goto", {"target_ref": "poi:gate"}))
    refs = ("poi:gate (大门 / gate)", "poi:entrance (入口 / entrance)")
    findings = check_plan_intent(plan, "到入口悬停。", refs)
    assert any(f.code == "place_alias_conflict" and not f.blocking for f in findings)
    assert any(f.code == "ungrounded_place" and f.blocking for f in findings)


def test_slash_separated_aliases_are_prompt_and_intent_compatible():
    from scripts.onboard_policy import labelled_refs

    refs = {"poi:gate": [40.0, 0.0]}
    labels = {"poi:gate": "门 / 大门 / 入口 / entrance / gate"}
    rendered = labelled_refs(refs, labels)
    assert rendered == ("poi:gate (门 / 大门 / 入口 / entrance / gate)",)
    assert not [
        f
        for f in check_plan_intent(
            _plan(("goto", {"target_ref": "poi:gate"})), "飞到入口", rendered
        )
        if f.blocking
    ]


def _scripted(*documents):
    responses = iter(json.dumps(doc) for doc in documents)
    prompts = []

    def post(prompt, schema):
        prompts.append(prompt)
        return next(responses)

    return post, prompts


def test_return_violation_is_fed_back_then_accepted():
    bad = _plan(("goto", {"target_ref": "poi:gate"}), ("land", {}))
    good = _plan(("goto", {"target_ref": "poi:gate"}), ("return_home", {}))
    post, prompts = _scripted(bad, good)
    plan, report = compile_mission_plan(post, "到大门看一下，然后返航。", refs=GATE_REFS)
    assert [s.skill for s in plan.steps] == ["goto", "return_home"]
    assert "task asks to return home" in prompts[1]
    assert report["error"] is None and report["repairs"] == []


def test_last_attempt_repairs_land_to_return_home():
    bad = _plan(("goto", {"target_ref": "poi:gate"}), ("land", {}))
    post, _ = _scripted(bad, bad)
    plan, report = compile_mission_plan(post, "到大门看一下，然后返航。", refs=GATE_REFS)
    assert [s.skill for s in plan.steps] == ["goto", "return_home"]
    assert any("land -> return_home" in note for note in report["repairs"])
    assert report["error"] is None


def test_last_attempt_drops_the_invented_object_step():
    bad = _plan(
        ("goto", {"target_ref": "poi:gate"}),
        ("inspect", {"target_ref": "finding:1", "query": "truck", "views": 4, "standoff_m": 12}),
        ("return_home", {}),
    )
    post, prompts = _scripted(bad, bad)
    plan, report = compile_mission_plan(post, "到大门看一下，然后返航。", refs=GATE_REFS)
    assert "query 'truck' is not in the task" in prompts[1]
    assert [s.skill for s in plan.steps] == ["goto", "return_home"]
    assert plan.steps[0].on_success == plan.steps[0].on_fail == plan.steps[1].id
    assert any("dropped inspect" in note for note in report["repairs"])


def test_ungrounded_place_is_never_repaired():
    bad = _plan(("goto", {"target_ref": "poi:bridge"}), ("land", {"target_ref": "poi:bridge"}))
    post, _ = _scripted(bad, bad)
    plan, report = compile_mission_plan(
        post,
        "Touch down at Raven Pier, which is absent from the known places. "
        "Do not substitute the bridge or pad.",
        refs=V3_REFS,
    )
    assert plan is None
    assert "task intent" in report["error"]
    assert {v["code"] for v in report["intent_violations"]} == {"ungrounded_place"}


def test_unknown_place_substituted_on_last_attempt_is_still_rejected():
    # The only known poi would be substituted for poi:raven_pier; the task never names it.
    bad = _plan(("goto", {"target_ref": "poi:raven_pier"}), ("land", {}))
    post, prompts = _scripted(bad, bad)
    plan, report = compile_mission_plan(post, "Land at Raven Pier.", refs=GATE_REFS)
    assert "unknown reference(s) poi:raven_pier" in prompts[1]
    assert plan is None and report["intent_violations"][0]["code"] == "ungrounded_place"


def test_named_place_is_rejected_when_no_places_are_known():
    bad = _plan(
        ("land", {"target_ref": "poi:up"}),
    )
    post, prompts = _scripted(bad, bad)
    plan, report = compile_mission_plan(post, "Find the SUV and land.")
    assert plan is None
    assert "unknown reference(s) poi:up; use only: zone:all, home" in prompts[1]


def test_intent_check_can_be_disabled():
    bad = _plan(("goto", {"target_ref": "poi:gate"}), ("land", {}))
    post, _ = _scripted(bad)
    plan, report = compile_mission_plan(
        post, "到大门看一下，然后返航。", refs=GATE_REFS, attempts=1, check_intent=False
    )
    assert [s.skill for s in plan.steps] == ["goto", "land"]


def test_warning_is_reported_without_blocking():
    patrol = _plan(
        ("patrol", {"route_ref": "route:patrol", "query": "person", "loops": 1}),
        ("return_home", {}),
    )
    post, _ = _scripted(patrol)
    plan, report = compile_mission_plan(post, "沿巡逻路线巡一圈，然后返航。", refs=GATE_REFS)
    assert plan is not None
    assert report["intent_warnings"][0]["code"] == "ungrounded_query"


def test_replay_of_real_r14_r19_plans():
    """r19 goto->land and r14 invented objects are caught; no correct plan is blocked."""
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    assert len(cases) == 52
    for case in cases:
        plan = {"plan_version": 1, "goal": "g", "steps": case["steps"], "report": []}
        codes = sorted(
            {f.code for f in check_plan_intent(plan, case["text"], case["refs"]) if f.blocking}
        )
        expected = [case["expect"]] if case["expect"] else []
        assert codes == expected, (case["source"], case["id"], codes)


def test_replay_of_planning_v3_expected_plans_has_no_findings():
    root = Path(__file__).resolve().parents[1]
    document = json.loads(
        (root / "configs/onboard/plan-eval-tasks-v3.json").read_text(encoding="utf-8")
    )
    checked = 0
    for task in document["tasks"]:
        if not task.get("expected_steps"):
            continue
        plan = validate_mission_plan(
            _plan(*[(s["skill"], s.get("params") or {}) for s in task["expected_steps"]])
        )
        refs = task.get("refs", document["refs"])
        assert check_plan_intent(plan, task["text"], refs) == [], task["text"]
        checked += 1
    assert checked == 72


def test_distant_chinese_negation_does_not_unground_the_target():
    from multidetect.autonomy.plan_intent import query_grounding

    assert query_grounding("red car", "不要在路口停留太久直接去停车场找红车") == "grounded"
    assert query_grounding("ambulance", "检查大门，不要凭空检查救护车") != "grounded"
