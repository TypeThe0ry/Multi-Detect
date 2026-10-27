import json

from multidetect.autonomy.crash_resume import CheckpointStore, checkpoint_from_state, decide_recovery


def cp(tmp_path, **kw):
    value = checkpoint_from_state(mission_id="m1", package_digest="d1", step_id="s2", phase="scan", sequence=1, **kw)
    CheckpointStore(tmp_path / "checkpoint.json").save(value)
    return value


def test_recovery_matrix_and_state_continuity(tmp_path):
    checkpoint = cp(tmp_path, counts={"count": 3}, alerts=[{"position": [1, 2]}])
    assert decide_recovery(landed=True, armed=False, checkpoint=checkpoint, mission_id="m1", package_digest="d1", now=checkpoint["saved_at"] + 1).action == "new_mission"
    assert decide_recovery(landed=True, armed=False, checkpoint=None, mission_id="m1", package_digest="d1").action == "new_mission"
    assert decide_recovery(landed=False, armed=True, nav_state="HOLD", checkpoint=checkpoint, mission_id="m1", package_digest="d1", now=checkpoint["saved_at"] + 1).action == "resume"
    assert decide_recovery(landed=False, armed=True, checkpoint=None, mission_id="m1", package_digest="d1").action == "return_home"
    assert decide_recovery(landed=False, armed=True, checkpoint={**checkpoint, "mission_id": "other"}, mission_id="m1", package_digest="d1").action == "return_home"
    assert decide_recovery(landed=False, armed=True, checkpoint=checkpoint, mission_id="m1", package_digest="d1", now=checkpoint["saved_at"] + 99999).action == "return_home"


def test_checkpoint_torn_json_and_sequence_rollback_fail_closed(tmp_path):
    path = tmp_path / "checkpoint.json"
    store = CheckpointStore(path)
    first = checkpoint_from_state(mission_id="m", package_digest="d", step_id="s", phase="x", sequence=2)
    store.save(first)
    path.write_text('{"mission_id":"m"', encoding="utf-8")
    assert store.load() is None
    store.save(first)
    newer = {**first, "sequence": 1}
    try:
        store.save(newer)
    except ValueError:
        pass
    else:
        raise AssertionError("sequence rollback accepted")


def test_failsafe_mode_and_boot_restart_are_fail_closed():
    checkpoint = checkpoint_from_state(mission_id="m", package_digest="d", step_id="s", phase="x", sequence=1, boot_time_ms=500)
    assert decide_recovery(landed=False, armed=True, nav_state="RTL", boot_time_ms=600, checkpoint=checkpoint, mission_id="m", package_digest="d").action == "monitor_failsafe"
    assert decide_recovery(landed=False, armed=True, nav_state="OFFBOARD", boot_time_ms=400, checkpoint=checkpoint, mission_id="m", package_digest="d").action == "return_home"
    assert decide_recovery(landed=False, armed=True, nav_state="RTL", boot_time_ms=600, resume_after_failsafe=True, checkpoint=checkpoint, mission_id="m", package_digest="d").action == "resume"
    assert decide_recovery(landed=False, armed=True, nav_state=None, boot_time_ms=600, checkpoint=checkpoint, mission_id="m", package_digest="d").action == "monitor_failsafe"


def test_failsafe_mode_does_not_reissue_rtl_with_invalid_checkpoint():
    assert decide_recovery(
        landed=False,
        armed=True,
        nav_state="RTL",
        checkpoint=None,
        mission_id="m",
        package_digest="d",
    ).action == "monitor_failsafe"
    assert decide_recovery(
        landed=False,
        armed=True,
        nav_state="LAND",
        checkpoint={"broken": True},
        mission_id="m",
        package_digest="d",
    ).action == "monitor_failsafe"
