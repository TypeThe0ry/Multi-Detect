"""Small CPU/orchestration fixtures, never an actual122 processor/model run."""

import copy
import hashlib
import json
import subprocess
import sys
import types
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.autonomy import onboard_navigation_supervision as nav
from scripts import audit_onboard_navigation_dataset_overlap as controls
from scripts import verify_onboard_navigation_indexed_cpu_inputs as audit

H = "a" * 64
RAW = b"{}\n"
SOURCE_SHA = hashlib.sha256(RAW).hexdigest()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.setattr(controls, "ROOT", tmp_path)
    (tmp_path / "artifacts/diagnostics").mkdir(parents=True)
    (tmp_path / "configs/training").mkdir(parents=True)
    return tmp_path


def source(path):
    return {"path": path, "bytes": len(RAW), "sha256": SOURCE_SHA}


def rows(count=2, route="143"):
    episode = "episode-" + route
    declared = {
        name: source(f"fixture/{name}.json")
        for name in (
            "annotation",
            "conversion",
            "license",
            "unit_verification",
            "alignment_verification",
        )
    }
    declared.update(
        repository_id="IPEC-COMMUNITY/OpenFly",
        revision="a12316d56a4e35a32ad626fb725ed7089937a1c4",
        original_split="train",
        annotation_record_id=route,
        pose=source(f"fixture/{route}/pose.jsonl"),
        review_receipt=None,
    )
    prompt = {
        "text_original": "Go to the building.",
        "utf8_sha256": hashlib.sha256(b"Go to the building.").hexdigest(),
        "source_record_sha256": SOURCE_SHA,
        "source_pointer": {
            "format": "json",
            "json_pointer": f"/{route}/gpt_instruction",
            "record_index": None,
        },
    }
    result, prior = [], None
    for step in range(count):
        terminal = step == count - 1
        frame = {
            "frame_id": f"{episode}-frame-{step}",
            "episode_id": episode,
            "scene_id": "env_airsim_26",
            "step_index": step,
            **source(f"fixture/{route}/{step}.png"),
            "width": 3,
            "height": 2,
            "encoding": "png",
            "source_clock_id": None,
            "source_stamp": None,
            "stamp_unit": None,
        }
        raw = {
            "imageid": step,
            "pos": [float(step * 3), 0.0, 0.0],
            "yaw": 0.0,
            "type": "stop" if terminal else "go straight",
            "value": 0 if terminal else 3,
        }
        result.append(
            {
                "schema_version": 1,
                "contract": nav.SUPERVISION_CONTRACT,
                "sample_id": f"{episode}-sample-{step}",
                "episode_id": episode,
                "scene_id": "env_airsim_26",
                "step_index": step,
                "source": copy.deepcopy(declared),
                "prompt": copy.deepcopy(prompt),
                "frames": [copy.deepcopy(prior), frame] if prior else [frame],
                "partition": "train",
                "label_provenance": "external_planned_navigation_supervision",
                "alignment": {
                    "observation_step": step,
                    "action_step": step,
                    "next_step": None if terminal else step + 1,
                    "dt_s": None,
                    "evidence_sha256": SOURCE_SHA,
                },
                "pose": {
                    "coordinate_frame": "openfly-world-xy-z-up-not-geographic",
                    "length_unit": "m",
                    "origin": "planned_pose",
                    "position_xyz": raw["pos"],
                    "source_record_sha256": SOURCE_SHA,
                    "yaw": 0.0,
                    "yaw_unit": "rad",
                },
                "planned_action": {
                    "kind": "stop" if terminal else "translate",
                    "raw": raw,
                    "raw_record_sha256": SOURCE_SHA,
                    "raw_source_pointer": {
                        "format": "json_sequence",
                        "json_pointer": "/action",
                        "record_index": step,
                    },
                    "translation_m": None if terminal else [3.0, 0.0, 0.0],
                    "yaw_delta_rad": None,
                    "unit_verification_sha256": None if terminal else SOURCE_SHA,
                },
            }
        )
        prior = frame
    return result


def source_actor(row):
    return {
        "sample_id": row["sample_id"],
        "episode_id": row["episode_id"],
        "actor_identity_sha256": H,
        "ordered_frame_steps": [frame["step_index"] for frame in row["frames"]],
        "target": copy.deepcopy(audit.target_from_sample(row)),
    }


def all_rows():
    result = []
    for route, count in audit.EXPECTED_ROUTES.items():
        result.extend(rows(count, route))
    return result


def tensor(shape, dtype="torch.int64"):
    return {"dtype": dtype, "shape": shape, "sha256": H}


def metadata(row):
    image_count = len(row["frames"])
    records = {
        "input_ids": tensor([1, 10]),
        "attention_mask": tensor([1, 10]),
        "image_grid_thw": tensor([image_count, 3]),
        "pixel_values": tensor([256 * image_count, 1176], "torch.float32"),
    }
    prefix = {
        "prefix_tokens": 10,
        "actual_image_grid_thw": [[1, 16, 16] for _ in row["frames"]],
        "actual_image_pixels": [50176] * image_count,
        "patch_size": 14,
        "temporal_patch_size": 2,
        "tensors": records,
    }
    reference = {
        "sample_id": row["sample_id"],
        "actor_identity_sha256": H,
        "source_prompt_sha256": row["prompt"]["utf8_sha256"],
        "source_frames": [
            {
                "sequence": frame["step_index"],
                **{
                    key: frame[key]
                    for key in ("path", "bytes", "sha256", "width", "height", "encoding")
                },
            }
            for frame in row["frames"]
        ],
        "retained_history_valid": True,
        "actor_identities_equal": True,
        "training_prefix": prefix,
        "observable_prefix": copy.deepcopy(prefix),
        "all_four_tensor_bytes_equal": True,
        "labels_pose_future_ids_entered_model_messages": False,
    }
    full = copy.deepcopy(records)
    full["input_ids"] = tensor([1, 15])
    full["attention_mask"] = tensor([1, 15])
    prepared = {
        "prefix_tokens": 10,
        "total_tokens": 15,
        "response_tokens": 5,
        "supervised_tokens": 5,
        "prefix_tensors": records,
        "input_tensors": full,
        "response_labels_sha256": H,
        "target_sha256": hashlib.sha256(
            nav.canonical_json(audit.target_from_sample(row)).encode()
        ).hexdigest(),
        "actor_identity_sha256": H,
        "prefix_reference_verified": True,
        "prompt_label_mask_verified": True,
    }
    return reference, prepared


def basic_api():
    return SimpleNamespace(
        nav=nav, pilot=SimpleNamespace(core=SimpleNamespace(canonical_bytes=audit.canonical))
    )


def encoding_api(row, *, failure=None, suppressed=False):
    events, sessions, tensor_refs = [], [], []
    reference, prepared = metadata(row)

    class TensorSentinel:
        pass

    class Session:
        def __init__(self, root):
            self.verified, self._active = False, False
            sessions.append(self)

        def __enter__(self):
            self._active = True
            events.append("enter")
            return self

        def prime(self, samples):
            assert len(samples) == 1
            events.append("prime")

        def __exit__(self, error_type, error, traceback):
            events.append("exit")
            assert all(ref() is None for ref in tensor_refs), "Tensor buffers retained through exit"
            self._active = False
            if failure == "exit":
                raise ValueError("fixture source exit drift")
            self.verified = error_type is None and failure != "unverified"
            if failure == "active":
                self._active = True
            return suppressed

    def pair(*args, **kwargs):
        events.append("paired_prefix")
        if failure == "pair":
            raise ValueError("fixture paired prefix failure")
        return copy.deepcopy(reference)

    def encode(*args, **kwargs):
        assert kwargs["session"] is sessions[-1]
        assert audit.canonical(kwargs["prefix_reference"]) == audit.canonical(reference)
        events.append("full_encoding")
        if failure == "full":
            raise ValueError("fixture full encoding failure")
        sentinel = TensorSentinel()
        tensor_refs.append(weakref.ref(sentinel))
        value = {
            **copy.deepcopy(prepared),
            "prefix": sentinel,
            "full": sentinel,
            "labels": sentinel,
        }
        if failure == "metadata":
            value["target_sha256"] = "b" * 64
        return value

    api = basic_api()
    api.closed = SimpleNamespace(ClosedNavigationSourceValidationSession=Session)
    api.checker = SimpleNamespace(verify_sample_pair=pair)
    api.encoding = SimpleNamespace(encode_navigation_sample=encode)
    api.actor = SimpleNamespace()
    return api, events, sessions


def test_first_reference_shared_session_releases_tensors_before_verified_exit(sandbox):
    row = rows()[0]
    api, events, sessions = encoding_api(row)
    sample = nav.NavigationSupervisionSample.from_mapping(row)
    reference, prepared = audit.encode_first_reference(
        sample,
        sandbox,
        processor=object(),
        torch=object(),
        budget=SimpleNamespace(**audit.LIMITS),
        source_actor=source_actor(row),
        api=api,
    )
    assert events == ["enter", "prime", "paired_prefix", "full_encoding", "exit"]
    assert len(sessions) == 1 and sessions[0].verified and not sessions[0]._active
    assert not {"prefix", "full", "labels"} & set(prepared)
    assert reference["sample_id"] == sample.sample_id
    assert audit.LIMITS == {
        "max_pixels_per_image": 50176,
        "max_input_tokens": 4096,
        "max_new_tokens": 192,
        "cpu_threads": 1,
        "resident_samples": 1,
    }


@pytest.mark.parametrize(
    "failure,suppressed",
    [
        ("pair", False),
        ("full", False),
        ("metadata", False),
        ("exit", False),
        ("unverified", False),
        ("active", False),
        ("pair", True),
        ("full", True),
    ],
)
def test_failed_or_suppressed_source_session_cannot_return_metadata(sandbox, failure, suppressed):
    row = rows()[0]
    api, events, sessions = encoding_api(row, failure=failure, suppressed=suppressed)
    with pytest.raises(ValueError):
        audit.encode_first_reference(
            nav.NavigationSupervisionSample.from_mapping(row),
            sandbox,
            processor=object(),
            torch=object(),
            budget=SimpleNamespace(**audit.LIMITS),
            source_actor=source_actor(row),
            api=api,
        )
    assert "exit" in events
    assert len(sessions) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "pose_in_reference",
        "actor",
        "source_path",
        "source_bool_bytes",
        "history",
        "prefix_pixel",
        "full_pixel",
        "full_grid",
        "full_shape_bool",
        "labels_hash",
        "target_hash",
        "response_budget",
        "total_budget",
        "zero_supervision",
        "mask_flag",
        "image_grid_bool",
        "pixel_cap",
        "prefix_dtype",
        "extra_tensor",
    ],
)
def test_first_reference_metadata_gate_rejects_truth_budget_or_tensor_drift(mutation):
    row = rows()[1]
    sample = nav.NavigationSupervisionSample.from_mapping(row)
    reference, prepared = metadata(row)
    if mutation == "pose_in_reference":
        reference["pose"] = row["pose"]
    elif mutation == "actor":
        reference["actor_identity_sha256"] = "b" * 64
    elif mutation == "source_path":
        reference["source_frames"][0]["path"] = "fake.png"
    elif mutation == "source_bool_bytes":
        reference["source_frames"][0]["bytes"] = True
    elif mutation == "history":
        reference["source_frames"][0]["sequence"] = sample.step_index + 1
    elif mutation == "prefix_pixel":
        prepared["prefix_tensors"] = copy.deepcopy(prepared["prefix_tensors"])
        prepared["prefix_tensors"]["pixel_values"]["sha256"] = "b" * 64
    elif mutation in {"full_pixel", "full_grid"}:
        prepared["input_tensors"]["pixel_values" if mutation == "full_pixel" else "image_grid_thw"][
            "sha256"
        ] = "b" * 64
    elif mutation == "full_shape_bool":
        prepared["input_tensors"]["input_ids"]["shape"][0] = True
    elif mutation == "labels_hash":
        prepared["response_labels_sha256"] = "x" * 64
    elif mutation == "target_hash":
        prepared["target_sha256"] = "b" * 64
    elif mutation == "response_budget":
        prepared["response_tokens"] = 193
    elif mutation == "total_budget":
        prepared["total_tokens"] = 4097
    elif mutation == "zero_supervision":
        prepared["supervised_tokens"] = 0
    elif mutation == "mask_flag":
        prepared["prompt_label_mask_verified"] = 1
    elif mutation == "image_grid_bool":
        reference["training_prefix"]["actual_image_grid_thw"][0][0] = True
    elif mutation == "pixel_cap":
        reference["training_prefix"]["actual_image_pixels"][0] = 50177
    elif mutation == "prefix_dtype":
        reference["training_prefix"]["tensors"]["pixel_values"]["dtype"] = "torch.bfloat16"
    else:
        prepared["input_tensors"]["gold"] = tensor([1])
    with pytest.raises(ValueError):
        audit.validate_encoding_metadata(
            reference, prepared, sample, source_actor(row), basic_api()
        )


def test_exact122_typed_routes_and_first_table_not_historical_parity():
    data = all_rows()
    source_report = {"sample_actor_target_bindings": [source_actor(row) for row in data]}
    samples = audit.admit_rows(data, source_report, basic_api())
    assert len(samples) == 122
    assert len({sample.episode_id for sample in samples}) == 6
    references, prepared = [], {}
    for row in data:
        reference, value = metadata(row)
        references.append(reference)
        prepared[row["sample_id"]] = value
    assert (
        audit.validate_table(references, prepared, samples, source_report, basic_api()) == prepared
    )
    assert "historical_cpu86_metadata_parity_claimed" in audit.FALSE_CLAIMS
    assert sum(len(sample.frames) for sample in samples) == 238


@pytest.mark.parametrize(
    "mutation",
    [
        "dev",
        "wrong_scene",
        "missing",
        "order",
        "target",
        "bool_target",
        "prior_history",
        "duplicate",
        "past_binding",
    ],
)
def test122_source_admission_rejects_partition_identity_target_and_closure(mutation):
    data = all_rows()
    report = {"sample_actor_target_bindings": [source_actor(row) for row in data]}
    if mutation == "dev":
        data[0]["partition"] = "dev-test"
    elif mutation == "wrong_scene":
        data[0]["scene_id"] = "env_airsim_16"
    elif mutation == "missing":
        data.pop()
    elif mutation == "order":
        report["sample_actor_target_bindings"].reverse()
    elif mutation == "target":
        report["sample_actor_target_bindings"][0]["target"]["translation_m"][0] = 4.0
    elif mutation == "bool_target":
        report["sample_actor_target_bindings"][0]["target"]["schema_version"] = True
    elif mutation == "prior_history":
        report["sample_actor_target_bindings"][0]["ordered_frame_steps"] = [False]
    elif mutation == "duplicate":
        data[1]["sample_id"] = data[0]["sample_id"]
    else:
        data[1]["frames"][0]["sha256"] = H
    with pytest.raises((ValueError, TypeError)):
        audit.admit_rows(data, report, basic_api())


def runtime_stubs(monkeypatch, root):
    modules = {}
    for name in (
        "numpy",
        "PIL",
        "PIL.Image",
        "PIL.ImageFile",
        "PIL.PngImagePlugin",
        "PIL._imaging",
        "tokenizers",
        "tokenizers.tokenizers",
        "torchvision",
        "transformers",
        "torch",
        "torch._C",
    ):
        module = types.ModuleType(name)
        module.__file__ = str(root / (name.replace(".", "/") + ".py"))
        module.__path__ = []
        modules[name] = module
        monkeypatch.setitem(sys.modules, name, module)
    modules["PIL"].Image = modules["PIL.Image"]
    modules["PIL"].ImageFile = modules["PIL.ImageFile"]
    modules["PIL"].PngImagePlugin = modules["PIL.PngImagePlugin"]
    modules["PIL.Image"].core = modules["PIL._imaging"]
    modules["tokenizers"].tokenizers = modules["tokenizers.tokenizers"]
    modules["torch"]._C = modules["torch._C"]
    modules["torch"].cuda = SimpleNamespace(is_initialized=lambda: False)
    classes = [
        type(name, (), {"source_path": str(root / (name + ".py"))})
        for name in ("Qwen3VLProcessor", "ImageProcessor", "Tokenizer")
    ]
    processor = classes[0]()
    processor.image_processor, processor.tokenizer = classes[1](), classes[2]()
    monkeypatch.setattr(audit.inspect, "getsourcefile", lambda cls: cls.source_path)
    files = [
        {"path": str(Path(module.__file__).resolve()), "bytes": 3, "sha256": H}
        for module in modules.values()
    ]
    files.extend({"path": cls.source_path, "bytes": 3, "sha256": H} for cls in classes)
    return processor, modules, {"source_files": files}


@pytest.mark.parametrize(
    "mutation",
    ["none", "native_torch", "native_pillow", "native_tokenizer", "class", "imagefile", "cuda"],
)
def test_actual_runtime_origins_not_merely_distribution_paths(sandbox, monkeypatch, mutation):
    processor, modules, runtime = runtime_stubs(monkeypatch, sandbox)
    if mutation.startswith("native_"):
        module = {
            "native_torch": "torch._C",
            "native_pillow": "PIL._imaging",
            "native_tokenizer": "tokenizers.tokenizers",
        }[mutation]
        modules[module].__file__ += ".wrong"
    elif mutation == "class":
        type(processor).source_path += ".wrong"
    elif mutation == "imagefile":
        modules["PIL.ImageFile"].__file__ += ".wrong"
    elif mutation == "cuda":
        modules["torch"].cuda.is_initialized = lambda: True
    if mutation == "none":
        assert len(audit.validate_runtime_origins(processor, modules["torch"], runtime)) == 15
    else:
        with pytest.raises(ValueError):
            audit.validate_runtime_origins(processor, modules["torch"], runtime)


def run_fixture(sandbox, monkeypatch):
    data = all_rows()
    source_report = {"sample_actor_target_bindings": [source_actor(row) for row in data]}
    dataset_path = sandbox / "dataset.jsonl"
    dataset_path.write_text("\n".join(json.dumps(row) for row in data) + "\n", encoding="utf-8")
    (sandbox / "source-report.json").write_text(json.dumps(source_report), encoding="utf-8")
    (sandbox / "runtime.json").write_text("{}", encoding="utf-8")
    runtime = {"source_files": [], "package_versions": {}, "fixture": True}
    plan = {
        "output_dir": "artifacts/diagnostics/onboard-navigation-indexed-cpu122-fixture",
        "sources": [
            controls.bind_file(str(dataset_path)),
            controls.bind_file("source-report.json"),
        ],
        "images": [],
        "processor_files": [],
        "runtime": runtime,
        "dataset": controls.bind_file(str(dataset_path)),
        "source_report": controls.bind_file("source-report.json"),
        "sample_ids": [row["sample_id"] for row in data],
        "processor_snapshot": "fixture-only",
        "reference_kind": "first-reference-fixture-not-official",
    }
    (sandbox / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(audit, "bootstrap", lambda: controls)
    monkeypatch.setattr(audit, "build_plan", lambda directory: plan)
    monkeypatch.setattr(audit, "RUNTIME", controls.bind_file("runtime.json"))
    monkeypatch.setattr(audit, "runtime_inventory", lambda *a: runtime)
    monkeypatch.setattr(audit, "validate_runtime_origins", lambda *a: ["fixture-only"])
    api = basic_api()
    api.checker = SimpleNamespace(official_processor=lambda *a: object())
    monkeypatch.setattr(audit, "dependencies", lambda controls: api)
    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = SimpleNamespace(is_initialized=lambda: False)
    fake_torch.set_num_threads = lambda value: None
    fake_torch.get_num_threads = lambda: 1
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    return plan, data


@pytest.mark.parametrize("failure", ["none", "sample14", "source_after", "cuda", "threads"])
def test_run122_software_orchestration_success_partial_and_source_drift(
    sandbox, monkeypatch, failure
):
    plan, data = run_fixture(sandbox, monkeypatch)
    calls = []

    def encode(sample, *args, **kwargs):
        calls.append(sample.sample_id)
        if failure == "sample14" and len(calls) == 14:
            raise ValueError("fixture final source exit failed")
        if failure == "cuda":
            sys.modules["torch"].cuda.is_initialized = lambda: True
        return metadata(data[len(calls) - 1])

    rechecks = []

    def recheck(*args, **kwargs):
        rechecks.append(1)
        if failure == "source_after" and len(rechecks) == 2:
            raise ValueError("fixture final source drift")

    monkeypatch.setattr(audit, "encode_first_reference", encode)
    monkeypatch.setattr(audit, "recheck", recheck)
    if failure == "threads":
        sys.modules["torch"].get_num_threads = lambda: 2
    digest = controls.bind_file("plan.json")["sha256"]
    report = audit.run("plan.json", digest)
    assert report["success"] is (failure == "none")
    assert report["native_exit_code"] == (0 if failure == "none" else 1)
    assert report["training_dataset_cpu_admitted"] is (failure == "none")
    assert all(report[name] is False for name in audit.FALSE_CLAIMS)
    expected = {"none": 122, "sample14": 13, "source_after": 122, "cuda": 0, "threads": 0}[failure]
    assert report["sample_count"] == expected
    assert report["source_validation_sessions_verified"] == expected
    output = sandbox / plan["output_dir"]
    assert {path.name for path in output.iterdir()} == {
        "report.json",
        "prefix-reference.json",
        "prepared-inputs.json",
    }
    assert len(json.loads((output / "prepared-inputs.json").read_bytes())) == expected
    assert len(json.loads((output / "prefix-reference.json").read_bytes())["samples"]) == expected
    assert [row["stage"] for row in report["observations"]].count("sample_admitted") == expected
    with pytest.raises(ValueError):
        audit.run("plan.json", digest)


def test_wrong_plan_sha_refused_before_processor_or_output(sandbox, monkeypatch):
    (sandbox / "plan.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(audit, "bootstrap", lambda: controls)
    monkeypatch.setattr(
        audit, "build_plan", lambda *a: pytest.fail("Must not execute unbound plan")
    )
    with pytest.raises(ValueError, match="SHA"):
        audit.run("plan.json", H)
    assert not list((sandbox / "artifacts/diagnostics").iterdir())


@pytest.mark.parametrize(
    "path",
    [
        "artifacts/training/onboard-navigation-indexed-cpu122-x",
        "artifacts/diagnostics/another",
        "artifacts/diagnostics/onboard-navigation-indexed-cpu122-x/nested",
        "../outside",
    ],
)
def test_output_paths_are_create_only_and_scope_limited(sandbox, path):
    with pytest.raises(ValueError):
        audit.fresh_output(path, controls)


def test_streamed_processor_metadata_can_exceed_two_mib(sandbox):
    target = sandbox / "tokenizer.json"
    target.write_bytes(b"x" * (2 * 1024 * 1024 + 9))
    row = (
        controls.bind_file("plan.json")
        if (sandbox / "plan.json").exists()
        else {"path": "tokenizer.json", **controls.hash_file(target, target.stat().st_size)}
    )
    plan = {"sources": [], "processor_files": [row], "runtime": {"source_files": []}, "images": []}
    audit.recheck(plan, controls, include_images=False)
    target.write_bytes(b"y" * target.stat().st_size)
    with pytest.raises(ValueError, match="Processor"):
        audit.recheck(plan, controls, include_images=False)


@pytest.mark.parametrize(
    "name", ["scripts/probe_onboard_vlm.py", "scripts/verify_onboard_vlm_grounding_adapter.py"]
)
def test_bootstrap_catches_indirect_repository_dependency_drift(sandbox, monkeypatch, name):
    target = sandbox / name
    target.parent.mkdir(parents=True)
    target.write_bytes(b"changed")
    monkeypatch.setattr(audit, "SOURCE_PINS", {name: (7, H)})
    with pytest.raises(ValueError, match="Frozen dependency"):
        audit.bootstrap()


def test_import_is_stdlib_only_no_processor_or_model():
    entry = Path(audit.__file__).resolve()
    code = (
        f"import runpy,sys;runpy.run_path({str(entry)!r},run_name='not_main');"
        + "assert not any(n.split('.')[0] in "
        + "('torch','transformers','PIL','numpy','pyarrow','peft','datasets') for n in sys.modules)"
    )
    process = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], text=True, capture_output=True, check=False
    )
    assert process.returncode == 0, process.stderr
