"""CPU-only stable sidecar primitives; no model, processor, or flight admission.

Importing this module is stdlib-only. Operations lazily use Torch and the frozen
readout loss. Normalization is fitted exclusively on explicitly labelled train
rows. A cache authenticates its bytes and bound small extraction evidence, not
the caller's claim that features came from an actual admitted model. No budget
is granted here: callers must supply one precommitted cumulative ledger.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

CONTRACT = "multidetect.onboard-navigation-stable-readout-v1"
FEATURE_DIM = 2048
CLASS_NAMES = ("forward", "turn_yaw_left", "turn_yaw_right", "sequence_stop")
STAGES = ("attempted", "returned", "synchronized", "completed")
FOUR_PREFIX = {"input_ids", "attention_mask", "pixel_values", "image_grid_thw"}
MAX_TENSOR_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
LOSS_PATH = Path(__file__).resolve().with_name("onboard_navigation_action_readout.py")
LOSS_BYTES = 11143
LOSS_SHA256 = "b4191c30b7e927fad7845347932f699e32c928ed45bca291d2f526b5e8860367"
FALSE_CLAIMS = {
    "actual_model_identity_verified": False,
    "prefix_only_independently_verified": False,
    "quality_claim": False,
    "candidate_promoted": False,
    "execution_authority": False,
    "final_release_allowed": False,
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(value):
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("Canonical lowercase SHA-256 required")
    return value


def _ids(values):
    if (
        not isinstance(values, (list, tuple))
        or not values
        or any(type(value) is not str or not value for value in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError("Nonempty unique ordered sample IDs required")
    return tuple(values)


def _positive(value, name, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError(f"Invalid {name}")
    return float(value)


def _torch():
    return importlib.import_module("torch")


def _loss_api():
    if LOSS_PATH.stat().st_size != LOSS_BYTES or file_binding(LOSS_PATH)["sha256"] != LOSS_SHA256:
        raise ValueError("Frozen loss component source drift")
    module = importlib.import_module("scripts.onboard_navigation_action_readout")
    if Path(module.__file__).resolve() != LOSS_PATH:
        raise ValueError("Frozen loss component origin differs")
    return module


def _tensor(value, *, shape, name="tensor", detached=True):
    torch = _torch()
    if (
        not isinstance(value, torch.Tensor)
        or value.layout != torch.strided
        or value.dtype != torch.float32
        or value.device.type != "cpu"
        or value.ndim != len(shape)
        or any(a < 1 or b is not None and a != b for a, b in zip(value.shape, shape, strict=True))
        or detached
        and value.requires_grad
        or not torch.isfinite(value).all().item()
    ):
        raise ValueError(f"Finite dense CPU FP32 {name} with exact shape required")
    if value.numel() * value.element_size() > MAX_TENSOR_BYTES:
        raise ValueError("Small tensor byte cap exceeded")
    return value


def tensor_record(value):
    _tensor(value, shape=tuple(value.shape), detached=False)
    raw = value.detach().contiguous().view(_torch().uint8).numpy().tobytes()
    return {
        "shape": list(value.shape),
        "dtype": "torch.float32",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


@dataclass(frozen=True)
class ReadoutLabel:
    action_class: str
    distance_m: float | None = None
    angle_magnitude_rad: float | None = None

    def __post_init__(self):
        if type(self.action_class) is not str or self.action_class not in CLASS_NAMES:
            raise ValueError("Unsupported label class")
        if self.distance_m is not None:
            if self.action_class != "forward":
                raise ValueError("Only forward may have distance")
            object.__setattr__(self, "distance_m", _positive(self.distance_m, "distance", 500))
        if self.angle_magnitude_rad is not None:
            if self.action_class not in CLASS_NAMES[1:3]:
                raise ValueError("Only yaw may have angle")
            object.__setattr__(
                self, "angle_magnitude_rad", _positive(self.angle_magnitude_rad, "angle", math.pi)
            )


def _labels(labels, count=None):
    if (
        not isinstance(labels, Sequence)
        or not labels
        or any(type(row) is not ReadoutLabel for row in labels)
        or count is not None
        and len(labels) != count
    ):
        raise ValueError("Exact typed labels must align with feature rows")
    api = _loss_api()
    return [api.ActionReadoutLabel(**asdict(row)) for row in labels]


def class_weights(labels):
    return _loss_api().class_balanced_weights(_labels(labels))


def losses(outputs, labels, *, weights):
    _tensor(outputs, shape=(None, 6), name="outputs", detached=False)
    _tensor(weights, shape=(4,), name="class weights")
    return _loss_api().readout_loss(
        outputs, _labels(labels, outputs.shape[0]), class_weights=weights
    )


@dataclass(frozen=True)
class TrainingRule:
    updates: int
    maximum_total_attempts: int
    learning_rate: float = 0.0001
    weight_decay: float = 0.01
    epsilon: float = 0.000001
    minimum_improvement: float = 0.0
    seed: int = 0

    def __post_init__(self):
        if (
            type(self.updates) is not int
            or type(self.maximum_total_attempts) is not int
            or not 1 <= self.updates <= self.maximum_total_attempts
            or type(self.seed) is not int
            or not 0 <= self.seed < 2**63
        ):
            raise ValueError("Explicit positive precommitted update cap/seed required")
        _positive(self.learning_rate, "small AdamW learning rate", 0.001)
        _positive(self.epsilon, "normalization epsilon", 1)
        for name in ("weight_decay", "minimum_improvement"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid {name}")


class UpdateLedger:
    """Shared cumulative counters. A failed reservation is never refunded.

    An unfinished step prevents another fit, since optimizer outcome may be
    uncertain. This object does not durably authenticate counters; a caller
    must bind and persist its initial state and its event sink.
    """

    def __init__(self, maximum_attempts, *, counts=None):
        self.maximum_attempts = maximum_attempts
        self.counts = dict.fromkeys(STAGES, 0) if counts is None else dict(counts)
        self.optimizer_update_uncertain = False
        self.optimizer_api_calls = 0
        self._validate()
        self._floor = dict(self.counts)
        self.optimizer_update_uncertain = self.counts["attempted"] != self.counts["completed"]

    def _validate(self):
        if (
            type(self.maximum_attempts) is not int
            or self.maximum_attempts < 1
            or set(self.counts) != set(STAGES)
            or any(type(value) is not int or value < 0 for value in self.counts.values())
            or any(
                value < getattr(self, "_floor", {}).get(name, 0)
                for name, value in self.counts.items()
            )
            or not (
                self.counts["completed"]
                <= self.counts["synchronized"]
                <= self.counts["returned"]
                <= self.counts["attempted"]
                <= self.maximum_attempts
            )
        ):
            raise ValueError("Invalid cumulative optimizer ledger")

    def require_room(self, rule):
        self._validate()
        if (
            self.maximum_attempts != rule.maximum_total_attempts
            or self.optimizer_update_uncertain
            or self.counts["attempted"] != self.counts["completed"]
            or self.counts["attempted"] + rule.updates > self.maximum_attempts
        ):
            raise ValueError("Cumulative budget exhausted, mismatched, or uncertain")

    def publish(self, stage, sink, *, fit_id, update, pre_step_loss):
        if stage not in STAGES:
            raise ValueError("Unknown optimizer stage")
        if stage == "attempted":
            if self.counts[stage] >= self.maximum_attempts:
                raise ValueError("Attempt cap exhausted")
            self.optimizer_update_uncertain = True
        else:
            previous = STAGES[STAGES.index(stage) - 1]
            if self.counts[stage] + 1 != self.counts[previous]:
                raise ValueError("Optimizer stages out of order")
        self.counts[stage] += 1
        self._floor = dict(self.counts)
        sink(
            {
                "fit_id": fit_id,
                "update": update,
                "stage": stage,
                "global_count": self.counts[stage],
                "pre_step_loss": pre_step_loss,
            }
        )
        if stage == "completed":
            self.optimizer_update_uncertain = False


@dataclass(frozen=True)
class TrainNormalization:
    center: object
    scale: object
    sample_ids: tuple[str, ...]
    epsilon: float

    def validate(self):
        _ids(self.sample_ids)
        _positive(self.epsilon, "epsilon", 1)
        _tensor(self.center, shape=(None,), name="normalization center")
        _tensor(self.scale, shape=tuple(self.center.shape), name="normalization scale")
        if not (self.scale > 0).all().item():
            raise ValueError("Normalization scales must be positive")


def fit_normalization(features, *, sample_ids, partitions, epsilon=0.000001):
    features = _tensor(features, shape=(None, None), name="features")
    ids = _ids(sample_ids)
    if len(ids) != features.shape[0] or list(partitions) != ["train"] * len(ids):
        raise ValueError("Fit normalization on explicitly train-only rows")
    _positive(epsilon, "epsilon", 1)
    # Float64 moments avoid FP32 sum overflow; statistics are saved as FP32.
    moments = features.detach().double()
    center = moments.mean(dim=0).float()
    std = moments.std(dim=0, correction=0).float()
    scale = _torch().where(std >= epsilon, std, _torch().ones_like(std))
    norm = TrainNormalization(center.detach().clone(), scale.detach().clone(), ids, epsilon)
    norm.validate()
    return norm


def normalize(features, normalization):
    if type(normalization) is not TrainNormalization:
        raise ValueError("Exact train normalization required")
    normalization.validate()
    features = _tensor(features, shape=(None, normalization.center.numel()), name="features")
    values = (features.detach() - normalization.center) / normalization.scale
    return _tensor(values, shape=tuple(features.shape), name="normalized features")


def make_head(feature_dim=FEATURE_DIM, *, seed=0):
    if type(feature_dim) is not int or not 1 <= feature_dim <= MAX_TENSOR_BYTES // 24:
        raise ValueError("Bounded positive feature dimension required")
    if type(seed) is not int or not 0 <= seed < 2**63:
        raise ValueError("Canonical seed required")
    torch = _torch()
    previous = torch.get_rng_state()
    try:
        torch.random.default_generator.manual_seed(seed)
        return torch.nn.Linear(feature_dim, 6, device="cpu", dtype=torch.float32)
    finally:
        torch.set_rng_state(previous)


def predict(head, normalization, features):
    values = normalize(features, normalization)
    _tensor(head.weight, shape=(6, values.shape[1]), name="head weight", detached=False)
    _tensor(head.bias, shape=(6,), name="head bias", detached=False)
    return _tensor(head(values), shape=(values.shape[0], 6), name="output", detached=False)


@dataclass(frozen=True)
class FitResult:
    head: object
    normalization: TrainNormalization
    report: dict


class FitRejected(RuntimeError):
    def __init__(self, message, report):
        self.report = report
        super().__init__(message)


def _loss_record(values):
    return {name: float(value.detach()) for name, value in values.items()}


def fit_readout(features, labels, *, sample_ids, partitions, rule, ledger, event_sink, fit_id):
    """Precommitted last iterate only; final loss is recomputed AFTER updates.

    There is no dev input, early stopping, best-checkpoint selection, retry,
    or budget reset. Improvement of training loss is a numerical smoke gate,
    not generalization, metric accuracy, or authorization to deploy.
    """
    if (
        type(rule) is not TrainingRule
        or type(ledger) is not UpdateLedger
        or not callable(event_sink)
        or type(fit_id) is not str
        or not fit_id
    ):
        raise ValueError("Explicit rule/ledger/event sink/fit ID required")
    ledger.require_room(rule)
    normalization = fit_normalization(
        features, sample_ids=sample_ids, partitions=partitions, epsilon=rule.epsilon
    )
    _labels(labels, len(sample_ids))
    weights = class_weights(labels)
    torch = _torch()
    head = make_head(features.shape[1], seed=rule.seed)
    normalized = normalize(features, normalization)
    before_counts = dict(ledger.counts)
    report = {
        "contract": CONTRACT,
        "success": False,
        "training_loss_improved": False,
        "final_loss_recomputed_after_updates": False,
        "rule": asdict(rule),
        "training_sample_ids": list(normalization.sample_ids),
        "class_weights": weights.tolist(),
        "optimizer_counts_before": before_counts,
        "claims": dict(FALSE_CLAIMS),
        "optimizer_state_restored": False,
        "optimizer_kind": "fresh_cpu_fp32_AdamW",
    }
    try:
        with torch.no_grad():
            report["initial_train_loss"] = _loss_record(
                losses(head(normalized), labels, weights=weights)
            )
        optimizer = torch.optim.AdamW(
            head.parameters(), lr=rule.learning_rate, weight_decay=rule.weight_decay
        )
        for update in range(1, rule.updates + 1):
            optimizer.zero_grad(set_to_none=True)
            value = losses(head(normalized), labels, weights=weights)["total"]
            value.backward()
            if any(
                p.grad is None or not torch.isfinite(p.grad).all().item() for p in head.parameters()
            ):
                raise ValueError("Finite FP32 head gradients required")
            args = {"fit_id": fit_id, "update": update, "pre_step_loss": float(value.detach())}
            ledger.publish("attempted", event_sink, **args)
            ledger.optimizer_api_calls += 1
            optimizer.step()
            ledger.publish("returned", event_sink, **args)
            # CPU optimizer steps are synchronous; this stage never calls CUDA.
            ledger.publish("synchronized", event_sink, **args)
            for parameter in head.parameters():
                _tensor(parameter, shape=tuple(parameter.shape), detached=False)
            ledger.publish("completed", event_sink, **args)
            del value
        head.zero_grad(set_to_none=True)
        head.eval()
        with torch.no_grad():
            report["final_train_loss"] = _loss_record(
                losses(head(normalized), labels, weights=weights)
            )
        report["final_loss_recomputed_after_updates"] = True
        report["training_loss_improved"] = (
            report["initial_train_loss"]["total"] - report["final_train_loss"]["total"]
            > rule.minimum_improvement
        )
        if not report["training_loss_improved"]:
            raise ValueError("Final-after training loss did not improve")
        report["final_tensor_records"] = {
            "weight": tensor_record(head.weight),
            "bias": tensor_record(head.bias),
            "center": tensor_record(normalization.center),
            "scale": tensor_record(normalization.scale),
        }
        report["success"] = True
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise FitRejected(str(exc), report) from exc
    finally:
        report["optimizer_counts_after"] = dict(ledger.counts)
        report["optimizer_update_uncertain"] = ledger.optimizer_update_uncertain
        report["optimizer_api_calls_observed_since_ledger_creation"] = ledger.optimizer_api_calls
    return FitResult(head, normalization, report)


def file_binding(path):
    path = Path(path).resolve(strict=True)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return {"path": path.as_posix(), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def _sources(rows):
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("Explicit small extraction-evidence source bindings required")
    verified, seen = [], set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"path", "bytes", "sha256"}:
            raise ValueError("Exact file binding required")
        _sha(row["sha256"])
        if type(row["bytes"]) is not int or not 0 <= row["bytes"] <= MAX_TENSOR_BYTES:
            raise ValueError("Small bound evidence only; model weights are admitted elsewhere")
        if Path(row["path"]).stat().st_size != row["bytes"]:
            raise ValueError("Source drift before bounded byte hashing")
        actual = file_binding(row["path"])
        if actual != dict(row) or actual["path"] in seen:
            raise ValueError("Source drift, duplicate, or noncanonical absolute binding")
        seen.add(actual["path"])
        verified.append(actual)
    return verified


def _json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(value):
        raise ValueError(f"Nonfinite JSON value: {value}")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)


def _write(path, raw):
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _save_bundle(directory, tensors, metadata, *, sources):
    from safetensors.torch import load, save

    before = _sources(sources)
    if sum(value.numel() * value.element_size() for value in tensors.values()) > MAX_TENSOR_BYTES:
        raise ValueError("Small bundle payload cap exceeded")
    records = {name: tensor_record(value) for name, value in tensors.items()}
    raw = save({name: value.detach().contiguous().clone() for name, value in tensors.items()})
    if len(raw) > MAX_TENSOR_BYTES:
        raise ValueError("Safetensors file cap exceeded")
    metadata = metadata | {
        "contract": CONTRACT,
        "tensors": records,
        "source_bindings": before,
        "claims": dict(FALSE_CLAIMS),
    }
    meta_raw = canonical(metadata) + b"\n"
    if len(meta_raw) > MAX_METADATA_BYTES:
        raise ValueError("Metadata byte cap exceeded")
    directory = Path(directory).absolute()
    if not directory.parent.is_dir() or directory.is_symlink():
        raise ValueError("Existing parent and fresh nonsymlink output required")
    directory.mkdir(exist_ok=False)
    _write(directory / "tensors.safetensors", raw)
    _write(directory / "metadata.json", meta_raw)
    reloaded = load((directory / "tensors.safetensors").read_bytes())
    if set(reloaded) != set(tensors) or any(
        canonical(tensor_record(reloaded[name])) != canonical(records[name]) for name in tensors
    ):
        raise ValueError("Exact saved tensor reload failed")
    after = _sources(before)
    metadata_binding = file_binding(directory / "metadata.json")
    weights_binding = file_binding(directory / "tensors.safetensors")
    if (
        metadata_binding["sha256"] != hashlib.sha256(meta_raw).hexdigest()
        or metadata_binding["bytes"] != len(meta_raw)
        or weights_binding["sha256"] != hashlib.sha256(raw).hexdigest()
        or weights_binding["bytes"] != len(raw)
    ):
        raise ValueError("Published output byte drift after writing")
    receipt = {
        "contract": CONTRACT,
        "kind": metadata["kind"],
        "metadata": metadata_binding,
        "weights": weights_binding,
        "source_bindings_before": before,
        "source_bindings_after": after,
        "source_bindings_unchanged": before == after,
        "exact_tensor_reload": True,
        "claims": dict(FALSE_CLAIMS),
    }
    _write(directory / "receipt.json", canonical(receipt) + b"\n")
    return file_binding(directory / "receipt.json")


def _read_bound(row, *, cap):
    if type(row["bytes"]) is not int or not 0 <= row["bytes"] <= cap:
        raise ValueError("Bound file byte cap exceeded")
    _sha(row["sha256"])
    path = Path(row["path"])
    if path.stat().st_size != row["bytes"]:
        raise ValueError("Bound file size drift")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != row["sha256"]:
        raise ValueError("Bound file SHA drift")
    return raw


def _load_bundle(directory, *, receipt_sha256, kind):
    directory = Path(directory).resolve(strict=True)
    if any(path.is_symlink() or not path.is_file() for path in directory.iterdir()):
        raise ValueError("Only three regular nonsymlink bundle files permitted")
    if {p.name for p in directory.iterdir()} != {
        "receipt.json",
        "metadata.json",
        "tensors.safetensors",
    }:
        raise ValueError("Exact three-file bundle required")
    receipt_path = directory / "receipt.json"
    receipt_binding = {
        "path": receipt_path.as_posix(),
        "bytes": receipt_path.stat().st_size,
        "sha256": _sha(receipt_sha256),
    }
    receipt = _json(_read_bound(receipt_binding, cap=MAX_METADATA_BYTES))
    if (
        receipt["contract"] != CONTRACT
        or receipt["kind"] != kind
        or receipt["exact_tensor_reload"] is not True
        or receipt["source_bindings_unchanged"] is not True
        or receipt["source_bindings_before"] != receipt["source_bindings_after"]
        or canonical(receipt["claims"]) != canonical(FALSE_CLAIMS)
    ):
        raise ValueError("Bundle receipt scope or source gate differs")
    for name, filename in (("metadata", "metadata.json"), ("weights", "tensors.safetensors")):
        if receipt[name]["path"] != (directory / filename).as_posix():
            raise ValueError("Bundle path escape or relocation")
    metadata = _json(_read_bound(receipt["metadata"], cap=MAX_METADATA_BYTES))
    before = _sources(receipt["source_bindings_before"])
    if (
        metadata["contract"] != CONTRACT
        or metadata["kind"] != kind
        or metadata["source_bindings"] != before
        or canonical(metadata["claims"]) != canonical(FALSE_CLAIMS)
    ):
        raise ValueError("Metadata contract or source binding differs")
    from safetensors.torch import load

    tensors = load(_read_bound(receipt["weights"], cap=MAX_TENSOR_BYTES))
    if set(tensors) != set(metadata["tensors"]) or any(
        canonical(tensor_record(value)) != canonical(metadata["tensors"][name])
        for name, value in tensors.items()
    ):
        raise ValueError("Actual tensor metadata mismatch")
    if _sources(before) != before:
        raise ValueError("Source changed while loading")
    return tensors, metadata


def _cache_metadata(
    features, *, observations, model_identity, partitions, episode_ids, extraction_metadata
):
    features = _tensor(features, shape=(None, None), name="cached features")
    if not isinstance(observations, list) or len(observations) != features.shape[0]:
        raise ValueError("Ordered feature observations required")
    ids = _ids([row["sample_id"] for row in observations])
    if len(partitions) != len(ids) or any(
        part not in ("train", "dev-validation", "dev-test") for part in partitions
    ):
        raise ValueError("Explicit row partitions required")
    if len(episode_ids) != len(ids) or any(
        type(value) is not str or not value for value in episode_ids
    ):
        raise ValueError("Explicit aligned episode identity required")
    episode_roles = {}
    for episode, part in zip(episode_ids, partitions, strict=True):
        if episode_roles.setdefault(episode, part) != part:
            raise ValueError("An entire episode must retain a single partition")
    if not isinstance(extraction_metadata, Mapping) or not extraction_metadata:
        raise ValueError("Explicit caller-declared runtime/extraction metadata required")
    canonical(extraction_metadata)
    if set(model_identity) != {"base", "adapter"}:
        raise ValueError("Explicit declared base/adapter parameter identities required")
    _sha(model_identity["base"])
    # Explicit None is the declared pristine/no-adapter baseline, not a fake SHA.
    if model_identity["adapter"] is not None:
        _sha(model_identity["adapter"])
    for index, row in enumerate(observations):
        _sha(row["actor_identity_sha256"])
        if (
            row.get("gold_in_model_inputs") is not False
            or row.get("source_session_closed_verified") is not True
            or set(row["prefix_tensors"]) != FOUR_PREFIX
            or canonical(row["feature"]) != canonical(tensor_record(features[index : index + 1]))
        ):
            raise ValueError("Closed prefix-only declaration or actual feature bytes differ")
        for name, record in row["prefix_tensors"].items():
            if set(record) != {"dtype", "shape", "sha256"}:
                raise ValueError("Exact prefix tensor record required")
            _sha(record["sha256"])
            if (
                not isinstance(record["shape"], list)
                or not record["shape"]
                or any(type(size) is not int or size < 1 for size in record["shape"])
                or record["dtype"] != ("torch.float32" if name == "pixel_values" else "torch.int64")
            ):
                raise ValueError("Typed actual prefix tensor record required")
    return {
        "kind": "feature-cache",
        "sample_ids": list(ids),
        "partitions": list(partitions),
        "episode_ids": list(episode_ids),
        "extraction_metadata_declared": dict(extraction_metadata),
        "model_identity_declared": dict(model_identity),
        "observations": observations,
        "feature_dim": features.shape[1],
        "source_authentication": "byte_bindings_only_not_model_or_prefix_admission",
    }


def save_feature_cache(
    directory,
    features,
    *,
    observations,
    model_identity,
    partitions,
    episode_ids,
    extraction_metadata,
    sources,
):
    metadata = _cache_metadata(
        features,
        observations=observations,
        model_identity=model_identity,
        partitions=partitions,
        episode_ids=episode_ids,
        extraction_metadata=extraction_metadata,
    )
    return _save_bundle(directory, {"features": features}, metadata, sources=sources)


def load_feature_cache(directory, *, receipt_sha256, expected_sample_ids, expected_model_identity):
    tensors, metadata = _load_bundle(directory, receipt_sha256=receipt_sha256, kind="feature-cache")
    if set(tensors) != {"features"}:
        raise ValueError("Feature cache may contain only actual feature tensor")
    actual = _cache_metadata(
        tensors["features"],
        observations=metadata["observations"],
        model_identity=metadata["model_identity_declared"],
        partitions=metadata["partitions"],
        episode_ids=metadata["episode_ids"],
        extraction_metadata=metadata["extraction_metadata_declared"],
    )
    if (
        any(canonical(metadata[key]) != canonical(value) for key, value in actual.items())
        or list(_ids(expected_sample_ids)) != metadata["sample_ids"]
        or canonical(expected_model_identity) != canonical(metadata["model_identity_declared"])
    ):
        raise ValueError("Feature order, model declaration, or metadata differs")
    return tensors["features"], metadata


def save_readout(directory, result, *, sources):
    if type(result) is not FitResult or result.report.get("success") is not True:
        raise ValueError("Only a final-after loss-gated fit may be saved")
    norm = result.normalization
    norm.validate()
    dim = norm.center.numel()
    _tensor(result.head.weight, shape=(6, dim), detached=False)
    _tensor(result.head.bias, shape=(6,), detached=False)
    tensors = {
        "weight": result.head.weight,
        "bias": result.head.bias,
        "center": norm.center,
        "scale": norm.scale,
    }
    if (
        result.report.get("training_loss_improved") is not True
        or result.report.get("final_loss_recomputed_after_updates") is not True
        or result.report.get("optimizer_update_uncertain") is not False
        or result.report.get("training_sample_ids") != list(norm.sample_ids)
        or result.report.get("final_tensor_records")
        != {name: tensor_record(value) for name, value in tensors.items()}
    ):
        raise ValueError("Loss-gated head/normalization changed after fit")
    metadata = {
        "kind": "readout",
        "feature_dim": dim,
        "normalization_train_ids": list(norm.sample_ids),
        "epsilon": norm.epsilon,
        "small_or_constant_scale_rule": "population_std_below_epsilon_uses_one",
        "training_report": result.report,
        "optimizer_state_saved": False,
    }
    return _save_bundle(directory, tensors, metadata, sources=sources)


def load_readout(directory, *, receipt_sha256):
    tensors, metadata = _load_bundle(directory, receipt_sha256=receipt_sha256, kind="readout")
    if set(tensors) != {"weight", "bias", "center", "scale"}:
        raise ValueError("Exact head plus normalization tensors required")
    report = metadata["training_report"]
    if (
        report["success"] is not True
        or report["training_loss_improved"] is not True
        or report["final_loss_recomputed_after_updates"] is not True
        or metadata["optimizer_state_saved"] is not False
        or canonical(report["claims"]) != canonical(FALSE_CLAIMS)
        or report["training_sample_ids"] != metadata["normalization_train_ids"]
        or report["final_tensor_records"] != metadata["tensors"]
        or report["optimizer_update_uncertain"] is not False
        or metadata["small_or_constant_scale_rule"] != "population_std_below_epsilon_uses_one"
    ):
        raise ValueError("Final-after loss gate or train-only normalization identity differs")
    normalization = TrainNormalization(
        tensors["center"],
        tensors["scale"],
        _ids(metadata["normalization_train_ids"]),
        metadata["epsilon"],
    )
    normalization.validate()
    dim = metadata["feature_dim"]
    _tensor(normalization.center, shape=(dim,))
    _tensor(tensors["weight"], shape=(6, dim))
    _tensor(tensors["bias"], shape=(6,))
    head = make_head(dim)
    head.load_state_dict({"weight": tensors["weight"], "bias": tensors["bias"]}, strict=True)
    head.eval()
    for parameter in head.parameters():
        parameter.requires_grad_(False)
    return head, normalization, metadata
