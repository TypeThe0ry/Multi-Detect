"""Injected CPU orchestration only; no official processor/model/GPU is executed."""

from __future__ import annotations

import copy
import gc
import hashlib
import io
import json
import subprocess
import sys
import weakref
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_onboard_navigation_actor import source_sample as source_sample

from scripts import onboard_navigation_balanced_encoding as old_encoder
from scripts import onboard_navigation_balanced_journal as journal_module
from scripts import onboard_navigation_closed_sample_encoding as closed
from scripts import onboard_navigation_host_memory as memory
from scripts import profile_onboard_navigation_closed_samples as profile
from scripts import train_onboard_navigation_streamed as legacy
from scripts import verify_onboard_navigation_actor_inputs as checker


def selection_inputs():
    observed = {
        **{
            f"optimizer_steps_{stage}": 13
            for stage in ("attempted", "returned", "synchronized", "completed")
        },
        "event_count": 52,
        "last_phase": "completed",
        "optimizer_update_uncertain": False,
        "complete": False,
    }
    old = {
        "diagnostics": {
            "smoke_probes": [
                {"sample_id": name, "action_class": name}
                for name in ("forward", "left", "right", "stop")
            ]
        }
    }
    schedule = {"draws": [{"draw_index": index + 1, "sample_id": "failed"} for index in range(280)]}
    failure = {
        "journal": copy.deepcopy(observed),
        "failure_type": "MemoryError",
        "phase": "train",
        "plan_sha256": profile.FROZEN_SHA,
    }
    completion = {
        "plan_sha256": profile.FROZEN_SHA,
        "native_exit_code": 1,
        "phases": [
            {"phase": phase, "native_exit_code": status}
            for phase, status in (("cpu-encode", 0), ("baseline", 0), ("train", 1))
        ],
    }
    return old, schedule, failure, completion, observed


def test_selection_is_exact_next_draw_then_original_probes_in_order():
    rows = profile.select_samples(*selection_inputs())
    assert [row["sample_id"] for row in rows] == ["failed", "forward", "left", "right", "stop"]
    assert rows[0]["roles"] == ["failed_draw14"]
    assert rows[3]["roles"] == ["original_probe_right"]


def test_selection_deduplicates_only_identity_preserving_both_roles():
    old, schedule, failure, completion, observed = selection_inputs()
    schedule["draws"][13]["sample_id"] = "left"
    rows = profile.select_samples(old, schedule, failure, completion, observed)
    assert [row["sample_id"] for row in rows] == ["left", "forward", "right", "stop"]
    assert rows[0]["roles"] == ["failed_draw14", "original_probe_left"]


@pytest.mark.parametrize(
    "mutation",
    [
        "count",
        "bool",
        "event_count",
        "complete",
        "uncertain",
        "report",
        "failure_type",
        "phase",
        "plan",
        "native",
        "phases",
        "draw14",
        "probes",
    ],
)
def test_selection_requires_real_failed_journal_chronology(mutation):
    old, schedule, failure, completion, observed = selection_inputs()
    if mutation == "count":
        observed["optimizer_steps_completed"] = 12
    elif mutation == "bool":
        observed["optimizer_steps_completed"] = True
    elif mutation == "event_count":
        observed["event_count"] = 51
    elif mutation == "complete":
        observed["complete"] = True
    elif mutation == "uncertain":
        observed["optimizer_update_uncertain"] = True
    elif mutation == "report":
        failure["journal"]["last_phase"] = "attempted"
    elif mutation == "failure_type":
        failure["failure_type"] = "OutOfMemoryError"
    elif mutation == "phase":
        failure["phase"] = "baseline"
    elif mutation == "plan":
        failure["plan_sha256"] = "0" * 64
    elif mutation == "native":
        completion["native_exit_code"] = True
    elif mutation == "phases":
        completion["phases"][-1]["native_exit_code"] = 0
    elif mutation == "draw14":
        schedule["draws"][13]["draw_index"] = 15
    elif mutation == "probes":
        old["diagnostics"]["smoke_probes"].pop()
    with pytest.raises(ValueError):
        profile.select_samples(old, schedule, failure, completion, observed)


class Lease:
    pass


def encoded():
    return {
        "prefix": Lease(),
        "full": Lease(),
        "labels": Lease(),
        "prefix_tokens": 3,
        "total_tokens": 11,
        "response_tokens": 8,
        "supervised_tokens": 8,
        "prefix_tensors": {
            name: {"shape": [1, 3], "dtype": "torch.int64", "sha256": "a" * 64}
            for name in checker.FIELDS
        },
        "input_tensors": {
            name: {"shape": [1, 11], "dtype": "torch.int64", "sha256": "b" * 64}
            for name in checker.FIELDS
        },
        "response_labels_sha256": "c" * 64,
        "target_sha256": "d" * 64,
        "actor_identity_sha256": "e" * 64,
        "prefix_reference_verified": True,
        "prompt_label_mask_verified": True,
    }


@pytest.fixture
def paired(monkeypatch, tmp_path):
    selection = profile.select_samples(*selection_inputs())
    samples = tuple(SimpleNamespace(sample_id=row["sample_id"]) for row in selection)
    prepared = {sample.sample_id: legacy.metadata(encoded()) for sample in samples}
    references = {sample.sample_id: {"sample_id": sample.sample_id} for sample in samples}
    state = SimpleNamespace(calls=[], leases=[], fail=None, drift=None, cuda=False, events=[])
    processor, torch, budget = (
        object(),
        SimpleNamespace(cuda=SimpleNamespace(is_initialized=lambda: state.cuda)),
        object(),
    )

    def produce(mode, sample, root, kwargs):
        gc.collect()
        assert all(reference() is None for reference in state.leases)
        assert (
            root == tmp_path
            and kwargs["processor"] is processor
            and kwargs["torch_module"] is torch
        )
        assert (
            kwargs["budget"] is budget
            and kwargs["prefix_reference"] == references[sample.sample_id]
        )
        if mode == "legacy_owned":
            assert kwargs["session"] is None
        else:
            assert kwargs["expected_metadata"] == prepared[sample.sample_id]
            assert issubclass(
                kwargs["session_factory"], closed.ClosedNavigationSourceValidationSession
            )
        state.calls.append((mode, sample.sample_id))
        if state.fail == (mode, sample.sample_id):
            raise MemoryError("Injected CPU allocation failure")
        value = encoded()
        state.leases.extend(weakref.ref(value[name]) for name in ("prefix", "full", "labels"))
        if state.drift == mode:
            value["target_sha256"] = "f" * 64
        return value

    monkeypatch.setattr(
        old_encoder,
        "encode_navigation_sample",
        lambda sample, root, **kw: produce("legacy_owned", sample, root, kw),
    )
    monkeypatch.setattr(
        closed,
        "encode_closed_navigation_sample",
        lambda sample, root, **kw: produce("closed_sample", sample, root, kw),
    )

    def stage(name, operation, **fields):
        state.events.append((name, fields))
        return operation()

    state.arguments = (
        selection,
        samples,
        tmp_path,
        processor,
        torch,
        budget,
        references,
        prepared,
        stage,
        lambda *a, **kw: None,
    )
    return state


def test_pair_uses_same_processor_old_owned_then_closed_full_expected_row_and_no_tensor_cache(
    paired,
):
    rows = profile.paired_encodings(*paired.arguments)
    assert paired.calls == [
        (mode, name)
        for name in ("failed", "forward", "left", "right", "stop")
        for mode in ("legacy_owned", "closed_sample")
    ]
    assert len(rows) == 10 and all(row["full_metadata_equal"] is True for row in rows)
    assert [row["closed_session_verified_before_admission"] for row in rows] == [False, True] * 5
    assert all(not {"prefix", "full", "labels"} & set(row["metadata"]) for row in rows)
    gc.collect()
    assert all(reference() is None for reference in paired.leases)


@pytest.mark.parametrize("mode,admitted", [("legacy_owned", 0), ("closed_sample", 1)])
def test_complete_metadata_drift_rejects_without_admitting_current_row(paired, mode, admitted):
    paired.drift = mode
    rows = []
    with pytest.raises(ValueError, match="Full prepared metadata"):
        profile.paired_encodings(*paired.arguments, rows=rows)
    assert len(rows) == admitted
    assert len(paired.calls) == admitted + 1


def test_unknown_selection_identity_never_encodes(paired):
    arguments = ([{"sample_id": "unknown", "roles": []}], *paired.arguments[1:])
    with pytest.raises(KeyError):
        profile.paired_encodings(*arguments)
    assert not paired.calls


def test_timed_factory_is_real_closed_subclass_with_inclusive_scalar_events(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(
        closed.ClosedNavigationSourceValidationSession, "prime", lambda self, samples: None
    )
    monkeypatch.setattr(
        closed.ClosedNavigationSourceValidationSession, "validate_sample", lambda self, sample: ()
    )

    def exit_session(self, error_type, error, traceback):
        self.verified = error_type is None
        return False

    monkeypatch.setattr(closed.ClosedNavigationSourceValidationSession, "__exit__", exit_session)
    ticks = iter(range(20))
    factory = profile.timed_session_factory(
        lambda *args, **fields: events.append((args, fields)), lambda: next(ticks)
    )
    session = factory(tmp_path)
    assert isinstance(session, closed.ClosedNavigationSourceValidationSession)
    with session:
        session.prime((object(),))
        session.validate_sample(object())
        assert not session.verified
    assert session.verified
    assert [args for args, _ in events] == [
        (name, boundary)
        for name in ("session_prime", "session_validate_sample", "session_exit")
        for boundary in ("started", "finished")
    ]
    assert events[-1][1] == {"elapsed_s": 1, "verified": True}
    assert all(
        all(isinstance(value, (str, int, float, bool)) for value in fields.values())
        for _, fields in events
    )


@pytest.mark.parametrize("boundary", ["started", "finished"])
@pytest.mark.parametrize("failure", [MemoryError, KeyboardInterrupt, SystemExit])
def test_exit_observer_failure_cannot_skip_real_source_cleanup(
    source_sample, tmp_path, boundary, failure
):
    def observe(name, actual_boundary, **fields):
        if name == "session_exit" and actual_boundary == boundary:
            raise failure("exit observation failed")

    session = profile.timed_session_factory(observe)(tmp_path)
    with pytest.raises(failure, match="exit observation failed"):
        with session:
            session.prime((source_sample,))
            assert session._pointers and session._active
    assert not session._active and session._pointers == {}


@pytest.mark.parametrize("boundary", ["started", "finished"])
def test_exit_observer_failure_preserves_original_body_MemoryError(
    source_sample, tmp_path, boundary
):
    def observe(name, actual_boundary, **fields):
        if name == "session_exit" and actual_boundary == boundary:
            raise OSError("observer failure must not replace body")

    session = profile.timed_session_factory(observe)(tmp_path)
    with pytest.raises(MemoryError, match="original body") as error:
        with session:
            session.prime((source_sample,))
            raise MemoryError("original body")
    assert not session.verified and not session._active and session._pointers == {}
    assert any("observation also failed" in note for note in error.value.__notes__)


def test_base_source_SHA_failure_precedes_finish_observer_error(source_sample, tmp_path):
    def observe(name, boundary, **fields):
        if name == "session_exit" and boundary == "finished":
            raise OSError("secondary observer failure")

    session = profile.timed_session_factory(observe)(tmp_path)
    with pytest.raises(ValueError) as error:
        with session:
            session.prime((source_sample,))
            path = tmp_path / source_sample.source.annotation.path
            path.write_bytes(path.read_bytes() + b" ")
    assert not isinstance(error.value, OSError)
    assert not session.verified and not session._active and session._pointers == {}


@pytest.fixture
def run_fixture(monkeypatch, tmp_path, paired):
    monkeypatch.setattr(profile, "ROOT", tmp_path)
    monkeypatch.setattr(legacy, "ROOT", tmp_path)
    diagnostics = tmp_path / "artifacts/diagnostics"
    diagnostics.mkdir(parents=True)
    original, schedule, failure, completion, observed = selection_inputs()
    original["base"] = {"snapshot": str(tmp_path)}
    original["schedule"] = None
    original["cpu_prefix_report"] = None
    original["source_files"] = []
    for name in (
        "scripts/train_onboard_navigation_streamed.py",
        "scripts/onboard_navigation_balanced_journal.py",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Fixture-only bootstrap source\n")
        original["source_files"].append(legacy.record(path))
    prepared = {
        **paired.arguments[7],
        **{f"other_{index}": legacy.metadata(encoded()) for index in range(81)},
    }
    payloads = {
        "legacy_plan": original,
        "failure_evidence": {},
        "failure_report": failure,
        "failure_journal": {},
        "completion": completion,
        "prepared_inputs": prepared,
        "schedule": schedule,
        "prefix": {"samples": list(paired.arguments[6].values())},
    }
    bindings = {}
    for name, value in payloads.items():
        path = tmp_path / (name + ".json")
        path.write_bytes(profile.canonical(value))
        bindings[name] = legacy.record(path)
    original["schedule"], original["cpu_prefix_report"] = bindings["schedule"], bindings["prefix"]
    planpath = tmp_path / "legacy_plan.json"
    planpath.write_bytes(profile.canonical(original))
    bindings["legacy_plan"] = legacy.record(planpath)
    monkeypatch.setattr(profile, "FROZEN_SHA", bindings["legacy_plan"]["sha256"])
    for name in ("failure_report", "completion"):
        payloads[name]["plan_sha256"] = profile.FROZEN_SHA
        path = tmp_path / (name + ".json")
        path.write_bytes(profile.canonical(payloads[name]))
        bindings[name] = legacy.record(path)
    monkeypatch.setattr(
        profile,
        "BOUND",
        {
            name: (row["path"], row["sha256"])
            for name, row in bindings.items()
            if name not in {"schedule", "prefix"}
        },
    )
    for name in profile.NEW_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Fixture-only bound source\n")
    monkeypatch.setattr(journal_module, "verify_journal", lambda path, draws: observed)
    gates = []

    def old_admit(path, digest):
        gates.append("legacy_admit")
        assert digest == profile.FROZEN_SHA
        return original, paired.arguments[5], [bindings["legacy_plan"]]

    def validate_prepared(table, plan, budget):
        gates.append("full86_prepared")
        assert len(table) == 86 and plan is original and budget is paired.arguments[5]
        return table

    monkeypatch.setattr(legacy, "admit_plan", old_admit)
    monkeypatch.setattr(
        legacy,
        "semantic_admission",
        lambda plan: (
            gates.append("semantic") or original,
            paired.arguments[1],
            {},
            schedule,
            None,
        ),
    )
    monkeypatch.setattr(legacy, "validate_prepared_inputs", validate_prepared)
    monkeypatch.setattr(legacy, "repository_imports", nullcontext)
    monkeypatch.setattr(
        profile, "import_cpu_torch", lambda: gates.append("cpu_torch") or paired.arguments[4]
    )
    monkeypatch.setattr(
        checker,
        "official_processor",
        lambda snapshot, budget: gates.append("official_processor") or paired.arguments[3],
    )
    monkeypatch.setattr(
        memory, "snapshot", lambda: {"fixture_only": True, "process_working_set_bytes": 123}
    )
    plan = profile.build_plan()
    path = tmp_path / "profile-plan.json"
    path.write_bytes(profile.canonical(plan))
    args = SimpleNamespace(
        plan=path,
        plan_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        output_dir=diagnostics / "new-profile",
    )
    return SimpleNamespace(
        args=args, gates=gates, paired=paired, plan=plan, source=tmp_path / profile.NEW_FILES[0]
    )


def test_run_strict_full86_admission_precedes_shared_processor_and_ten_pairs(run_fixture):
    value = run_fixture
    assert profile.run_profile(value.args) == 0
    assert value.gates == [
        "legacy_admit",
        "semantic",
        "full86_prepared",
        "cpu_torch",
        "official_processor",
    ]
    report = legacy.strict_json((value.args.output_dir / "report.json").read_bytes())
    assert report["success"] is True and len(report["rows"]) == 10
    assert report["source_bindings_before"] == report["source_bindings_after"]
    assert report["source_bindings_unchanged"] is True
    assert report["CUDA_uninitialized_before_processor"] is True
    assert report["CUDA_uninitialized_after_encoding"] is True
    assert all(report[name] is False for name in profile.FALSE_CLAIMS)
    assert not (value.args.output_dir / "failure.json").exists()


def test_run_memoryerror_is_native_failure_with_no_current_result_or_retry(run_fixture):
    value = run_fixture
    value.paired.fail = ("legacy_owned", "failed")
    assert profile.run_profile(value.args) == 1
    report = legacy.strict_json((value.args.output_dir / "report.json").read_bytes())
    assert report["success"] is False and report["rows"] == []
    assert report["failure"]["type"] == "MemoryError"
    assert report["source_bindings_after"] is None and not report["source_bindings_unchanged"]
    assert value.paired.calls == [("legacy_owned", "failed")]
    assert (value.args.output_dir / "failure.json").exists()


def test_outer_stage_finish_observation_preserves_original_encoder_failure(
    run_fixture, monkeypatch
):
    run_fixture.paired.fail = ("legacy_owned", "failed")

    def snapshot():
        if run_fixture.paired.calls:
            raise OSError("secondary stage observation failed")
        return {"fixture_only": True}

    monkeypatch.setattr(memory, "snapshot", snapshot)
    assert profile.run_profile(run_fixture.args) == 1
    report = legacy.strict_json((run_fixture.args.output_dir / "report.json").read_bytes())
    assert report["failure"]["type"] == "MemoryError"
    assert report["failure"]["message"] == "Injected CPU allocation failure"
    assert not report["success"] and report["rows"] == []


def test_run_preserves_only_previously_admitted_rows_on_later_failure(run_fixture):
    value = run_fixture
    value.paired.fail = ("closed_sample", "forward")
    assert profile.run_profile(value.args) == 1
    report = legacy.strict_json((value.args.output_dir / "report.json").read_bytes())
    assert [(row["sample_id"], row["mode"]) for row in report["rows"]] == [
        ("failed", "legacy_owned"),
        ("failed", "closed_sample"),
        ("forward", "legacy_owned"),
    ]


def test_run_source_drift_after_encoding_prevents_success(run_fixture, monkeypatch):
    original = legacy.record

    def record(path):
        row = original(path)
        if Path(path) == run_fixture.source and len(run_fixture.paired.calls) == 10:
            row["sha256"] = "f" * 64
        return row

    monkeypatch.setattr(legacy, "record", record)
    assert profile.run_profile(run_fixture.args) == 1
    report = legacy.strict_json((run_fixture.args.output_dir / "report.json").read_bytes())
    assert len(report["rows"]) == 10 and not report["source_bindings_unchanged"]
    assert report["source_bindings_before"] != report["source_bindings_after"]
    assert "Source bytes changed" in report["failure"]["message"]


def test_run_CUDA_initialized_observation_rejects_before_encoding(run_fixture):
    run_fixture.paired.cuda = True
    assert profile.run_profile(run_fixture.args) == 1
    report = legacy.strict_json((run_fixture.args.output_dir / "report.json").read_bytes())
    assert not report["success"] and not run_fixture.paired.calls
    assert report["failure"]["type"] == "ValueError"
    assert any(row["CUDA_initialized"] is True for row in report["observations"])


def test_run_host_memory_API_failure_is_not_fake_zero_snapshot(run_fixture, monkeypatch):
    def fail():
        raise OSError("Memory API failed")

    monkeypatch.setattr(memory, "snapshot", fail)
    assert profile.run_profile(run_fixture.args) == 1
    report = legacy.strict_json((run_fixture.args.output_dir / "report.json").read_bytes())
    assert report["observations"] == [] and report["failure"]["type"] == "OSError"
    assert not run_fixture.gates and not run_fixture.paired.calls


def test_existing_output_is_never_overwritten(run_fixture):
    run_fixture.args.output_dir.mkdir()
    marker = run_fixture.args.output_dir / "report.json"
    marker.write_bytes(b"original receipt")
    with pytest.raises(FileExistsError):
        profile.run_profile(run_fixture.args)
    assert marker.read_bytes() == b"original receipt" and not run_fixture.gates


@pytest.mark.parametrize("mutation", ["unknown", "bool", "selection", "source"])
def test_profile_plan_exact_fields_and_source_bindings_gate_before_old_admission(
    run_fixture, mutation
):
    plan = copy.deepcopy(run_fixture.plan)
    if mutation == "unknown":
        plan["unknown"] = False
    elif mutation == "bool":
        plan["schema_version"] = True
    elif mutation == "selection":
        plan["selection"].reverse()
    elif mutation == "source":
        plan["source_files"][0]["sha256"] = "f" * 64
    run_fixture.args.plan.write_bytes(profile.canonical(plan))
    digest = hashlib.sha256(run_fixture.args.plan.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="exact frozen experiment"):
        profile.admit_plan(run_fixture.args.plan, digest)
    assert not run_fixture.gates


def test_profile_plan_SHA_mismatch_rejects_before_admission(run_fixture):
    with pytest.raises(ValueError, match="SHA changed"):
        profile.admit_plan(run_fixture.args.plan, "f" * 64)
    assert not run_fixture.gates


@pytest.mark.parametrize(
    "name",
    [
        "scripts/onboard_navigation_host_memory.py",
        "scripts/train_onboard_navigation_streamed.py",
        "scripts/onboard_navigation_closed_sample_encoding.py",
        "scripts/onboard_navigation_balanced_journal.py",
    ],
)
def test_bootstrap_SHA_rejection_precedes_dependency_import(run_fixture, monkeypatch, name):
    path = profile.ROOT / name
    sentinel = profile.ROOT / "executed-sentinel.txt"
    path.write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('EXECUTED')\n",
        encoding="utf-8",
    )
    imports = []

    def forbidden_import():
        imports.append("legacy")
        raise AssertionError("Dependency import occurred before bootstrap admission")

    monkeypatch.setattr(profile, "legacy_api", forbidden_import)
    assert profile.run_profile(run_fixture.args) == 1
    report = json.loads((run_fixture.args.output_dir / "report.json").read_bytes())
    assert report["failure"]["type"] == "ValueError"
    assert "Bootstrap SHA changed" in report["failure"]["message"]
    assert not sentinel.exists() and imports == [] and not run_fixture.gates


def test_emit_plan_writes_Python_JSON_bytes_preserving_float_zero(monkeypatch):
    output = io.BytesIO()
    monkeypatch.setattr(profile, "build_plan", lambda: {"yaw": 0.0, "unknown": None})
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=output))
    assert profile.main(["--emit-plan"]) == 0
    assert output.getvalue() == b'{"unknown": null, "yaw": 0.0}\n'
    assert type(json.loads(output.getvalue())["yaw"]) is float


@pytest.mark.parametrize(
    "arguments", [[], ["--emit-plan", "--plan", "x"], ["--plan", "x", "--plan-sha256", "a" * 64]]
)
def test_CLI_requires_one_complete_mode(arguments):
    with pytest.raises(SystemExit) as error:
        profile.main(arguments)
    assert error.value.code == 2


def test_static_import_is_guarded_stdlib_only():
    path = Path(profile.__file__).resolve()
    code = (
        "import importlib.util,sys; "
        f"spec=importlib.util.spec_from_file_location('cpu_profile',{str(path)!r}); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "assert not {'torch','transformers','PIL','peft','datasets','accelerate'} "
        "& set(sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
