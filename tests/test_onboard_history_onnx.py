from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest
from test_onboard_history import _input, _scope

from multidetect.autonomy.onboard_history import OnboardHistoryError
from multidetect.autonomy.onboard_history_onnx import (
    OnboardHistoryOnnxModelRunner,
    onboard_history_metadata,
)
from multidetect.autonomy.onboard_onnx import (
    ONBOARD_MODEL_ACTION_ORDER,
    ONBOARD_MODEL_TASK_ACTION_ORDER,
    decode_onboard_policy_output,
)


def _output() -> np.ndarray:
    output = np.zeros((1, 20), dtype=np.float32)
    output[0, 7] = 8.0  # task HOLD
    output[0, 9] = 8.0  # flight HOLD_POSITION
    return output


class _Session:
    def __init__(self):
        self.inputs = [
            SimpleNamespace(name=name, shape=shape, type="tensor(float)")
            for name, shape in (
                ("features", [1, 8, 40]),
                ("valid_mask", [1, 8]),
                ("dt_s", [1, 8]),
                ("reset_mask", [1, 8]),
            )
        ]
        self.outputs = [SimpleNamespace(name="policy_output", shape=[1, 20], type="tensor(float)")]
        self.metadata = {
            **onboard_history_metadata(),
            "parent_model_sha256": "a" * 64,
            "temporal_training_performed": "false",
            "candidate_promoted": "false",
        }
        self.result = [_output()]
        self.calls = []
        self.error = None

    def get_inputs(self):
        return self.inputs

    def get_outputs(self):
        return self.outputs

    def get_modelmeta(self):
        return SimpleNamespace(custom_metadata_map=self.metadata)

    def run(self, names, inputs):
        self.calls.append((names, inputs))
        if self.error is not None:
            raise self.error
        return self.result


def test_runner_requires_scope_and_produces_exact_float32_right_aligned_tensors() -> None:
    session = _Session()
    runner = OnboardHistoryOnnxModelRunner(session)
    with pytest.raises(OnboardHistoryError, match="explicit session scope"):
        runner.infer(_input())
    runner.observe_scoped(_input(), _scope())
    assert session.calls == []
    output = runner.infer_scoped(_input(10.25), _scope())
    assert output == decode_onboard_policy_output(session.result, _input(10.25))
    names, tensors = session.calls[-1]
    assert names == ["policy_output"]
    assert tensors["features"].shape == (1, 8, 40)
    assert all(tensor.dtype == np.float32 for tensor in tensors.values())
    np.testing.assert_equal(tensors["valid_mask"], [[0] * 6 + [1, 1]])
    np.testing.assert_equal(tensors["dt_s"], [[0] * 7 + [0.25]])
    np.testing.assert_equal(tensors["reset_mask"], [[0] * 6 + [1, 0]])
    np.testing.assert_equal(
        tensors["features"][0, -1], np.asarray(_input(10.25).feature_vector(), dtype=np.float32)
    )
    assert runner.last_inference_receipt["event_count"] == 2
    assert runner.last_inference_receipt["truth_free"] is True
    assert runner.last_inference_receipt["training_ready"] is False


@pytest.mark.parametrize(
    ("node", "field", "value"),
    [
        (0, "name", "truth_features"),
        (0, "shape", [1, "time", 40]),
        (0, "shape", [1, 16, 40]),
        (0, "shape", [True, 8, 40]),
        (0, "type", "tensor(double)"),
        (1, "shape", [1, 8, 1]),
    ],
)
def test_runner_rejects_wrong_or_dynamic_input_contract(node, field, value) -> None:
    session = _Session()
    setattr(session.inputs[node], field, value)
    with pytest.raises(OnboardHistoryError, match="input|static"):
        OnboardHistoryOnnxModelRunner(session)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "legacy_logits"),
        ("shape", [1, 11]),
        ("type", "tensor(double)"),
    ],
)
def test_runner_rejects_legacy_or_wrong_output_contract(field, value) -> None:
    session = _Session()
    setattr(session.outputs[0], field, value)
    with pytest.raises(OnboardHistoryError):
        OnboardHistoryOnnxModelRunner(session)


@pytest.mark.parametrize(
    "key",
    list(onboard_history_metadata())
    + ["parent_model_sha256", "temporal_training_performed", "candidate_promoted"],
)
def test_runner_requires_exact_versioned_metadata(key: str) -> None:
    session = _Session()
    session.metadata.pop(key)
    with pytest.raises(ValueError):
        OnboardHistoryOnnxModelRunner(session)


def test_runner_rejects_reordered_features_and_promoted_provenance() -> None:
    session = _Session()
    names = json.loads(session.metadata["feature_names"])
    names[0], names[1] = names[1], names[0]
    session.metadata["feature_names"] = json.dumps(names)
    with pytest.raises(OnboardHistoryError, match="feature_names"):
        OnboardHistoryOnnxModelRunner(session)
    session = _Session()
    session.metadata["candidate_promoted"] = "true"
    with pytest.raises(OnboardHistoryError, match="unpromoted"):
        OnboardHistoryOnnxModelRunner(session)


@pytest.mark.parametrize("failure", ["runtime", "shape", "dtype", "nan", "list"])
def test_inference_failure_does_not_commit_or_replace_last_receipt(failure: str) -> None:
    session = _Session()
    runner = OnboardHistoryOnnxModelRunner(session)
    runner.infer_scoped(_input(), _scope())
    before = runner.history.entries
    receipt = runner.last_inference_receipt
    if failure == "runtime":
        session.error = RuntimeError("fixture inference failed")
    elif failure == "shape":
        session.result = [np.zeros((1, 11), dtype=np.float32)]
    elif failure == "dtype":
        session.result = [np.zeros((1, 20), dtype=np.float64)]
    elif failure == "nan":
        session.result = [np.full((1, 20), np.nan, dtype=np.float32)]
    else:
        session.result = _output()
    with pytest.raises((OnboardHistoryError, RuntimeError)):
        runner.infer_scoped(_input(10.1), _scope())
    assert runner.history.entries == before
    assert runner.last_inference_receipt == receipt
    session.error = None
    session.result = [_output()]
    runner.infer_scoped(_input(10.1), _scope())
    assert len(runner.history.entries) == 2


def test_explicit_runner_reset_has_receipt_and_allows_new_scope() -> None:
    from dataclasses import replace

    session = _Session()
    runner = OnboardHistoryOnnxModelRunner(session)
    runner.infer_scoped(_input(), _scope())
    scope = replace(_scope(), clock_epoch="explicit-reconnect-epoch")
    with pytest.raises(OnboardHistoryError, match="scope changed"):
        runner.infer_scoped(_input(10.1), scope)
    runner.reset_history(reason="explicit_stream_reconnect", scope=scope)
    assert runner.last_inference_receipt is None
    assert runner.history.last_reset_receipt["reason"] == "explicit_stream_reconnect"
    runner.infer_scoped(_input(20), scope)
    assert session.calls[-1][1]["reset_mask"][0, -1] == 1.0


def test_runtime_metadata_agrees_with_training_export_order() -> None:
    metadata = onboard_history_metadata()
    assert json.loads(metadata["task_action_order"]) == [
        item.value for item in ONBOARD_MODEL_TASK_ACTION_ORDER
    ]
    assert json.loads(metadata["flight_action_order"]) == [
        item.value for item in ONBOARD_MODEL_ACTION_ORDER
    ]
    assert metadata["contract"] == "multidetect.onboard-history-policy-v1"


def _native_runner(tmp_path, raw_output: np.ndarray) -> OnboardHistoryOnnxModelRunner:
    onnx = pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    helper = onnx.helper
    inputs = [
        helper.make_tensor_value_info(node.name, onnx.TensorProto.FLOAT, node.shape)
        for node in _Session().inputs
    ]
    output = helper.make_tensor_value_info("policy_output", onnx.TensorProto.FLOAT, [1, 20])
    constant = helper.make_node(
        "Constant", [], ["policy_output"], value=onnx.numpy_helper.from_array(raw_output)
    )
    graph = helper.make_graph(
        [constant], "tiny_history_contract_not_a_trained_policy", inputs, [output]
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9
    helper.set_model_props(model, _Session().metadata)
    path = tmp_path / "tiny-contract-fixture.onnx"
    onnx.save(model, path)
    return OnboardHistoryOnnxModelRunner.from_path(path, providers=["CPUExecutionProvider"])


def test_from_path_runs_native_cpu_ort_tiny_contract_fixture(tmp_path) -> None:
    runner = _native_runner(tmp_path, _output())
    actual = runner.infer_scoped(_input(), _scope())
    assert actual == decode_onboard_policy_output([_output()], _input())
    assert len(runner.history.entries) == 1


def _lifecycle_sortie(tmp_path):
    from test_onboard_history import _provider
    from test_onboard_realtime_bridge import _bridge, _context

    from multidetect.autonomy.realtime_bridge import OnboardAutonomousSortie

    raw = np.zeros((1, 20), dtype=np.float32)
    # A tiny native-ORT contract fixture, not learned behavior or flight proof.
    raw[0, 3] = 12.0  # MATCH when SEARCH is the accepted current phase
    raw[0, 4] = 18.0  # RECORD when a new recording observation is supplied
    raw[0, 5] = 10.0
    raw[0, 6] = 10.0
    raw[0, 9] = 8.0
    raw[0, 13] = 12.0  # FOLLOW_TARGET
    raw[0, 14] = 18.0  # RETURN_HOME
    runner = _native_runner(tmp_path, raw)
    bridge, _, _, _ = _bridge()
    bridge.event_loop.adapter.executor.decision_provider = _provider(runner)
    sortie = OnboardAutonomousSortie(bridge)
    sortie.prepare(_context(10.0))
    sortie.ingest_telemetry(_context(11.0, altitude_m=30.0))
    return sortie, runner


def _qualified_observation(at_s: float, identifier: str):
    from multidetect.autonomy.realtime_bridge import OnboardPerceptionObservation

    return OnboardPerceptionObservation(
        observation_id=identifier,
        observed_at_s=at_s,
        source="tiny-native-ort-observation-fixture",
        source_frame_sha256=f"{int(at_s * 10):064x}",
        target_visible=True,
        target_class="vehicle",
        target_color="red",
        target_class_match=True,
        target_color_match=True,
        target_geometry_frame="ned",
        target_north_offset_m=50.0,
        target_east_offset_m=12.0,
        target_distance_m=55.0,
        target_bearing_deg=15.0,
        target_confidence=0.97,
        target_match_score=0.98,
        obstacle_distance_m=5_000.0,
    )


@pytest.mark.parametrize(
    "second", ["missing", "same-time", "older", "same-snapshot", "same-frame", "expired"]
)
def test_temporal_match_record_preflight_rejects_without_any_model_or_mission_side_effect(
    tmp_path, second: str
) -> None:
    from dataclasses import replace

    from test_onboard_realtime_bridge import _context

    from multidetect.autonomy.onboard_agent import OnboardTaskAction

    sortie, runner = _lifecycle_sortie(tmp_path)
    observation = _qualified_observation(12.0, "rgb-frame-1")
    kwargs = {}
    if second != "missing":
        time = (
            11.9
            if second == "older"
            else 12.0
            if second == "same-time"
            else 18.0
            if second == "expired"
            else 12.2
        )
        context = _context(time)
        if second == "same-snapshot":
            context = replace(context, snapshot_id=_context(12.0).snapshot_id)
        kwargs = {
            "record_context": context,
            "record_observation": _qualified_observation(
                time, "rgb-frame-1" if second == "same-frame" else "rgb-frame-2"
            ),
        }
    with pytest.raises((RuntimeError, ValueError), match="real|distinct|newer|gap limit"):
        sortie.confirm_and_record_target_location(_context(12.0), observation, **kwargs)
    assert runner.history.entries == ()
    assert runner.last_inference_receipt is None
    assert len(sortie.bridge.revisions) == 1  # only prepare, never MATCH
    assert sortie.status.phase is OnboardTaskAction.SEARCH


def test_temporal_lifecycle_uses_real_new_observation_and_report_context_without_duplicate_time(
    tmp_path,
) -> None:
    from test_onboard_realtime_bridge import _context

    from multidetect.autonomy.onboard_agent import OnboardTaskAction

    sortie, runner = _lifecycle_sortie(tmp_path)
    matched, recorded = sortie.confirm_and_record_target_location(
        _context(12.0),
        _qualified_observation(12.0, "rgb-frame-1"),
        record_context=_context(12.2),
        record_observation=_qualified_observation(12.2, "rgb-frame-2"),
    )
    assert matched.task_receipt.phase is OnboardTaskAction.MATCH_TARGET
    assert recorded.task_receipt.phase is OnboardTaskAction.RECORD_LOCATION
    assert recorded.task_receipt.target_position is not None
    assert recorded.event.observed_at_s == 12.2
    assert recorded.event.event_id == "rgb-frame-2:record"
    assert [entry.observed_at_s for entry in runner.history.entries] == [12.0, 12.2]

    before = (runner.history.entries, tuple(sortie.bridge.revisions), sortie.status.phase)
    for invalid_context in (None, _context(16.0), _context(15.9), _context(22.0)):
        with pytest.raises((RuntimeError, ValueError), match="real|newer|gap limit"):
            sortie.complete_return_and_report(_context(16.0), report_context=invalid_context)
        assert (
            runner.history.entries,
            tuple(sortie.bridge.revisions),
            sortie.status.phase,
        ) == before

    returned, reported = sortie.complete_return_and_report(
        _context(16.0), report_context=_context(16.4)
    )
    assert returned.task_receipt.phase is OnboardTaskAction.RETURN_HOME
    assert reported.task_receipt.phase is OnboardTaskAction.REPORT
    assert sortie.status.report_committed
    assert [entry.observed_at_s for entry in runner.history.entries] == [12.0, 12.2, 16.0, 16.4]
    assert [entry.dt_s for entry in runner.history.entries] == pytest.approx([0.0, 0.2, 3.8, 0.4])
    assert all(
        revision.decision.reason != "model_provider_fault" for revision in sortie.bridge.revisions
    )


def test_non_combined_temporal_phase_progression_waits_for_next_real_inputs(tmp_path) -> None:
    from dataclasses import replace

    from test_onboard_realtime_bridge import _context

    from multidetect.autonomy.onboard_agent import OnboardTaskAction

    sortie, runner = _lifecycle_sortie(tmp_path)
    matched = sortie.ingest_perception(
        _context(12.0),
        replace(_qualified_observation(12.0, "match"), task_phase=OnboardTaskAction.SEARCH),
    )
    assert matched.task_receipt.phase is OnboardTaskAction.MATCH_TARGET
    recorded = sortie.record_target_location(
        _context(12.5),
        replace(
            _qualified_observation(12.5, "record"), task_phase=OnboardTaskAction.RECORD_LOCATION
        ),
    )
    assert recorded.task_receipt.phase is OnboardTaskAction.RECORD_LOCATION
    returned = sortie.complete_return(_context(16.0))
    assert returned.task_receipt.phase is OnboardTaskAction.RETURN_HOME
    reported = sortie.report_after_return(_context(16.5))
    assert reported.task_receipt.phase is OnboardTaskAction.REPORT
    assert len(runner.history.entries) == 4
