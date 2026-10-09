"""Exact, exclusive-process live inference loading; no dataset or optimizer I/O.

Import is stdlib-only. The caller SHA-admits a data-only configuration and owns
the process watchdog. Streaming aliases precede all Transformers/PEFT imports.
Historical head provenance is retained, not recursively reopened: only its
receipt, metadata and four small tensors are consumed. This is not deployment,
navigation quality, clock, source-camera, or flight authority.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CONFIG_CONTRACT = "multidetect.onboard-navigation-live-load-config-v1"
CONTRACT = "multidetect.onboard-navigation-live-loader-v1"
_EXCLUSIVE = threading.Lock()
BUDGET = dict(
    seed=20261008,
    rank=4,
    alpha=8,
    max_pixels_per_image=50176,
    max_input_tokens=4096,
    max_new_tokens=192,
    gradient_checkpointing=False,
    max_cuda_reserved_bytes=8053063680,
)
PACKAGES = {
    "torch": "2.6.0+cu124",
    "transformers": "4.57.6",
    "peft": "0.17.1",
    "accelerate": "1.12.0",
    "safetensors": "0.8.0",
}
PINS = {
    "configs/training/onboard-vlm-navigation-pilot-plan-20261007-r1.json": (
        "f4d76902c220812201f2ca4e961b0f427e4378ba6ee5449bf96e86be23999c53"
    ),
    "configs/training/onboard-navigation-importpath-runtime-20261007-r2.json": (
        "3d4413dcb69e3671252fbe06ee84e34ad12d242329e3e2bd379952ae59a60c43"
    ),
    "artifacts/models/onboard-vlm-navigation-pilot-importpath-20261007-r2/report.json": (
        "996879ac7fc86f818487f2db831c0bdd09bbb61eaf8f181f3624e9148835f430"
    ),
    "docs/evidence/onboard-navigation-recovery267-terminal-20261008-r2.json": (
        "4a40567318bffbd547f68f025e5ce5f60012056df5ed0af10615f6eccf553203"
    ),
    "artifacts/diagnostics/onboard-navigation-stable-cache-20261008-r1/final-all208/receipt.json": (
        "0d31701b9bf50de1b2570df280f5e7ad3672cc1db52aa2525dea07bd4767ff2b"
    ),
}
CACHED_READER_MODULES = (
    "transformers.modeling_utils",
    "peft.peft_model",
    "peft.utils.loftq_utils",
    "accelerate.utils.modeling",
    "accelerate.utils.offload",
)
RUNTIME_MODULES = (
    "torch",
    "torch._C",
    "transformers",
    "transformers.modeling_utils",
    "transformers.models.qwen3_vl.modeling_qwen3_vl",
    "transformers.models.qwen3_vl.processing_qwen3_vl",
    "peft",
    "peft.peft_model",
    "peft.utils.save_and_load",
    "peft.utils.loftq_utils",
    "accelerate",
    "accelerate.big_modeling",
    "accelerate.utils.modeling",
    "accelerate.utils.offload",
    "safetensors",
    "safetensors.torch",
    "safetensors._safetensors_rust",
    "numpy",
    "PIL",
    "PIL.Image",
    "tokenizers",
)
DEPENDENCIES = (
    "run_onboard_navigation_pilot_streaming",
    "run_onboard_navigation_pilot_streaming_no_warmup",
    "run_onboard_navigation_pilot_supervised",
    "onboard_navigation_streaming_storage",
    "train_onboard_vlm_navigation_pilot",
    "train_onboard_vlm_grounding",
    "verify_onboard_vlm_grounding_adapter",
    "probe_onboard_vlm",
    "onboard_navigation_stable_readout",
    "onboard_navigation_action_readout",
    "onboard_navigation_inference_backend",
    "onboard_navigation_learned_readout_backend",
)
WARMUP_KEYS = (
    "allocator_warmup_source",
    "allocator_warmup_function_sha256",
    "allocator_warmup_parameter_names",
    "warmup_policy",
    "warmup_scope",
)
SNAPSHOT_NAMES = {
    "chat_template.json",
    "config.json",
    "generation_config.json",
    "merges.txt",
    "preprocessor_config.json",
    "README.md",
    "tokenizer.json",
    "tokenizer_config.json",
    "video_preprocessor_config.json",
    "vocab.json",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def reject(value):
        raise ValueError("Non-finite JSON constant: " + value)

    value = json.loads(raw, object_pairs_hook=unique, parse_constant=reject)
    canonical(value)  # Also rejects overflowed ordinary JSON numbers.
    return value


def _sha(value):
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("Canonical SHA256 required")
    return value


def _path(value):
    path = Path(value)
    return (path if path.is_absolute() else ROOT / path).resolve(strict=True)


def file_binding(path, *, hash_bytes=True):
    path = _path(path)
    size = path.stat().st_size
    if not path.is_file():
        raise ValueError("Regular bound file required")
    digest = hashlib.sha256()
    if hash_bytes:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return {
        "path": path.as_posix(),
        "bytes": size,
        "sha256": digest.hexdigest() if hash_bytes else None,
    }


def _binding(row):
    if type(row) is not dict or set(row) != {"path", "bytes", "sha256"}:
        raise ValueError("Exact file binding required")
    if type(row["path"]) is not str or type(row["bytes"]) is not int or row["bytes"] < 1:
        raise ValueError("Typed file binding required")
    _sha(row["sha256"])
    return {**row, "path": _path(row["path"]).as_posix()}


def read_bound(row, *, cap=2 * 1024 * 1024):
    row = _binding(row)
    if row["bytes"] > cap or file_binding(row["path"]) != row:
        raise ValueError("Small bound metadata changed or exceeds cap")
    return strict_json(Path(row["path"]).read_bytes())


def _sealed(name):
    row = file_binding(name)
    if row["bytes"] > 2 * 1024 * 1024 or row["sha256"] != PINS[name]:
        raise ValueError("Sealed small identity metadata changed")
    return row, read_bound(row)


def _module_binding(name):
    top, _, relative = name.partition(".")
    spec = importlib.util.find_spec(top)  # Only top-level lookup; no ML import.
    if spec is None or spec.origin is None:
        raise ValueError("Runtime module origin unavailable: " + name)
    root = Path(spec.origin).parent
    if not relative:
        path = Path(spec.origin)
    elif relative in {"_C", "_safetensors_rust"}:
        matches = [p for p in root.glob(relative + "*") if p.suffix in {".pyd", ".so"}]
        if len(matches) != 1:
            raise ValueError("Exact native runtime entrypoint required")
        path = matches[0]
    else:
        path = root.joinpath(*relative.split(".")).with_suffix(".py")
    return file_binding(path)


def build_load_config():
    """Metadata-only projection; no model payload, sample, gold or cache access."""
    rows, docs = {}, {}
    for name in PINS:
        rows[name], docs[name] = _sealed(name)
    old, runtime, audit_doc, terminal, receipt = docs.values()
    if (
        terminal.get("complete267_verified") is not True
        or terminal.get("fresh_saved_adapter_reload_verified") is not True
        or terminal.get("saved_parameter_digest_verified") is not True
    ):
        raise ValueError("Closed candidate267 identity required")

    def terminal_file(filename):
        matches = [r for r in terminal["bindings"] if Path(r["path"]).name == filename]
        if len(matches) != 1:
            raise ValueError("Exact candidate file declaration required")
        return _binding(matches[0])

    directory = _path(next(reversed(rows))).parent
    origins = {name: _module_binding(name) for name in RUNTIME_MODULES}
    deps = [file_binding(ROOT / f"scripts/{name}.py") for name in DEPENDENCIES]
    metadata = _binding(receipt["metadata"])
    weights = _binding(receipt["weights"])
    config = {
        "schema_version": 1,
        "contract": CONFIG_CONTRACT,
        "model": {
            "base_snapshot": _path(old["base"]["snapshot"]).as_posix(),
            "base_weights": _binding(old["base"]["safetensors"]),
            "snapshot_files": [_binding(r) for r in old["base"]["processor_files"]],
            "adapter_weights": terminal_file("adapter_model.safetensors"),
            "adapter_config": terminal_file("adapter_config.json"),
            "expected_parameter_sha256": {
                "base": terminal["identity_checks"]["base_parameter_digest_reported_unchanged"],
                "adapter": terminal["identity_checks"][
                    "final_ordered_saved_adapter_parameter_sha256"
                ],
            },
            "trainable_audit": audit_doc["trainable_audit"],
            "audit_source": list(rows.values())[2],
            "budget": dict(BUDGET),
        },
        "readout": {
            "directory": directory.as_posix(),
            "receipt": list(rows.values())[-1],
            "metadata": metadata,
            "weights": weights,
            "normalization_scope": "final-all208-TRAIN-only",
        },
        "runtime": {
            "python_executable": runtime["python_executable"],
            "python_binding": file_binding(runtime["python_executable"]),
            "python_version": "3.12.7",
            "package_versions": dict(PACKAGES),
            "source_files": list(origins.values()),
            "module_origins": origins,
            "warmup_manifest": {k: runtime[k] for k in WARMUP_KEYS},
        },
        "loader_dependencies": deps,
        "source_files": [
            *rows.values(),
            *deps,
            file_binding(__file__),
            file_binding(ROOT / "tests/test_onboard_navigation_live_loader.py"),
        ],
    }
    validate_load_config(config)
    return config


def _all_files(config):
    model, readout, runtime = (config[k] for k in ("model", "readout", "runtime"))
    rows = [model[k] for k in ("base_weights", "adapter_weights", "adapter_config", "audit_source")]
    rows += model["snapshot_files"] + [readout[k] for k in ("receipt", "metadata", "weights")]
    rows += config["source_files"] + config["loader_dependencies"] + runtime["source_files"]
    rows += [
        runtime["python_binding"],
        *runtime["module_origins"].values(),
        runtime["warmup_manifest"]["allocator_warmup_source"],
    ]
    unique = {}
    for row in rows:
        bound = _binding(row)
        if bound["path"] in unique and unique[bound["path"]] != bound:
            raise ValueError("Contradictory source bindings")
        unique[bound["path"]] = bound
    return [unique[k] for k in sorted(unique)]


def validate_load_config(config, *, verify_sources=False):
    if (
        type(config) is not dict
        or set(config)
        != {
            "schema_version",
            "contract",
            "model",
            "readout",
            "runtime",
            "loader_dependencies",
            "source_files",
        }
        or type(config["schema_version"]) is not int
        or config["schema_version"] != 1
        or config["contract"] != CONFIG_CONTRACT
    ):
        raise ValueError("Exact narrow live configuration required")
    model, readout, runtime = (config[k] for k in ("model", "readout", "runtime"))
    if set(model) != {
        "base_snapshot",
        "base_weights",
        "snapshot_files",
        "adapter_weights",
        "adapter_config",
        "expected_parameter_sha256",
        "trainable_audit",
        "audit_source",
        "budget",
    }:
        raise ValueError("Exact model identity fields required")
    if canonical(model["budget"]) != canonical(BUDGET):
        raise ValueError("Only frozen inference settings; no optimizer budget")
    snapshot = _path(model["base_snapshot"])
    if (
        _path(model["base_weights"]["path"]) != snapshot / "model.safetensors"
        or {Path(r["path"]).name for r in model["snapshot_files"]} != SNAPSHOT_NAMES
        or len(model["snapshot_files"]) != len(SNAPSHOT_NAMES)
        or any(_path(r["path"]).parent != snapshot for r in model["snapshot_files"])
        or {p.name for p in snapshot.iterdir()} != SNAPSHOT_NAMES | {"model.safetensors"}
    ):
        raise ValueError("Exact original snapshot and processor files required")
    if set(model["expected_parameter_sha256"]) != {"base", "adapter"}:
        raise ValueError("Separate base and adapter parameter identities required")
    for value in model["expected_parameter_sha256"].values():
        _sha(value)
    audit = model["trainable_audit"]
    entries = audit["trainable_parameters"]
    if (
        len(entries) != 112
        or len({r["name"] for r in entries}) != 112
        or any(
            r["dtype"] != "torch.float32" or type(r["numel"]) is not int or r["numel"] < 1
            for r in entries
        )
        or sum(r["numel"] for r in entries) != audit["trainable_parameter_count"]
        or audit["trainable_dtypes"] != ["torch.float32"]
        or audit["frozen_base_dtypes"] != ["torch.bfloat16"]
        or audit["all_base_and_vision_parameters_frozen"] is not True
    ):
        raise ValueError("Original ordered 112 FP32 q/v audit required")
    if (
        set(readout) != {"directory", "receipt", "metadata", "weights", "normalization_scope"}
        or readout["normalization_scope"] != "final-all208-TRAIN-only"
    ):
        raise ValueError("Exactly saved final-all208 TRAIN normalization required")
    directory = _path(readout["directory"])
    for key, filename in (
        ("receipt", "receipt.json"),
        ("metadata", "metadata.json"),
        ("weights", "tensors.safetensors"),
    ):
        if _path(readout[key]["path"]) != directory / filename:
            raise ValueError("Head bundle cannot escape or relocate")
    if (
        set(runtime)
        != {
            "python_executable",
            "python_binding",
            "python_version",
            "package_versions",
            "source_files",
            "module_origins",
            "warmup_manifest",
        }
        or runtime["package_versions"] != PACKAGES
    ):
        raise ValueError("Exact explicit runtime required")
    if _path(runtime["python_executable"]) != _path(runtime["python_binding"]["path"]):
        raise ValueError("Interpreter binding differs")
    if (
        set(runtime["warmup_manifest"]) != set(WARMUP_KEYS)
        or set(runtime["module_origins"]) != set(RUNTIME_MODULES)
        or runtime["python_version"] != "3.12.7"
    ):
        raise ValueError("Exact allocator and runtime origins required")
    expected_runtime = {
        _binding(r)["path"]: _binding(r) for r in runtime["module_origins"].values()
    }
    declared_runtime = {_binding(r)["path"]: _binding(r) for r in runtime["source_files"]}
    if expected_runtime != declared_runtime:
        raise ValueError("Runtime entrypoint sources must exactly cover actual origin declarations")
    manifest = runtime["warmup_manifest"]
    if (
        manifest["allocator_warmup_parameter_names"]
        != ["model", "expanded_device_map", "hf_quantizer"]
        or manifest["warmup_policy"] != "skip_optional_caching_allocator_warmup"
        or manifest["warmup_scope"] != "this_child_only_performance_preallocation"
        or manifest["allocator_warmup_function_sha256"]
        != "c2760d2ecbead7dc6a7f081eed2f48187dc6c67507ce23e9b03ff28924cbe2c6"
        or _binding(manifest["allocator_warmup_source"])
        != _binding(runtime["module_origins"]["transformers.modeling_utils"])
    ):
        raise ValueError("Pinned allocator function/policy/source projection differs")
    files = _all_files(config)
    declared = {r["path"] for r in files}
    for name in DEPENDENCIES:
        if _path(ROOT / f"scripts/{name}.py").as_posix() not in declared:
            raise ValueError("Imported loader dependency is not source-bound")
    if verify_sources:
        capture_sources(config)
    return config


def capture_sources(config):
    rows = _all_files(config)
    for row in rows:
        if file_binding(row["path"]) != row:
            raise ValueError("Live loader source/model byte identity changed")
    return rows


def _runtime(config):
    runtime = config["runtime"]
    if (
        _path(sys.executable) != _path(runtime["python_executable"])
        or platform.python_version() != runtime["python_version"]
    ):
        raise ValueError("Actual Python interpreter/version differs")
    for package, version in runtime["package_versions"].items():
        if importlib.metadata.version(package) != version:
            raise ValueError("Runtime distribution version changed")
    result = {}
    for name, bound in runtime["module_origins"].items():
        module = importlib.import_module(name)
        if _path(module.__file__) != _path(bound["path"]):
            raise ValueError("Actual imported runtime origin differs: " + name)
        if (
            name in CACHED_READER_MODULES
            and module.safe_open is not sys.modules["safetensors"].safe_open
        ):
            raise ValueError("Actual cached reader alias differs: " + name)
        result[name] = _path(module.__file__).as_posix()
    return result


def _code_origins(config):
    declared = {r["path"] for r in _all_files(config)}
    for name in DEPENDENCIES:
        module = sys.modules.get("scripts." + name)
        if module is not None and (
            _path(module.__file__).as_posix() not in declared
            or _path(module.__file__) != _path(ROOT / f"scripts/{name}.py")
        ):
            raise ValueError("Actual imported repository loader origin differs")


@contextmanager
def streaming_scope(config, observation):
    """Fresh, exclusive process only; no old phases/plan dispatch or scratch I/O."""
    if any(
        name == "peft" or name.startswith("peft.") or name in CACHED_READER_MODULES
        for name in sys.modules
    ):
        raise ValueError("PEFT/modeling_utils must not be imported before streaming hooks")
    streaming = importlib.import_module("scripts.run_onboard_navigation_pilot_streaming")
    body = importlib.import_module("scripts.run_onboard_navigation_pilot_streaming_no_warmup")
    safetensors = importlib.import_module("safetensors")
    root_alias = safetensors.safe_open
    prior_torch = sys.modules.get("safetensors.torch")
    torch_alias = getattr(prior_torch, "safe_open", root_alias)
    allowed = {
        str(_path(config["model"][key]["path"])): {
            "kind": kind,
            "header": streaming.header_binding(_path(config["model"][key]["path"])),
        }
        for key, kind in (("base_weights", "base"), ("adapter_weights", "adapter"))
    }
    openings, restores = [], []
    warmup_module = pilot = original_warmup = original_constructor = None
    error = None
    installed_alias = None
    try:
        observation["loader_counts"] = streaming.install_streaming_loader(allowed, openings)
        installed_alias = safetensors.safe_open
        warmup_module = importlib.import_module("transformers.modeling_utils")
        if warmup_module.safe_open is not safetensors.safe_open:
            raise ValueError("Transformers cached a nonstreaming safe_open alias")
        pilot = importlib.import_module("scripts.train_onboard_vlm_navigation_pilot")
        original_warmup = warmup_module.caching_allocator_warmup
        original_constructor = pilot.NavigationBackend.__init__
        manifest = {
            **config["runtime"]["warmup_manifest"],
            "source_files": [{**r, "path": str(_path(r["path"]))} for r in _all_files(config)],
        }
        state, restore = body.install_warmup_skip(manifest)
        observation["warmup"] = state
        restores.append(restore)
        state, restore = body.install_constructor_traceback(manifest)
        observation["constructor"] = state
        restores.append(restore)
        yield pilot
    except BaseException as exc:
        error = exc
        raise
    finally:
        cleanup = []
        for restore in reversed(restores):
            try:
                restore()
            except BaseException as exc:
                cleanup.append(type(exc).__name__ + ": " + str(exc))
        if original_warmup is not None:
            warmup_module.caching_allocator_warmup = original_warmup
        if original_constructor is not None:
            pilot.NavigationBackend.__init__ = original_constructor
        observation["outer_hook_fallbacks_restored"] = (
            original_warmup is not None
            and original_constructor is not None
            and warmup_module.caching_allocator_warmup is original_warmup
            and pilot.NavigationBackend.__init__ is original_constructor
        )
        observation["reader_openings"] = []
        for handle, row in openings:
            try:
                handle.close()
                observation["reader_openings"].append(
                    {**row, "final_diagnostics": handle.diagnostics()}
                )
            except BaseException as exc:
                cleanup.append(type(exc).__name__ + ": " + str(exc))
        safetensors.safe_open = root_alias
        if "safetensors.torch" in sys.modules:
            sys.modules["safetensors.torch"].safe_open = torch_alias
        observation["loader_aliases_restored"] = safetensors.safe_open is root_alias and (
            "safetensors.torch" not in sys.modules
            or sys.modules["safetensors.torch"].safe_open is torch_alias
        )
        if warmup_module is not None:
            warmup_module.safe_open = root_alias
        observation["modeling_utils_alias_restored"] = (
            warmup_module is not None and warmup_module.safe_open is root_alias
        )
        aliases = {}
        for name in CACHED_READER_MODULES:
            module = sys.modules.get(name)
            if module is not None:
                alias = getattr(module, "safe_open", None)
                if alias is installed_alias or alias is root_alias:
                    module.safe_open = root_alias
                else:
                    cleanup.append("Unexpected cached reader alias: " + name)
                aliases[name] = module.safe_open is root_alias
        observation["cached_reader_aliases_restored"] = aliases
        if cleanup:
            observation["cleanup_errors"] = cleanup
            if error is not None:
                error.add_note("Live loader cleanup also failed: " + "; ".join(cleanup))
            else:
                raise ValueError("Live loader cleanup failed: " + "; ".join(cleanup))


def check_loader_observation(observation, config, *, header_reader=None):
    if header_reader is None:
        header_reader = importlib.import_module(
            "scripts.run_onboard_navigation_pilot_streaming"
        ).header_binding
    if (
        observation.get("loader_aliases_restored") is not True
        or observation.get("modeling_utils_alias_restored") is not True
        or observation.get("outer_hook_fallbacks_restored") is not True
        or observation.get("cleanup_errors")
        or any(
            value is not True
            for value in observation.get("cached_reader_aliases_restored", {}).values()
        )
    ):
        raise ValueError("Every loader hook and reader must restore normally")
    warmup, ctor = observation["warmup"], observation["constructor"]
    if (
        warmup["pid"] != os.getpid()
        or warmup["warmup_override_installed"] is not True
        or warmup["warmup_override_restored"] is not True
        or warmup["warmup_calls_attempted"] != 1
        or warmup["warmup_calls_skipped"] != 1
        or ctor["installed"] is not True
        or ctor["restored"] is not True
        or ctor["calls_attempted"] != 1
        or ctor["exceptions_observed"] != 0
    ):
        raise ValueError("Original constructor and allocator hook observation differs")
    counts = observation["loader_counts"]
    if counts["safe_open_calls_attempted"] != counts["safe_open_calls_returned"] or counts[
        "safe_open_calls_returned"
    ] != len(observation["reader_openings"]):
        raise ValueError("Every attempted checkpoint opening must return")
    allowed = {
        str(_path(config["model"][key]["path"])): (kind, count, size, dtype, device)
        for key, kind, count, size, dtype, device in (
            ("base_weights", "base", 625, 4255064064, "BF16", "cuda:0"),
            ("adapter_weights", "adapter", 112, 3211264, "F32", "cpu"),
        )
    }
    complete = []
    for row in observation["reader_openings"]:
        if row["path"] not in allowed:
            raise ValueError("Unbound checkpoint reader")
        kind, count, size, dtype, device = allowed[row["path"]]
        header, entries = header_reader(row["path"], with_entries=True)
        diag, reads = row["final_diagnostics"], row["final_diagnostics"]["tensor_reads"]
        if (
            row["kind"] != kind
            or row["header"] != header
            or diag["filename"] != row["path"]
            or diag["closed"] is not True
            or any(diag[k] != value for k, value in header.items())
            or diag["tensor_calls_attempted"] != diag["tensor_calls_completed"]
        ):
            raise ValueError("Reader/header/closure mismatch")
        if reads:
            if (
                len(reads) != count
                or {r["name"] for r in reads} != set(entries)
                or diag["tensor_calls_completed"] != count
                or diag["tensor_payload_bytes_read"] != size
                or diag["normalized_device"] != device
            ):
                raise ValueError("Complete native BF16/F32 payload coverage differs")
            for read in reads:
                entry = entries[read["name"]]
                if (
                    read["completed"] is not True
                    or read["dtype"] != dtype
                    or read["shape"] != entry["shape"]
                    or read["declared_bytes"] != entry["data_offsets"][1] - entry["data_offsets"][0]
                    or read["actual_bytes_read"] != read["declared_bytes"]
                ):
                    raise ValueError("Actual tensor shape/dtype/span differs")
            complete.append(kind)
        elif diag["tensor_calls_completed"] != 0 or diag["tensor_payload_bytes_read"] != 0:
            raise ValueError("Metadata-only checkpoint reader cannot claim payload reads")
    if sorted(complete) != ["adapter", "base"]:
        raise ValueError("Exactly one complete base and adapter read required")


def parameter_identity(engine, pilot):
    state = engine.peft.get_peft_model_state_dict(engine.model, adapter_name="default")
    return {
        "base": pilot.core.parameter_digest(engine.model, trainable=False),
        "adapter": pilot.recovery.saved_parameter_digest(state, engine.trainable_audit, rank=4),
    }


def load_engine(config, pilot, observation):
    model = config["model"]
    budget = SimpleNamespace(**model["budget"])
    pilot.recovery.verify_config(read_bound(model["adapter_config"]), budget)
    sf = importlib.import_module("safetensors.torch")
    state = sf.load_file(str(_path(model["adapter_weights"]["path"])), device="cpu")
    expected, audit = model["expected_parameter_sha256"], model["trainable_audit"]
    if pilot.recovery.saved_parameter_digest(state, audit, rank=4) != expected["adapter"]:
        raise ValueError("Saved adapter parameter identity differs")
    engine = pilot.NavigationBackend(_path(model["base_snapshot"]), budget)
    observation["model_loaded"] = True
    if engine.base_digest_initial != expected["base"] or engine.trainable_audit != audit:
        raise ValueError("Loaded native BF16 base or ordered FP32 audit differs")
    observation["exact_adapter_load"] = pilot.recovery.load_saved_exactly(
        engine.model, state, audit, expected["adapter"], rank=4
    )
    del state
    freeze_engine(engine, observation)
    return engine


def freeze_engine(engine, observation):
    model = engine.model
    model.gradient_checkpointing_disable()
    base = model.get_base_model()
    had_input_hook = hasattr(base, "_require_grads_hook")
    if had_input_hook:
        base.disable_input_require_grads()
        delattr(base, "_require_grads_hook")
    model.requires_grad_(False)
    model.zero_grad(set_to_none=True)
    model.eval()
    model.config.use_cache = False
    if model.is_gradient_checkpointing or hasattr(base, "_require_grads_hook"):
        raise ValueError("Inference loader must disable checkpointing/input-gradient hooks")
    observation["gradient_checkpointing_disabled"] = True
    observation["input_gradient_hook_was_present"] = had_input_hook
    observation["input_gradient_hook_disabled"] = True


def load_head_bundle(config):
    """Do not call stable.load_readout: its historical sources contain TRAIN data."""
    stable = importlib.import_module("scripts.onboard_navigation_stable_readout")
    learned = importlib.import_module("scripts.onboard_navigation_learned_readout_backend")
    readout = config["readout"]
    directory = _path(readout["directory"])
    if {p.name for p in directory.iterdir()} != {
        "receipt.json",
        "metadata.json",
        "tensors.safetensors",
    } or any(not p.is_file() or p.is_symlink() for p in directory.iterdir()):
        raise ValueError("Exact three-file saved readout bundle required")
    receipt, metadata = read_bound(readout["receipt"]), read_bound(readout["metadata"])
    if (
        receipt["contract"] != stable.CONTRACT
        or receipt["kind"] != "readout"
        or receipt["exact_tensor_reload"] is not True
        or receipt["source_bindings_unchanged"] is not True
        or receipt["source_bindings_before"] != receipt["source_bindings_after"]
        or canonical(receipt["claims"]) != canonical(stable.FALSE_CLAIMS)
        or _binding(receipt["metadata"]) != _binding(readout["metadata"])
        or _binding(receipt["weights"]) != _binding(readout["weights"])
        or metadata["source_bindings"] != receipt["source_bindings_before"]
    ):
        raise ValueError("Saved readout provenance differs")
    learned._expected_head_state(metadata)
    report = metadata["training_report"]
    if (
        len(metadata["normalization_train_ids"]) != 208
        or report["training_loss_improved"] is not True
        or report["final_loss_recomputed_after_updates"] is not True
        or report["optimizer_update_uncertain"] is not False
        or report["final_tensor_records"] != metadata["tensors"]
        or canonical(report["claims"]) != canonical(stable.FALSE_CLAIMS)
        or canonical(metadata["claims"]) != canonical(stable.FALSE_CLAIMS)
    ):
        raise ValueError("Final-all208 train-only normalization metadata differs")
    bound = _binding(readout["weights"])
    if bound["bytes"] > stable.MAX_TENSOR_BYTES or file_binding(bound["path"]) != bound:
        raise ValueError("Small saved head bytes differ")
    tensors = importlib.import_module("safetensors.torch").load(Path(bound["path"]).read_bytes())
    if (
        set(tensors) != {"weight", "bias", "center", "scale"}
        or {k: stable.tensor_record(v) for k, v in tensors.items()} != metadata["tensors"]
    ):
        raise ValueError("Actual saved head/normalization tensors differ")
    normalization = stable.TrainNormalization(
        tensors["center"],
        tensors["scale"],
        tuple(metadata["normalization_train_ids"]),
        metadata["epsilon"],
    )
    normalization.validate()
    head = stable.make_head(2048)
    head.load_state_dict({"weight": tensors["weight"], "bias": tensors["bias"]}, strict=True)
    head.eval()
    head.requires_grad_(False)
    return head, normalization, metadata


def check_frozen(engine, head, normalization, metadata):
    learned = importlib.import_module("scripts.onboard_navigation_learned_readout_backend")
    inference = importlib.import_module("scripts.onboard_navigation_inference_backend")
    inference.PreloadedNavigationInferenceBackend._check_adapter_state(engine.model)
    if engine.model.training or any(
        p.requires_grad or p.grad is not None for p in engine.model.parameters()
    ):
        raise ValueError("Every base/adapter parameter must be eval/frozen/no-gradient")
    if engine.model.config.use_cache is not False:
        raise ValueError("Live feature-only model must disable KV cache")
    snapshot = learned._default_head_snapshot(head, normalization, torch_module=engine.torch)
    if canonical(snapshot) != learned._expected_head_state(metadata):
        raise ValueError("Loaded CPU head and TRAIN normalization state differs")
    return snapshot


@dataclass
class LoadedLiveNavigation:
    engine: object
    head: object
    normalization: object
    readout_metadata: dict
    engine_identity: object
    readout_identity: object
    observations: dict


@contextmanager
def load_preloaded(config, *, observations=None, operations=None):
    """Yield exact preloaded objects; caller owns watchdog, capture and inference.

    operations is a software-test injection only, never evidence of actual load.
    The context does not create files, output scratch, datasets, or optimizers.
    End checks run on failure too; the primary body exception remains primary.
    """
    validate_load_config(config)
    if not _EXCLUSIVE.acquire(blocking=False):
        raise ValueError("Nested/concurrent live load denied; standalone process required")
    observation = {} if observations is None else observations
    observation.update(
        contract=CONTRACT,
        pid=os.getpid(),
        model_loaded=False,
        load_succeeded=False,
        context_body_completed=False,
        closed_successfully=False,
        source_bindings_before=None,
        source_bindings_after=None,
        source_bindings_unchanged=False,
        optimizer_steps_performed=0,
        generation_calls_performed=0,
        training_data_read=False,
        historical_head_sources_reopened=False,
        quality_claim=False,
        execution_authority=False,
        final_release_allowed=False,
    )
    ops = SimpleNamespace(
        scope=streaming_scope,
        sources=capture_sources,
        runtime=_runtime,
        engine=load_engine,
        head=load_head_bundle,
        identity=parameter_identity,
        readers=check_loader_observation,
        frozen=check_frozen,
    )
    if operations is not None:
        ops = operations
    engine = head = normalization = loaded = pilot = None
    error = None
    try:
        observation["source_bindings_before"] = ops.sources(config)
        with ops.scope(config, observation) as pilot:
            observation["runtime_origins_before"] = ops.runtime(config)
            _code_origins(config)
            engine = ops.engine(config, pilot, observation)
        ops.readers(observation, config)
        head, normalization, metadata = ops.head(config)
        _code_origins(config)
        expected = config["model"]["expected_parameter_sha256"]
        observation["parameter_identity_before"] = ops.identity(engine, pilot)
        if observation["parameter_identity_before"] != expected:
            raise ValueError("Exact frozen live parameter identities differ")
        observation["head_state_before"] = ops.frozen(engine, head, normalization, metadata)
        from scripts.onboard_navigation_inference_backend import NavigationEngineIdentity
        from scripts.onboard_navigation_learned_readout_backend import SavedReadoutIdentity

        model, readout = config["model"], config["readout"]
        loaded = LoadedLiveNavigation(
            engine,
            head,
            normalization,
            metadata,
            NavigationEngineIdentity(
                *(model[k]["sha256"] for k in ("base_weights", "adapter_weights", "adapter_config"))
            ),
            SavedReadoutIdentity(
                *(readout[k]["sha256"] for k in ("receipt", "weights", "metadata"))
            ),
            observation,
        )
        observation["load_succeeded"] = True
        yield loaded
        observation["context_body_completed"] = True
    except BaseException as exc:
        error = exc
        observation["error"] = {"type": type(exc).__name__, "message": str(exc)[:2048]}
        raise
    finally:
        try:
            if engine is not None and head is not None:
                observation["parameter_identity_after"] = ops.identity(engine, pilot)
                observation["head_state_after"] = ops.frozen(engine, head, normalization, metadata)
                if (
                    observation["parameter_identity_after"]
                    != config["model"]["expected_parameter_sha256"]
                    or observation["head_state_after"] != observation["head_state_before"]
                ):
                    raise ValueError("Live parameter/head state changed during context")
                observation["runtime_origins_after"] = ops.runtime(config)
                if observation["runtime_origins_after"] != observation["runtime_origins_before"]:
                    raise ValueError("Actual runtime module origin changed")
                _code_origins(config)
            observation["source_bindings_after"] = ops.sources(config)
            observation["source_bindings_unchanged"] = (
                observation["source_bindings_after"] == observation["source_bindings_before"]
            )
            if observation["source_bindings_unchanged"] is not True:
                raise ValueError("Late live loader source/model drift")
            observation["closed_successfully"] = (
                observation["context_body_completed"] and error is None
            )
        except BaseException as close_error:
            observation["close_error"] = {
                "type": type(close_error).__name__,
                "message": str(close_error)[:2048],
            }
            if error is not None:
                error.add_note("Live loader final check also failed: " + str(close_error))
            else:
                raise
        finally:
            if loaded is not None:
                loaded.engine = loaded.head = loaded.normalization = None
            engine = head = normalization = None
            _EXCLUSIVE.release()
