"""First official CPU prefix/full-chat reference for six existing TRAIN episodes.

Only the NEW122 source table is processed. No model weights, network, optimizer,
CUDA initialization, dataset edits, historical CPU86 parity, or flight authority.
Emission is stdlib-only; execution imports the admitted CPU tensor/processor stack.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import inspect
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.onboard-navigation-indexed-cpu122-inputs-v1"
NEW_FILES = (
    "scripts/verify_onboard_navigation_indexed_cpu_inputs.py",
    "tests/test_verify_onboard_navigation_indexed_cpu_inputs.py",
)
OVERLAP = {
    "path": "docs/evidence/onboard-navigation-dataset-overlap-20261008-r1.json",
    "bytes": 5061,
    "sha256": "9ddcd89ab37bbf234aaec2030d391623ce838d608a9435f0690547296805448b",
}
DATA = {
    "path": "docs/evidence/openfly-indexed-training-supervision-20261008-r1.json",
    "bytes": 9671,
    "sha256": "9eb51ec796269363fda0c2586c0c11d0bb34f022badce1ec8f2e07f1c5c0a295",
}
ORIGINAL = {
    "path": "configs/training/onboard-vlm-navigation-pilot-plan-20261007-r1.json",
    "bytes": 13152,
    "sha256": "f4d76902c220812201f2ca4e961b0f427e4378ba6ee5449bf96e86be23999c53",
}
RUNTIME = {
    "path": "configs/training/onboard-navigation-importpath-runtime-20261007-r2.json",
    "bytes": 9763,
    "sha256": "3d4413dcb69e3671252fbe06ee84e34ad12d242329e3e2bd379952ae59a60c43",
}
SOURCE_PINS = {
    "scripts/audit_onboard_navigation_dataset_overlap.py": (
        30254,
        "e86490dabaecce6bdc518b995d0ab2cf541ea8991ac55a6755fb81a3e189ebff",
    ),
    "scripts/onboard_navigation_balanced_encoding.py": (
        8277,
        "622db7200a634237451ae01d8d4c4b1a2433a8a7ce7191fa0437cf811b9c6198",
    ),
    "scripts/onboard_navigation_closed_sample_encoding.py": (
        5165,
        "5f3bc0e7284c5a92bca1a4781898546eda2ec30c58626b1f321a023a179455b9",
    ),
    "scripts/verify_onboard_navigation_actor_inputs.py": (
        19144,
        "5fb7a2fbf46ae880fbace6f947bf77d571c3bc227af68c19808a88074c7d74b6",
    ),
    "scripts/train_onboard_vlm_navigation_pilot.py": (
        40430,
        "c12215422127173d130e7b452ffe0f9837b59c0fb688fad5992d10f8d9e1d50d",
    ),
    "scripts/train_onboard_vlm_grounding.py": (
        49187,
        "aa761e92a419c3a1ab8cc44c47696e6612a8b5e66a17d6dd2f83dcfe4bbe3c46",
    ),
    "scripts/verify_onboard_vlm_grounding_adapter.py": (
        16642,
        "809d622fe46e19157a4e1d8a2764cb1a683cbf68132e9f3870147dafc1da62a6",
    ),
    "scripts/probe_onboard_vlm.py": (
        31713,
        "6404ab0c2f3d03d8628f963a4bc5f9a18e17a216e8dba793585d5399dbb567f4",
    ),
    "src/multidetect/autonomy/onboard_navigation_actor.py": (
        13167,
        "eaefc34cd199295c43cab927d3c51182bfa728fd1753c7ac2e4150a1400a83e1",
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
LIMITS = {
    "max_pixels_per_image": 50176,
    "max_input_tokens": 4096,
    "max_new_tokens": 192,
    "cpu_threads": 1,
    "resident_samples": 1,
}
EXPECTED_ROUTES = {"143": 15, "150": 29, "153": 17, "154": 25, "148": 15, "144": 21}
RUNTIME_FILES = {
    "torch": ("torch/__init__.py",),
    "transformers": (
        "transformers/__init__.py",
        "transformers/processing_utils.py",
        "transformers/models/auto/processing_auto.py",
        "transformers/models/qwen3_vl/processing_qwen3_vl.py",
        "transformers/models/qwen3_vl/video_processing_qwen3_vl.py",
        "transformers/models/qwen2_vl/image_processing_qwen2_vl.py",
        "transformers/models/qwen2_vl/image_processing_qwen2_vl_fast.py",
        "transformers/models/qwen2/tokenization_qwen2.py",
        "transformers/models/qwen2/tokenization_qwen2_fast.py",
    ),
    "Pillow": ("PIL/__init__.py", "PIL/Image.py", "PIL/ImageFile.py", "PIL/PngImagePlugin.py"),
    "tokenizers": ("tokenizers/__init__.py",),
    "numpy": ("numpy/__init__.py",),
    "torchvision": ("torchvision/__init__.py",),
}
MAX_RUNTIME_BYTES = 32 * 1024 * 1024
FALSE_CLAIMS = (
    "model_loaded",
    "model_weights_read",
    "GPU_used",
    "network_used",
    "optimizer_performed",
    "training_ready_for_candidate",
    "training_or_candidate_plan_authorized",
    "quality_claim",
    "generalization_verified",
    "official_holdout_verified",
    "hil_real_evidence",
    "physical_performance_claimed",
    "outdoor_gnss_evidence",
    "candidate_promoted",
    "default_changed",
    "final_release_allowed",
    "execution_authority",
    "dataset_merged_or_split_modified",
    "historical_cpu86_metadata_parity_claimed",
)


def bootstrap():
    """Authenticate shared stdlib controls before importing shared repository code."""
    for name, (size, sha) in SOURCE_PINS.items():
        path = ROOT / name
        if path.stat().st_size != size or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError(f"Frozen dependency source drift: {name}")
    name = "scripts/audit_onboard_navigation_dataset_overlap.py"
    spec = importlib.util.spec_from_file_location("indexed_cpu_controls", ROOT / name)
    controls = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controls)
    return controls


def dependencies(controls):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))
    from multidetect.autonomy import onboard_navigation_actor as actor
    from multidetect.autonomy import onboard_navigation_supervision as nav
    from scripts import onboard_navigation_balanced_encoding as encoding
    from scripts import onboard_navigation_closed_sample_encoding as closed
    from scripts import train_onboard_vlm_navigation_pilot as pilot
    from scripts import verify_onboard_navigation_actor_inputs as checker

    modules = (
        actor,
        nav,
        encoding,
        closed,
        pilot,
        checker,
        pilot.core,
        pilot.recovery,
        pilot.core.probe,
    )
    allowed = {(ROOT / name).resolve() for name in SOURCE_PINS}
    if any(Path(module.__file__).resolve() not in allowed for module in modules):
        raise ValueError("Shared module origin differs from admitted repository source")
    return SimpleNamespace(
        actor=actor, nav=nav, encoding=encoding, closed=closed, pilot=pilot, checker=checker
    )


def assert_no_forbidden_modules(*, stdlib_phase=False):
    banned = {"pyarrow", "datasets"}
    if stdlib_phase:
        banned |= {"torch", "transformers", "PIL", "peft", "accelerate", "numpy"}
    if any(name.split(".")[0] in banned for name in sys.modules):
        raise ValueError("Forbidden module imported in this phase")


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def require_sha(value):
    if (
        type(value) is not str
        or len(value) != 64
        or any(letter not in "0123456789abcdef" for letter in value)
    ):
        raise ValueError("Require a canonical SHA256")


def runtime_record(path, controls):
    # Only explicitly listed, installed runtime files can authorize external paths.
    path = Path(path).resolve(strict=True)
    return {"path": str(path), **controls.hash_file(path, MAX_RUNTIME_BYTES)}


def runtime_inventory(retained, controls):
    if Path(sys.executable).resolve() != Path(retained["python_executable"]).resolve():
        raise ValueError("Use the previously verified navigation Python executable")
    versions = retained["package_versions"]
    if set(versions) != {"torch", "transformers", "peft", "accelerate", "safetensors"}:
        raise ValueError("The five previously pinned runtime versions are required")
    for name, version in versions.items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f"Pinned distribution version drift: {name}")
    files = [runtime_record(sys.executable, controls)]
    entries, package_roots = {}, {}
    actual_versions = dict(versions)
    for name, selected in RUNTIME_FILES.items():
        dist = importlib.metadata.distribution(name)
        actual_versions[name] = dist.version
        package_roots[name] = str(Path(dist.locate_file("")).resolve())
        listed = {item.as_posix(): item for item in dist.files or ()}
        for relative in selected:
            if relative not in listed:
                raise ValueError(f"Required CPU runtime entry is missing: {relative}")
            row = runtime_record(dist.locate_file(listed[relative]), controls)
            files.append(row)
            entries[relative] = row["path"]
        if name in {"torch", "Pillow", "tokenizers"}:
            package = {
                "torch": "torch/_C.",
                "Pillow": "PIL/_imaging.",
                "tokenizers": "tokenizers/tokenizers.",
            }[name]
            extensions = [
                item
                for relative, item in listed.items()
                if relative.startswith(package) and relative.endswith((".pyd", ".so"))
            ]
            if len(extensions) != 1:
                raise ValueError(f"Require one actual CPU core extension for {name}")
            files.append(runtime_record(dist.locate_file(extensions[0]), controls))
    unique = {row["path"]: row for row in files}
    return {
        "python_executable": str(Path(sys.executable).resolve()),
        "package_versions": actual_versions,
        "legacy_five_package_versions": versions,
        "package_roots": package_roots,
        "entry_origins": entries,
        "source_files": [unique[key] for key in sorted(unique)],
        "scope": (
            "Listed CPU entrypoints/extensions and five pinned versions; "
            "not exhaustive DLL/transitive attestation"
        ),
    }


def fresh_output(value, controls):
    path = controls.repo_path(value)
    if (
        path.parent != (ROOT / "artifacts/diagnostics").resolve()
        or not path.name.startswith("onboard-navigation-indexed-cpu122-")
        or path.exists()
    ):
        raise ValueError("Require fresh direct indexed-cpu122 diagnostics output")
    return path


def target_from_sample(row):
    action = row["planned_action"]
    return {
        "schema_version": 1,
        "contract": "multidetect.onboard-planned-navigation-target-v1",
        "kind": action["kind"],
        "translation_frame": "body_frd",
        "yaw_convention": "clockwise_right_positive",
        "translation_m": action["translation_m"],
        "yaw_delta_rad": action["yaw_delta_rad"],
    }


def admit_rows(rows, source_report, api):
    typed = tuple(api.nav.NavigationSupervisionSample.from_mapping(row) for row in rows)
    api.nav.validate_navigation_episode_samples(typed)
    ids = [sample.sample_id for sample in typed]
    bindings = source_report["sample_actor_target_bindings"]
    if len(ids) != 122 or len(set(ids)) != 122 or ids != [row["sample_id"] for row in bindings]:
        raise ValueError("Exactly all122 original source identities/order are required")
    route_counts, episodes = {}, {}
    for sample, row, prior in zip(typed, rows, bindings, strict=True):
        if (
            sample.partition != "train"
            or sample.scene_id != "env_airsim_26"
            or sample.source.original_split != "train"
        ):
            raise ValueError("Only six original scene26 TRAIN episodes are admitted")
        index = sample.source.annotation_record_id
        route_counts[index] = route_counts.get(index, 0) + 1
        episodes.setdefault(index, set()).add(sample.episode_id)
        if (
            prior["episode_id"] != sample.episode_id
            or canonical(prior["ordered_frame_steps"])
            != canonical([frame.step_index for frame in sample.frames])
            or canonical(prior["target"]) != canonical(target_from_sample(row))
        ):
            raise ValueError("Prior source-only Actor/target/chronology differs")
    if (
        route_counts != EXPECTED_ROUTES
        or list(route_counts) != list(EXPECTED_ROUTES)
        or any(len(value) != 1 for value in episodes.values())
        or len({sample.episode_id for sample in typed}) != 6
        or sum(sample.planned_action.kind == "stop" for sample in typed) != 6
    ):
        raise ValueError("Fixed six whole routes/116 successors/6 STOP differ")
    current = {(sample.episode_id, sample.step_index): sample.frames[-1] for sample in typed}
    if any(
        current.get((sample.episode_id, frame.step_index)) != frame
        for sample in typed
        for frame in sample.frames
    ):
        raise ValueError("Current/past frame closure differs")
    return typed


def build_plan(output_directory):
    controls = bootstrap()
    output = fresh_output(output_directory, controls)
    overlap = controls.parse_json(controls.read_bound(OVERLAP))
    data = controls.parse_json(controls.read_bound(DATA))
    original = controls.parse_json(controls.read_bound(ORIGINAL))
    retained = controls.parse_json(controls.read_bound(RUNTIME))
    bindings = [OVERLAP, DATA, ORIGINAL, RUNTIME, *overlap["bindings"], *data["bindings"]]
    if (
        overlap["actual_execution"]["actual_native_exit_code"] != 0
        or overlap["observations"]["candidate_exact_overlap_gate_passed"] is not True
        or data["actual_execution"]["actual_native_exit_code"] != 0
    ):
        raise ValueError("Sealed source-only/overlap stages must have passed")
    overlap_binding = next(
        row for row in overlap["bindings"] if row["path"].endswith("/report.json")
    )
    overlap_report = controls.parse_json(controls.read_bound(overlap_binding))
    if (
        overlap_report["native_exit_code"] != 0
        or overlap_report["candidate_exact_overlap_gate_passed"] is not True
        or overlap_report["partial_failures"]
        or overlap_report["sources_unchanged"] is not True
        or overlap_report["actor_current_past_frame_closure_verified"] is not True
    ):
        raise ValueError("Actual closed overlap report does not admit this candidate analysis")
    source_report_binding = next(
        row for row in data["bindings"] if row["path"].endswith("/report.json")
    )
    dataset = next(row for row in data["bindings"] if row["path"].endswith("/samples.jsonl"))
    source_report = controls.parse_json(controls.read_bound(source_report_binding))
    if (
        source_report["native_exit_code"] != 0
        or source_report["source_validation_session_verified"] is not True
        or controls.normalize(source_report["dataset"]) != controls.normalize(dataset)
    ):
        raise ValueError("Exact NEW122 source table/report stage required")
    rows = [controls.parse_json(line) for line in controls.read_bound(dataset).splitlines()]
    api = dependencies(controls)
    samples = admit_rows(rows, source_report, api)
    images = []
    for sample in samples:
        for value in sample.source.to_mapping().values():
            if type(value) is dict and set(value) == {"path", "bytes", "sha256"}:
                bindings.append(value)
        images.extend(
            {"path": frame.path, "bytes": frame.bytes, "sha256": frame.sha256}
            for frame in sample.frames
        )
    bindings.extend(
        {"path": name, "bytes": size, "sha256": sha} for name, (size, sha) in SOURCE_PINS.items()
    )
    bindings.extend(controls.bind_file(name) for name in NEW_FILES)
    processor_files = controls.merge_bindings(original["base"]["processor_files"])
    if len(processor_files) != 10 or any(
        row["path"].endswith(".safetensors") for row in processor_files
    ):
        raise ValueError("Only ten original processor metadata files, never model weights")
    sources = controls.merge_bindings(bindings)
    for row in sources:
        controls.read_bound(row)
    for row in processor_files:
        if controls.hash_file(controls.repo_path(row["path"]), row["bytes"]) != {
            key: row[key] for key in ("bytes", "sha256")
        }:
            raise ValueError("Original processor metadata byte/SHA drift")
    plan = {
        "schema_version": 1,
        "contract": CONTRACT + "-plan",
        "output_dir": output.relative_to(ROOT).as_posix(),
        "dataset": controls.normalize(dataset),
        "source_report": controls.normalize(source_report_binding),
        "sample_ids": [sample.sample_id for sample in samples],
        "routes": EXPECTED_ROUTES,
        "sample_count": 122,
        "episode_count": 6,
        "partition_counts": {"train": 122},
        "limits": LIMITS,
        "sources": sources,
        "images": controls.merge_bindings(images),
        "processor_files": processor_files,
        "processor_snapshot": original["base"]["snapshot"],
        "runtime": runtime_inventory(retained, controls),
        "reference_kind": (
            "first_official_cpu122_prefix_and_full_chat_metadata_not_historical_parity"
        ),
        "claims": dict.fromkeys(FALSE_CLAIMS, False),
    }
    for row in sources:
        controls.read_bound(row)
    assert_no_forbidden_modules(stdlib_phase=True)
    return plan


def validate_encoding_metadata(reference, prepared, sample, source_actor, api):
    """First-reference structural/source check, not self-certified old parity."""
    fields = {"input_ids", "attention_mask", "pixel_values", "image_grid_thw"}
    if set(reference) != {
        "sample_id",
        "actor_identity_sha256",
        "source_prompt_sha256",
        "source_frames",
        "retained_history_valid",
        "actor_identities_equal",
        "training_prefix",
        "observable_prefix",
        "all_four_tensor_bytes_equal",
        "labels_pose_future_ids_entered_model_messages",
    } or set(prepared) != {
        "prefix_tokens",
        "total_tokens",
        "response_tokens",
        "supervised_tokens",
        "prefix_tensors",
        "input_tensors",
        "response_labels_sha256",
        "target_sha256",
        "actor_identity_sha256",
        "prefix_reference_verified",
        "prompt_label_mask_verified",
    }:
        raise ValueError("Unknown or missing first-reference/full metadata fields")
    require_sha(reference["actor_identity_sha256"])
    if (
        reference["sample_id"] != sample.sample_id
        or reference["actor_identity_sha256"] != source_actor["actor_identity_sha256"]
        or prepared["actor_identity_sha256"] != reference["actor_identity_sha256"]
        or reference["actor_identities_equal"] is not True
        or reference["all_four_tensor_bytes_equal"] is not True
        or reference["labels_pose_future_ids_entered_model_messages"] is not False
        or reference["retained_history_valid"] is not True
    ):
        raise ValueError("Actual first reference/source-only Actor binding differs")
    expected_frames = [
        {
            "sequence": frame.step_index,
            "path": frame.path,
            "bytes": frame.bytes,
            "sha256": frame.sha256,
            "width": frame.width,
            "height": frame.height,
            "encoding": frame.encoding,
        }
        for frame in sample.frames
    ]
    if reference["source_prompt_sha256"] != sample.prompt.utf8_sha256 or canonical(
        reference["source_frames"]
    ) != canonical(expected_frames):
        raise ValueError("Verbatim prompt/current-past chronology differs")
    prefix = reference["training_prefix"]
    if (
        canonical(prefix) != canonical(reference["observable_prefix"])
        or canonical(prepared["prefix_tensors"]) != canonical(prefix["tensors"])
        or set(prefix["tensors"]) != fields
    ):
        raise ValueError("Four actual prefix tensor records differ")
    if set(prefix) != {
        "prefix_tokens",
        "actual_image_grid_thw",
        "actual_image_pixels",
        "patch_size",
        "temporal_patch_size",
        "tensors",
    }:
        raise ValueError("Unexpected actual prefix record fields")
    for name in ("patch_size", "temporal_patch_size", "prefix_tokens"):
        if type(prefix[name]) is not int or prefix[name] <= 0:
            raise ValueError("Positive typed actual patch/token dimensions required")
    grid = prefix["actual_image_grid_thw"]
    if (
        type(grid) is not list
        or len(grid) != len(sample.frames)
        or any(
            type(row) is not list
            or len(row) != 3
            or any(type(item) is not int or item <= 0 for item in row)
            or row[0] != 1
            for row in grid
        )
    ):
        raise ValueError("Actual image grids must cover exact current/past frames")
    pixels = [h * w * prefix["patch_size"] ** 2 for _, h, w in grid]
    if canonical(pixels) != canonical(prefix["actual_image_pixels"]) or any(
        not 1024 <= count <= LIMITS["max_pixels_per_image"] for count in pixels
    ):
        raise ValueError("Actual processed-image pixel budget differs")
    for name, record in prefix["tensors"].items():
        if (
            set(record) != {"dtype", "shape", "sha256"}
            or type(record["shape"]) is not list
            or any(type(item) is not int or item <= 0 for item in record["shape"])
        ):
            raise ValueError("Prefix tensor shape must be exact typed dimensions")
        require_sha(record["sha256"])
        expected_dtype = "torch.float32" if name == "pixel_values" else "torch.int64"
        expected_shape = {
            "input_ids": [1, prefix["prefix_tokens"]],
            "attention_mask": [1, prefix["prefix_tokens"]],
            "image_grid_thw": [len(sample.frames), 3],
            "pixel_values": [
                sum(t * h * w for t, h, w in grid),
                3 * prefix["temporal_patch_size"] * prefix["patch_size"] ** 2,
            ],
        }[name]
        if record["dtype"] != expected_dtype or record["shape"] != expected_shape:
            raise ValueError("Actual prefix tensor dtype/shape/grid differs")
    for key in ("prefix_tokens", "total_tokens", "response_tokens", "supervised_tokens"):
        if type(prepared[key]) is not int or prepared[key] <= 0:
            raise ValueError("Exact positive typed token counts required")
    if (
        prepared["prefix_tokens"] != prefix["prefix_tokens"]
        or prepared["total_tokens"] != prepared["prefix_tokens"] + prepared["response_tokens"]
        or prepared["supervised_tokens"] != prepared["response_tokens"]
        or prepared["total_tokens"] > LIMITS["max_input_tokens"]
        or prepared["response_tokens"] > LIMITS["max_new_tokens"]
        or prepared["prefix_reference_verified"] is not True
        or prepared["prompt_label_mask_verified"] is not True
    ):
        raise ValueError("Full-chat response-only label/token budget differs")
    if set(prepared["input_tensors"]) != fields:
        raise ValueError("Exactly four full-chat tensor records required")
    for name in fields:
        value = prepared["input_tensors"][name]
        if (
            set(value) != {"dtype", "shape", "sha256"}
            or type(value["shape"]) is not list
            or any(type(item) is not int or item <= 0 for item in value["shape"])
        ):
            raise ValueError("Full-chat tensor record is not an exact byte binding")
        require_sha(value["sha256"])
        if name in {"input_ids", "attention_mask"}:
            if value["dtype"] != "torch.int64" or value["shape"] != [1, prepared["total_tokens"]]:
                raise ValueError("Full-chat token dtype/shape differs")
        elif canonical(value) != canonical(prefix["tensors"][name]):
            raise ValueError("Target suffix changed image/grid tensor bytes")
    target = api.nav.canonical_json(source_actor["target"]).encode()
    require_sha(prepared["response_labels_sha256"])
    if prepared["target_sha256"] != hashlib.sha256(target).hexdigest():
        raise ValueError("Actual target/response label binding differs")


def encode_first_reference(sample, root, *, processor, torch, budget, source_actor, api):
    """Return metadata only after one source session's normal verified closure."""
    session = api.closed.ClosedNavigationSourceValidationSession(root)
    value = None
    body_completed = False
    try:
        with session:
            session.prime((sample,))
            reference = api.checker.verify_sample_pair(
                sample,
                root,
                session,
                processor,
                actor_module=api.actor,
                pilot=api.pilot,
                budget=api.nav.NavigationSupervisionBudget(),
                torch_module=torch,
            )
            try:
                value = api.encoding.encode_navigation_sample(
                    sample,
                    root,
                    processor=processor,
                    torch_module=torch,
                    budget=budget,
                    prefix_reference=reference,
                    session=session,
                )
                if type(value) is not dict:
                    raise ValueError("Existing frozen encoder must return a mapping")
                prepared = {
                    key: item
                    for key, item in value.items()
                    if key not in {"prefix", "full", "labels"}
                }
                validate_encoding_metadata(reference, prepared, sample, source_actor, api)
                body_completed = True
            finally:
                # Do not hold tensor/label buffers through source verification, including failures.
                value = None
        if not body_completed or session.verified is not True or session._active:
            raise ValueError("Provisional metadata cannot escape an unfinished source session")
        return json.loads(api.pilot.core.canonical_bytes(reference)), json.loads(
            api.pilot.core.canonical_bytes(prepared)
        )
    except BaseException:
        value = None
        raise


def validate_table(references, prepared, samples, source_report, api):
    ids = [sample.sample_id for sample in samples]
    if (
        len(ids) != 122
        or len(set(ids)) != 122
        or [row["sample_id"] for row in references] != ids
        or set(prepared) != set(ids)
    ):
        raise ValueError("All122 first-reference/prepared identities required")
    actors = {row["sample_id"]: row for row in source_report["sample_actor_target_bindings"]}
    for sample, reference in zip(samples, references, strict=True):
        validate_encoding_metadata(
            reference, prepared[sample.sample_id], sample, actors[sample.sample_id], api
        )
    return prepared


def validate_runtime_origins(processor, torch, runtime):
    import numpy
    import PIL
    import tokenizers
    import tokenizers.tokenizers
    import torchvision
    import transformers
    from PIL import Image, ImageFile, PngImagePlugin

    expected = set(row["path"] for row in runtime["source_files"])
    actual = [
        Path(module.__file__).resolve()
        for module in (
            torch,
            torch._C,
            transformers,
            PIL,
            Image,
            ImageFile,
            PngImagePlugin,
            Image.core,
            numpy,
            tokenizers,
            tokenizers.tokenizers,
            torchvision,
        )
    ]
    for value in (processor, processor.image_processor, processor.tokenizer):
        source = inspect.getsourcefile(type(value))
        if source is None:
            raise ValueError("Processor class must have a bound Python source origin")
        actual.append(Path(source).resolve())
    if any(str(path) not in expected for path in actual):
        raise ValueError("Actual CPU processor/tensor class origin is not admitted")
    if type(processor).__name__ != "Qwen3VLProcessor" or torch.cuda.is_initialized():
        raise ValueError("Official CPU processor required without CUDA initialization")
    return [str(path) for path in actual]


def recheck(plan, controls, *, include_images):
    for row in plan["sources"]:
        controls.read_bound(row)
    for row in plan["processor_files"]:
        if controls.hash_file(controls.repo_path(row["path"]), row["bytes"]) != {
            key: row[key] for key in ("bytes", "sha256")
        }:
            raise ValueError("Processor metadata changed")
    for row in plan["runtime"]["source_files"]:
        if runtime_record(row["path"], controls) != row:
            raise ValueError("Admitted CPU runtime source changed")
    if include_images:
        for row in plan["images"]:
            if controls.hash_file(controls.repo_path(row["path"]), 8 * 1024 * 1024) != {
                key: row[key] for key in ("bytes", "sha256")
            }:
                raise ValueError("Original sample PNG changed")


def run(plan_path, plan_sha):
    require_sha(plan_sha)
    controls = bootstrap()
    binding = controls.bind_file(plan_path)
    if binding["sha256"] != plan_sha:
        raise ValueError("Explicit CPU122 plan SHA differs")
    plan = controls.parse_json(controls.read_bound(binding))
    if controls.canonical(plan) != controls.canonical(build_plan(plan["output_dir"])):
        raise ValueError("CPU122 plan/source/runtime drift before processor import")
    recheck(plan, controls, include_images=True)
    output = fresh_output(plan["output_dir"], controls)
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        "pid": os.getpid(),
        "plan": binding,
        "success": False,
        "native_exit_code": 1,
        "training_dataset_cpu_admitted": False,
        "reference_kind": plan["reference_kind"],
        "historical_metadata_reference_exists": False,
        "sample_count": 0,
        "sample_ids": plan["sample_ids"],
        "admitted_sample_ids": [],
        "source_validation_sessions_verified": 0,
        "source_bindings_before": plan["sources"],
        "source_bindings_after": None,
        "source_bindings_unchanged": False,
        "processor_files_before": plan["processor_files"],
        "processor_files_after": None,
        "runtime_before": plan["runtime"],
        "runtime_after": None,
        "limits": LIMITS,
        "observations": [],
        "partial_failures": [],
        **dict.fromkeys(FALSE_CLAIMS, False),
    }
    references, prepared = [], {}

    def observe(stage, **fields):
        row = {"stage": stage, "elapsed_since_start_s": time.perf_counter() - started, **fields}
        report["observations"].append(row)
        print(json.dumps({"event": "indexed_cpu122_progress", **row}), flush=True)

    started = time.perf_counter()
    torch = None
    try:
        api = dependencies(controls)
        rows = [
            controls.parse_json(line) for line in controls.read_bound(plan["dataset"]).splitlines()
        ]
        source_report = controls.parse_json(controls.read_bound(plan["source_report"]))
        samples = admit_rows(rows, source_report, api)
        if [sample.sample_id for sample in samples] != plan["sample_ids"]:
            raise ValueError("All122 source order changed")
        actors = {row["sample_id"]: row for row in source_report["sample_actor_target_bindings"]}
        import torch as torch_module

        torch = torch_module
        if torch.cuda.is_initialized():
            raise ValueError("CPU inputs require CUDA to remain uninitialized")
        report["CUDA_uninitialized_before"] = True
        torch.set_num_threads(LIMITS["cpu_threads"])
        if torch.get_num_threads() != LIMITS["cpu_threads"]:
            raise ValueError("Explicit one-thread CPU plan was not applied")
        observe("processor_load_started")
        processor = api.checker.official_processor(
            Path(plan["processor_snapshot"]), SimpleNamespace(**LIMITS)
        )
        report["actual_runtime_origins"] = validate_runtime_origins(
            processor, torch, plan["runtime"]
        )
        report["official_processor_used"] = True
        observe("processor_load_finished")
        for sample in samples:
            observe("sample_started", sample_id=sample.sample_id, admitted_count=len(prepared))
            reference, row = encode_first_reference(
                sample,
                ROOT,
                processor=processor,
                torch=torch,
                budget=SimpleNamespace(**LIMITS),
                source_actor=actors[sample.sample_id],
                api=api,
            )
            if torch.cuda.is_initialized():
                raise ValueError("CPU sample encoding initialized CUDA")
            references.append(reference)
            prepared[sample.sample_id] = row
            report["admitted_sample_ids"].append(sample.sample_id)
            report["sample_count"] = report["source_validation_sessions_verified"] = len(prepared)
            observe(
                "sample_admitted",
                sample_id=sample.sample_id,
                admitted_count=len(prepared),
                source_session_normally_closed=True,
            )
        validate_table(references, prepared, samples, source_report, api)
        observe("source_recheck_started")
        recheck(plan, controls, include_images=True)
        controls.read_bound(binding)
        if (
            runtime_inventory(controls.parse_json(controls.read_bound(RUNTIME)), controls)
            != plan["runtime"]
        ):
            raise ValueError("Versions/entry origins changed during CPU preparation")
        report["source_bindings_after"] = plan["sources"]
        report["processor_files_after"] = plan["processor_files"]
        report["runtime_after"] = plan["runtime"]
        if (
            validate_runtime_origins(processor, torch, plan["runtime"])
            != report["actual_runtime_origins"]
        ):
            raise ValueError("Actual imported CPU class/extension origins changed")
        report["source_bindings_unchanged"] = True
        report["CUDA_uninitialized_after"] = not torch.cuda.is_initialized()
        assert_no_forbidden_modules()
        observe("source_recheck_finished")
        if not report["CUDA_uninitialized_after"]:
            raise ValueError("CPU preparation initialized CUDA")
        report.update(success=True, native_exit_code=0, training_dataset_cpu_admitted=True)
    except Exception as error:
        report["partial_failures"].append(
            {"type": type(error).__name__, "message": str(error), "admitted_count": len(prepared)}
        )
        if torch is not None:
            report["CUDA_uninitialized_after"] = not torch.cuda.is_initialized()
    report["prefix_reference"] = controls.write_json(
        output / "prefix-reference.json",
        {"reference_kind": plan["reference_kind"], "samples": references},
    )
    report["prepared_inputs"] = controls.write_json(output / "prepared-inputs.json", prepared)
    report["duration_s"] = time.perf_counter() - started
    controls.write_json(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-plan")
    parser.add_argument("--output-dir")
    parser.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    args = parser.parse_args()
    try:
        if args.emit_plan and args.output_dir and not args.plan and not args.plan_sha256:
            controls = bootstrap()
            plan = build_plan(args.output_dir)
            target = controls.repo_path(args.emit_plan)
            if (
                target.parent != (ROOT / "configs/training").resolve()
                or not target.name.startswith("onboard-navigation-indexed-cpu122-")
                or target.suffix != ".json"
            ):
                raise ValueError("Require NEW indexed-cpu122 plan in configs/training")
            print(json.dumps(controls.write_json(target, plan), sort_keys=True))
            return 0
        if args.plan and args.plan_sha256 and not args.emit_plan and not args.output_dir:
            report = run(args.plan, args.plan_sha256)
            print(
                json.dumps(
                    {
                        key: report[key]
                        for key in (
                            "native_exit_code",
                            "sample_count",
                            "training_dataset_cpu_admitted",
                            "training_ready_for_candidate",
                        )
                    },
                    sort_keys=True,
                )
            )
            return report["native_exit_code"]
        raise ValueError("Use exactly emit-plan/output-dir or plan/plan-sha256")
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
