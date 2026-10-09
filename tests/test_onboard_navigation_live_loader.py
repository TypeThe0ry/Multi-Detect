"""Small file/CPU-head and injected lifecycle fixtures; never a real model load."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import onboard_navigation_live_loader as live


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else live.canonical(value))
    return live.file_binding(path)


@pytest.fixture
def config(tmp_path):
    snapshot = tmp_path / "snapshot"
    base = write(snapshot / "model.safetensors", b"fixture not real weights")
    processor = [write(snapshot / name, b"fixture-only") for name in sorted(live.SNAPSHOT_NAMES)]
    adapter = write(tmp_path / "adapter/adapter_model.safetensors", b"fixture adapter")
    adapter_config = write(tmp_path / "adapter/adapter_config.json", {})
    audit_source = write(tmp_path / "ordered-audit.json", {})
    py = write(tmp_path / "python.exe", b"fixture interpreter")
    warmup = write(tmp_path / "modeling_utils.py", b"fixture source")
    readout_dir = tmp_path / "final-all208"
    readout = {
        k: write(readout_dir / name, b"fixture")
        for k, name in (
            ("receipt", "receipt.json"),
            ("metadata", "metadata.json"),
            ("weights", "tensors.safetensors"),
        )
    }
    entries = [
        dict(
            name=f"base_model.model.model.language_model.layers.{i}.self_attn.{projection}.lora_{kind}.default.weight",
            numel=1,
            dtype="torch.float32",
        )
        for i in range(28)
        for projection in ("q_proj", "v_proj")
        for kind in ("A", "B")
    ]
    deps = [live.file_binding(live.ROOT / f"scripts/{name}.py") for name in live.DEPENDENCIES]
    return {
        "schema_version": 1,
        "contract": live.CONFIG_CONTRACT,
        "model": {
            "base_snapshot": str(snapshot),
            "base_weights": base,
            "snapshot_files": processor,
            "adapter_weights": adapter,
            "adapter_config": adapter_config,
            "expected_parameter_sha256": {"base": "1" * 64, "adapter": "2" * 64},
            "audit_source": audit_source,
            "budget": dict(live.BUDGET),
            "trainable_audit": {
                "trainable_parameters": entries,
                "trainable_parameter_count": 112,
                "frozen_parameter_count": 625,
                "all_base_and_vision_parameters_frozen": True,
                "trainable_dtypes": ["torch.float32"],
                "frozen_base_dtypes": ["torch.bfloat16"],
            },
        },
        "readout": {
            **readout,
            "directory": str(readout_dir),
            "normalization_scope": "final-all208-TRAIN-only",
        },
        "runtime": {
            "python_executable": str(py["path"]),
            "python_binding": py,
            "python_version": "3.12.7",
            "package_versions": dict(live.PACKAGES),
            "source_files": [warmup],
            "module_origins": {name: warmup for name in live.RUNTIME_MODULES},
            "warmup_manifest": {
                "allocator_warmup_source": warmup,
                "allocator_warmup_function_sha256": (
                    "c2760d2ecbead7dc6a7f081eed2f48187dc6c67507ce23e9b03ff28924cbe2c6"
                ),
                "allocator_warmup_parameter_names": [
                    "model",
                    "expanded_device_map",
                    "hf_quantizer",
                ],
                "warmup_policy": "skip_optional_caching_allocator_warmup",
                "warmup_scope": "this_child_only_performance_preallocation",
            },
        },
        "loader_dependencies": deps,
        "source_files": [audit_source],
    }


class Operations:
    def __init__(self, config):
        self.config, self.events = config, []
        self.engine_value = SimpleNamespace(model=object())
        self.head_value, self.norm_value = object(), object()
        self.identity_after = None
        self.state_after = None
        self.source_after = None
        self.runtime_after = None
        self.calls = dict(identity=0, sources=0, runtime=0, frozen=0)
        self.fail = None

    def hit(self, name):
        self.events.append(name)
        if self.fail == name:
            raise ValueError("injected " + name)

    @contextmanager
    def scope(self, config, observation):
        self.hit("scope-installed")
        try:
            yield SimpleNamespace()
        finally:
            self.hit("scope-restored")

    def sources(self, config):
        self.hit("sources")
        self.calls["sources"] += 1
        return (
            self.source_after
            if self.calls["sources"] > 1 and self.source_after is not None
            else [dict(path="fixture", bytes=1, sha256="a" * 64)]
        )

    def runtime(self, config):
        self.hit("runtime")
        self.calls["runtime"] += 1
        return (
            self.runtime_after
            if self.calls["runtime"] > 1 and self.runtime_after is not None
            else {"fixture": "bound"}
        )

    def engine(self, config, pilot, observation):
        self.hit("engine")
        observation["model_loaded"] = True
        return self.engine_value

    def readers(self, observation, config):
        self.hit("readers")

    def head(self, config):
        self.hit("head")
        return self.head_value, self.norm_value, {"fixture": True}

    def identity(self, engine, pilot):
        self.hit("identity")
        self.calls["identity"] += 1
        return (
            self.identity_after
            if self.calls["identity"] > 1 and self.identity_after is not None
            else dict(self.config["model"]["expected_parameter_sha256"])
        )

    def frozen(self, engine, head, normalization, metadata):
        self.hit("frozen")
        self.calls["frozen"] += 1
        return (
            self.state_after
            if self.calls["frozen"] > 1 and self.state_after is not None
            else {"tiny_head_fixture": True}
        )


def test_configuration_schema_and_actual_small_sources(config):
    assert live.validate_load_config(config, verify_sources=True) is config
    assert len(live.capture_sources(config)) >= len(live.DEPENDENCIES)


@pytest.mark.parametrize(
    "change",
    [
        "extra",
        "schema-bool",
        "budget",
        "gradient",
        "sha",
        "order-count",
        "scope",
        "dep",
        "snapshot-extra",
        "relocate",
        "runtime-version",
    ],
)
def test_bad_configuration_refused(config, change):
    if change == "extra":
        config["gold"] = {}
    elif change == "schema-bool":
        config["schema_version"] = True
    elif change == "budget":
        config["model"]["budget"]["rank"] = 8
    elif change == "gradient":
        config["model"]["budget"]["gradient_checkpointing"] = True
    elif change == "sha":
        config["model"]["expected_parameter_sha256"]["adapter"] = "invalid"
    elif change == "order-count":
        config["model"]["trainable_audit"]["trainable_parameters"].pop()
    elif change == "scope":
        config["readout"]["normalization_scope"] = "dev-selected"
    elif change == "dep":
        config["loader_dependencies"].pop()
    elif change == "snapshot-extra":
        write(Path(config["model"]["base_snapshot"]) / "model.safetensors.index.json", {})
    elif change == "relocate":
        config["readout"]["directory"] = config["model"]["base_snapshot"]
    else:
        config["runtime"]["package_versions"]["peft"] = "other"
    with pytest.raises(ValueError):
        live.validate_load_config(config)


def test_sources_byte_drift_and_contradictory_binding(config):
    row = config["model"]["adapter_weights"]
    Path(row["path"]).write_bytes(b"drift")
    with pytest.raises(ValueError, match="identity changed"):
        live.capture_sources(config)
    config["source_files"].append({**row, "sha256": "f" * 64})
    with pytest.raises(ValueError, match="Contradictory"):
        live.validate_load_config(config)


def test_preloaded_context_order_and_file_identity(config):
    ops, observation = Operations(config), {}
    with live.load_preloaded(config, operations=ops, observations=observation) as loaded:
        assert loaded.engine is ops.engine_value and loaded.head is ops.head_value
        assert loaded.engine_identity.model_sha256 == config["model"]["base_weights"]["sha256"]
        assert loaded.readout_identity.head_receipt_sha256 == config["readout"]["receipt"]["sha256"]
        assert observation["load_succeeded"] and not observation["closed_successfully"]
        ops.events.append("caller-only-observation")
    assert (
        ops.events.index("scope-installed")
        < ops.events.index("runtime")
        < ops.events.index("engine")
    )
    assert (
        ops.events.index("scope-restored")
        < ops.events.index("head")
        < ops.events.index("caller-only-observation")
    )
    assert observation["source_bindings_unchanged"] and observation["closed_successfully"]
    assert loaded.engine is loaded.head is loaded.normalization is None
    assert (
        observation["optimizer_steps_performed"] == observation["generation_calls_performed"] == 0
    )
    assert observation["training_data_read"] is False


@pytest.mark.parametrize("gate", ["engine", "readers", "head", "identity", "frozen"])
def test_partial_load_never_yields_or_claims_success(config, gate):
    ops, observation = Operations(config), {}
    ops.fail = gate
    with pytest.raises(ValueError, match="injected"):
        with live.load_preloaded(config, operations=ops, observations=observation):
            pytest.fail("Partial objects escaped")
    assert observation["load_succeeded"] is False and observation["closed_successfully"] is False
    if gate == "engine":
        assert "scope-restored" in ops.events
    assert not live._EXCLUSIVE.locked()


@pytest.mark.parametrize("gate", ["identity_after", "state_after", "source_after", "runtime_after"])
def test_late_identity_source_or_mode_drift_fails_closed(config, gate):
    ops, observation = Operations(config), {}
    setattr(ops, gate, {"drift": True} if gate != "source_after" else [{"drift": True}])
    with pytest.raises(ValueError):
        with live.load_preloaded(config, operations=ops, observations=observation):
            pass
    assert observation["closed_successfully"] is False and "close_error" in observation
    assert not live._EXCLUSIVE.locked()


@pytest.mark.parametrize(
    "error", [RuntimeError("body cause"), KeyboardInterrupt("interrupt"), SystemExit(31)]
)
def test_body_error_remains_primary_when_late_gate_fails(config, error):
    ops, observation = Operations(config), {}
    ops.source_after = [{"changed": True}]
    with pytest.raises(type(error)) as result:
        with live.load_preloaded(config, operations=ops, observations=observation):
            raise error
    assert result.value is error and any("final check" in note for note in error.__notes__)
    assert observation["context_body_completed"] is False


def test_nested_load_refused(config):
    with live.load_preloaded(config, operations=Operations(config)):
        with pytest.raises(ValueError, match="Nested/concurrent"):
            with live.load_preloaded(config, operations=Operations(config)):
                pass


def reader_fixture(config):
    headers, openings = {}, []
    for key, kind, count, payload, dtype, device in (
        ("base_weights", "base", 625, 4255064064, "BF16", "cuda:0"),
        ("adapter_weights", "adapter", 112, 3211264, "F32", "cpu"),
    ):
        path = str(live._path(config["model"][key]["path"]))
        entries = {
            "tensor-" + str(i): {"shape": [1], "data_offsets": [i, i + 1]} for i in range(count)
        }
        header = {"file_bytes": 100, "header_declared_bytes": 8, "header_sha256": "a" * 64}
        headers[path] = header, entries
        reads = [
            dict(
                name=name,
                completed=True,
                dtype=dtype,
                shape=[1],
                declared_bytes=1,
                actual_bytes_read=1,
            )
            for name in entries
        ]
        diag = {
            **header,
            "filename": path,
            "closed": True,
            "tensor_calls_attempted": count,
            "tensor_calls_completed": count,
            "tensor_payload_bytes_read": payload,
            "normalized_device": device,
            "tensor_reads": reads,
        }
        openings.append(dict(path=path, kind=kind, header=header, final_diagnostics=diag))
    metadata = copy.deepcopy(openings[0])
    metadata["final_diagnostics"].update(
        tensor_calls_attempted=0,
        tensor_calls_completed=0,
        tensor_payload_bytes_read=0,
        tensor_reads=[],
        normalized_device="cpu",
    )
    observation = dict(
        loader_aliases_restored=True,
        modeling_utils_alias_restored=True,
        outer_hook_fallbacks_restored=True,
        reader_openings=[metadata, *openings],
        warmup=dict(
            pid=os.getpid(),
            warmup_override_installed=True,
            warmup_override_restored=True,
            warmup_calls_attempted=1,
            warmup_calls_skipped=1,
        ),
        constructor=dict(installed=True, restored=True, calls_attempted=1, exceptions_observed=0),
        loader_counts=dict(safe_open_calls_attempted=3, safe_open_calls_returned=3),
    )
    return observation, lambda path, **kwargs: headers[path]


def test_reader_schema_metadata_only_cpu_and_constructor_without_pid(config):
    observation, reader = reader_fixture(config)
    assert "pid" not in observation["constructor"]
    live.check_loader_observation(observation, config, header_reader=reader)


@pytest.mark.parametrize(
    "mutation",
    [
        "root",
        "mu",
        "ctor",
        "pid",
        "closed",
        "count",
        "dtype",
        "span",
        "device",
        "missing",
        "metadata-payload",
    ],
)
def test_reader_and_hook_schema_failures(config, mutation):
    obs, reader = reader_fixture(config)
    if mutation == "root":
        obs["loader_aliases_restored"] = False
    elif mutation == "mu":
        obs["modeling_utils_alias_restored"] = False
    elif mutation == "ctor":
        obs["constructor"]["exceptions_observed"] = 1
    elif mutation == "pid":
        obs["warmup"]["pid"] += 1
    elif mutation == "closed":
        obs["reader_openings"][1]["final_diagnostics"]["closed"] = False
    elif mutation == "count":
        obs["loader_counts"]["safe_open_calls_attempted"] += 1
    elif mutation == "dtype":
        obs["reader_openings"][1]["final_diagnostics"]["tensor_reads"][0]["dtype"] = "F32"
    elif mutation == "span":
        obs["reader_openings"][1]["final_diagnostics"]["tensor_reads"][0]["actual_bytes_read"] = 0
    elif mutation == "device":
        obs["reader_openings"][1]["final_diagnostics"]["normalized_device"] = "cpu"
    elif mutation == "missing":
        obs["reader_openings"].pop()
    else:
        obs["reader_openings"][0]["final_diagnostics"]["tensor_payload_bytes_read"] = 1
    with pytest.raises(ValueError):
        live.check_loader_observation(obs, config, header_reader=reader)


def fake_imports(monkeypatch, config):
    events, openings = [], []

    def original(*args, **kwargs):
        return None

    root = SimpleNamespace(safe_open=original)
    sf_torch = SimpleNamespace(safe_open=original)
    mu = SimpleNamespace(safe_open=None, caching_allocator_warmup=original)

    class Backend:
        def __init__(self):
            pass

    pilot = SimpleNamespace(NavigationBackend=Backend)
    initial_constructor = Backend.__init__

    def installed(*args, **kwargs):
        return None

    def install(allowed, target):
        events.append("stream-installed")
        root.safe_open = sf_torch.safe_open = installed
        monkeypatch.setitem(sys.modules, "safetensors.torch", sf_torch)
        return dict(safe_open_calls_attempted=0, safe_open_calls_returned=0)

    def hook(kind):
        def install_hook(manifest):
            events.append(kind + "-installed")
            state = {"restored": False}

            def restore():
                events.append(kind + "-restored")
                state["restored"] = True

            if kind == "warmup":
                mu.caching_allocator_warmup = installed
            else:
                Backend.__init__ = installed
            return state, restore

        return install_hook

    body = SimpleNamespace(
        install_warmup_skip=hook("warmup"), install_constructor_traceback=hook("ctor")
    )
    streaming = SimpleNamespace(install_streaming_loader=install, header_binding=lambda path: {})

    def imports(name):
        events.append("import:" + name)
        if name == "transformers.modeling_utils":
            assert events.index("stream-installed") < len(events) - 1
            mu.safe_open = root.safe_open
            return mu
        return {
            "scripts.run_onboard_navigation_pilot_streaming": streaming,
            "scripts.run_onboard_navigation_pilot_streaming_no_warmup": body,
            "safetensors": root,
            "scripts.train_onboard_vlm_navigation_pilot": pilot,
        }[name]

    monkeypatch.setattr(live.importlib, "import_module", imports)
    return SimpleNamespace(
        events=events,
        root=root,
        sf_torch=sf_torch,
        mu=mu,
        pilot=pilot,
        original=original,
        constructor=initial_constructor,
        body=body,
        openings=openings,
    )


def test_scope_import_order_cleanup_and_aliases(config, monkeypatch):
    f, observation = fake_imports(monkeypatch, config), {}
    with live.streaming_scope(config, observation) as pilot:
        assert pilot is f.pilot
        f.events.append("backend-consumed")
    assert f.events.index("stream-installed") < f.events.index("import:transformers.modeling_utils")
    assert f.events.index("ctor-installed") < f.events.index("backend-consumed")
    assert f.events.index("ctor-restored") < f.events.index("warmup-restored")
    assert f.root.safe_open is f.sf_torch.safe_open is f.mu.safe_open is f.original
    assert f.mu.caching_allocator_warmup is f.original
    assert f.pilot.NavigationBackend.__init__ is f.constructor
    assert observation["loader_aliases_restored"] and observation["modeling_utils_alias_restored"]


@pytest.mark.parametrize("error", [ValueError("load"), KeyboardInterrupt("stop"), SystemExit(19)])
def test_scope_body_exception_always_restores(config, monkeypatch, error):
    f, observation = fake_imports(monkeypatch, config), {}
    with pytest.raises(type(error)) as result:
        with live.streaming_scope(config, observation):
            raise error
    assert result.value is error
    assert f.root.safe_open is f.sf_torch.safe_open is f.mu.safe_open is f.original
    assert observation["outer_hook_fallbacks_restored"] is True


def test_scope_installer_partial_failure_restores_fallback(config, monkeypatch):
    f, observation = fake_imports(monkeypatch, config), {}
    original = f.body.install_constructor_traceback

    def install(manifest):
        original(manifest)
        raise OSError("installer failed after patching")

    f.body.install_constructor_traceback = install
    with pytest.raises(OSError):
        with live.streaming_scope(config, observation):
            pass
    assert f.pilot.NavigationBackend.__init__ is f.constructor
    assert f.root.safe_open is f.sf_torch.safe_open is f.original


def test_all_known_cached_reader_aliases_restore_original(config, monkeypatch):
    f, observation = fake_imports(monkeypatch, config), {}
    aliases = {}
    with live.streaming_scope(config, observation):
        for name in live.CACHED_READER_MODULES:
            aliases[name] = SimpleNamespace(safe_open=f.root.safe_open)
            monkeypatch.setitem(sys.modules, name, aliases[name])
    assert all(module.safe_open is f.original for module in aliases.values())
    assert observation["cached_reader_aliases_restored"] == dict.fromkeys(
        live.CACHED_READER_MODULES, True
    )


def test_drifted_cached_reader_alias_rejected_but_other_hooks_restore(config, monkeypatch):
    f, observation = fake_imports(monkeypatch, config), {}
    with pytest.raises(ValueError, match="cleanup failed"):
        with live.streaming_scope(config, observation):
            monkeypatch.setitem(sys.modules, "peft.peft_model", SimpleNamespace(safe_open=object()))
    assert observation["cached_reader_aliases_restored"]["peft.peft_model"] is False
    assert f.root.safe_open is f.sf_torch.safe_open is f.mu.safe_open is f.original


@pytest.mark.parametrize("change", ["origin-drop", "source-drop", "python", "function", "policy"])
def test_runtime_projection_cannot_silently_shrink(config, change):
    runtime = config["runtime"]
    if change == "origin-drop":
        runtime["module_origins"].pop("peft.peft_model")
    elif change == "source-drop":
        runtime["source_files"] = []
    elif change == "python":
        runtime["python_version"] = "3.12.8"
    elif change == "function":
        runtime["warmup_manifest"]["allocator_warmup_function_sha256"] = "a" * 64
    else:
        runtime["warmup_manifest"]["warmup_policy"] = "skip_loading"
    with pytest.raises(ValueError):
        live.validate_load_config(config)


@pytest.mark.parametrize("had_hook", [False, True])
def test_inference_freeze_disables_checkpointing_and_optional_input_grad_hook(had_hook):
    events = []

    class Model:
        def __init__(self):
            self.config = SimpleNamespace(use_cache=True)
            self.is_gradient_checkpointing = True
            if had_hook:
                self._require_grads_hook = object()

        def gradient_checkpointing_disable(self):
            events.append("checkpointing-disabled")
            self.is_gradient_checkpointing = False

        def get_base_model(self):
            return self

        def disable_input_require_grads(self):
            events.append("input-hook-removed")

        def requires_grad_(self, value):
            events.append(("requires-grad", value))

        def zero_grad(self, **kwargs):
            events.append(("zero-grad", kwargs))

        def eval(self):
            events.append("eval")

    model, observation = Model(), {}
    live.freeze_engine(SimpleNamespace(model=model), observation)
    assert model.is_gradient_checkpointing is False and not hasattr(model, "_require_grads_hook")
    assert model.config.use_cache is False
    assert ("requires-grad", False) in events
    assert observation["input_gradient_hook_was_present"] is had_hook
    assert ("input-hook-removed" in events) is had_hook


@pytest.mark.parametrize(
    "preloaded", ["peft", "peft.utils.save_and_load", "transformers.modeling_utils"]
)
def test_preimport_refused_before_any_stream_install(config, monkeypatch, preloaded):
    monkeypatch.setitem(sys.modules, preloaded, SimpleNamespace())
    with pytest.raises(ValueError, match="before streaming"):
        with live.streaming_scope(config, {}):
            pass


def test_parameter_identity_adapter_not_requires_grad_filter():
    calls = []
    engine = SimpleNamespace(
        model=object(),
        trainable_audit={"ordered": True},
        peft=SimpleNamespace(get_peft_model_state_dict=lambda model, **kwargs: {"saved-f32": 1}),
    )

    def base(model, *, trainable):
        calls.append(("base", trainable))
        return "1" * 64

    def adapter(state, audit, *, rank):
        calls.append(("adapter", state, audit, rank))
        return "2" * 64

    pilot = SimpleNamespace(
        core=SimpleNamespace(parameter_digest=base),
        recovery=SimpleNamespace(saved_parameter_digest=adapter),
    )
    assert live.parameter_identity(engine, pilot) == dict(base="1" * 64, adapter="2" * 64)
    assert calls[0] == ("base", False)
    assert calls[1] == ("adapter", {"saved-f32": 1}, {"ordered": True}, 4)


def test_import_is_stdlib_only_and_no_training_or_cache_entry():
    root = Path(__file__).resolve().parents[1]
    code = (
        f"import sys;sys.path[:0]=[{str(root)!r},{str(root / 'src')!r}];"
        "import scripts.onboard_navigation_live_loader;"
        "assert not {'torch','peft','transformers','PIL','numpy',"
        "'safetensors','datasets','pyarrow'}.intersection(sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


def test_small_head_bundle_does_not_reopen_historical_gold_sources(config):
    import torch
    from safetensors.torch import save

    from scripts import onboard_navigation_stable_readout as stable

    assert torch.cuda.is_initialized() is False
    tensors = {
        "weight": torch.zeros(6, 2048),
        "bias": torch.zeros(6),
        "center": torch.zeros(2048),
        "scale": torch.ones(2048),
    }
    ids = [f"fixture-train-{i}" for i in range(208)]
    records = {k: stable.tensor_record(v) for k, v in tensors.items()}
    historical = [
        {"path": "/do-not-reopen/train-gold.jsonl", "bytes": 500000000, "sha256": "a" * 64}
    ]
    metadata = {
        "contract": stable.CONTRACT,
        "kind": "readout",
        "feature_dim": 2048,
        "optimizer_state_saved": False,
        "small_or_constant_scale_rule": "population_std_below_epsilon_uses_one",
        "epsilon": 1e-6,
        "normalization_train_ids": ids,
        "tensors": records,
        "source_bindings": historical,
        "claims": stable.FALSE_CLAIMS,
        "training_report": {
            "success": True,
            "training_loss_improved": True,
            "final_loss_recomputed_after_updates": True,
            "optimizer_update_uncertain": False,
            "training_sample_ids": ids,
            "final_tensor_records": records,
            "claims": stable.FALSE_CLAIMS,
        },
    }
    folder = Path(config["readout"]["directory"])
    config["readout"]["weights"] = write(folder / "tensors.safetensors", save(tensors))
    config["readout"]["metadata"] = write(folder / "metadata.json", metadata)
    receipt = {
        "contract": stable.CONTRACT,
        "kind": "readout",
        "exact_tensor_reload": True,
        "source_bindings_unchanged": True,
        "source_bindings_before": historical,
        "source_bindings_after": historical,
        "claims": stable.FALSE_CLAIMS,
        "weights": config["readout"]["weights"],
        "metadata": config["readout"]["metadata"],
    }
    config["readout"]["receipt"] = write(folder / "receipt.json", receipt)
    head, normalization, actual_metadata = live.load_head_bundle(config)
    assert head.weight.dtype == torch.float32 and head.weight.device.type == "cpu"
    assert not head.training and not any(p.requires_grad for p in head.parameters())
    assert tuple(normalization.sample_ids) == tuple(ids) and actual_metadata == metadata
    assert torch.cuda.is_initialized() is False
    tensors["scale"][0] = 0
    config["readout"]["weights"] = write(folder / "tensors.safetensors", save(tensors))
    with pytest.raises(ValueError):
        live.load_head_bundle(config)
