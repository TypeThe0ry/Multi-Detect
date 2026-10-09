"""Read-only CPU linear-head diagnostics; no backbone, optimizer or quality gate.

Each small bound input is read once, then its size/mtime/file identity is checked
at exit. NumPy FP32 replay is not a bitwise replay of PyTorch's CPU BLAS. Live
feature records contain hashes, not feature bytes: their contributions cannot
be independently reconstructed from the live report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 8 * 1024 * 1024
CLASSES = ("forward", "turn_yaw_left", "turn_yaw_right", "sequence_stop")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def decode(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(value):
        raise ValueError("Nonfinite JSON: " + value)

    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
    canonical(result)
    return result


class BoundInputs:
    def __init__(self):
        self.rows = {}
        self.raw = {}
        self.stats = {}

    @staticmethod
    def stat(path):
        value = path.stat()
        return (value.st_size, value.st_mtime_ns, value.st_dev, value.st_ino)

    def read(self, row):
        if set(row) != {"path", "bytes", "sha256"} or type(row["bytes"]) is not int:
            raise ValueError("Exact file binding required")
        path = Path(row["path"])
        path = (path if path.is_absolute() else ROOT / path).resolve(strict=True)
        if not path.is_relative_to(ROOT.resolve()) or not 0 < row["bytes"] <= MAX_BYTES:
            raise ValueError("Only bounded repository inputs allowed")
        key = path.as_posix()
        if key not in self.raw:
            before = self.stat(path)
            raw = path.read_bytes()
            if self.stat(path) != before:
                raise ValueError("Input changed during read")
            self.raw[key], self.stats[key] = raw, before
        raw = self.raw[key]
        if len(raw) != row["bytes"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError("Current file bytes/SHA differ")
        self.rows[key] = {**row, "path": key}
        return raw

    def json(self, row):
        return decode(self.read(row))

    def close(self):
        if any(self.stat(Path(key)) != value for key, value in self.stats.items()):
            raise ValueError("Input identity changed after CPU diagnostics")


def bundle(raw, records, np):
    if len(raw) < 8:
        raise ValueError("Truncated safetensors")
    size = int.from_bytes(raw[:8], "little")
    header = decode(raw[8 : 8 + size])
    entries = {k: v for k, v in header.items() if k != "__metadata__"}
    if set(entries) != set(records):
        raise ValueError("Exact tensor key set required")
    cursor, values = 0, {}
    for name, row in sorted(entries.items(), key=lambda item: item[1]["data_offsets"]):
        start, end = row["data_offsets"]
        shape = row["shape"]
        if (
            row["dtype"] != "F32"
            or type(start) is not int
            or type(end) is not int
            or start < 0
            or start != cursor
            or any(type(n) is not int or n < 1 for n in shape)
            or end - start != math.prod(shape) * 4
        ):
            raise ValueError("Contiguous exact FP32 tensor payload required")
        payload = memoryview(raw)[8 + size + start : 8 + size + end]
        record = {
            "shape": shape,
            "dtype": "torch.float32",
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        if record != records[name] or len(payload) != end - start:
            raise ValueError("Tensor payload differs from admitted metadata")
        value = np.frombuffer(payload, dtype="<f4").reshape(shape)
        if not np.isfinite(value).all():
            raise ValueError("Finite cached tensor required")
        values[name], cursor = value, end
    if 8 + size + cursor != len(raw):
        raise ValueError("Trailing safetensor bytes")
    return values


def replay(features, tensors, np):
    dim = features.shape[1]
    if (
        tensors["weight"].shape != (6, dim)
        or tensors["bias"].shape != (6,)
        or tensors["center"].shape != (dim,)
        or tensors["scale"].shape != (dim,)
        or not (tensors["scale"] > 0).all()
    ):
        raise ValueError("Exact saved head/normalization shape required")
    normalized = (features - tensors["center"]) / tensors["scale"]
    output = normalized @ tensors["weight"].T + tensors["bias"]
    if not np.isfinite(normalized).all() or not np.isfinite(output).all():
        raise ValueError("Nonfinite CPU replay")
    return normalized, output


def describe(values, np):
    values = np.asarray(values, dtype=np.float64)
    if not values.size:
        return None
    return {
        "min": float(values.min()),
        "median": float(np.median(values)),
        "mean": float(values.mean()),
        "max": float(values.max()),
    }


def summarize(output, gold, tensors, np):
    pred = output[:, :4].argmax(axis=1)
    competitors = output[:, :4].copy()
    competitors[np.arange(len(pred)), pred] = -np.inf
    margin = output[np.arange(len(pred)), pred] - competitors.max(axis=1)
    stop_margin = output[:, 3] - output[:, :3].max(axis=1)
    bias = tensors["bias"]
    bias_competitor = output[:, :3].argmax(axis=1)
    stop_bias_margin = bias[3] - bias[bias_competitor]
    return {
        "count": len(pred),
        "predicted": {name: int((pred == k).sum()) for k, name in enumerate(CLASSES)},
        "raw_classifier_correct": int((pred == gold).sum()),
        "per_gold_class": {
            name: {
                "support": int((gold == k).sum()),
                "correct": int(((gold == k) & (pred == k)).sum()),
                "STOP_predictions": int(((gold == k) & (pred == 3)).sum()),
                "gold_margin": describe(
                    output[gold == k, k] - np.delete(output[gold == k, :4], k, axis=1).max(axis=1),
                    np,
                )
                if (gold == k).any()
                else None,
            }
            for k, name in enumerate(CLASSES)
        },
        "winner_margin": describe(margin, np),
        "STOP_margin": describe(stop_margin, np),
        "STOP_winner_bias_margin": describe(stop_bias_margin[pred == 3], np),
        "STOP_winner_feature_margin": describe((stop_margin - stop_bias_margin)[pred == 3], np),
        "mean_logits": output[:, :4].mean(axis=0, dtype=np.float64).tolist(),
        "metric_outputs": {
            "distance": describe(output[:, 4], np),
            "angle": describe(output[:, 5], np),
        },
    }


def gold_class(row):
    action = row["planned_action"]
    if action["kind"] == "stop":
        return 3
    if action["kind"] == "turn_yaw":
        angle = action["yaw_delta_rad"]
        if type(angle) in (int, float) and math.isfinite(angle) and 0 < abs(angle) <= math.pi:
            return 1 if angle < 0 else 2
    if action["kind"] == "translate" and action["translation_m"] == [3.0, 0.0, 0.0]:
        return 0
    raise ValueError("Unsupported or unknown label in this fixed diagnostic cohort")


def audit(plan_binding, live_binding):
    inputs = BoundInputs()
    plan = inputs.json(plan_binding)
    report_path = Path(plan["output_dir"]) / "report.json"
    # The terminal receipt binds this exact training report, not a reconstructed report.
    terminal = inputs.json(
        {
            "path": (
                "docs/evidence/onboard-navigation-stable-cache-training-actual-20261008-r1.json"
            ),
            "bytes": 8261,
            "sha256": "7dcd555477da803209f79579767ede4cac31c6ac9b045bd683db385eec314bda",
        }
    )
    matches = [
        r for r in terminal["bindings"] if (ROOT / r["path"]).resolve() == report_path.resolve()
    ]
    if len(matches) != 1:
        raise ValueError("One terminal-bound training report required")
    report = inputs.json(matches[0])
    if report["success"] is not True or report["plan"]["sha256"] != plan_binding["sha256"]:
        raise ValueError("Closed, exact training plan/report required")
    cache_receipt = inputs.json(plan["cache_receipt"])
    cache_meta = inputs.json(cache_receipt["metadata"])
    raw_matrix = inputs.read(cache_receipt["weights"])
    datasets = [decode(line) for row in plan["datasets"] for line in inputs.read(row).splitlines()]
    if [r["sample_id"] for r in datasets] != cache_meta["sample_ids"]:
        raise ValueError("Ordered208 dataset/cache IDs required")
    ids = cache_meta["sample_ids"]
    if len(ids) != 208 or ids != plan["sample_ids"] or cache_meta["partitions"] != ["train"] * 208:
        raise ValueError("Exact all-TRAIN208 identity required")
    gold = [gold_class(row) for row in datasets]
    raw_rows = [decode(line) for line in inputs.read(report["predictions"]).splitlines()]
    live = inputs.json(live_binding)
    if live["success"] is not True or len(live["observations"]) != 8:
        raise ValueError("Closed eight-observation R2 report required")
    import numpy as np

    if any(x in sys.modules for x in ("torch", "PIL", "transformers", "peft")):
        raise ValueError("Diagnostic process must not import model/processor modules")
    matrix = bundle(raw_matrix, cache_meta["tensors"], np)["features"]
    if matrix.shape != (208, 2048):
        raise ValueError("Exact208x2048 matrix required")
    for index, observation in enumerate(cache_meta["observations"]):
        if (
            observation["sample_id"] != ids[index]
            or hashlib.sha256(matrix[index].tobytes()).hexdigest()
            != observation["feature"]["sha256"]
        ):
            raise ValueError("Ordered feature row SHA differs")
    gold = np.asarray(gold)
    id_index = {name: k for k, name in enumerate(ids)}
    heads = []
    raw_checked, largest_raw_difference = 0, 0.0
    if len(report["heads"]) != 11 or len(raw_rows) != 502:
        raise ValueError("Exact11 heads/502 durable predictions required")
    for entry in report["heads"]:
        receipt = inputs.json(entry["receipt"])
        meta = inputs.json(receipt["metadata"])
        tensors = bundle(inputs.read(receipt["weights"]), meta["tensors"], np)
        train = np.asarray([id_index[x] for x in entry["train_ids"]])
        test = np.asarray([id_index[x] for x in entry["eval_ids"]])
        if meta["normalization_train_ids"] != entry["train_ids"]:
            raise ValueError("Train-only saved normalization identity differs")
        normalized, output = replay(matrix, tensors, np)
        relevant = [r for r in raw_rows if r["head_id"] == entry["head_id"]]
        if [r["sample_id"] for r in relevant] != entry["eval_ids"]:
            raise ValueError("Scheduled durable raw row order differs")
        for row, index in zip(relevant, test, strict=True):
            raw = row["raw"]
            expected = np.asarray(
                raw["raw_logits"] + [raw["raw_distance_m"], raw["raw_angle_magnitude_rad"]]
            )
            error = float(np.max(np.abs(output[index].astype(np.float64) - expected)))
            largest_raw_difference = max(largest_raw_difference, error)
            if not np.allclose(output[index], expected, rtol=1e-5, atol=2e-5):
                raise ValueError("CPU replay materially differs from sealed raw outputs")
            if CLASSES[int(output[index, :4].argmax())] != raw["selected_class"]:
                raise ValueError("CPU replay class differs from sealed raw")
            raw_checked += 1
        raw_feature_outputs = normalized @ tensors["weight"].T
        heads.append(
            {
                "head_id": entry["head_id"],
                "scope": entry["evaluation_scope"],
                "bias": tensors["bias"].tolist(),
                "weight_row_L2": np.linalg.norm(tensors["weight"], axis=1).tolist(),
                "normalization_scale": describe(tensors["scale"], np),
                "train_normalized_mean_L2": float(np.linalg.norm(normalized[train].mean(axis=0))),
                "eval_normalized_mean_L2": float(np.linalg.norm(normalized[test].mean(axis=0))),
                "train_feature_logits_std": raw_feature_outputs[train, :4].std(axis=0).tolist(),
                "train": summarize(output[train], gold[train], tensors, np),
                "evaluation": summarize(output[test], gold[test], tensors, np),
                "initial_loss": meta["training_report"]["initial_train_loss"],
                "final_loss": meta["training_report"]["final_train_loss"],
            }
        )
    live_logits = np.asarray([r["raw_prediction"]["raw_logits"] for r in live["observations"]])
    live_distance = [r["raw_prediction"]["raw_distance_m"] for r in live["observations"]]
    live_angle = [r["raw_prediction"]["raw_angle_magnitude_rad"] for r in live["observations"]]
    inputs.close()
    return {
        "contract": "multidetect.navigation-head-readonly-diagnostics-v1",
        "bound_inputs": list(inputs.rows.values()),
        "bound_inputs_read_once_and_final_stat_unchanged": True,
        "features_L2": describe(np.linalg.norm(matrix, axis=1), np),
        "feature_channels_population_std": describe(matrix.astype(np.float64).std(axis=0), np),
        "heads": heads,
        "sealed_raw_rows_CPU_replayed": raw_checked,
        "largest_CPU_replay_absolute_difference": largest_raw_difference,
        "live_R2": {
            "count": 8,
            "predicted": {
                name: int((live_logits.argmax(axis=1) == k).sum()) for k, name in enumerate(CLASSES)
            },
            "STOP_margin": describe(live_logits[:, 3] - live_logits[:, :3].max(axis=1), np),
            "raw_distance": describe(live_distance, np),
            "raw_angle": describe(live_angle, np),
            "feature_bytes_available": False,
            "feature_record_matches_training_cache": sum(
                r["metrics"]["actual_feature"]["sha256"]
                in {o["feature"]["sha256"] for o in cache_meta["observations"]}
                for r in live["observations"]
            ),
        },
        "optimizer_calls": 0,
        "backbone_or_processor_loaded": False,
        "causal_attribution_proven": False,
        "generalization_or_flight_quality_proven": False,
        "heavy_modules_imported": [
            x for x in ("torch", "PIL", "transformers", "peft") if x in sys.modules
        ],
        "CPU_numeric_runtime": {"numpy_version": np.__version__, "numpy_origin": np.__file__},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--live-report", required=True)
    parser.add_argument("--live-report-sha256", required=True)
    parser.add_argument("--output", help="Create-only JSON under artifacts/diagnostics")
    args = parser.parse_args(argv)
    output = None
    if args.output is not None:
        output = Path(args.output)
        output = (output if output.is_absolute() else ROOT / output).resolve()
        if (
            not output.parent.is_dir()
            or not output.is_relative_to((ROOT / "artifacts" / "diagnostics").resolve())
            or output.suffix != ".json"
            or output.exists()
        ):
            raise ValueError("Fresh diagnostics JSON output with existing parent required")
    rows = []
    for path, digest in (
        (args.plan, args.plan_sha256),
        (args.live_report, args.live_report_sha256),
    ):
        rows.append({"path": path, "bytes": Path(path).stat().st_size, "sha256": digest})
    report = audit(*rows)
    raw = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if output is None:
        print(raw.decode("utf-8"), end="")
    else:
        with output.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        print(
            json.dumps(
                {
                    "path": output.relative_to(ROOT.resolve()).as_posix(),
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
