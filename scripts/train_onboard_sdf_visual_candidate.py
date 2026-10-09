"""Warm-start an auxiliary visual candidate on actual offline 3-D renders.

This developer experiment never replaces the existing COCO detector or the
OnboardAI policy reference. Blender renders are not Gazebo/PX4 flight evidence.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET_CONTRACT = "multidetect.onboard-sdf-blender-visual-dataset-v1"
PARENT_SHA256 = "9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef"


def resolve_parent_binding(
    parent: Path, report: Path | None = None, report_sha256: str | None = None
) -> tuple[str, dict | None]:
    """Continue an explicitly bound developer candidate, or the pinned COCO parent."""
    if (report is None) != (report_sha256 is None):
        raise ValueError("parent training report and its SHA-256 must be supplied together")
    if report is None:
        if sha256(parent) != PARENT_SHA256:
            raise ValueError("parent must be the existing hash-pinned COCO YOLO26n checkpoint")
        return PARENT_SHA256, None
    report = report.resolve()
    if sha256(report) != report_sha256:
        raise ValueError("parent training report identity mismatch")
    document = json.loads(report.read_text(encoding="utf-8"))
    if (
        document.get("contract") != "multidetect.onboard-sdf-visual-candidate-development-v1"
        or document.get("names") != {"0": "search_target"}
        or any(
            document.get(key) is not False
            for key in (
                "training_ready",
                "gazebo_capture",
                "candidate_promoted",
                "original_detector_changed",
                "onboard_policy_reference_changed",
                "final_release_allowed",
            )
        )
    ):
        raise ValueError("parent report is not a compatible isolated visual candidate")
    checkpoint = document.get("checkpoint", {})
    expected = checkpoint.get("sha256")
    if (
        not isinstance(checkpoint.get("path"), str)
        or not isinstance(expected, str)
        or len(expected) != 64
        or sha256(parent) != expected
    ):
        raise ValueError("parent checkpoint does not match its bound training report")
    # The report records the producer's location. Byte identity, not that
    # machine's absolute path, remains authoritative after a verified transfer.
    return expected, {"path": str(report), "sha256": report_sha256}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bound_file(base: Path, relative: object, expected_hash: object) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("bound file requires a nonempty relative path")
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()) or Path(relative).is_absolute():
        raise ValueError("bound file must remain inside its source root")
    if not path.is_file() or sha256(path) != expected_hash:
        raise ValueError(f"bound file identity mismatch: {relative}")
    return path


def verify_dataset(manifest: Path) -> tuple[dict, Path]:
    base = manifest.resolve().parent
    document = json.loads(manifest.read_text(encoding="utf-8"))
    if document.get("contract") != DATASET_CONTRACT:
        raise ValueError("expected the offline SDF/Blender visual dataset contract")
    if document.get("names") != ["search_target"]:
        raise ValueError("candidate requires the explicit search_target class mapping")
    if document.get("training_ready") is not False or document.get("gazebo_capture") is not False:
        raise ValueError("offline developer renders must not claim qualified Gazebo capture")
    frames = document.get("frames")
    if not isinstance(frames, list) or len(frames) != document.get("frame_count"):
        raise ValueError("dataset frame inventory mismatch")
    split_worlds: dict[str, set[str]] = {name: set() for name in ("train", "val", "test")}
    image_splits: dict[str, str] = {}
    paths: set[Path] = set()
    for frame in frames:
        split = frame.get("split")
        if split not in split_worlds:
            raise ValueError("unknown dataset split")
        world = bound_file(ROOT, frame["world_path"], frame["world_sha256"])
        split_worlds[split].add(str(world))
        sources = frame.get("source_hashes")
        if (
            not isinstance(sources, dict)
            or sources.get(frame["world_path"]) != frame["world_sha256"]
        ):
            raise ValueError("frame must bind its actual world and included model source files")
        for source_path, source_hash in sources.items():
            bound_file(ROOT, source_path, source_hash)
        for kind in ("image", "label", "mask"):
            path = bound_file(base, frame[f"{kind}_path"], frame[f"{kind}_sha256"])
            if path in paths:
                raise ValueError("dataset must not reuse output paths")
            paths.add(path)
        image_hash = frame["image_sha256"]
        if image_hash in image_splits and image_splits[image_hash] != split:
            raise ValueError("identical rendered pixels cannot cross splits")
        image_splits[image_hash] = split
    if any(not worlds for worlds in split_worlds.values()):
        raise ValueError("train, validation and test must all contain frames")
    if any(
        split_worlds[left] & split_worlds[right]
        for left, right in (("train", "val"), ("train", "test"), ("val", "test"))
    ):
        raise ValueError("different splits must use independent source worlds")
    data = bound_file(base, document["dataset_yaml"], document["dataset_yaml_sha256"])
    return document, data


def stage_training_inputs(manifest: Path, document: dict, destination: Path) -> Path:
    """Build a closed dataset; source YAML and unlisted directory files cannot enter it."""
    destination.mkdir(parents=True, exist_ok=False)
    source = manifest.resolve().parent
    inventory = []
    for index, frame in enumerate(document["frames"]):
        split = frame["split"]
        image_source = bound_file(source, frame["image_path"], frame["image_sha256"])
        label_source = bound_file(source, frame["label_path"], frame["label_sha256"])
        if image_source.suffix.lower() != ".png":
            raise ValueError("renderer must provide lossless PNG images")
        name = f"frame-{index:06d}"
        image = destination / "images" / split / f"{name}.png"
        label = destination / "labels" / split / f"{name}.txt"
        for original, staged, expected in (
            (image_source, image, frame["image_sha256"]),
            (label_source, label, frame["label_sha256"]),
        ):
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, staged)
            if sha256(staged) != expected:
                raise ValueError("training input changed during staging")
        inventory.append(
            {
                "split": split,
                "image": str(image.relative_to(destination)),
                "image_sha256": frame["image_sha256"],
                "label": str(label.relative_to(destination)),
                "label_sha256": frame["label_sha256"],
            }
        )
    # JSON is valid YAML, and keeps class names and split roots explicit.
    data = destination / "dataset.yaml"
    with data.open("x", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "path": str(destination.resolve()),
                    "train": "images/train",
                    "val": "images/val",
                    "test": "images/test",
                    "names": {"0": "search_target"},
                },
                indent=2,
            )
            + "\n"
        )
    with (destination / "input-inventory.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(inventory, indent=2) + "\n")
    return data


def verify_staged_inputs(document: dict, inputs: Path) -> Path:
    inventory = json.loads((inputs / "input-inventory.json").read_text(encoding="utf-8"))
    if len(inventory) != len(document["frames"]):
        raise ValueError("staged inventory length mismatch")
    expected_paths = set()
    for index, (item, frame) in enumerate(zip(inventory, document["frames"], strict=True)):
        split, name = frame["split"], f"frame-{index:06d}"
        for kind, group, suffix in (("image", "images", ".png"), ("label", "labels", ".txt")):
            expected = f"{group}/{split}/{name}{suffix}"
            if (
                Path(item[kind]).as_posix() != expected
                or item[f"{kind}_sha256"] != frame[f"{kind}_sha256"]
            ):
                raise ValueError("staged inventory does not match source frame identity")
            expected_paths.add(bound_file(inputs, expected, frame[f"{kind}_sha256"]))
    actual_paths = set((inputs / "images").rglob("*.png")) | set((inputs / "labels").rglob("*.txt"))
    if {path.resolve() for path in actual_paths} != expected_paths:
        raise ValueError("staged inputs contain extra or missing samples")
    data = inputs / "dataset.yaml"
    if json.loads(data.read_text(encoding="utf-8")) != {
        "path": str(inputs.resolve()),
        "train": "images/train",
        "val": "images/val",
        "test": "images/test",
        "names": {"0": "search_target"},
    }:
        raise ValueError("closed training configuration changed")
    return data


def bind_training_invocation(inputs: Path, expected: dict, *, create: bool) -> dict:
    """Pin initial byte identities before optimization, including failed finalization."""
    path = inputs / "invocation-binding.json"
    document = {"contract": "multidetect.visual-training-invocation-v1", **expected}
    if create:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(document, indent=2, allow_nan=False) + "\n")
    elif not path.is_file():
        raise ValueError("finalization requires the initial training invocation binding")
    if json.loads(path.read_text(encoding="utf-8")) != document:
        raise ValueError("initial training invocation identity changed; cannot relabel old weights")
    return {"path": str(path.resolve()), "sha256": sha256(path)}


def verify_parent_names(names: dict, *, developer_parent: bool) -> None:
    if developer_parent and names != {0: "search_target"}:
        raise ValueError("actual parent checkpoint classes do not match its developer report")


def bind_export_receipt(checkpoint: Path, exported: Path, *, create: bool) -> dict:
    path = exported.with_suffix(".export-receipt.json")
    document = {
        "contract": "multidetect.visual-checkpoint-raw-export-v1",
        "checkpoint_sha256": sha256(checkpoint),
        "onnx_sha256": sha256(exported),
        "names": {"0": "search_target"},
    }
    if create:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(document, indent=2) + "\n")
    elif not path.is_file():
        raise ValueError("existing ONNX has no checkpoint-bound export receipt; refusing reuse")
    if json.loads(path.read_text(encoding="utf-8")) != document:
        raise ValueError("existing ONNX does not match the checkpoint-bound export receipt")
    return {"path": str(path.resolve()), "sha256": sha256(path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--parent-training-report", type=Path)
    parser.add_argument("--parent-training-report-sha256")
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--development-experiment", action="store_true")
    parser.add_argument(
        "--finalize-existing-training",
        action="store_true",
        help="finish evaluation/export after a failed post-training step, never retrain",
    )
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument("--device", default="0")
    parser.add_argument("--seed", type=int, default=20261007)
    args = parser.parse_args(argv)
    if not args.development_experiment:
        raise ValueError("unreviewed synthetic renders require --development-experiment")
    if min(args.epochs, args.imgsz, args.batch) <= 0:
        raise ValueError("epochs, image size and batch must be positive")
    manifest = args.manifest.resolve()
    manifest_hash = sha256(manifest)
    document, _source_yaml = verify_dataset(manifest)
    parent = args.parent.resolve()
    parent_sha256, parent_report = resolve_parent_binding(
        parent, args.parent_training_report, args.parent_training_report_sha256
    )
    output = args.output_directory.resolve()
    if output.exists() and not args.finalize_existing_training:
        raise FileExistsError(f"refusing to replace candidate directory: {output}")
    inputs = output.with_name(output.name + "-inputs")
    if inputs.exists() and not args.finalize_existing_training:
        raise FileExistsError(f"refusing to replace training inputs: {inputs}")
    if (output / "visual-training-report.json").exists():
        raise FileExistsError("candidate finalization already completed")
    invocation = {
        "manifest": {"path": str(manifest), "sha256": manifest_hash},
        "parent": {"path": str(parent), "sha256": parent_sha256},
        "parent_training_report": parent_report,
        "configuration": {
            "epochs": args.epochs,
            "imgsz": args.imgsz,
            "batch": args.batch,
            "device": args.device,
            "seed": args.seed,
        },
    }

    import torch
    import ultralytics
    from ultralytics import YOLO

    if args.device != "cpu" and not torch.cuda.is_available():
        raise RuntimeError("CUDA requested but not available")
    parent_model = YOLO(str(parent))
    verify_parent_names(parent_model.names, developer_parent=parent_report is not None)
    if args.finalize_existing_training:
        import yaml

        invocation_binding = bind_training_invocation(inputs, invocation, create=False)
        data = verify_staged_inputs(document, inputs)
        recorded = yaml.safe_load((output / "args.yaml").read_text(encoding="utf-8"))
        for field, expected in {
            "epochs": args.epochs,
            "imgsz": args.imgsz,
            "batch": args.batch,
            "seed": args.seed,
            "data": str(data),
            "model": str(parent),
        }.items():
            if recorded.get(field) != expected:
                raise ValueError(f"existing training configuration mismatch: {field}")
        result = None
    else:
        data = stage_training_inputs(manifest, document, inputs)
        invocation_binding = bind_training_invocation(inputs, invocation, create=True)
        result = parent_model.train(
            data=str(data),
            epochs=args.epochs,
            imgsz=args.imgsz,
            batch=args.batch,
            device=args.device,
            workers=0,
            cache=False,
            project=str(output.parent),
            name=output.name,
            exist_ok=False,
            seed=args.seed,
            deterministic=True,
            amp=False,  # Do not download another model for Ultralytics' AMP self-test.
            plots=False,
            save_period=-1,
            patience=args.epochs,
            optimizer="AdamW",
            lr0=0.001,
            warmup_epochs=1.0,
        )
    if result is not None and Path(result.save_dir).resolve() != output:
        raise RuntimeError("trainer selected an unexpected output directory")
    checkpoint = output / "weights/best.pt"
    checkpoint_hash = sha256(checkpoint)
    candidate = YOLO(str(checkpoint))
    if candidate.names != {0: "search_target"}:
        raise RuntimeError("candidate class names do not match the training contract")
    validation = result
    if validation is None:
        validation = candidate.val(
            data=str(data),
            split="val",
            imgsz=args.imgsz,
            batch=args.batch,
            device=args.device,
            workers=0,
            plots=False,
            project=str(output),
            name="validation-finalization",
            exist_ok=False,
        )
    heldout = candidate.val(
        data=str(data),
        split="test",
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        workers=0,
        plots=False,
        project=str(output),
        name="independent-world-test",
        exist_ok=False,
    )
    exported = checkpoint.with_suffix(".onnx")
    fresh_export = not exported.exists()
    if not fresh_export:
        bind_export_receipt(checkpoint, exported, create=False)
    else:
        # val() fuses the model and removes YOLO26's one-to-many head in place.
        # A fresh checkpoint must be loaded before traditional raw export.
        exported = Path(
            YOLO(str(checkpoint)).export(
                format="onnx",
                imgsz=args.imgsz,
                batch=1,
                dynamic=False,
                simplify=False,
                opset=17,
                nms=False,
                end2end=False,
            )
        ).resolve()
    import onnx

    graph = onnx.load(str(exported))
    onnx.checker.check_model(graph)
    shape = [dim.dim_value for dim in graph.graph.output[0].type.tensor_type.shape.dim]
    if len(shape) != 3 or shape[:2] != [1, 5] or shape[2] <= 0:
        raise RuntimeError(f"unexpected traditional single-class ONNX output: {shape}")
    if (
        len(graph.graph.output) != 1
        or sum(item.key == "names" for item in graph.metadata_props) != 1
        or ast.literal_eval(
            next(item.value for item in graph.metadata_props if item.key == "names")
        )
        != {0: "search_target"}
    ):
        raise RuntimeError("exported ONNX class contract differs from its checkpoint")
    export_receipt = bind_export_receipt(checkpoint, exported, create=fresh_export)
    if (
        sha256(parent) != parent_sha256
        or sha256(manifest) != manifest_hash
        or sha256(checkpoint) != checkpoint_hash
    ):
        raise RuntimeError("training input identity changed during execution")
    if parent_report is not None and sha256(Path(parent_report["path"])) != parent_report["sha256"]:
        raise RuntimeError("parent training report changed during execution")
    verify_dataset(manifest)
    verify_staged_inputs(document, inputs)
    if bind_training_invocation(inputs, invocation, create=False) != invocation_binding:
        raise RuntimeError("initial training invocation binding changed during execution")
    report = {
        "contract": "multidetect.onboard-sdf-visual-candidate-development-v1",
        "parent": {"path": str(parent), "sha256": parent_sha256},
        "parent_training_report": parent_report,
        "invocation_binding": invocation_binding,
        "dataset": {
            "path": str(manifest),
            "sha256": manifest_hash,
            "frames": len(document["frames"]),
            "renderer": "Blender",
        },
        "closed_training_inputs": {
            "path": str(inputs),
            "inventory_sha256": sha256(inputs / "input-inventory.json"),
            "source_yaml_executed": False,
        },
        "checkpoint": {"path": str(checkpoint), "sha256": sha256(checkpoint)},
        "onnx": {"path": str(exported), "sha256": sha256(exported)},
        "export_receipt": export_receipt,
        "names": {"0": "search_target"},
        "epochs_requested": args.epochs,
        "seed": args.seed,
        "ultralytics": ultralytics.__version__,
        "torch": torch.__version__,
        "device": args.device,
        "gpu": None if args.device == "cpu" else torch.cuda.get_device_name(0),
        "validation_metrics": dict(validation.results_dict),
        "independent_world_test_metrics": dict(heldout.results_dict),
        "metric_backend": "Ultralytics_PyTorch_before_traditional_ONNX_export",
        "shared_onnx_runtime_evaluation": "required_separately_not_claimed_by_PyTorch_metrics",
        "finalized_existing_training": args.finalize_existing_training,
        "parent_transfer": "compatible_backbone_and_detector_parameters_only",
        "role": "auxiliary_search_target_candidate_not_common_detector_replacement",
        "old_80_class_capability_preservation": "not_claimed",
        "original_detector_changed": False,
        "onboard_policy_reference_changed": False,
        "training_ready": False,
        "gazebo_capture": False,
        "autonomous_flight_verified": False,
        "candidate_promoted": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }
    with (output / "visual-training-report.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
