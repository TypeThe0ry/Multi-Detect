"""Bounded, offline RGB/whole-episode overlap analysis; never training admission.

Plan emission is stdlib-only. Execution decodes one original RGB8 PNG at a time
with the existing strict PNG validator and Pillow. No model/processor/Arrow,
transcoding, automatic sample removal, split edits, or official holdout claims.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import sys
import warnings
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.onboard-navigation-dataset-overlap-plan-v1"
REPORT_CONTRACT = "multidetect.onboard-navigation-dataset-overlap-analysis-v1"
NEW_FILES = (
    "scripts/audit_onboard_navigation_dataset_overlap.py",
    "tests/test_audit_onboard_navigation_dataset_overlap.py",
)
RECEIPT = {
    "path": "docs/evidence/openfly-indexed-training-supervision-20261008-r1.json",
    "bytes": 9671,
    "sha256": "9eb51ec796269363fda0c2586c0c11d0bb34f022badce1ec8f2e07f1c5c0a295",
}
EVALUATION = {
    "path": "configs/training/onboard-navigation-original59-evaluation-20261008-r1.json",
    "bytes": 107290,
    "sha256": "298ae322ef320a590d6853cc4b997f3f9691e8d18f8d1c81c02f18845197fb6c",
}
PINS = {
    "scripts/extract_openfly_parquet_images.py": (
        26806,
        "fb071ac675892d97dabf0dd7781ca7397daf04c3a5306c90769b550906775b36",
    ),
    "src/multidetect/autonomy/onboard_navigation_supervision.py": (
        52186,
        "bd1adc75a6954c2eb2453ae358536cae2db65305dd0c3afcf57d24a1711bb14e",
    ),
    "src/multidetect/autonomy/onboard_navigation_inference.py": (
        18843,
        "3838a9ee4ee1eef58a680369076157263cfe824bc75c6dcbfe265a977564954f",
    ),
    "src/multidetect/autonomy/serialization.py": (
        8656,
        "719578fb4c0a66ca63ee407ac9a05738515cb590946cc9a5cd2625da2762ab56",
    ),
}
BUDGET = {
    "max_control_bytes": 2 * 1024 * 1024,
    "max_runtime_file_bytes": 8 * 1024 * 1024,
    "max_encoded_image_bytes": 8 * 1024 * 1024,
    "max_decoded_pixels": 16 * 1024 * 1024,
    "max_current_frame_occurrences": 290,
    "max_decoded_images": 290,
    "resident_decoded_images": 1,
    "hash_read_bytes": 65536,
}
FALSE_CLAIMS = (
    "training_ready",
    "processor_admission_passed",
    "model_loaded",
    "GPU_used",
    "optimizer_performed",
    "network_used",
    "quality_claim",
    "generalization_verified",
    "official_seen_unseen_holdout_verified",
    "physical_performance_claimed",
    "hil_real_evidence",
    "candidate_promoted",
    "default_changed",
    "final_release_allowed",
    "samples_dropped_or_datasets_merged_or_split_modified",
    "near_duplicate_independence_proved",
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def parse_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError(f"Nonfinite JSON value: {value}")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def integer(value, label, maximum):
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError(f"Invalid bounded integer {label}")
    return value


def no_link(path):
    for parent in (path, *path.parents):
        if parent.exists():
            stat = parent.lstat()
            if parent.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"Symlink/reparse path is forbidden: {parent}")


def repo_path(value):
    if type(value) is not str or not value or ".." in Path(value).parts:
        raise ValueError("Require explicit safe repository path")
    unresolved = Path(value) if Path(value).is_absolute() else ROOT / value
    no_link(unresolved)
    path = unresolved.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise ValueError("Path escapes authoritative repository")
    return path


def output_path(value):
    path = repo_path(value)
    if (
        path.parent != (ROOT / "artifacts/diagnostics").resolve()
        or not path.name.startswith("onboard-navigation-dataset-overlap-")
        or path.exists()
    ):
        raise ValueError("Require fresh top-level dataset-overlap diagnostics directory")
    return path


def normalize(binding):
    if type(binding) is not dict or set(binding) != {"path", "bytes", "sha256"}:
        raise ValueError("Require exact path/bytes/sha256 binding")
    size = integer(binding["bytes"], "source bytes", 1024 * 1024 * 1024)
    sha = binding["sha256"]
    if (
        type(sha) is not str
        or len(sha) != 64
        or any(letter not in "0123456789abcdef" for letter in sha)
    ):
        raise ValueError("Require canonical SHA256")
    path = repo_path(binding["path"])
    return {"path": path.relative_to(ROOT).as_posix(), "bytes": size, "sha256": sha}


def hash_file(path, maximum):
    no_link(path)
    size = integer(path.stat().st_size, "file size", maximum)
    digest = hashlib.sha256()
    consumed = 0
    with path.open("rb") as stream:
        while chunk := stream.read(min(BUDGET["hash_read_bytes"], maximum - consumed + 1)):
            consumed += len(chunk)
            if consumed > maximum:
                raise ValueError("File grew beyond bounded byte cap")
            digest.update(chunk)
    if consumed != size or path.stat().st_size != size:
        raise ValueError("File changed size during read")
    return {"bytes": size, "sha256": digest.hexdigest()}


def read_bound(binding, maximum=None):
    binding = normalize(binding)
    maximum = maximum or BUDGET["max_control_bytes"]
    path = repo_path(binding["path"])
    if binding["bytes"] > maximum or hash_file(path, maximum) != {
        key: binding[key] for key in ("bytes", "sha256")
    }:
        raise ValueError(f"Bound source differs: {binding['path']}")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) != binding["bytes"] or hashlib.sha256(raw).hexdigest() != binding["sha256"]:
        raise ValueError("Bound source changed between verification and parse")
    return raw


def bind_file(value):
    path = repo_path(value)
    return {
        "path": path.relative_to(ROOT).as_posix(),
        **hash_file(path, BUDGET["max_control_bytes"]),
    }


def merge_bindings(rows):
    table = {}
    for row in rows:
        normalized = normalize(row)
        prior = table.setdefault(normalized["path"], normalized)
        if prior != normalized:
            raise ValueError(f"Conflicting source declarations: {normalized['path']}")
    return [table[key] for key in sorted(table)]


def check_no_heavy():
    forbidden = ("torch", "transformers", "peft", "accelerate", "pyarrow", "datasets")
    if any(name.split(".")[0] in forbidden for name in sys.modules):
        raise ValueError("Forbidden ML/GPU/Arrow module imported")


def metadata_api():
    for name, (size, sha) in PINS.items():
        read_bound({"path": name, "bytes": size, "sha256": sha})
    sys.path.insert(0, str(ROOT / "src"))
    from multidetect.autonomy import onboard_navigation_supervision as nav

    if Path(nav.__file__).resolve() != repo_path(
        "src/multidetect/autonomy/onboard_navigation_supervision.py"
    ):
        raise ValueError("Typed metadata module has unexpected origin")
    check_no_heavy()
    return nav


def png_api():
    name = "scripts/extract_openfly_parquet_images.py"
    size, sha = PINS[name]
    read_bound({"path": name, "bytes": size, "sha256": sha})
    spec = importlib.util.spec_from_file_location("dataset_overlap_png_validator", repo_path(name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    check_no_heavy()
    return module


def pillow_bindings():
    """Resolve only listed CPU Pillow files without importing Pillow."""
    distribution = importlib.metadata.distribution("Pillow")
    names = {"PIL/__init__.py", "PIL/Image.py", "PIL/ImageFile.py", "PIL/PngImagePlugin.py"}
    selected = []
    for item in distribution.files or ():
        name = item.as_posix()
        if name in names or (name.startswith("PIL/_imaging.") and name.endswith((".pyd", ".so"))):
            path = Path(distribution.locate_file(item)).resolve()
            selected.append(
                {"path": str(path), **hash_file(path, BUDGET["max_runtime_file_bytes"])}
            )
    if len(selected) != 5:
        raise ValueError("Require four Pillow CPU modules and one core extension")
    return {"version": distribution.version, "files": sorted(selected, key=lambda row: row["path"])}


def validate_pillow_origins(runtime):
    import PIL
    from PIL import Image, ImageFile, PngImagePlugin

    actual = {
        str(Path(module.__file__).resolve())
        for module in (PIL, Image, ImageFile, PngImagePlugin, Image.core)
    }
    if actual != {row["path"] for row in runtime["files"]}:
        raise ValueError("Actual Pillow module/extension origin differs from admitted runtime")
    if pillow_bindings() != runtime:
        raise ValueError("Pillow source changed before image decode")
    check_no_heavy()


def publisher_episode(row):
    source = row["source"]
    parts = Path(source["pose"]["path"]).parts
    if parts.count("Image") != 1 or parts[-1] != "pose.jsonl":
        raise ValueError("Require unambiguous publisher Image episode pose path")
    route = parts[parts.index("Image") + 1 : -1]
    if len(route) != 4 or route[0] != row["scene_id"] or route[1] != "astar_data":
        raise ValueError("Publisher route/scene identity differs")
    annotation = source["annotation_record_id"]
    if (
        type(annotation) is not str
        or not annotation.isdigit()
        or str(int(annotation)) != annotation
    ):
        raise ValueError("Require original canonical annotation index")
    original = [source[key] for key in ("repository_id", "revision", "original_split")]
    if original != ["IPEC-COMMUNITY/OpenFly", "a12316d56a4e35a32ad626fb725ed7089937a1c4", "train"]:
        raise ValueError("Require pinned publisher TRAIN provenance")
    return {
        "route_key": canonical([*original, "/".join(route)]),
        "annotation_key": canonical([*original, annotation]),
        "publisher_route": "/".join(route),
        "annotation_record_id": annotation,
    }


def collect_dataset(alias, rows, nav):
    typed = tuple(nav.NavigationSupervisionSample.from_mapping(row) for row in rows)
    nav.validate_navigation_episode_samples(typed)
    episodes, occurrences, current = {}, [], {}
    for row in rows:
        episode = row["episode_id"]
        publisher = publisher_episode(row)
        summary = {
            "dataset": alias,
            "episode_id": episode,
            "scene_id": row["scene_id"],
            "partition": row["partition"],
            **publisher,
        }
        prior = episodes.setdefault(episode, {**summary, "sample_ids": []})
        if any(prior[key] != value for key, value in summary.items()):
            raise ValueError("Episode identity/partition changed")
        prior["sample_ids"].append(row["sample_id"])
        frame = row["frames"][-1]
        if frame["step_index"] != row["step_index"]:
            raise ValueError("Last frame must be the exact current observation")
        binding = normalize({key: frame[key] for key in ("path", "bytes", "sha256")})
        integer(binding["bytes"], "PNG bytes", BUDGET["max_encoded_image_bytes"])
        width = integer(frame["width"], "PNG width", BUDGET["max_decoded_pixels"])
        height = integer(frame["height"], "PNG height", BUDGET["max_decoded_pixels"])
        if width * height > BUDGET["max_decoded_pixels"] or frame["encoding"] != "png":
            raise ValueError("Current PNG dimensions/encoding exceed budget")
        occurrence = {
            "dataset": alias,
            "sample_id": row["sample_id"],
            "episode_id": episode,
            "partition": row["partition"],
            "step_index": row["step_index"],
            "frame_id": frame["frame_id"],
            "width": width,
            "height": height,
            "image": binding,
        }
        current[(episode, row["step_index"])] = frame
        occurrences.append(occurrence)
    for row in rows:
        for frame in row["frames"]:
            if current.get((row["episode_id"], frame["step_index"])) != frame:
                raise ValueError("Past/current frame must equal admitted original step metadata")
    return {
        "alias": alias,
        "samples": len(rows),
        "partitions": dict(Counter(row["partition"] for row in rows)),
        "episodes": list(episodes.values()),
        "actor_frame_reference_occurrences": sum(len(row["frames"]) for row in rows),
        "actor_current_past_frame_closure_verified": True,
    }, occurrences


def read_rows(binding):
    lines = read_bound(binding).splitlines()
    if not lines or any(not line.strip() for line in lines):
        raise ValueError("Require nonempty canonical sample JSONL")
    return [parse_json(line) for line in lines]


def inputs():
    controls = [RECEIPT, EVALUATION]
    receipt = parse_json(read_bound(RECEIPT))
    evaluation = parse_json(read_bound(EVALUATION))
    if receipt["actual_execution"]["actual_native_exit_code"] != 0:
        raise ValueError("NEW122 receipt does not bind successful source-only execution")
    controls.extend(receipt["bindings"])
    for binding in receipt["bindings"]:
        read_bound(binding)
    report122_binding = next(
        row
        for row in receipt["bindings"]
        if row["path"].endswith("/report.json") and "indexed-training-supervision" in row["path"]
    )
    report122 = parse_json(read_bound(report122_binding))
    report82 = parse_json(read_bound(evaluation["dataset_report"]))
    legacy = parse_json(read_bound(evaluation["legacy_plan"]))
    original = parse_json(read_bound(legacy["original_plan"]))
    report86 = parse_json(read_bound(original["dataset_report"]))
    controls.extend(
        [
            evaluation["dataset_report"],
            evaluation["dataset"],
            evaluation["legacy_plan"],
            legacy["original_plan"],
            original["dataset_report"],
            original["dataset_samples"],
        ]
    )
    if (
        report122["native_exit_code"] != 0
        or report82["native_exit_code"] != 0
        or report86["sources_unchanged"] is not True
    ):
        raise ValueError("Previous source-only dataset stages must have succeeded")
    if normalize(report82["dataset"]) != normalize(evaluation["dataset"]) or normalize(
        report86["dataset"]
    ) != normalize(original["dataset_samples"]):
        raise ValueError("Dataset report binding differs from prior plan")
    sample122 = next(row for row in receipt["bindings"] if row["path"].endswith("/samples.jsonl"))
    if normalize(report122["dataset"]) != normalize(sample122):
        raise ValueError("NEW122 dataset differs from sealed receipt")
    datasets = {
        "old86": read_rows(original["dataset_samples"]),
        "new82": read_rows(evaluation["dataset"]),
        "new122": read_rows(sample122),
    }
    if [row["sample_id"] for row in datasets["old86"]] != original["train_sample_ids"]:
        raise ValueError("Old86 training sample order differs")
    if [row["sample_id"] for row in datasets["new82"] if row["partition"] != "train"] != evaluation[
        "sample_ids"
    ]:
        raise ValueError("Observed development59 membership/order differs")
    nav = metadata_api()
    descriptions, occurrences = [], []
    expected = {
        "old86": (86, 3, {"train": 86}),
        "new82": (82, 3, {"train": 23, "dev-validation": 17, "dev-test": 42}),
        "new122": (122, 6, {"train": 122}),
    }
    for alias, rows in datasets.items():
        description, frames = collect_dataset(alias, rows, nav)
        count, episodes, partitions = expected[alias]
        if (
            description["samples"] != count
            or len(description["episodes"]) != episodes
            or description["partitions"] != partitions
        ):
            raise ValueError(f"Fixed complete dataset grouping differs: {alias}")
        descriptions.append(description)
        occurrences.extend(frames)
        for row in rows:
            for value in row["source"].values():
                if (
                    type(value) is dict
                    and set(value) == {"path", "bytes", "sha256"}
                    and value["bytes"] <= BUDGET["max_control_bytes"]
                ):
                    controls.append(value)
    descriptions[0]["prior_partition_grouping"] = report86["partitions"]
    descriptions[1]["prior_partition_grouping"] = report82["partitions"]
    descriptions[2]["prior_partition_grouping"] = {
        "assignment": {row["episode_id"]: row["partition"] for row in descriptions[2]["episodes"]},
        "groups": [[row["episode_id"]] for row in descriptions[2]["episodes"]],
        "method": "original-six-complete-publisher-episodes-all-train; not-new-split",
    }
    controls.extend(
        {"path": key, "bytes": size, "sha256": sha} for key, (size, sha) in PINS.items()
    )
    controls.extend(bind_file(name) for name in NEW_FILES)
    controls = merge_bindings(controls)
    for binding in controls:
        read_bound(binding)
    if len(occurrences) != BUDGET["max_current_frame_occurrences"]:
        raise ValueError("Require 290 current-frame occurrences; history not counted twice")
    images = merge_bindings([row["image"] for row in occurrences])
    for binding in images:
        if repo_path(binding["path"]).stat().st_size != binding["bytes"]:
            raise ValueError("Image metadata differs before plan emission")
    return controls, descriptions, occurrences, images


def build_plan(output_directory):
    out = output_path(output_directory)
    controls, descriptions, occurrences, images = inputs()
    result = {
        "schema_version": 1,
        "contract": CONTRACT,
        "output_dir": out.relative_to(ROOT).as_posix(),
        "budget": BUDGET,
        "sources": controls,
        "runtime": pillow_bindings(),
        "datasets": descriptions,
        "current_frame_occurrences": occurrences,
        "images": images,
        "observed59_is_development_not_official_holdout": True,
        "claims": dict.fromkeys(FALSE_CLAIMS, False),
        "source_scope": (
            "Small control/provenance sources and original PNG only; no whole annotation, "
            "Parquet, model, or processor reads. Prior source-only geometry/provenance "
            "checks reused, not repeated."
        ),
    }
    for binding in controls:
        read_bound(binding)
    check_no_heavy()
    return result


def decode_image(binding, validator):
    raw = read_bound(binding, BUDGET["max_encoded_image_bytes"])
    metadata = validator.check_png(raw)
    from PIL import Image

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            if image.mode != "RGB" or image.size != (metadata["width"], metadata["height"]):
                raise ValueError("Require exact original RGB8 PNG pixels without conversion")
            pixel_sha = hashlib.sha256(image.tobytes()).hexdigest()
    return {
        "image": normalize(binding),
        "width": metadata["width"],
        "height": metadata["height"],
        "rgb_payload_sha256": pixel_sha,
    }


def analyze(descriptions, occurrences, decoded):
    """Report conflicts by entire episode; never alter data or assignments."""
    image_table = {row["image"]["path"]: row for row in decoded}
    episodes = {
        f"{row['dataset']}:{row['episode_id']}": row
        for dataset in descriptions
        for row in dataset["episodes"]
    }
    duplicate_groups, identity_groups = [], []
    pixels = defaultdict(list)
    for row in occurrences:
        image = image_table[row["image"]["path"]]
        if row["image"] != image["image"] or (row["width"], row["height"]) != (
            image["width"],
            image["height"],
        ):
            raise ValueError("Decoded image/current observation binding differs")
        pixels[(image["width"], image["height"], image["rgb_payload_sha256"])].append(row)
    for key, rows in sorted(pixels.items()):
        if len(rows) > 1:
            duplicate_groups.append(
                {
                    "width": key[0],
                    "height": key[1],
                    "rgb_payload_sha256": key[2],
                    "occurrences": rows,
                }
            )
    for field in ("episode_id", "route_key", "annotation_key"):
        grouped = defaultdict(list)
        for key, row in episodes.items():
            grouped[row[field]].append(key)
        for identity, keys in sorted(grouped.items()):
            if len(keys) > 1:
                identity_groups.append(
                    {"identity_kind": field, "identity": identity, "episodes": keys}
                )
    parent = {key: key for key in episodes}

    def find(key):
        while parent[key] != key:
            key = parent[key]
        return key

    def union(keys):
        for key in keys[1:]:
            parent[find(key)] = find(keys[0])

    for group in duplicate_groups:
        union(sorted({f"{row['dataset']}:{row['episode_id']}" for row in group["occurrences"]}))
    for group in identity_groups:
        union(group["episodes"])
    for dataset in descriptions:
        prior = dataset.get("prior_partition_grouping", {})
        prior_groups = prior.get("groups", prior.get("grouping", {}).get("groups", []))
        for group in prior_groups:
            keys = [f"{dataset['alias']}:{episode}" for episode in group]
            # Prior historical visual anchors are preserved in report metadata,
            # not fabricated as samples in this analysis.
            present = [key for key in keys if key in episodes]
            if present:
                union(present)
    components = defaultdict(list)
    for key in episodes:
        components[find(key)].append(key)
    conflicts, train_repeats = [], []
    for keys in sorted(components.values()):
        keys = sorted(keys)
        roles = {episodes[key]["partition"] for key in keys}
        datasets = {episodes[key]["dataset"] for key in keys}
        if "train" in roles and roles - {"train"}:
            conflicts.append(
                {
                    "episodes": [episodes[key] for key in keys],
                    "action_required": (
                        "explicit_whole_episode_quarantine_or_split_decision; no automatic mutation"
                    ),
                }
            )
        elif len(keys) > 1 and roles == {"train"} and len(datasets) > 1:
            train_repeats.append({"episodes": [episodes[key] for key in keys]})
    return {
        "exact_rgb_duplicate_groups": duplicate_groups,
        "episode_identity_overlap_groups": identity_groups,
        "cross_train_development_conflicts": conflicts,
        "cross_dataset_train_repeated_exposure_components": train_repeats,
        "analysis_gate": "conflicts_require_whole_episode_quarantine"
        if conflicts
        else "no_exact_train_development_conflict_detected",
        "candidate_exact_overlap_gate_passed": not conflicts,
        "absence_of_exact_overlap_proves_generalization_or_independence": False,
        "near_duplicate_analysis_performed": False,
    }


def write_json(path, value):
    raw = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def execute(plan_path, plan_sha):
    plan_path = repo_path(plan_path)
    if type(plan_sha) is not str or len(plan_sha) != 64:
        raise ValueError("Explicit plan SHA256 required")
    binding = {
        "path": plan_path.relative_to(ROOT).as_posix(),
        **hash_file(plan_path, BUDGET["max_control_bytes"]),
    }
    if binding["sha256"] != plan_sha:
        raise ValueError("Plan SHA differs")
    plan = parse_json(read_bound(binding))
    if canonical(plan) != canonical(build_plan(plan["output_dir"])):
        raise ValueError("Plan/source/metadata/runtime admission drift")
    out = output_path(plan["output_dir"])
    out.mkdir(parents=True, exist_ok=False)
    decoded, failures, source_after, runtime_after = [], [], [], None
    result = {
        "contract": REPORT_CONTRACT,
        "plan": binding,
        "native_exit_code": 1,
        "claims": dict.fromkeys(FALSE_CLAIMS, False),
        "observed59_is_development_not_official_holdout": True,
        "datasets": plan["datasets"],
        "source_scope": plan["source_scope"],
        "runtime_scope": (
            "Five listed CPU Pillow source/extension files; "
            "not exhaustive transitive DLL/dependency attestation"
        ),
        "source_bindings_before": plan["sources"],
        "runtime_before": plan["runtime"],
        "history_frames_counted_again": False,
        "actor_current_past_frame_closure_verified": all(
            row["actor_current_past_frame_closure_verified"] for row in plan["datasets"]
        ),
        "actor_frame_reference_occurrences": sum(
            row["actor_frame_reference_occurrences"] for row in plan["datasets"]
        ),
        "current_frame_reference_occurrences": len(plan["current_frame_occurrences"]),
        "past_frame_reference_occurrences": sum(
            row["actor_frame_reference_occurrences"] for row in plan["datasets"]
        )
        - len(plan["current_frame_occurrences"]),
        "unique_original_image_count": len(plan["images"]),
    }
    try:
        validate_pillow_origins(plan["runtime"])
        validator = png_api()
        for image in plan["images"]:
            decoded.append(decode_image(image, validator))
            if len(decoded) > BUDGET["max_decoded_images"]:
                raise ValueError("Decoded image cap exceeded")
        result.update(analyze(plan["datasets"], plan["current_frame_occurrences"], decoded))
    except Exception as error:
        failures.append(
            {"stage": "decode_or_overlap", "type": type(error).__name__, "message": str(error)}
        )
    try:
        for source in plan["sources"]:
            read_bound(source)
            source_after.append(source)
        for image in plan["images"]:
            path = repo_path(image["path"])
            if hash_file(path, BUDGET["max_encoded_image_bytes"]) != {
                key: image[key] for key in ("bytes", "sha256")
            }:
                raise ValueError("Original PNG changed after decode")
        runtime_after = pillow_bindings()
        if runtime_after != plan["runtime"]:
            raise ValueError("Pillow runtime changed during execution")
        read_bound(binding)
        check_no_heavy()
    except Exception as error:
        failures.append(
            {
                "stage": "final_source_runtime_recheck",
                "type": type(error).__name__,
                "message": str(error),
            }
        )
    result.update(
        {
            "source_bindings_after": source_after,
            "runtime_after": runtime_after,
            "sources_unchanged": not failures and source_after == plan["sources"],
            "verified_current_frame_occurrences": len(plan["current_frame_occurrences"])
            if not failures
            else 0,
            "decoded_image_count": len(decoded),
            "all_original_images_byte_and_pixel_verified": not failures
            and len(decoded) == len(plan["images"]),
            "partial_failures": failures,
        }
    )
    if failures:
        result["candidate_exact_overlap_gate_passed"] = False
    result["image_identities"] = write_json(out / "image-identities.json", decoded)
    result["native_exit_code"] = 1 if failures else 0
    write_json(out / "report.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-plan")
    parser.add_argument("--output-dir")
    parser.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    args = parser.parse_args()
    try:
        if args.emit_plan and args.output_dir and not args.plan and not args.plan_sha256:
            plan = build_plan(args.output_dir)
            target = repo_path(args.emit_plan)
            if (
                target.parent != (ROOT / "configs/training").resolve()
                or not target.name.startswith("onboard-navigation-dataset-overlap-")
                or target.suffix != ".json"
            ):
                raise ValueError("Require NEW dataset-overlap plan in configs/training")
            print(canonical(write_json(target, plan)))
            return 0
        if args.plan and args.plan_sha256 and not args.emit_plan and not args.output_dir:
            report = execute(args.plan, args.plan_sha256)
            print(
                canonical(
                    {
                        "native_exit_code": report["native_exit_code"],
                        "decoded_image_count": report["decoded_image_count"],
                        "candidate_exact_overlap_gate_passed": report[
                            "candidate_exact_overlap_gate_passed"
                        ],
                        "training_ready": False,
                    }
                )
            )
            return report["native_exit_code"]
        raise ValueError("Use exactly emit-plan/output-dir or plan/plan-sha256")
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
