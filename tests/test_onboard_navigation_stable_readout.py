"""CPU small-tensor fixtures, not actual feature extraction or model training."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import onboard_navigation_stable_readout as stable

torch = pytest.importorskip("torch")
pytest.importorskip("safetensors")


@pytest.fixture(autouse=True)
def cpu_only(monkeypatch):
    assert not torch.cuda.is_initialized()

    def forbidden(*args, **kwargs):
        raise AssertionError("Fixtures may not initialize CUDA")

    monkeypatch.setattr(torch.cuda, "_lazy_init", forbidden)
    yield
    assert not torch.cuda.is_initialized()


@pytest.fixture(scope="module", autouse=True)
def one_thread():
    previous = torch.get_num_threads()
    torch.set_num_threads(1)
    yield
    torch.set_num_threads(previous)


@pytest.fixture
def train():
    return {
        "features": torch.eye(4, dtype=torch.float32) * 100,
        "labels": [
            stable.ReadoutLabel("forward", 3),
            stable.ReadoutLabel("turn_yaw_left", angle_magnitude_rad=0.5),
            stable.ReadoutLabel("turn_yaw_right", angle_magnitude_rad=0.2),
            stable.ReadoutLabel("sequence_stop"),
        ],
        "sample_ids": ["s0", "s1", "s2", "s3"],
        "partitions": ["train"] * 4,
    }


def fit(train, *, rule=None, ledger=None, sink=None, fit_id="fixture"):
    rule = rule or stable.TrainingRule(40, 80, learning_rate=0.001, weight_decay=0)
    ledger = ledger or stable.UpdateLedger(rule.maximum_total_attempts)
    events = []
    result = stable.fit_readout(
        **train, rule=rule, ledger=ledger, event_sink=sink or events.append, fit_id=fit_id
    )
    return result, ledger, events


def source(tmp_path):
    path = tmp_path / "fixture-extraction-evidence.json"
    path.write_text('{"scope":"small_fixture_not_actual_model"}', encoding="utf-8")
    return [stable.file_binding(path)]


def cache_metadata(features):
    observations = []
    for index in range(features.shape[0]):
        records = {
            name: {
                "dtype": "torch.float32" if name == "pixel_values" else "torch.int64",
                "shape": [1, 3],
                "sha256": "c" * 64,
            }
            for name in stable.FOUR_PREFIX
        }
        observations.append(
            {
                "sample_id": f"s{index}",
                "actor_identity_sha256": "d" * 64,
                "prefix_tensors": records,
                "feature": stable.tensor_record(features[index : index + 1]),
                "gold_in_model_inputs": False,
                "source_session_closed_verified": True,
            }
        )
    return {
        "observations": observations,
        "model_identity": {"base": "a" * 64, "adapter": "b" * 64},
        "partitions": ["train"] * features.shape[0],
        "episode_ids": ["fixture-episode"] * features.shape[0],
        "extraction_metadata": {"scope": "declared_fixture_no_actual_model", "runtime": "cpu"},
    }


def load_cache(path, binding, metadata):
    return stable.load_feature_cache(
        path,
        receipt_sha256=binding["sha256"],
        expected_sample_ids=[row["sample_id"] for row in metadata["observations"]],
        expected_model_identity=metadata["model_identity"],
    )


def test_stdlib_import_never_imports_torch_safetensors_or_models():
    root = Path(__file__).resolve().parents[1]
    code = (
        f"import sys; sys.path.insert(0,{str(root)!r}); "
        "from scripts import onboard_navigation_stable_readout; "
        "assert not {'torch','numpy','safetensors','transformers','PIL','peft'} & "
        "{name.split('.')[0] for name in sys.modules}"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_train_only_population_normalization_never_refits_on_dev():
    values = torch.tensor([[10, 7, 3], [14, 7, 3.0000002]], dtype=torch.float32)
    norm = stable.fit_normalization(values, sample_ids=["a", "b"], partitions=["train"] * 2)
    assert norm.center.tolist() == pytest.approx([12, 7, 3])
    assert norm.scale.tolist() == pytest.approx([2, 1, 1])
    assert torch.allclose(stable.normalize(values, norm)[:, 0], torch.tensor([-1.0, 1.0]))
    center, scale = norm.center.clone(), norm.scale.clone()
    dev = torch.tensor([[1000, -1000, 5]], dtype=torch.float32)
    assert stable.normalize(dev, norm)[0, 0].item() == 494
    assert torch.equal(norm.center, center) and torch.equal(norm.scale, scale)
    assert norm.sample_ids == ("a", "b")
    values.fill_(100)
    assert torch.equal(norm.center, center) and torch.equal(norm.scale, scale)


@pytest.mark.parametrize("partitions", [["train", "dev-test"], ["train"], "train"])
def test_nontrain_or_misaligned_normalization_rejected(partitions):
    with pytest.raises(ValueError, match="train-only"):
        stable.fit_normalization(torch.ones(2, 4), sample_ids=["a", "b"], partitions=partitions)


@pytest.mark.parametrize(
    "values",
    [
        torch.zeros(0, 4),
        torch.zeros(4),
        torch.zeros(2, 4).double(),
        torch.full((2, 4), float("nan")),
        torch.full((2, 4), float("inf")),
        torch.zeros(2, 4, requires_grad=True),
        torch.zeros(2, 4).to_sparse(),
    ],
)
def test_nonfinite_dtype_shape_grad_sparse_features_rejected(values):
    with pytest.raises(ValueError):
        stable.fit_normalization(values, sample_ids=["a", "b"], partitions=["train"] * 2)


def test_large_finite_moments_do_not_overflow_float32_sum():
    values = torch.full((4, 2), 2e38, dtype=torch.float32)
    norm = stable.fit_normalization(values, sample_ids=list("abcd"), partitions=["train"] * 4)
    assert torch.isfinite(norm.center).all() and torch.equal(norm.scale, torch.ones(2))
    assert torch.equal(stable.normalize(values, norm), torch.zeros_like(values))


@pytest.mark.parametrize("mutation", ["negative_scale", "nan_center", "wrong_shape"])
def test_mutated_normalization_fails_before_prediction(mutation):
    norm = stable.fit_normalization(torch.eye(2), sample_ids=["a", "b"], partitions=["train"] * 2)
    if mutation == "negative_scale":
        norm.scale[0] = -1
    elif mutation == "nan_center":
        norm.center[0] = float("nan")
    else:
        norm = replace(norm, scale=torch.ones(3))
    with pytest.raises(ValueError):
        stable.predict(stable.make_head(2), norm, torch.eye(2))


def test_loss_keeps_known_only_denominators_and_does_not_zero_supervise_stop():
    labels = [
        stable.ReadoutLabel("forward", 2),
        stable.ReadoutLabel("forward"),
        stable.ReadoutLabel("turn_yaw_left", angle_magnitude_rad=0.5),
        stable.ReadoutLabel("sequence_stop"),
    ]
    values = torch.tensor(
        [
            [0, 0, 0, 0, 1, 200],
            [0, 0, 0, 0, 500, -200],
            [0, 0, 0, 0, -200, 0.25],
            [0, 0, 0, 0, 400, 400],
        ],
        dtype=torch.float32,
        requires_grad=True,
    )
    terms = stable.losses(values, labels, weights=stable.class_weights(labels))
    assert terms["distance"].item() == 0.5
    assert terms["angle"].item() == pytest.approx(0.03125)
    terms["total"].backward()
    assert values.grad[:, 4].tolist() == [-1, 0, 0, 0]
    assert values.grad[:, 5].tolist() == [0, 0, -0.25, 0]
    for label in (stable.ReadoutLabel("sequence_stop"), stable.ReadoutLabel("forward")):
        outputs = torch.zeros(1, 6, requires_grad=True)
        loss = stable.losses(outputs, [label], weights=stable.class_weights([label]))
        loss["total"].backward()
        assert outputs.grad[0, 4:].tolist() == [0, 0]


@pytest.mark.parametrize(
    "fields",
    [
        {"action_class": "up"},
        {"action_class": "sequence_stop", "distance_m": 1},
        {"action_class": "forward", "distance_m": True},
        {"action_class": "forward", "distance_m": float("nan")},
        {"action_class": "turn_yaw_left", "angle_magnitude_rad": 4},
    ],
)
def test_label_validation(fields):
    with pytest.raises(ValueError):
        stable.ReadoutLabel(**fields)


def test_inverse_class_support_is_train_only_and_missing_classes_zero():
    labels = [stable.ReadoutLabel("forward")] * 3 + [stable.ReadoutLabel("sequence_stop")]
    assert stable.class_weights(labels).tolist() == pytest.approx([2 / 3, 0, 0, 2])


def test_final_after_loss_finite_improved_and_fourstage_cumulative(train):
    result, ledger, events = fit(train)
    report = result.report
    assert report["success"] and report["training_loss_improved"]
    assert report["final_loss_recomputed_after_updates"]
    final = stable.losses(
        stable.predict(result.head, result.normalization, train["features"]),
        train["labels"],
        weights=stable.class_weights(train["labels"]),
    )["total"].item()
    assert report["final_train_loss"]["total"] == final
    assert final < report["initial_train_loss"]["total"]
    assert ledger.counts == dict.fromkeys(stable.STAGES, 40)
    assert len(events) == 160 and not ledger.optimizer_update_uncertain
    for number in range(40):
        rows = events[4 * number : 4 * number + 4]
        assert [row["stage"] for row in rows] == list(stable.STAGES)
        assert all(row["global_count"] == number + 1 for row in rows)
        assert len({row["pre_step_loss"] for row in rows}) == 1
    assert all(value is False for value in report["claims"].values())
    assert not report["optimizer_state_restored"]
    assert "dev" not in json.dumps(report)


def test_shared_ledger_second_fit_does_not_reset_and_exhaustion_precedes_adamw(train, monkeypatch):
    _, ledger, _ = fit(train)
    _, same, events = fit(train, ledger=ledger, fit_id="second")
    assert same is ledger and same.counts == dict.fromkeys(stable.STAGES, 80)
    assert events[0]["global_count"] == 41

    def forbidden(*args, **kwargs):
        raise AssertionError("Exhausted budget must precede optimizer construction")

    monkeypatch.setattr(torch.optim, "AdamW", forbidden)
    with pytest.raises(ValueError, match="budget"):
        fit(train, ledger=ledger)
    ledger.counts.update(dict.fromkeys(stable.STAGES, 0))
    with pytest.raises(ValueError, match="ledger"):
        fit(train, ledger=ledger)


def test_failed_final_loss_gate_keeps_all_completed_cost_no_best_checkpoint(train):
    rule = stable.TrainingRule(2, 2, minimum_improvement=1e9)
    ledger = stable.UpdateLedger(2)
    with pytest.raises(stable.FitRejected, match="did not improve") as error:
        fit(train, rule=rule, ledger=ledger)
    report = error.value.report
    assert not report["success"] and not report["training_loss_improved"]
    assert report["final_loss_recomputed_after_updates"]
    assert report["optimizer_counts_after"] == dict.fromkeys(stable.STAGES, 2)
    assert not report["optimizer_update_uncertain"]
    assert "best" not in json.dumps(report)


@pytest.mark.parametrize("failure_stage", list(stable.STAGES))
def test_failed_event_reservation_cost_is_not_refunded(train, failure_stage):
    ledger = stable.UpdateLedger(80)

    def fail(event):
        if event["stage"] == failure_stage:
            raise OSError("durable event write failed")

    with pytest.raises(stable.FitRejected) as error:
        fit(train, ledger=ledger, sink=fail)
    report = error.value.report
    assert ledger.counts["attempted"] == 1 and ledger.optimizer_update_uncertain
    assert report["optimizer_update_uncertain"]
    assert ledger.optimizer_api_calls == (0 if failure_stage == "attempted" else 1)
    with pytest.raises(ValueError, match="uncertain"):
        fit(train, ledger=ledger)


def test_optimizer_may_update_then_raise_is_uncertain_and_counted(train, monkeypatch):
    original = torch.optim.AdamW

    class RaisesAfterUpdate(original):
        def step(self, *args, **kwargs):
            super().step(*args, **kwargs)
            raise RuntimeError("failure after possible update")

    monkeypatch.setattr(torch.optim, "AdamW", RaisesAfterUpdate)
    ledger = stable.UpdateLedger(80)
    with pytest.raises(stable.FitRejected) as error:
        fit(train, ledger=ledger)
    assert error.value.report["optimizer_counts_after"] == {
        "attempted": 1,
        "returned": 0,
        "synchronized": 0,
        "completed": 0,
    }
    assert ledger.optimizer_api_calls == 1 and ledger.optimizer_update_uncertain


@pytest.mark.parametrize(
    "fields",
    [
        {"updates": True},
        {"updates": 0},
        {"updates": 81},
        {"learning_rate": 0.01},
        {"learning_rate": float("nan")},
        {"epsilon": 0},
        {"minimum_improvement": -1},
    ],
)
def test_rule_is_small_explicit_typed_precommit(fields):
    with pytest.raises(ValueError):
        stable.TrainingRule(**({"updates": 40, "maximum_total_attempts": 80} | fields))


def test_cache_roundtrip_exact_bytes_order_and_no_self_certified_model(tmp_path, train):
    features = train["features"]
    metadata = cache_metadata(features)
    path = tmp_path / "cache"
    binding = stable.save_feature_cache(path, features, **metadata, sources=source(tmp_path))
    loaded, meta = load_cache(path, binding, metadata)
    assert torch.equal(loaded, features)
    assert meta["sample_ids"] == train["sample_ids"]
    assert meta["observations"] == metadata["observations"]
    assert all(value is False for value in meta["claims"].values())
    assert meta["source_authentication"] == "byte_bindings_only_not_model_or_prefix_admission"
    assert {p.name for p in path.iterdir()} == {
        "receipt.json",
        "metadata.json",
        "tensors.safetensors",
    }
    with pytest.raises(FileExistsError):
        stable.save_feature_cache(path, features, **metadata, sources=source(tmp_path))
    features.fill_(500)
    assert loaded[0, 0].item() == 100


@pytest.mark.parametrize("mutation", ["tensor", "metadata", "receipt", "source"])
def test_cache_corruption_and_source_drift_rejected(tmp_path, train, mutation):
    metadata = cache_metadata(train["features"])
    sources = source(tmp_path)
    path = tmp_path / "cache"
    binding = stable.save_feature_cache(path, train["features"], **metadata, sources=sources)
    filename = {
        "tensor": "tensors.safetensors",
        "metadata": "metadata.json",
        "receipt": "receipt.json",
    }.get(mutation)
    target = path / filename if filename else Path(sources[0]["path"])
    raw = bytearray(target.read_bytes())
    raw[-2] ^= 1
    target.write_bytes(raw)
    with pytest.raises(ValueError, match="drift"):
        load_cache(path, binding, metadata)


def test_feature_identity_mutation_and_order_expectation_rejected(tmp_path, train):
    metadata = cache_metadata(train["features"])
    path = tmp_path / "cache"
    binding = stable.save_feature_cache(
        path, train["features"], **metadata, sources=source(tmp_path)
    )
    wrong = copy.deepcopy(metadata)
    wrong["observations"].reverse()
    with pytest.raises(ValueError, match="order"):
        load_cache(path, binding, wrong)
    wrong = copy.deepcopy(metadata)
    wrong["model_identity"]["adapter"] = "e" * 64
    with pytest.raises(ValueError, match="model"):
        load_cache(path, binding, wrong)
    bad = copy.deepcopy(metadata)
    bad["observations"][0]["feature"]["sha256"] = "e" * 64
    with pytest.raises(ValueError, match="feature bytes"):
        stable.save_feature_cache(
            tmp_path / "bad", train["features"], **bad, sources=source(tmp_path)
        )
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("mutation", ["gold", "open", "missingprefix", "dtype", "boolshape"])
def test_cache_prefix_declarations_fail_closed(tmp_path, train, mutation):
    metadata = cache_metadata(train["features"])
    first = metadata["observations"][0]
    if mutation == "gold":
        first["gold_in_model_inputs"] = True
    elif mutation == "open":
        first["source_session_closed_verified"] = False
    elif mutation == "missingprefix":
        del first["prefix_tensors"]["pixel_values"]
    elif mutation == "dtype":
        first["prefix_tensors"]["input_ids"]["dtype"] = "torch.float32"
    else:
        first["prefix_tensors"]["input_ids"]["shape"] = [True, 3]
    with pytest.raises(ValueError):
        stable.save_feature_cache(
            tmp_path / "bad", train["features"], **metadata, sources=source(tmp_path)
        )


def test_duplicate_sources_and_source_drift_after_write_leave_no_success_receipt(
    tmp_path, train, monkeypatch
):
    metadata = cache_metadata(train["features"])
    sources = source(tmp_path)
    with pytest.raises(ValueError, match="duplicate"):
        stable.save_feature_cache(
            tmp_path / "bad", train["features"], **metadata, sources=sources * 2
        )
    original = stable._write

    def source_changes(path, raw):
        original(path, raw)
        if path.name == "metadata.json":
            Path(sources[0]["path"]).write_text("changed", encoding="utf-8")

    monkeypatch.setattr(stable, "_write", source_changes)
    with pytest.raises(ValueError, match="Source drift"):
        stable.save_feature_cache(
            tmp_path / "partial", train["features"], **metadata, sources=sources
        )
    assert not (tmp_path / "partial" / "receipt.json").exists()
    assert (tmp_path / "partial" / "tensors.safetensors").exists()


def test_saved_head_plus_train_norm_roundtrip_matches_predictions(tmp_path, train):
    result, _, _ = fit(train)
    path = tmp_path / "head"
    receipt = stable.save_readout(path, result, sources=source(tmp_path))
    head, norm, metadata = stable.load_readout(path, receipt_sha256=receipt["sha256"])
    expected = stable.predict(result.head, result.normalization, train["features"]).detach()
    assert torch.equal(stable.predict(head, norm, train["features"]), expected)
    assert norm.sample_ids == tuple(train["sample_ids"])
    assert not metadata["optimizer_state_saved"]
    assert all(not parameter.requires_grad for parameter in head.parameters())
    assert torch.equal(norm.center, result.normalization.center)
    assert torch.equal(norm.scale, result.normalization.scale)


@pytest.mark.parametrize("mutation", ["head", "norm"])
def test_mutated_postfit_state_cannot_reuse_old_loss_gate(tmp_path, train, mutation):
    result, _, _ = fit(train)
    with torch.no_grad():
        if mutation == "head":
            result.head.bias[0] += 1
        else:
            result.normalization.center[0] += 1
    with pytest.raises(ValueError, match="changed after fit"):
        stable.save_readout(tmp_path / "bad", result, sources=source(tmp_path))
    assert not (tmp_path / "bad").exists()


def test_cache_path_escape_binding_cannot_be_rewritten_to_other_file(tmp_path, train):
    metadata = cache_metadata(train["features"])
    path = tmp_path / "cache"
    binding = stable.save_feature_cache(
        path, train["features"], **metadata, sources=source(tmp_path)
    )
    receipt = json.loads((path / "receipt.json").read_bytes())
    receipt["weights"]["path"] = (tmp_path / "elsewhere.safetensors").as_posix()
    raw = stable.canonical(receipt) + b"\n"
    (path / "receipt.json").write_bytes(raw)
    binding["sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="path escape"):
        load_cache(path, binding, metadata)


def test_episode_partition_cannot_be_split_and_metadata_preserved(tmp_path, train):
    metadata = cache_metadata(train["features"])
    metadata["partitions"][0] = "dev-test"
    with pytest.raises(ValueError, match="entire episode"):
        stable.save_feature_cache(
            tmp_path / "bad", train["features"], **metadata, sources=source(tmp_path)
        )
    metadata["episode_ids"][0] = "heldout-episode"
    binding = stable.save_feature_cache(
        tmp_path / "good", train["features"], **metadata, sources=source(tmp_path)
    )
    _, actual = load_cache(tmp_path / "good", binding, metadata)
    assert actual["episode_ids"] == metadata["episode_ids"]
    assert actual["partitions"] == metadata["partitions"]
    assert actual["extraction_metadata_declared"] == metadata["extraction_metadata"]


def test_declared_small_source_size_is_checked_before_hashing_large_file(tmp_path, monkeypatch):
    path = tmp_path / "source.bin"
    path.write_bytes(b"actual data")

    def forbidden(*args, **kwargs):
        raise AssertionError("Drifted actual source must not be opened and hashed")

    monkeypatch.setattr(stable, "file_binding", forbidden)
    with pytest.raises(ValueError, match="before bounded"):
        stable._sources([{"path": path.as_posix(), "bytes": 1, "sha256": "a" * 64}])


def test_default2048_head_is_exact_cpu_fp32_small_sidecar():
    before = torch.get_rng_state().clone()
    head = stable.make_head()
    assert torch.equal(torch.get_rng_state(), before)
    assert sum(parameter.numel() for parameter in head.parameters()) == 12294
    assert all(
        parameter.dtype == torch.float32 and parameter.device.type == "cpu"
        for parameter in head.parameters()
    )


def test_feature_boolean_shape_cannot_alias_integer_shape(tmp_path, train):
    metadata = cache_metadata(train["features"])
    metadata["observations"][0]["feature"]["shape"][0] = True
    with pytest.raises(ValueError, match="feature bytes"):
        stable.save_feature_cache(
            tmp_path / "bad", train["features"], **metadata, sources=source(tmp_path)
        )


def test_output_only_postwrite_drift_prevents_success_receipt(tmp_path, train, monkeypatch):
    metadata = cache_metadata(train["features"])
    original = stable._sources
    calls = 0

    def changed_at_after(rows):
        nonlocal calls
        calls += 1
        if calls == 2:
            path = tmp_path / "partial" / "metadata.json"
            path.write_bytes(path.read_bytes() + b" ")
        return original(rows)

    monkeypatch.setattr(stable, "_sources", changed_at_after)
    with pytest.raises(ValueError, match="output byte drift"):
        stable.save_feature_cache(
            tmp_path / "partial", train["features"], **metadata, sources=source(tmp_path)
        )
    assert not (tmp_path / "partial" / "receipt.json").exists()


def test_pristine_adapter_none_roundtrip_is_declared_not_authenticated(tmp_path, train):
    metadata = cache_metadata(train["features"])
    metadata["model_identity"]["adapter"] = None
    path = tmp_path / "pristine"
    binding = stable.save_feature_cache(
        path, train["features"], **metadata, sources=source(tmp_path)
    )
    _, loaded = load_cache(path, binding, metadata)
    assert loaded["model_identity_declared"] == {"base": "a" * 64, "adapter": None}
    assert loaded["claims"]["actual_model_identity_verified"] is False
    wrong = copy.deepcopy(metadata)
    wrong["model_identity"]["adapter"] = "b" * 64
    with pytest.raises(ValueError, match="model declaration"):
        load_cache(path, binding, wrong)


@pytest.mark.parametrize(
    "identity",
    [
        {"base": "a" * 64},
        {"adapter": None},
        {"base": None, "adapter": None},
        {"base": "a" * 64, "adapter": False},
    ],
)
def test_missing_or_invalid_model_identity_cannot_be_filled_with_defaults(
    tmp_path, train, identity
):
    metadata = cache_metadata(train["features"])
    metadata["model_identity"] = identity
    with pytest.raises(ValueError):
        stable.save_feature_cache(
            tmp_path / "bad", train["features"], **metadata, sources=source(tmp_path)
        )
    assert not (tmp_path / "bad").exists()
