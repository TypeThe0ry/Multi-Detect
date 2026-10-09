"""Real local CP-v2 source subchecks only, NOT complete Mission/Grant admission."""

import json
import threading
from types import SimpleNamespace

import pytest
import test_teacher_live_navigation_checkpoint_v2 as cp_tests
from test_sim_navigation_reference import binding, document
from test_sim_teacher_environment import GROUND, VEHICLE

from multidetect.autonomy.sim_teacher_environment import load_static_teacher_environment
from scripts import onboard_teacher_mission_live_revalidation_v2 as api


@pytest.fixture
def source_case(tmp_path):
    generator = cp_tests.live_v4_fixture.__wrapped__(tmp_path)
    args, cap, clock, budget = next(generator)
    root = args["artifact_root"]
    xml = (root / "world.sdf").read_text().replace("</world>", GROUND + "</world>")
    args["world_source"] = binding(root, "world.sdf", xml.encode())
    checkpoint = cp_tests.api.qualify_teacher_live_navigation_checkpoint_v2(**args)
    reply = ("data: " + json.dumps(xml.replace("</world>", VEHICLE + "</world>")) + "\n").encode()
    native = dict(args["navigation_after"])
    stdout = binding(root, "loaded-world-original.bin", reply)
    native.update(
        command=["docker", "exec", args["container_id"], *api.WORLD_ARGS],
        started_at_s=100.46,
        finished_at_s=100.50,
    )
    native["streams"] = {
        **native["streams"],
        "stdout": {
            **native["streams"]["stdout"],
            "original_bytes": stdout.to_mapping(),
            "observed_bytes": len(reply),
            "full_output_sha256": stdout.sha256,
        },
    }
    environment = load_static_teacher_environment(
        artifact_root=root,
        world_source=args["world_source"],
        generated_sdf_source=stdout,
        include_model_sources={},
    )
    inputs = api.TeacherMissionLiveInputs(
        checkpoint, environment, document(root, "loaded-native.json", native)
    )
    check = dict(
        expected_runtime=checkpoint.runtime_attestation,
        expected_world=checkpoint.world_source,
        expected_scene=environment.scene,
        expected_origin_continuity_token=checkpoint.origin_continuity_token,
        root=root,
        clock=clock.session_id,
        start=100,
        now=clock.monotonic(),
    )
    try:
        yield SimpleNamespace(
            inputs=inputs, cap=cap, clock=clock, budget=budget, root=root, check=check
        )
    finally:
        generator.close()


def test_current_actual_CP_v2_sources_keep_original_budget(source_case):
    h = source_case
    cp, at = api._current_phase_sources(h.inputs, **h.check)
    assert cp is h.inputs.navigation_checkpoint and cp.budget == h.budget
    assert at == 100.46 and not any(cp.to_mapping()["claims"].values())


def test_real_RX_readonly_reproduction_with_different_phase_and_CP_time(source_case):
    h = source_case
    h.clock.value = 100.8
    checks = {**h.check, "now": 100.6, "_reproduce_checkpoint_identity_only": True}
    returned = []

    def receive_owner():
        try:
            returned.append(api._current_phase_sources(h.inputs, **checks))
        except BaseException as error:
            returned.append(error)

    thread = threading.Thread(target=receive_owner)
    thread.start()
    thread.join()
    assert len(returned) == 1 and isinstance(returned[0], tuple)
    assert returned[0][0] is h.inputs.navigation_checkpoint
    assert h.inputs.navigation_checkpoint.qualified_at_s == 100.51
    assert not h.cap._failure and not h.cap._closed


@pytest.mark.parametrize(
    "kind", ["expired", "different-token", "closed", "wrong-clock", "modified-pixels"]
)
def test_current_sources_cannot_be_replayed_or_modified(source_case, kind):
    h = source_case
    checks = dict(h.check)
    if kind == "expired":
        h.clock.value = checks["now"] = h.inputs.navigation_checkpoint.expires_at_s
    elif kind == "different-token":
        token = dict(checks["expected_origin_continuity_token"])
        token["ref_timestamp"] += 1
        checks["expected_origin_continuity_token"] = token
    elif kind == "closed":
        h.cap.abort("CPU closed")
    elif kind == "wrong-clock":
        checks["clock"] = "foreign-clock"
    else:
        path = h.root / "rgb-1.bin"
        path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises((ValueError, RuntimeError)):
        api._current_phase_sources(h.inputs, **checks)
