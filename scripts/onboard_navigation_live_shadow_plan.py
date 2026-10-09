"""Small SHA-bound Gazebo shadow plan; no datasets, models or flight authority.

The two large weight identities are declarations plus current stat evidence here.
Only the separate exact loader may claim full weight/parameter verification.
Plans are written directly by Python so Windows uint64 file identities survive.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.onboard-navigation-live-shadow-plan-v1"
LOADER = "scripts.onboard_navigation_live_loader"
MAX_SMALL_BYTES = 8 * 1024 * 1024
MODULE_FILES = (
    "scripts/onboard_navigation_live_shadow_plan.py",
    "tests/test_onboard_navigation_live_shadow_plan.py",
    "scripts/onboard_navigation_live_loader.py",
    "tests/test_onboard_navigation_live_loader.py",
    "scripts/run_onboard_navigation_gazebo_learned_shadow.py",
    "tests/test_onboard_navigation_gazebo_learned_shadow.py",
    "scripts/run_gazebo_navigation_input_capture.py",
    "tests/test_gazebo_navigation_input_capture.py",
    "scripts/onboard_navigation_gazebo_shadow_input.py",
    "scripts/onboard_navigation_inference_backend.py",
    "scripts/verify_onboard_navigation_actor_inputs.py",
    "scripts/onboard_navigation_learned_readout_backend.py",
    "tests/test_onboard_navigation_learned_readout_backend.py",
    "scripts/run_simulator_docker_acceptance.py",
    "scripts/run_simulator_docker.py",
    "scripts/gazebo_sensor_stream.py",
    "src/multidetect/clock.py",
    "src/multidetect/backends/gazebo_sim.py",
    "src/multidetect/autonomy/onboard_navigation_actor.py",
    "src/multidetect/autonomy/onboard_navigation_inference.py",
    "src/multidetect/autonomy/onboard_navigation_supervision.py",
    "src/multidetect/autonomy/onboard_live_vlm_contract.py",
    "src/multidetect/autonomy/onboard_vlm_runtime_contract.py",
    "src/multidetect/autonomy/onboard_multimodal.py",
    "src/multidetect/autonomy/onboard_history.py",
    "src/multidetect/autonomy/onboard_agent.py",
    "src/multidetect/autonomy/contracts.py",
    "src/multidetect/autonomy/serialization.py",
    "src/multidetect/gazebo_depth.py",
)
SCENARIO = "configs/scenarios/urban_inspection_multirotor_001.scenario.json"
CAPTURE_PACKAGE_VERSIONS = {"pymavlink": "2.4.50", "fastcrc": "0.5.0", "lxml": "6.1.3"}
MODEL_PINS = {
    "base_weights": (
        4255140312,
        "7de1838c87a5349b016c26a1c3f7d2bc400a3d485f95ef39a7059ffd734977a0",
    ),
    "adapter_weights": (
        3227648,
        "6b30a9753ed763a13755ac2666356ec2cdc439840c69a75392a31fca44076f19",
    ),
    "adapter_config": (931, "7b6be286e2eb42bbd2eb614c3ea754c2d14d7c394f1db60fa78d82cf949bd10d"),
}
PARAMETER_PINS = {
    "base": "c04c7a37e3a717329d2f6b06c9fd7109f431e938e4d201c319e819662113aac6",
    "adapter": "ee922c7960f60e4ef4d5b6fcee00e87e4f0713171b21c5e286b43486523abe6e",
}
HEAD_PIN = (28481, "0d31701b9bf50de1b2570df280f5e7ad3672cc1db52aa2525dea07bd4767ff2b")
PROTECTED = {
    "configs/product-version.json": (
        162,
        "567888e837749c98e7c45c518f914b66228e7ed5bfcde53e796e94dc086e9729",
    ),
    "configs/release-parity.json": (
        13874,
        "ab71adb31104f738ab2a5d605872367cc7f0769ea2514a1bd45866d49dc2f81c",
    ),
    "configs/training/onboard-policy-current-reference-v1.json": (
        1343,
        "dc6bf892db1af3e6694bed5bf7e684e341141bd41627eaccd530bdb70e6808ed",
    ),
}
EVIDENCE = {
    "docs/evidence/onboard-navigation-recovery267-terminal-20261008-r2.json": (
        6446,
        "4a40567318bffbd547f68f025e5ce5f60012056df5ed0af10615f6eccf553203",
    ),
    "docs/evidence/onboard-navigation-candidate208-feature-cache-actual-20261008-r1.json": (
        5456,
        "d48bb8831f558f4363f36cb889701b79760fb0e5b086b9c1e77e61552ea92569",
    ),
    "docs/evidence/onboard-navigation-stable-cache-training-actual-20261008-r1.json": (
        8261,
        "7dcd555477da803209f79579767ede4cac31c6ac9b045bd683db385eec314bda",
    ),
}
RULE = {
    "model_variant": "candidate267_only",
    "readout_variant": "locked_final_all208",
    "maximum_new_camera_frames": 8,
    "worker_timeout_s": 600,
    "capture_timeout_s": 120,
    "max_frame_age_s": 5.0,
    "request_deadline_s": 5.0,
    "maximum_models": 1,
    "maximum_optimizer_attempts": 0,
    "maximum_generation_calls": 0,
    "inherited_head_optimizer_attempts": 3000,
    "inherited_base_lora_optimizer_attempts": 280,
    "preload_before_real_capture": True,
    "input": "original_prompt_and_current_latest_past_RGB_only",
    "source_inventory_scope": "explicit_entrypoints_not_recursive_training_or_transitive_assets",
    "large_weight_admission": "declared_SHA_and_stat_only_until_separate_exact_loader",
}
FALSE_CLAIMS = dict.fromkeys(
    (
        "training_performed",
        "generation_performed",
        "training_budget_reset",
        "model_quality_verified",
        "end_to_end_realtime_verified",
        "sensor_authenticity_verified",
        "mission_runner_used",
        "transport_authority",
        "execution_authority",
        "flight_performed",
        "candidate_promoted",
        "default_model_changed",
        "final_release_allowed",
    ),
    False,
)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha(value):
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("Canonical SHA-256 required")
    return value


def _object(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError("Exact fields required")
    return value


def _pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _json(data):
    def reject(value):
        raise ValueError("Nonfinite JSON rejected: " + value)

    value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=reject)
    canonical(value)
    return value


def safe_path(value, *, external=False):
    if type(value) not in (str, Path) and not isinstance(value, Path):
        raise ValueError("Explicit file path required")
    value = Path(value)
    if external and not value.is_absolute():
        raise ValueError("Runtime origins must be absolute paths")
    path = (value if value.is_absolute() else ROOT / value).resolve(strict=True)
    if not external and not path.is_relative_to(ROOT.resolve()):
        raise ValueError("Repository path escaped workspace")
    if not path.is_file():
        raise ValueError("Regular file required")
    return path


def record(value, *, external=False):
    path = safe_path(value, external=external)
    before = path.stat()
    if before.st_size > MAX_SMALL_BYTES:
        raise ValueError("Bounded small file required; large weight bytes must not be read here")
    raw = path.read_bytes()
    after = path.stat()
    # Reading may update access time; only content/replacement identity is causal.
    fields = ("st_size", "st_mtime_ns", "st_dev", "st_ino")
    if (
        any(getattr(after, field) != getattr(before, field) for field in fields)
        or len(raw) != before.st_size
    ):
        raise ValueError("File changed while hashing")
    return {"path": path.as_posix(), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _binding(row, *, external=False, hash_bytes=True):
    _object(row, ("path", "bytes", "sha256"))
    _sha(row["sha256"])
    if type(row["bytes"]) is not int or row["bytes"] < 0:
        raise ValueError("Exact nonnegative file size required")
    path = safe_path(row["path"], external=external)
    if row["path"] != path.as_posix() or path.stat().st_size != row["bytes"]:
        raise ValueError("Canonical file path/size differs")
    if hash_bytes and record(path, external=external) != row:
        raise ValueError("Current small-file SHA differs")
    return path


def _normalize(rows, *, external=False, maximum=256):
    if type(rows) is not list or not 1 <= len(rows) <= maximum:
        raise ValueError("Bounded explicit source inventory required")
    result = {}
    for row in rows:
        _binding(row, external=external)
        prior = result.setdefault(row["path"], row)
        if prior != row:
            raise ValueError("Conflicting source declarations")
    return [result[key] for key in sorted(result)]


def _pinned_rows(pins):
    rows = []
    for name, (size, digest) in pins.items():
        row = {"path": safe_path(name).as_posix(), "bytes": size, "sha256": digest}
        _binding(row)
        rows.append(row)
    return sorted(rows, key=lambda row: row["path"])


def output_path(value):
    path = Path(value)
    path = (path if path.is_absolute() else ROOT / path).resolve(strict=False)
    parent = (ROOT / "artifacts/evaluation").resolve(strict=True)
    if path.parent != parent or not path.name.startswith("onboard-navigation-live-shadow-"):
        raise ValueError("Fresh direct navigation live-shadow evaluation directory required")
    if path.exists():
        raise FileExistsError("Output directory already exists")
    return path


def _loader(source_files):
    expected = (ROOT / "scripts/onboard_navigation_live_loader.py").resolve(strict=True)
    matches = [row for row in source_files if row["path"] == expected.as_posix()]
    if len(matches) != 1:
        raise ValueError("Loader source binding required before import")
    _binding(matches[0])
    previous = sys.path[:]
    try:
        sys.path[:0] = [str(ROOT), str(ROOT / "src")]
        module = importlib.import_module(LOADER)
        if Path(module.__file__).resolve() != expected:
            raise ValueError("Imported loader origin differs")
        _binding(matches[0])
        return module
    finally:
        sys.path[:] = previous


def _sources(config, extras):
    paths = [*MODULE_FILES, SCENARIO, *extras]
    for extra in extras:
        p = safe_path(extra)
        if p.suffix != ".py" or p.relative_to(ROOT.resolve()).parts[0] not in (
            "scripts",
            "src",
            "tests",
        ):
            raise ValueError("Only explicit implementation/test entrypoints may be added")
    model, readout = config["model"], config["readout"]
    return _normalize(
        [
            *[record(name) for name in paths],
            *config["loader_dependencies"],
            *config["source_files"],
            *model["snapshot_files"],
            model["audit_source"],
            model["adapter_config"],
            *[readout[key] for key in ("receipt", "metadata", "weights")],
        ]
    )


def _capture_runtime(value):
    _object(
        value,
        (
            "contract",
            "python_executable",
            "provider_site",
            "packages",
            "source_files",
            "module_origins",
            "no_sys_path_insertion",
            "allowed_fallback_top_level_names",
            "shadow_existing_modules",
        ),
    )
    if (
        value["contract"] != "multidetect.navigation-live-capture-dependencies-v1"
        or (
            value["no_sys_path_insertion"] is not True
            or value["shadow_existing_modules"] is not False
        )
        or value["allowed_fallback_top_level_names"] != ["fastcrc", "lxml", "pymavlink"]
    ):
        raise ValueError("Exact nonshadowing capture fallback contract required")
    if safe_path(value["python_executable"], external=True) != (
        ROOT / ".venv-vlm-navigation/Scripts/python.exe"
    ).resolve(strict=True):
        raise ValueError("Capture must use the same pinned navigation Python")
    site = Path(value["provider_site"])
    if not site.is_absolute() or not site.resolve(strict=True).is_dir():
        raise ValueError("Explicit existing capture provider directory required")
    site = site.resolve(strict=True)
    packages = _object(value["packages"], ("pymavlink", "fastcrc", "lxml"))
    all_rows = []
    for name, package in packages.items():
        _object(package, ("version", "root", "files"))
        if package["version"] != CAPTURE_PACKAGE_VERSIONS[name]:
            raise ValueError("Exact pinned capture package version required")
        package_root = Path(package["root"])
        if not package_root.is_absolute() or package_root.resolve(strict=True) != site / name:
            raise ValueError("Capture package root must be inside the explicit provider")
        all_rows.extend(package["files"])
    origins = value["module_origins"]
    _object(origins, ("pymavlink", "fastcrc", "lxml"))
    rows = _normalize(value["source_files"], external=True, maximum=512)
    paths = {row["path"] for row in rows}
    if rows != _normalize(all_rows, external=True, maximum=512):
        raise ValueError("Exact complete declared capture package file union required")
    for name, path in origins.items():
        if path not in paths or safe_path(path, external=True) != site / name / "__init__.py":
            raise ValueError("Capture module init origin must have its exact binding")
    if any(not safe_path(row["path"], external=True).is_relative_to(site) for row in rows):
        raise ValueError("Capture dependency source escaped its provider")
    if rows != value["source_files"]:
        raise ValueError("Canonical capture runtime inventory required")
    return value


def _large_stats(config):
    result = {}
    for name in ("base_weights", "adapter_weights"):
        row = config["model"][name]
        path = _binding(row, hash_bytes=False)
        stat = path.stat()
        result[name] = {
            "bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "device": stat.st_dev,
            "inode": stat.st_ino,
        }
    return result


def _runtime_rows(config):
    runtime = config["runtime"]
    rows = [
        *runtime["source_files"],
        runtime["python_binding"],
        runtime["warmup_manifest"]["allocator_warmup_source"],
        *runtime["module_origins"].values(),
    ]
    # The sealed warmup reference uses Windows separators. Canonicalize only
    # this inventory projection, retaining its exact bytes/SHA and original config.
    projected = []
    for row in rows:
        _object(row, ("path", "bytes", "sha256"))
        projected.append({**row, "path": safe_path(row["path"], external=True).as_posix()})
    return _normalize(projected, external=True)


def _build_capture_runtime():
    path = ROOT / "scripts/run_onboard_navigation_gazebo_learned_shadow.py"
    before = record(path)
    previous = sys.path[:]
    try:
        sys.path[:0] = [str(ROOT), str(ROOT / "src")]
        worker = importlib.import_module("scripts.run_onboard_navigation_gazebo_learned_shadow")
        if Path(worker.__file__).resolve() != path.resolve(strict=True):
            raise ValueError("Actual capture metadata builder origin differs")
        result = worker.build_capture_runtime()
        _binding(before)
        return result
    finally:
        sys.path[:] = previous


def _identity(config):
    for name, (size, digest) in MODEL_PINS.items():
        row = config["model"][name]
        _binding(row, hash_bytes=name == "adapter_config")
        if (row["bytes"], row["sha256"]) != (size, digest):
            raise ValueError("Only the exact sealed candidate267 model is allowed")
    if canonical(config["model"]["expected_parameter_sha256"]) != canonical(PARAMETER_PINS):
        raise ValueError("Exact actual base and candidate parameter identities required")
    receipt = config["readout"]["receipt"]
    if (receipt["bytes"], receipt["sha256"]) != HEAD_PIN:
        raise ValueError("Only locked final-all208 readout bundle is allowed")
    for name in ("receipt", "metadata", "weights"):
        _binding(config["readout"][name])
    if config["readout"]["normalization_scope"] != "final-all208-TRAIN-only":
        raise ValueError("Saved TRAIN-only normalization required; no refit")


def build_plan(
    output_dir,
    *,
    prompt_text_original,
    capture_runtime=None,
    extra_source_files=(),
    load_config=None,
):
    if (
        type(prompt_text_original) is not str
        or not prompt_text_original.strip()
        or (len(prompt_text_original.encode("utf-8")) > 16 * 1024)
    ):
        raise ValueError("Bounded nonblank verbatim prompt required")
    initial = [record(name) for name in MODULE_FILES]
    loader = _loader(initial)
    config = loader.build_load_config() if load_config is None else load_config
    loader.validate_load_config(config, verify_sources=False)
    _identity(config)
    sources = _sources(config, extra_source_files)
    plan = {
        "schema_version": 1,
        "contract": CONTRACT,
        "output_dir": output_path(output_dir).as_posix(),
        "prompt_text_original": prompt_text_original,
        "prompt_sha256": hashlib.sha256(prompt_text_original.encode("utf-8")).hexdigest(),
        "capture": {
            "source": safe_path(SCENARIO).as_posix(),
            "sensor_frames": 8,
            "timeout_s": 120,
            "control_port": 24580,
            "telemetry_port": 24581,
        },
        "worker_timeout_s": 600,
        "rule": dict(RULE),
        "claims": dict(FALSE_CLAIMS),
        "load_config": config,
        "source_files": sources,
        "runtime_files": _runtime_rows(config),
        "capture_runtime": _capture_runtime(
            _build_capture_runtime() if capture_runtime is None else capture_runtime
        ),
        "extra_source_files": [safe_path(name).as_posix() for name in extra_source_files],
        "protected_files": _pinned_rows(PROTECTED),
        "inherited_evidence": _pinned_rows(EVIDENCE),
        "large_file_stats": _large_stats(config),
    }
    validate_plan(plan)
    for row in initial:
        _binding(row)
    return plan


def validate_plan(plan):
    _object(
        plan,
        (
            "schema_version",
            "contract",
            "output_dir",
            "prompt_text_original",
            "prompt_sha256",
            "capture",
            "worker_timeout_s",
            "rule",
            "claims",
            "load_config",
            "source_files",
            "runtime_files",
            "capture_runtime",
            "extra_source_files",
            "protected_files",
            "inherited_evidence",
            "large_file_stats",
        ),
    )
    if (
        type(plan["schema_version"]) is not int
        or plan["schema_version"] != 1
        or plan["contract"] != CONTRACT
    ):
        raise ValueError("Exact live-shadow plan contract required")
    if canonical(plan["rule"]) != canonical(RULE) or canonical(plan["claims"]) != canonical(
        FALSE_CLAIMS
    ):
        raise ValueError("Frozen observation-only budgets/claims changed")
    output_path(plan["output_dir"])
    text = plan["prompt_text_original"]
    if type(text) is not str or not text.strip() or len(text.encode("utf-8")) > 16384:
        raise ValueError("Bounded original prompt required")
    if _sha(plan["prompt_sha256"]) != hashlib.sha256(text.encode("utf-8")).hexdigest():
        raise ValueError("Original prompt SHA differs")
    if (
        canonical(plan["capture"])
        != canonical(
            {
                "source": safe_path(SCENARIO).as_posix(),
                "sensor_frames": 8,
                "timeout_s": 120,
                "control_port": 24580,
                "telemetry_port": 24581,
            }
        )
        or type(plan["worker_timeout_s"]) is not int
        or plan["worker_timeout_s"] != 600
    ):
        raise ValueError("Exact bounded urban capture/worker timeouts required")
    if type(plan["extra_source_files"]) is not list:
        raise ValueError("Explicit extra source list required")
    sources = _normalize(plan["source_files"])
    if sources != plan["source_files"] or sources != _sources(
        plan["load_config"], plan["extra_source_files"]
    ):
        raise ValueError("Complete explicit repository inventory differs")
    _identity(plan["load_config"])
    runtime = _normalize(plan["runtime_files"], external=True)
    if runtime != plan["runtime_files"] or runtime != _runtime_rows(plan["load_config"]):
        raise ValueError("Exact model runtime inventory differs")
    _capture_runtime(plan["capture_runtime"])
    for key, pins in (("protected_files", PROTECTED), ("inherited_evidence", EVIDENCE)):
        if plan[key] != _pinned_rows(pins):
            raise ValueError("Protected or inherited sealed identity differs")
    if canonical(plan["large_file_stats"]) != canonical(_large_stats(plan["load_config"])):
        raise ValueError("Declared large model stat identity changed")
    loader = _loader(sources)
    loader.validate_load_config(plan["load_config"], verify_sources=False)
    return plan


def admit_plan(path, sha256):
    row = record(path)
    if row["sha256"] != _sha(sha256):
        raise ValueError("Explicit plan SHA differs")
    plan = _json(safe_path(path).read_bytes())
    validate_plan(plan)
    _binding(row)
    return plan, row


def capture_sources(plan):
    """Small SHA rechecks and large stat only; never traverse head history/datasets."""
    return {
        "repository": [_binding_record(row) for row in plan["source_files"]],
        "runtime": [_binding_record(row, external=True) for row in plan["runtime_files"]],
        "capture_runtime": [
            _binding_record(row, external=True) for row in plan["capture_runtime"]["source_files"]
        ],
        "protected": [_binding_record(row) for row in plan["protected_files"]],
        "inherited": [_binding_record(row) for row in plan["inherited_evidence"]],
        "readout": [
            _binding_record(plan["load_config"]["readout"][key])
            for key in ("receipt", "metadata", "weights")
        ],
        "large_weights_stat_only": _large_stats(plan["load_config"]),
    }


def bootstrap(plan):
    """Authenticate explicit plan sources before importing the lazy exact loader."""
    validate_plan(plan)
    return _loader(plan["source_files"])


def _binding_record(row, *, external=False):
    _binding(row, external=external)
    return record(row["path"], external=external)


def write_plan(path, plan):
    validate_plan(plan)
    target = Path(path)
    target = (target if target.is_absolute() else ROOT / target).resolve(strict=False)
    if target.parent != (ROOT / "configs/training").resolve(strict=True) or not (
        target.name.startswith("onboard-navigation-live-shadow-") and target.suffix == ".json"
    ):
        raise ValueError("Fresh direct live-shadow plan filename required")
    raw = (json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    with target.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    row = record(target)
    validate_plan(plan)
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-plan", action="store_true")
    parser.add_argument("--plan-output")
    parser.add_argument("--output-dir")
    parser.add_argument("--prompt-original")
    parser.add_argument("--capture-runtime-json")
    parser.add_argument("--source-file", action="append", default=[])
    parser.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(argv)
    if args.emit_plan:
        if args.plan or args.plan_sha256 or not all((args.output_dir, args.prompt_original)):
            parser.error("Emission needs output/prompt; no admission arguments")
        capture = None
        if args.capture_runtime_json:
            capture = _json(safe_path(args.capture_runtime_json).read_bytes())
        plan = build_plan(
            args.output_dir,
            prompt_text_original=args.prompt_original,
            capture_runtime=capture,
            extra_source_files=args.source_file,
        )
        result = write_plan(args.plan_output, plan) if args.plan_output else plan
    else:
        if (
            not args.plan
            or not args.plan_sha256
            or any(
                (
                    args.plan_output,
                    args.output_dir,
                    args.prompt_original,
                    args.capture_runtime_json,
                    args.source_file,
                )
            )
        ):
            parser.error("Admission needs only --plan and --plan-sha256")
        plan, row = admit_plan(args.plan, args.plan_sha256)
        result = {
            "plan": row,
            "admitted": True,
            "model_loaded_here": False,
            "generation_calls": 0,
            "optimizer_attempts": 0,
            "claims": plan["claims"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
