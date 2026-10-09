"""CPU-only subprocess supervision; no torch/model/GPU constructor is executed."""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_onboard_navigation_pilot_supervised as supervisor


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def invocation(tmp_path):
    sealed_data = tmp_path / "sealed-data"
    sealed_data.mkdir()
    snapshot = tmp_path / "pinned-snapshot"
    snapshot.mkdir()
    original = sealed_data / "bound-original.txt"
    original.write_bytes(b"stable original source")
    bound = supervisor.file_record(original)
    plan = {
        "artifact_root": str(tmp_path),
        "dataset_report": bound,
        "dataset_samples": bound,
        "review_receipt": bound,
        "source_files": [bound],
        "base": {"snapshot": str(snapshot), "safetensors": bound, "processor_files": [bound]},
        "expected_samples": 86,
        "train_sample_ids": [f"source-{index}" for index in range(1, 87)],
        "optimization": {"seed": 20261007},
    }
    plan_path = tmp_path / "plan.json"
    write_json(plan_path, plan)
    return SimpleNamespace(
        plan=plan_path,
        plan_sha256=supervisor.sha256_file(plan_path),
        output_dir=tmp_path / "worker-output",
        supervisor_dir=tmp_path / "supervisor-evidence",
        mode="load-only",
    )


def completion(args):
    return json.loads((args.supervisor_dir / "completion.json").read_bytes())


def child_stub(monkeypatch, source):
    monkeypatch.setattr(
        supervisor,
        "child_command",
        lambda args, plan: [
            sys.executable,
            "-u",
            "-B",
            "-X",
            "utf8",
            "-X",
            "faulthandler",
            "-c",
            source,
            str(args.output_dir),
        ],
    )


def test_launch_uses_exact_frozen_trainer_or_self_load_only_entry(invocation):
    command = supervisor.child_command(invocation, invocation.plan)
    assert command[:8] == [
        sys.executable,
        "-u",
        "-B",
        "-X",
        "utf8",
        "-X",
        "faulthandler",
        str(Path(supervisor.__file__).resolve()),
    ]
    assert "--worker-load-only" in command
    invocation.mode = "train"
    command = supervisor.child_command(invocation, invocation.plan)
    assert str(supervisor.TRAINER) == command[7]
    assert "--worker-load-only" not in command
    assert "--plan-sha256" in command


@pytest.mark.parametrize("exit_code", [0, 7])
def test_actual_child_without_report_never_claims_success_or_zero_counts(
    invocation, monkeypatch, exit_code
):
    child_stub(
        monkeypatch,
        f"import sys; print('child stage',flush=True); "
        f"print('raw error stream',file=sys.stderr,flush=True); sys.exit({exit_code})",
    )
    assert supervisor.supervise(invocation) == 1
    value = completion(invocation)
    assert value["child_pid"] > 0
    assert value["child_returncode_raw"] == exit_code
    assert not value["child_completion_verified"]
    assert value["worker_report"]["exists"] is False
    assert value["worker_report"]["counters"] is None
    assert value["optimizer_journal"]["observed_counters"] is None
    assert value["source_bindings_unchanged"]
    assert (invocation.supervisor_dir / "stdout.bin").read_bytes().startswith(b"child stage")
    assert b"raw error stream" in (invocation.supervisor_dir / "stderr.bin").read_bytes()
    assert not invocation.output_dir.exists()


def test_load_only_success_requires_native_zero_and_matching_worker_report(invocation, monkeypatch):
    report = {
        "contract": supervisor.LOAD_CONTRACT,
        "native_exit_code": 0,
        "backend_initialized": True,
        "optimizer_performed": False,
        "inference_performed": False,
        "forward_backward_performed": False,
        "plan_sha256": invocation.plan_sha256,
        **supervisor.CLAIMS,
    }
    child_stub(
        monkeypatch,
        "import json,sys; from pathlib import Path; "
        "p=Path(sys.argv[1]); p.mkdir(); "
        f"(p/'load-only-report.json').write_text(json.dumps({report!r})); "
        "print('load_only_backend_initialized',flush=True)",
    )
    assert supervisor.supervise(invocation) == 0
    value = completion(invocation)
    assert value["child_completion_verified"]
    assert value["child_returncode_raw"] == 0
    assert value["worker_report"]["counters"] is None
    assert value["optimizer_journal"]["observed_counters"] is None
    assert not value["automatic_retry_performed"]
    assert not value["timeout_used"]


@pytest.mark.parametrize("returncode", [3221225477, -1073741819])
def test_windows_access_violation_returncode_preserved_in_all_representations(
    invocation, returncode
):
    observed = []

    class FailedChild:
        pid = 123456

        def wait(self):
            observed.append("waited")
            return returncode

    def fake_popen(command, **kwargs):
        assert kwargs["shell"] is False
        kwargs["stderr"].write(b"Windows fatal exception: access violation\n")
        return FailedChild()

    assert supervisor.supervise(invocation, popen_factory=fake_popen) == 1
    value = completion(invocation)
    assert observed == ["waited"]
    assert value["child_pid"] == 123456
    assert value["child_returncode_raw"] == returncode
    assert value["child_returncode_uint32"] == 3221225477
    assert value["child_returncode_signed32"] == -1073741819
    assert value["child_returncode_hex32"] == "0xc0000005"
    assert value["worker_report"]["exists"] is False
    assert value["optimizer_journal"]["observed_counters"] is None


def test_actual_process_immediate_exit_bypasses_python_report_but_controller_survives(
    invocation, monkeypatch
):
    child_stub(monkeypatch, "import os,sys; print('before_native_exit',flush=True); os._exit(13)")
    assert supervisor.supervise(invocation) == 1
    value = completion(invocation)
    assert value["child_returncode_raw"] == 13
    assert value["worker_report"]["exists"] is False
    assert b"before_native_exit" in (invocation.supervisor_dir / "stdout.bin").read_bytes()


def test_changed_frozen_source_prevents_launch(invocation):
    (invocation.plan.parent / "sealed-data/bound-original.txt").write_bytes(b"changed source")
    called = []
    assert supervisor.supervise(invocation, popen_factory=lambda *a, **k: called.append(a)) == 1
    assert not called
    value = completion(invocation)
    assert value["child_pid"] is None
    assert not value["source_bindings_unchanged"]


def test_source_change_during_child_disqualifies_success(invocation, monkeypatch):
    original = invocation.plan.parent / "sealed-data/bound-original.txt"
    child_stub(
        monkeypatch,
        f"from pathlib import Path; Path({str(original)!r}).write_bytes(b'changed during child')",
    )
    assert supervisor.supervise(invocation) == 1
    value = completion(invocation)
    assert value["child_returncode_raw"] == 0
    assert not value["source_bindings_unchanged"]
    assert value["failure_type"] == "ValueError"


def test_existing_output_not_overwritten(invocation):
    invocation.output_dir.mkdir()
    marker = invocation.output_dir / "untouched"
    marker.write_bytes(b"user data")
    with pytest.raises(FileExistsError):
        supervisor.supervise(invocation)
    assert marker.read_bytes() == b"user data"
    assert not invocation.supervisor_dir.exists()


def test_output_escape_and_overlap_rejected(invocation):
    invocation.output_dir = invocation.plan.parent.parent / "outside-planned-root"
    with pytest.raises(ValueError, match="below artifact_root"):
        supervisor.supervise(invocation)
    invocation.output_dir = invocation.supervisor_dir / "nested-worker"
    with pytest.raises(ValueError, match="overlap"):
        supervisor.supervise(invocation)


@pytest.mark.parametrize("target", ["snapshot", "dataset", "review"])
@pytest.mark.parametrize("which", ["output_dir", "supervisor_dir", "private_worker"])
def test_new_outputs_cannot_pollute_sealed_snapshot_dataset_or_review(invocation, target, which):
    value = json.loads(invocation.plan.read_bytes())
    if target == "snapshot":
        directory = Path(value["base"]["snapshot"])
    elif target == "dataset":
        directory = Path(value["dataset_samples"]["path"]).parent
    else:
        directory = invocation.plan.parent / "sealed-review"
        directory.mkdir()
        receipt = directory / "review.json"
        receipt.write_bytes(b"separate reviewed source")
        value["review_receipt"] = supervisor.file_record(receipt)
        write_json(invocation.plan, value)
        invocation.plan_sha256 = supervisor.sha256_file(invocation.plan)
    forbidden = directory / "unexpected-evidence"
    called = []
    if which == "private_worker":
        invocation.output_dir = forbidden
        with pytest.raises(ValueError, match="sealed base/dataset/review"):
            supervisor.worker_load_only(invocation)
    else:
        setattr(invocation, which, forbidden)
        with pytest.raises(ValueError, match="sealed base/dataset/review"):
            supervisor.supervise(invocation, popen_factory=lambda *a, **k: called.append(a))
    assert not called
    assert not forbidden.exists()


def test_changed_plan_prevents_controller_start(invocation):
    invocation.plan.write_bytes(invocation.plan.read_bytes() + b" ")
    with pytest.raises(ValueError, match="Plan differs"):
        supervisor.supervise(invocation)
    assert not invocation.supervisor_dir.exists()


def test_missing_journal_empty_or_partial_records_never_invent_zero(tmp_path):
    assert supervisor.journal_counters(tmp_path)["observed_counters"] is None
    path = tmp_path / "training-events.jsonl"
    path.write_bytes(b"")
    assert supervisor.journal_counters(tmp_path)["observed_counters"] is None
    path.write_bytes(b'{"stage":"optimizer_step_attempted"}\n{"stage":')
    result = supervisor.journal_counters(tmp_path)
    assert result["observed_counters"] is None
    assert "failure_type" in result


def test_complete_journal_counts_are_only_observed_events(tmp_path):
    path = tmp_path / "training-events.jsonl"
    path.write_bytes(
        b'{"stage":"optimizer_step_attempted","step":1,"sample_id":"a","split":"train","epoch":1}\n'
    )
    result = supervisor.journal_counters(tmp_path)
    assert result["observed_counters"] == {
        "optimizer_steps_attempted": 1,
        "optimizer_steps_returned": 0,
        "optimizer_steps_synchronized": 0,
        "optimizer_steps_completed": 0,
    }


@pytest.mark.parametrize("journal", [None, "different", "corrupt", "wrong_ids", "complete"])
def test_train_report_claiming86_requires_matching_complete_step_journal(
    invocation, monkeypatch, journal
):
    invocation.mode = "train"
    report = {
        "contract": "multidetect.onboard-vlm-navigation-pilot-training-v1",
        "native_exit_code": 0,
        "training_plan_sha256": invocation.plan_sha256,
        **supervisor.CLAIMS,
        **dict.fromkeys(supervisor.COUNTERS, 86),
    }
    source = (
        "import json,sys; from pathlib import Path; p=Path(sys.argv[1]); p.mkdir(); "
        f"(p/'report.json').write_text(json.dumps({report!r})); "
    )
    if journal == "corrupt":
        source += "(p/'training-events.jsonl').write_text('{'); "
    elif journal in {"complete", "different", "wrong_ids"}:
        stop = 87 if journal == "complete" else 2
        if journal == "wrong_ids":
            stop = 87
        sample_ids = [f"source-{index}" for index in range(1, 87)]
        if journal != "wrong_ids":
            random.Random(20261007).shuffle(sample_ids)
        stages = [
            "optimizer_step_attempted",
            "optimizer_step_returned",
            "optimizer_step_synchronized",
            "optimizer_step_completed",
        ]
        # Generate rows inside the tiny CPU child, not a >32KiB Windows argv.
        source += (
            f"sample_ids={sample_ids!r}; "
            "rows=[{'stage':stage,'step':step,'sample_id':sample_ids[step-1],"
            f"'split':'train','epoch':1}} for step in range(1,{stop}) for stage in {stages!r}]; "
        )
        source += (
            "(p/'training-events.jsonl').write_text(''.join(json.dumps(r)+'\\n' for r in rows)); "
        )
    child_stub(monkeypatch, source)
    assert supervisor.supervise(invocation) == (0 if journal == "complete" else 1)
    assert completion(invocation)["child_completion_verified"] is (journal == "complete")


@pytest.mark.parametrize(
    "mutation", ["sha_missing", "sha_wrong", "inference", "backward", "missing_flag"]
)
def test_load_report_must_bind_this_plan_and_disallow_inference_or_backward(tmp_path, mutation):
    report = {
        "contract": supervisor.LOAD_CONTRACT,
        "native_exit_code": 0,
        "plan_sha256": "a" * 64,
        "backend_initialized": True,
        "optimizer_performed": False,
        "inference_performed": False,
        "forward_backward_performed": False,
        **supervisor.CLAIMS,
    }
    if mutation == "sha_missing":
        del report["plan_sha256"]
    elif mutation == "sha_wrong":
        report["plan_sha256"] = "b" * 64
    elif mutation == "inference":
        report["inference_performed"] = True
    elif mutation == "backward":
        report["forward_backward_performed"] = True
    else:
        del report["forward_backward_performed"]
    write_json(tmp_path / "load-only-report.json", report)
    assert not supervisor.read_worker_report(tmp_path, "load-only", expected_plan_sha256="a" * 64)[
        "valid"
    ]


def test_train_report_from_another_plan_cannot_approve_current_invocation(tmp_path):
    report = {
        "contract": "multidetect.onboard-vlm-navigation-pilot-training-v1",
        "native_exit_code": 0,
        "training_plan_sha256": "b" * 64,
        **supervisor.CLAIMS,
        **dict.fromkeys(supervisor.COUNTERS, 86),
    }
    write_json(tmp_path / "report.json", report)
    assert not supervisor.read_worker_report(tmp_path, "train", expected_plan_sha256="a" * 64)[
        "valid"
    ]


@pytest.mark.parametrize("mutation", ["step", "sample", "stage", "duplicate"])
def test_journal_wrong_step_order_or_cross_stage_sample_is_not_evidence(tmp_path, mutation):
    stages = [
        "optimizer_step_attempted",
        "optimizer_step_returned",
        "optimizer_step_synchronized",
        "optimizer_step_completed",
    ]
    rows = [
        {"stage": stage, "step": 1, "sample_id": "source-1", "split": "train", "epoch": 1}
        for stage in stages
    ]
    if mutation == "step":
        rows[1]["step"] = 2
    elif mutation == "sample":
        rows[1]["sample_id"] = "different-source"
    elif mutation == "stage":
        rows[1]["stage"] = "optimizer_step_completed"
    else:
        rows.append({**rows[0], "step": 2})
    (tmp_path / "training-events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    assert supervisor.journal_counters(tmp_path)["observed_counters"] is None


def test_child_waited_even_if_progress_record_write_failed(invocation, monkeypatch):
    create = supervisor.create_json
    observed = []

    def fail_started(path, value):
        if Path(path).name == "child-started.json":
            raise OSError("injected progress write failure")
        create(path, value)

    class Child:
        pid = 11

        def wait(self):
            observed.append("observed terminal")
            return 1

    monkeypatch.setattr(supervisor, "create_json", fail_started)
    assert supervisor.supervise(invocation, popen_factory=lambda *a, **k: Child()) == 1
    assert observed == ["observed terminal"]
    assert completion(invocation)["progress_recording_failure"] == "OSError"


def test_load_only_entry_uses_frozen_backend_without_optimizer_or_inference(
    invocation, monkeypatch
):
    from scripts import train_onboard_vlm_navigation_pilot as trainer

    order = []
    fake_plan = json.loads(invocation.plan.read_bytes())
    args = invocation
    monkeypatch.setattr(trainer, "load_plan", lambda *a: order.append("load") or fake_plan)
    monkeypatch.setattr(
        trainer, "validate_plan_shape", lambda _: order.append("budget") or object()
    )
    monkeypatch.setattr(trainer, "verify_plan_sources", lambda _: order.append("verify"))
    monkeypatch.setattr(
        trainer, "admit_dataset", lambda _: order.append("admit") or ((object(),), {})
    )

    def initialize(*args):
        order.append("backend")
        return SimpleNamespace(
            pre_load_cuda_memory={},
            base_digest_initial="base",
            adapter_digest_initial="adapter",
            trainable_audit={},
        )

    monkeypatch.setattr(trainer, "NavigationBackend", initialize)
    monkeypatch.setattr(trainer, "train_epoch", lambda *a: pytest.fail("No optimizer allowed"))
    assert supervisor.worker_load_only(args) == 0
    assert order == ["load", "budget", "verify", "admit", "backend", "verify"]
    report = json.loads((args.output_dir / "load-only-report.json").read_bytes())
    assert report["backend_initialized"]
    assert not report["optimizer_performed"]
    assert not report["inference_performed"]
    assert not report["forward_backward_performed"]
    assert all(report[key] is False for key in supervisor.CLAIMS)
