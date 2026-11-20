from pathlib import Path
import json

import pytest

from scripts.realworld import x1_resume as resume


IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def spec(tmp_path, monkeypatch):
    monkeypatch.setattr(resume, "OUTPUT", tmp_path)
    monkeypatch.setattr(resume, "mount_check", lambda **kwargs: None)
    return resume.batch.read_object(resume.MANIFEST)


def record_for(job, *, accepted=True, p95=3.25):
    mode = job["inputs"]["gimbal_bridge_stabilize"]
    return {"job_id": job["job_id"], "attempt_id": job["job_id"] + "/attempt-1",
            "accepted": accepted, "valid_round": True,
            "image_id": IMAGE, "bridge_stabilize": mode,
            "bridge_stabilize_check": {"passed": True, "env_enabled": mode},
            "horizon_acceptance": {"feasible_error": {"p95_deg": p95}},
            "errors": [] if accepted else ["horizon_feasible_p95_exceeded"]}


def test_plans_preserve_baseline_and_use_one_immutable_candidate(spec):
    ab = resume.make_plan(spec, "ab", IMAGE)
    final = resume.make_plan(spec, "final", IMAGE, False)
    smoke = resume.make_plan(spec, "smoke", IMAGE)
    assert spec["image"].endswith(":gz-rendering-830-r1-gimbal-horizon2")
    assert [(r["inputs"]["seed"], r["inputs"]["gimbal_bridge_stabilize"]) for r in ab["jobs"]] == [
        (201, True), (202, False), (203, True), (204, False)]
    assert [r["inputs"]["seed"] for r in final["jobs"]] == [301, 302, 303]
    assert len(smoke["jobs"]) == 1 and smoke["jobs"][0]["inputs"]["seed"] == 201
    for plan in (ab, final, smoke):
        assert plan["gpu"] == 3 and plan["slots"] == [5] and plan["share_gpu"] is False
        assert all(r["inputs"]["image"] == IMAGE for r in plan["jobs"])


def test_negative_evidence_requires_valid_candidate_identity_and_bridge_state(spec):
    job = resume.make_plan(spec, "ab", IMAGE)["jobs"][0]
    record = record_for(job, accepted=False, p95=5)
    assert resume.qualify_ab(record, job)
    assert not resume.receipt_matches(record, job)
    for wrong in ({"image_id": "old"}, {"bridge_stabilize": False}, {"valid_round": False},
                  {"bridge_stabilize_check": None}, {"voided_reason": "load_spike"},
                  {"errors": ["horizon_data_missing"]}, {"horizon_acceptance": {}}):
        assert not resume.qualify_ab({**record, **wrong}, job)


def test_phase_order_selection_resume_and_completion(spec, monkeypatch):
    plans = []
    script_runs = ["partial", "finish"]
    monkeypatch.setattr(resume.batch, "MIN_FREE_BYTES", resume.batch.MIN_FREE_BYTES)
    monkeypatch.setenv("RTF_MATRIX_HOST_DISK_MIN_GIB", "0")
    # Restore environment keys changed by the worker on teardown.
    for key in ("WIDE_COLLECT_KEEP_HORIZON_REJECTED", "WIDE_COLLECT_MIN_FREE_GB", "SITL_PYTHON",
                "TOOLS_PYTHON", "RTF_MATRIX_CPU_MAX", "MULTIDETECT_SIM_CGROUP_PARENT"):
        monkeypatch.delenv(key, raising=False)

    def run_batch(plan, command, *, completed=None):
        plans.append(plan["batch_id"])
        root = Path(plan["output_root"])
        root.mkdir(exist_ok=True)
        assert command == resume.MATRIX
        jobs = plan["jobs"]
        if plan["batch_id"].endswith("-ab"):
            assert completed is resume.qualify_ab
            rows = [record_for(j, accepted=False, p95=6 if j["inputs"]["gimbal_bridge_stabilize"] else 4.5)
                    for j in jobs]
            if script_runs.pop(0) == "partial":
                rows.pop()
            path = root / "results.jsonl"
        else:
            assert all(j["inputs"]["gimbal_bridge_stabilize"] is False for j in jobs)
            rows = [record_for(j) for j in jobs]
            path = root / "accepted.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return int(completed is not None)

    monkeypatch.setattr(resume.batch, "run_batch", run_batch)
    assert resume.run(spec, IMAGE) == 1
    assert plans == ["x1-resume-ab"]
    assert not resume.complete(spec)
    assert resume.run(spec, IMAGE) == 0
    assert plans == ["x1-resume-ab", "x1-resume-ab", "x1-resume-final"]
    assert resume.complete(spec)
    choice = json.loads((resume.OUTPUT / "ab/CHOICE.json").read_text())
    assert choice["scores"] == {"on": [2, 6], "off": [2, 4.5]}
    with pytest.raises(ValueError, match="candidate image or manifest changed"):
        resume.run(spec, "sha256:" + "b" * 64)
    (resume.OUTPUT / "final/accepted.jsonl").write_text("")
    assert not resume.complete(spec)


def test_missing_candidate_never_reaches_dispatch(monkeypatch):
    def unavailable(reference):
        assert reference.endswith(":gz-rendering-830-r1-gimbal-horizon2")
        raise ValueError("candidate absent")

    monkeypatch.setattr(resume, "image_check", unavailable)
    monkeypatch.setattr(resume, "complete", lambda spec: False)
    monkeypatch.setattr(resume, "dispatch", lambda *args, **kwargs: pytest.fail("must not dispatch"))
    assert resume.main(["launch"]) == 75
    assert resume.main(["smoke"]) == 75


def test_mount_check_accepts_host_cifs_and_refuses_local_or_read_only(tmp_path, monkeypatch):
    share = tmp_path / "AdminHome"
    volatic = share / "volatic"
    volatic.mkdir(parents=True)
    monkeypatch.setattr(resume, "NAS_SHARE", share)
    monkeypatch.setattr(resume, "NAS", volatic)
    monkeypatch.setattr(resume, "OUTPUT", volatic / "x1-resume")
    mount = {"target": str(share), "source": "//192.168.1.1/AdminHome", "fstype": "cifs", "options": "rw"}
    monkeypatch.setattr(resume, "read_cmd", lambda _: json.dumps({"filesystems": [mount]}))
    resume.mount_check(write=True)
    assert not list(resume.OUTPUT.iterdir())
    for wrong in ({"fstype": "ext4"}, {"source": "//other/AdminHome"}, {"options": "ro"}):
        original = mount.copy()
        mount.update(wrong)
        with pytest.raises(ValueError, match="writable X1 NAS share"):
            resume.mount_check()
        mount.update(original)
