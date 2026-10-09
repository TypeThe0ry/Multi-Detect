"""Pure stdlib tiny identities; no real weights, datasets, processor or capture."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from scripts import onboard_navigation_live_shadow_plan as api


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "ROOT", tmp_path)
    for directory in ("configs/training", "artifacts/evaluation"):
        (tmp_path / directory).mkdir(parents=True)

    def put(name, raw=b"fixture\n"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return api.record(path)

    for name in (*api.MODULE_FILES, api.SCENARIO):
        put(name)
    protected = {name: put(name) for name in api.PROTECTED}
    evidence = {name: put(name) for name in api.EVIDENCE}
    monkeypatch.setattr(
        api, "PROTECTED", {name: (row["bytes"], row["sha256"]) for name, row in protected.items()}
    )
    monkeypatch.setattr(
        api, "EVIDENCE", {name: (row["bytes"], row["sha256"]) for name, row in evidence.items()}
    )
    base, adapter, adapter_config = (
        put("model/base.safetensors", b"base fixture"),
        put("model/adapter.safetensors", b"adapter fixture"),
        put("model/adapter_config.json", b"{}"),
    )
    monkeypatch.setattr(
        api,
        "MODEL_PINS",
        {
            name: (row["bytes"], row["sha256"])
            for name, row in zip(api.MODEL_PINS, (base, adapter, adapter_config), strict=True)
        },
    )
    directory = "artifacts/head/final-all208"
    receipt, metadata, weights = (
        put(directory + "/receipt.json", b'{"fixture_only":true}'),
        put(directory + "/metadata.json", b'{"fixture_only":true}'),
        put(directory + "/tensors.safetensors", b"small head fixture"),
    )
    monkeypatch.setattr(api, "HEAD_PIN", (receipt["bytes"], receipt["sha256"]))
    python = put(".venv-vlm-navigation/Scripts/python.exe", b"fixture Python")
    module = put("runtime/modeling.py", b"fixture runtime")
    audit = put("model/audit.json", b"fixture audit no labels")
    processor = put("model/preprocessor_config.json", b"{}")
    config = {
        "model": {
            "base_weights": base,
            "adapter_weights": adapter,
            "adapter_config": adapter_config,
            "expected_parameter_sha256": dict(api.PARAMETER_PINS),
            "snapshot_files": [processor],
            "audit_source": audit,
        },
        "readout": {
            "directory": (tmp_path / directory).as_posix(),
            "receipt": receipt,
            "metadata": metadata,
            "weights": weights,
            "normalization_scope": "final-all208-TRAIN-only",
        },
        "runtime": {
            "source_files": [module],
            "python_binding": python,
            "module_origins": {"fixture_runtime": module},
            "warmup_manifest": {"allocator_warmup_source": module},
        },
        "loader_dependencies": [api.record(tmp_path / "scripts/onboard_navigation_live_loader.py")],
        "source_files": [audit],
    }
    loader = ModuleType(api.LOADER)
    loader.__file__ = str(tmp_path / "scripts/onboard_navigation_live_loader.py")
    loader.build_load_config = lambda: copy.deepcopy(config)
    loader.validate_load_config = lambda value, *, verify_sources=False: value
    monkeypatch.setitem(sys.modules, api.LOADER, loader)
    site = tmp_path / "capture-site"
    packages, origins = {}, {}
    for name in ("pymavlink", "fastcrc", "lxml"):
        init = put(f"capture-site/{name}/__init__.py", name.encode())
        dist = put(f"capture-site/{name}-fixture.dist-info/METADATA", b"version fixture")
        packages[name] = {
            "version": api.CAPTURE_PACKAGE_VERSIONS[name],
            "root": (site / name).as_posix(),
            "files": [init, dist],
        }
        origins[name] = init["path"]
    capture = {
        "contract": "multidetect.navigation-live-capture-dependencies-v1",
        "python_executable": python["path"],
        "provider_site": site.as_posix(),
        "packages": packages,
        "module_origins": origins,
        "source_files": api._normalize(
            [row for p in packages.values() for row in p["files"]], external=True
        ),
        "no_sys_path_insertion": True,
        "allowed_fallback_top_level_names": ["fastcrc", "lxml", "pymavlink"],
        "shadow_existing_modules": False,
    }
    monkeypatch.setattr(api, "_build_capture_runtime", lambda: copy.deepcopy(capture))
    return {"root": tmp_path, "config": config, "capture": capture, "loader": loader, "put": put}


def make_plan(case):
    return api.build_plan(
        "artifacts/evaluation/onboard-navigation-live-shadow-fixture",
        prompt_text_original="  看前方的目标\n",
        capture_runtime=case["capture"],
    )


def test_tiny_build_admit_and_closure_are_deterministic(case):
    plan = make_plan(case)
    assert api.canonical(plan) == api.canonical(make_plan(case))
    assert plan["prompt_text_original"] == "  看前方的目标\n"
    assert (
        plan["prompt_sha256"] == hashlib.sha256(plan["prompt_text_original"].encode()).hexdigest()
    )
    assert plan["capture"]["sensor_frames"] == 8
    assert plan["worker_timeout_s"] == 600
    assert all(value is False for value in plan["claims"].values())
    path = "configs/training/onboard-navigation-live-shadow-fixture.json"
    row = api.write_plan(path, plan)
    admitted, bound = api.admit_plan(path, row["sha256"])
    assert admitted == plan and bound == row
    assert api.bootstrap(plan) is case["loader"]
    assert api.capture_sources(plan) == api.capture_sources(plan)
    assert not Path(plan["output_dir"]).exists()


def test_large_weight_bytes_are_never_opened_here(case, monkeypatch):
    large = {case["config"]["model"][key]["path"] for key in ("base_weights", "adapter_weights")}
    original = Path.read_bytes

    def guarded(path):
        assert path.as_posix() not in large, "Large model bytes read by plan layer"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    plan = make_plan(case)
    api.validate_plan(plan)
    api.capture_sources(plan)
    assert plan["rule"]["large_weight_admission"].startswith("declared_SHA_and_stat_only")


@pytest.mark.parametrize(
    "key,value",
    [
        ("maximum_new_camera_frames", 9),
        ("maximum_new_camera_frames", True),
        ("max_frame_age_s", 6.0),
        ("request_deadline_s", 6.0),
        ("capture_timeout_s", 121),
        ("worker_timeout_s", 601),
        ("maximum_optimizer_attempts", 1),
        ("maximum_generation_calls", 1),
        ("inherited_head_optimizer_attempts", 800),
        ("maximum_models", 2),
    ],
)
def test_no_budget_or_epoch_relaxation(case, key, value):
    plan = make_plan(case)
    plan["rule"][key] = value
    with pytest.raises(ValueError, match="budgets/claims"):
        api.validate_plan(plan)


@pytest.mark.parametrize("field", ["schema_version", "prompt_sha256", "claims", "capture"])
def test_plan_tampering_rejected(case, field):
    plan = make_plan(case)
    if field == "schema_version":
        plan[field] = True
    elif field == "prompt_sha256":
        plan[field] = "0" * 64
    elif field == "claims":
        plan[field]["execution_authority"] = True
    else:
        plan[field]["sensor_frames"] = True
    with pytest.raises(ValueError):
        api.validate_plan(plan)


@pytest.mark.parametrize(
    "field", ["source_files", "runtime_files", "protected_files", "inherited_evidence"]
)
def test_missing_inventory_not_self_admitted(case, field):
    plan = make_plan(case)
    plan[field] = plan[field][1:]
    with pytest.raises(ValueError):
        api.validate_plan(plan)


@pytest.mark.parametrize("which", ["source", "audit", "runtime", "protected", "head", "large"])
def test_late_current_file_drift_fails(case, which):
    plan = make_plan(case)
    paths = {
        "source": plan["source_files"][0]["path"],
        "audit": case["config"]["model"]["audit_source"]["path"],
        "runtime": plan["runtime_files"][0]["path"],
        "protected": plan["protected_files"][0]["path"],
        "head": plan["load_config"]["readout"]["metadata"]["path"],
        "large": plan["load_config"]["model"]["base_weights"]["path"],
    }
    Path(paths[which]).write_bytes(b"drift")
    with pytest.raises(ValueError):
        api.validate_plan(plan)
    with pytest.raises(ValueError):
        api.capture_sources(plan)


def test_same_size_weight_stat_change_not_claimed_as_fullhash(case):
    plan = make_plan(case)
    path = Path(plan["load_config"]["model"]["base_weights"]["path"])
    old = path.stat()
    path.write_bytes(b"X" * old.st_size)
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000))
    with pytest.raises(ValueError, match="stat identity"):
        api.validate_plan(plan)


def test_cached_wrong_loader_origin_rejected(case):
    case["loader"].__file__ = str(case["root"] / "wrong.py")
    with pytest.raises(ValueError, match="loader origin"):
        make_plan(case)


def test_loader_drift_before_import_no_sentinel(case, monkeypatch):
    plan = make_plan(case)
    path = case["root"] / "scripts/onboard_navigation_live_loader.py"
    path.write_bytes(b"import sentinel_bad_loader")
    monkeypatch.setattr(
        api.importlib, "import_module", lambda name: pytest.fail("Import before SHA gate")
    )
    with pytest.raises(ValueError):
        api.validate_plan(plan)


@pytest.mark.parametrize("name", ["torch", "numpy", "PIL", "transformers", "safetensors"])
def test_capture_fallback_cannot_shadow_model_runtime(case, name):
    capture = copy.deepcopy(case["capture"])
    capture["module_origins"][name] = next(iter(capture["module_origins"].values()))
    with pytest.raises(ValueError):
        api._capture_runtime(capture)


@pytest.mark.parametrize(
    "mutation", ["sys_path", "shadow", "extra_file", "missing_file", "wrong_origin"]
)
def test_capture_declared_file_and_isolation_gates(case, mutation):
    capture = copy.deepcopy(case["capture"])
    if mutation == "sys_path":
        capture["no_sys_path_insertion"] = False
    elif mutation == "shadow":
        capture["shadow_existing_modules"] = True
    elif mutation == "extra_file":
        capture["source_files"].append(case["config"]["runtime"]["python_binding"])
    elif mutation == "missing_file":
        capture["source_files"].pop()
    else:
        capture["module_origins"]["pymavlink"] = capture["module_origins"]["lxml"]
    with pytest.raises(ValueError):
        api._capture_runtime(capture)


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "artifacts/evaluation/not-navigation",
        "artifacts/evaluation/onboard-navigation-live-shadow-fixture/nested",
    ],
)
def test_output_path_fail_closed(case, path):
    with pytest.raises(ValueError):
        api.output_path(path)


def test_output_and_plan_create_only(case):
    plan = make_plan(case)
    name = "configs/training/onboard-navigation-live-shadow-create-only.json"
    row = api.write_plan(name, plan)
    original = Path(row["path"]).read_bytes()
    with pytest.raises(FileExistsError):
        api.write_plan(name, plan)
    assert Path(row["path"]).read_bytes() == original
    Path(plan["output_dir"]).mkdir()
    with pytest.raises(FileExistsError):
        api.validate_plan(plan)


def test_uint64_identity_survives_python_plan_output(case, monkeypatch):
    huge = 2**63 + 12345
    original = api._large_stats

    def stats(config):
        value = original(config)
        value["base_weights"]["device"] = huge
        return value

    monkeypatch.setattr(api, "_large_stats", stats)
    plan = make_plan(case)
    row = api.write_plan("configs/training/onboard-navigation-live-shadow-uint64.json", plan)
    restored, _ = api.admit_plan(row["path"], row["sha256"])
    assert restored["large_file_stats"]["base_weights"]["device"] == huge


def test_json_and_sha_admission_do_not_trust_unbound_input(case):
    plan = make_plan(case)
    row = api.write_plan("configs/training/onboard-navigation-live-shadow-sha.json", plan)
    with pytest.raises(ValueError, match="plan SHA"):
        api.admit_plan(row["path"], "0" * 64)
    for raw in (b'{"key":1,"key":2}', b'{"key":NaN}', b"\xff"):
        with pytest.raises((ValueError, UnicodeError)):
            api._json(raw)


@pytest.mark.parametrize("value", ["", " \n", None, "x" * 16385])
def test_invalid_original_prompt(case, value):
    with pytest.raises(ValueError):
        api.build_plan(
            "artifacts/evaluation/onboard-navigation-live-shadow-invalid",
            prompt_text_original=value,
            capture_runtime=case["capture"],
        )


def test_extra_source_conflict_and_path_only_inputs_fail(case):
    config = copy.deepcopy(case["config"])
    wrong = copy.deepcopy(config["loader_dependencies"][0])
    wrong["sha256"] = "0" * 64
    config["loader_dependencies"].append(wrong)
    with pytest.raises(ValueError):
        api.build_plan(
            "artifacts/evaluation/onboard-navigation-live-shadow-conflict",
            prompt_text_original="prompt",
            capture_runtime=case["capture"],
            load_config=config,
        )
    with pytest.raises(ValueError):
        api.build_plan(
            "artifacts/evaluation/onboard-navigation-live-shadow-extra",
            prompt_text_original="prompt",
            capture_runtime=case["capture"],
            extra_source_files=["model/audit.json"],
        )


def test_cli_emit_binding_then_admit_without_model(case, capsys):
    path = "configs/training/onboard-navigation-live-shadow-cli.json"
    assert (
        api.main(
            [
                "--emit-plan",
                "--plan-output",
                path,
                "--output-dir",
                "artifacts/evaluation/onboard-navigation-live-shadow-cli",
                "--prompt-original",
                "  original prompt\n",
            ]
        )
        == 0
    )
    row = json.loads(capsys.readouterr().out)
    assert set(row) == {"path", "bytes", "sha256"}
    assert api.main(["--plan", row["path"], "--plan-sha256", row["sha256"]]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["admitted"] is True and result["model_loaded_here"] is False
    assert result["generation_calls"] == result["optimizer_attempts"] == 0


@pytest.mark.parametrize(
    "args", [[], ["--emit-plan"], ["--plan", "x", "--plan-sha256", "0" * 64, "--plan-output", "x"]]
)
def test_cli_mode_conflicts(case, args):
    with pytest.raises(SystemExit):
        api.main(args)


def test_guarded_stdlib_import_does_not_import_heavy_or_training_modules():
    source = Path(api.__file__).resolve()
    script = (
        "import importlib.util,sys; "
        f"spec=importlib.util.spec_from_file_location('shadow_plan', {str(source)!r}); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "assert not any(n.split('.')[0] in ('torch','transformers','peft','PIL','numpy') "
        "for n in sys.modules); print('stdlib-only')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "stdlib-only"


def test_access_time_updates_are_not_source_drift(case, monkeypatch):
    original = Path.read_bytes

    def touch_access(path):
        raw = original(path)
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns + 1_000_000, stat.st_mtime_ns))
        return raw

    monkeypatch.setattr(Path, "read_bytes", touch_access)
    plan = make_plan(case)
    assert api.validate_plan(plan) == plan


def test_empty_capture_typing_marker_has_real_empty_sha(case):
    capture = copy.deepcopy(case["capture"])
    row = case["put"]("capture-site/fastcrc/py.typed", b"")
    assert row["bytes"] == 0 and row["sha256"] == hashlib.sha256(b"").hexdigest()
    capture["packages"]["fastcrc"]["files"].append(row)
    capture["source_files"] = api._normalize([*capture["source_files"], row], external=True)
    assert api._capture_runtime(capture) == capture


def test_source_drift_after_plan_fsync_leaves_unadmitted_artifact(case, monkeypatch):
    plan = make_plan(case)
    original = api.os.fsync

    def drift(fd):
        original(fd)
        Path(plan["source_files"][0]["path"]).write_bytes(b"late source drift")

    monkeypatch.setattr(api.os, "fsync", drift)
    with pytest.raises(ValueError):
        api.write_plan("configs/training/onboard-navigation-live-shadow-partial.json", plan)
    assert (case["root"] / "configs/training/onboard-navigation-live-shadow-partial.json").exists()


@pytest.mark.parametrize("mutation", ["base_parameter", "head_receipt"])
def test_exact_candidate_and_locked_bundle_are_not_caller_declarations_only(case, mutation):
    config = copy.deepcopy(case["config"])
    if mutation == "base_parameter":
        config["model"]["expected_parameter_sha256"]["base"] = "0" * 64
    else:
        config["readout"]["receipt"]["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        api.build_plan(
            "artifacts/evaluation/onboard-navigation-live-shadow-mutation",
            prompt_text_original="prompt",
            capture_runtime=case["capture"],
            load_config=config,
        )


def test_inherited_windows_runtime_path_is_canonical_inventory_not_config_rewrite(case):
    config = copy.deepcopy(case["config"])
    row = config["runtime"]["warmup_manifest"]["allocator_warmup_source"]
    original_path = str(Path(row["path"]))
    row["path"] = original_path
    plan = api.build_plan(
        "artifacts/evaluation/onboard-navigation-live-shadow-windows-runtime",
        prompt_text_original="prompt",
        capture_runtime=case["capture"],
        load_config=config,
    )
    assert plan["load_config"]["runtime"]["warmup_manifest"]["allocator_warmup_source"] == row
    assert row["path"] == original_path
    assert {r["path"] for r in plan["runtime_files"]} == {
        Path(r["path"]).resolve().as_posix() for r in plan["runtime_files"]
    }
    api.validate_plan(plan)


@pytest.mark.parametrize("field,value", [("sha256", "0" * 64), ("bytes", True)])
def test_runtime_path_projection_never_relaxes_identity(case, field, value):
    config = copy.deepcopy(case["config"])
    row = config["runtime"]["warmup_manifest"]["allocator_warmup_source"]
    row["path"] = str(Path(row["path"]))
    row[field] = value
    with pytest.raises(ValueError):
        api._runtime_rows(config)


@pytest.mark.parametrize("package", ["pymavlink", "fastcrc", "lxml"])
def test_capture_package_version_is_pinned_not_merely_nonempty(case, package):
    capture = copy.deepcopy(case["capture"])
    capture["packages"][package]["version"] = "unexpected-version"
    with pytest.raises(ValueError, match="pinned capture package version"):
        api._capture_runtime(capture)
