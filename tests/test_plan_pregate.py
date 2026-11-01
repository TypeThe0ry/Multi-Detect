from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from multidetect.autonomy.mission_plan import PlanDecision
from multidetect.autonomy.plan_pregate import preflight_plan_task
from scripts.onboard_policy import build_mission_from_text
from scripts.plan_from_task import compile_task

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from audit_plan_pregate_training import audit_training_file  # noqa: E402


REFS = ("poi:gate (大门 / gate)", "poi:pad (起降坪 / landing pad / pad)")


def test_payload_delivery_requires_action_object_and_missing_capability():
    result = preflight_plan_task("Deliver the parcel to the gate.", REFS)
    assert isinstance(result, PlanDecision)
    assert result.reason_code == "missing_capability"
    assert result.missing_capabilities == ("payload_drop",)
    assert preflight_plan_task("把样品投递到大门。", REFS).reason_code == "missing_capability"
    assert preflight_plan_task(
        "Deliver the parcel to the gate.", REFS, capabilities={"payload_drop"}
    ) is None


def test_fixed_wing_hover_and_target_landing_are_blocked():
    caps = {"airframe:fixed_wing"}
    assert preflight_plan_task("Have the fixed-wing aircraft hover over the lot.", REFS, caps).missing_capabilities == ("hover",)
    assert preflight_plan_task("固定翼飞到大门上方定点降落。", REFS, caps).missing_capabilities == ("landing_on_target",)


def test_unknown_place_needs_explicit_move_and_map_evidence():
    result = preflight_plan_task("Fly to the nonexistent Raven Pier and return home.", REFS)
    assert result.reason_code == "unknown_place"
    assert result.unresolved_refs == ("nonexistent Raven Pier",)
    assert preflight_plan_task("飞去地图上没有的Raven Pier，找到后再返航。", REFS).reason_code == "unknown_place"
    assert preflight_plan_task("Go to the gate and return home.", REFS) is None
    assert preflight_plan_task("Inspect the gate and return home.", REFS) is None


def test_known_place_parser_accepts_dataset_and_mission_package_ref_shapes():
    text_en = "Go to North Annex to check it and return to the launch point."
    text_zh = "飞到北侧附楼后返航。"
    assert preflight_plan_task(
        text_en, ["poi:north_annex (北侧附楼 / North Annex / North Wing)"]
    ) is None
    assert preflight_plan_task(
        "Go to Cedar Wharf for a look, then come back to base.",
        ["poi:cedar_wharf (Cedar码头 | Cedar Wharf; Cedar Dock)"],
    ) is None
    assert preflight_plan_task(
        text_zh, {"poi:north_annex": ["北侧附楼", "North Annex", "North Wing"]}
    ) is None
    assert preflight_plan_task(
        text_en,
        {"poi:north_annex": {"labels": ["北侧附楼", "North Annex"], "north_m": 1}},
    ) is None


def test_negated_capability_action_is_not_blocked():
    assert preflight_plan_task("Do not deliver the parcel; go to the gate.", REFS) is None
    assert preflight_plan_task("固定翼不要悬停，直接返航。", REFS, {"airframe:fixed_wing"}) is None


def test_builder_pregate_returns_decision_without_calling_model():
    def fail(*_args, **_kwargs):
        raise AssertionError("model should not be called")

    decision, report = build_mission_from_text(
        "Deliver the parcel to the gate.", REFS, fail, enable_plan_pregate=True
    )
    assert decision.reason_code == "missing_capability"
    assert report["source"] == "host_pregate"
    assert report["attempts"] == []


def test_builder_pregate_defaults_off():
    calls = []

    def post(*_args, **_kwargs):
        calls.append(True)
        return json.dumps({
            "plan_version": 1, "goal": "return", "steps": [
                {"id": "home", "skill": "return_home", "params": {},
                 "on_success": None, "on_fail": None}], "report": []
        })

    plan, _ = build_mission_from_text("Deliver the parcel to the gate.", REFS, post)
    assert plan.steps[0].skill == "return_home"
    assert calls


def test_evaluation_compile_task_pregate_skips_model_post():
    def fail(*_args, **_kwargs):
        raise AssertionError("model post should not be called")

    decision, raw, attempts, error = compile_task(
        fail, "Fly to the nonexistent Raven Pier.", REFS, {}, plan_pregate=True
    )
    assert decision.reason_code == "unknown_place"
    assert json.loads(raw)["unresolved_refs"] == ["nonexistent Raven Pier"]
    assert attempts == [0.0]
    assert error is None


def test_pregate_training_audit_has_no_executable_false_positives():
    for version in ("r24", "r27"):
        report = audit_training_file(
            ROOT / f"artifacts/scratch/cx-planner-round/{version}/dataset/train.jsonl"
        )
        assert report["executable_false_positive_count"] == 0
        assert report["unlabelled_annotation_gap_count"] == 133


def test_pregate_eval_hit_counts_and_normal_task_false_positives():
    expected = {
        "plan-eval-tasks-v3.json": 8,
        "plan-eval-tasks-v4.json": 8,
        "plan-eval-tasks-v5.json": 56,
        "plan-probe-tasks-return-home.json": 0,
    }
    for filename, expected_hits in expected.items():
        document = json.loads((ROOT / "configs/onboard" / filename).read_text(encoding="utf-8"))
        refs = document.get("refs", ())
        hits = Counter()
        false_positives = []
        for item in document["tasks"]:
            capabilities = item.get("capabilities", ())
            decision = preflight_plan_task(
                item["text"], item.get("refs", refs), capabilities,
                "airframe:fixed_wing" if "airframe:fixed_wing" in capabilities else None,
            )
            if decision is not None:
                hits[decision.reason_code] += 1
                if not item.get("expect_invalid", False):
                    false_positives.append(item["text"])
        assert sum(hits.values()) == expected_hits, filename
        assert false_positives == [], filename


def test_independent_paraphrase_fixture_has_no_false_positives():
    fixture = json.loads(
        (ROOT / "tests/fixtures/plan_pregate_paraphrases.json").read_text(encoding="utf-8")
    )
    cases = fixture["cases"]
    assert len({item["id"] for item in cases}) == len(cases)
    by_category = Counter(item["category"] for item in cases)
    assert by_category == {
        "missing_payload_drop": 52,
        "fixed_wing_hover": 26,
        "fixed_wing_target_land": 26,
        "unknown_place": 26,
        "legitimate": 64,
    }
    for category in ("missing_payload_drop", "fixed_wing_hover", "fixed_wing_target_land", "unknown_place"):
        expected_language_counts = {"zh": 26, "en": 26} if category == "missing_payload_drop" else {"zh": 13, "en": 13}
        assert Counter(item["language"] for item in cases if item["category"] == category) == expected_language_counts
    assert Counter(item["language"] for item in cases if item["category"] == "legitimate") == {
        "zh": 32, "en": 32,
    }

    intentional_chinese_place_misses = {"unknown-zh-11", "unknown-zh-13"}
    misses = set()
    false_positives = []
    for item in cases:
        decision = preflight_plan_task(item["text"], fixture["refs"], item["capabilities"])
        blocked = decision is not None
        if item["expect_block"] and not blocked:
            misses.add(item["id"])
        elif not item["expect_block"] and blocked:
            false_positives.append((item["id"], decision.reason_code))
    assert misses == intentional_chinese_place_misses
    assert false_positives == []
