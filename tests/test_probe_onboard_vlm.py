from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts import probe_onboard_vlm as probe
from scripts.onboard_vlm_shadow_input import prepare_shadow_case

REVISION = "89644892e4d85e24eaac8bacfd4f463576704203"
MODEL = "Qwen/Qwen3-VL-2B-Instruct"


def _fixture(tmp_path):
    image = tmp_path / "rgb.png"
    Image.new("RGB", (16, 12), (21, 85, 117)).save(image)
    case = {
        "case_id": "fixture-one",
        "prompt": "  检查场景后选择下一步。  ",
        "frames": [
            {
                "frame_id": "fixture-rgb-0",
                "path": "rgb.png",
                "sha256": probe.sha256_file(image),
                "captured_at_s": 0.0,
                "clock_domain": "offline-fixture-not-camera",
                "source_clip_id": "fixture",
                "width": 16,
                "height": 12,
                "encoding": "png",
            }
        ],
        "context_missing_fields": sorted(probe.CONTEXT_MISSING_FIELDS),
        "evaluation_metadata": {"truth": "MASK_TARGET_POSE_SECRET"},
    }
    document = {
        "path_resolution": "repository_root",
        "repository_root": str(tmp_path),
        "cases": [case],
    }
    manifest = tmp_path / "cases.json"
    manifest.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return case, manifest


def _args(tmp_path, manifest):
    return SimpleNamespace(
        model_id=MODEL,
        revision=REVISION,
        cache_directory=tmp_path / "cache",
        output_directory=tmp_path / "output",
        cases_manifest=manifest,
        cases_manifest_sha256=probe.sha256_file(manifest),
        repository_root=tmp_path,
        max_frames=8,
        max_pixels_per_frame=262144,
        max_input_tokens=8192,
        max_new_tokens=512,
        deadline_s=30.0,
    )


@pytest.mark.parametrize("revision", ["main", "a" * 39, "A" * 40, "a" * 41, "g" * 40])
def test_exact_revision_required(revision):
    with pytest.raises(ValueError, match="40-hex"):
        probe.require_model_identity(MODEL, revision)


def test_gated_and_unknown_models_not_admitted():
    with pytest.raises(ValueError, match="public Apache"):
        probe.require_model_identity("nvidia/Cosmos-Reason2-2B", REVISION)


@pytest.mark.parametrize(
    "name,value",
    [
        ("max_frames", 9),
        ("max_frames", True),
        ("max_pixels_per_frame", 1000000),
        ("max_input_tokens", 32768),
        ("max_new_tokens", 513),
        ("deadline_s", float("nan")),
        ("deadline_s", 0),
        ("deadline_s", True),
    ],
)
def test_budget_fail_closed(name, value):
    with pytest.raises(ValueError):
        replace(probe.ProbeBudget(), **{name: value}).validate()


def test_default_budget_bounded():
    probe.ProbeBudget().validate()
    assert probe.ProbeBudget().max_new_tokens == 512


@pytest.mark.parametrize("payload", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'])
def test_strict_manifest_json(payload):
    with pytest.raises(ValueError):
        probe.strict_json(payload)


def test_cases_manifest_hash_root_and_vocabulary(tmp_path):
    _case, manifest = _fixture(tmp_path)
    _document, cases = probe.load_cases(manifest, probe.sha256_file(manifest), tmp_path)
    assert len(cases) == 1
    with pytest.raises(ValueError, match="SHA-256"):
        probe.load_cases(manifest, "0" * 64, tmp_path)
    with pytest.raises(ValueError, match="repository_root"):
        probe.load_cases(manifest, probe.sha256_file(manifest), tmp_path / "other")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["cases"][0]["context_missing_fields"].append("GPS is 1,2")
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="vocabulary"):
        probe.load_cases(manifest, probe.sha256_file(manifest), tmp_path)


@pytest.mark.parametrize("path", ["target_mask.png", "labels/rgb.png", "truth/rgb.png"])
def test_label_truth_mask_paths_rejected(tmp_path, path):
    _case, manifest = _fixture(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["cases"][0]["frames"][0]["path"] = path
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="not RGB"):
        probe.load_cases(manifest, probe.sha256_file(manifest), tmp_path)


def test_real_bytes_and_original_prompt_without_evaluation_metadata(tmp_path):
    case, _manifest = _fixture(tmp_path)
    model_input = prepare_shadow_case(case, repository_root=tmp_path)
    messages, preprocessing = probe.image_messages(
        model_input, case["context_missing_fields"], probe.ProbeBudget()
    )
    text = "\n".join(
        item["text"]
        for message in messages
        for item in message["content"]
        if item["type"] == "text"
    )
    assert case["prompt"] in text
    assert "MASK_TARGET_POSE_SECRET" not in text
    assert case["case_id"] not in text
    assert case["frames"][0]["source_clip_id"] not in text
    assert "formal_input_metadata" not in text
    assert model_input.canonical_hash in text
    images = [item["image"] for item in messages[1]["content"] if item["type"] == "image"]
    assert len(images) == 1 and images[0].mode == "RGB"
    assert images[0].getpixel((0, 0)) == (21, 85, 117)
    assert preprocessing[0]["original_sha256"] == case["frames"][0]["sha256"]


def test_pixel_budget_records_actual_preprocessing(tmp_path):
    case, _manifest = _fixture(tmp_path)
    image = tmp_path / "rgb.png"
    Image.new("RGB", (640, 480), (1, 2, 3)).save(image)
    case["frames"][0].update(width=640, height=480, sha256=probe.sha256_file(image))
    model_input = prepare_shadow_case(case, repository_root=tmp_path)
    _messages, records = probe.image_messages(
        model_input, case["context_missing_fields"], probe.ProbeBudget()
    )
    assert records[0]["processed_width"] * records[0]["processed_height"] <= 262144
    assert records[0]["processed_width"] < 640


def test_semantic_frame_ids_not_sent_to_model(tmp_path):
    case, _manifest = _fixture(tmp_path)
    model_input = prepare_shadow_case(case, repository_root=tmp_path)
    frames = tuple(replace(frame, frame_id="near-hidden-answer") for frame in model_input.frames)
    altered = replace(model_input, frames=frames, ledger=replace(model_input.ledger, frames=frames))
    with pytest.raises(ValueError, match="opaque frame IDs"):
        probe.image_messages(altered, case["context_missing_fields"], probe.ProbeBudget())


def test_bound_source_mutation_rejected(tmp_path):
    path = tmp_path / "runtime.py"
    path.write_bytes(b"initial")
    record = probe.file_record(path)
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA-256"):
        probe.verify_records([record])


def test_official_lfs_content_not_just_size(tmp_path):
    path = tmp_path / "model.safetensors"
    path.write_bytes(b"actual")
    sibling = SimpleNamespace(size=6, lfs={"sha256": "0" * 64}, blob_id=None, rfilename=path.name)
    with pytest.raises(ValueError, match="LFS SHA"):
        probe.verify_official_file(path, sibling)
    sibling.lfs["sha256"] = probe.sha256_file(path)
    assert probe.verify_official_file(path, sibling)["official_lfs_sha256"] == sibling.lfs["sha256"]


def test_official_git_blob_digest(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(b"{}")
    blob = hashlib.sha1(b"blob 2\0{}", usedforsecurity=False).hexdigest()
    sibling = SimpleNamespace(size=2, lfs=None, blob_id=blob, rfilename=path.name)
    assert probe.verify_official_file(path, sibling)["official_git_blob_sha1"] == blob
    sibling.blob_id = "0" * 40
    with pytest.raises(ValueError, match="Git blob"):
        probe.verify_official_file(path, sibling)


def test_token_clock_ignores_prompt(monkeypatch):
    monkeypatch.setattr(probe.time, "perf_counter", lambda: 9.0)
    clock = probe.TokenClock()
    clock.put("prompt")
    assert clock.first_generated_at_s is None
    clock.put("generated")
    assert clock.first_generated_at_s == 9.0


class _FakeBackend:
    instances = 0
    output_kind = "invalid"

    def __init__(self, _snapshot, _model_id):
        type(self).instances += 1
        self.load_s = 0.0
        self.torch = SimpleNamespace(
            __version__="fixture-not-torch",
            version=SimpleNamespace(cuda="fixture"),
            cuda=SimpleNamespace(get_device_name=lambda _index: "fixture-not-GPU"),
        )
        self.transformers = SimpleNamespace(__version__="fixture-not-transformers")
        self.processor = SimpleNamespace(
            apply_chat_template=lambda *_args, **_kwargs: "unit-template"
        )

    def runtime_records(self):
        return []

    def generate(self, messages, _budget, *, started_at_s):
        if self.output_kind == "cuda_failure":
            raise RuntimeError("fixture simulated CUDA failure")
        metadata = json.loads(messages[1]["content"][-1]["text"])
        raw = "unrepaired fixture answer"
        if self.output_kind == "valid":
            raw = json.dumps(
                {
                    "schema_version": 1,
                    "contract": "multidetect.onboard-multimodal-shadow-intent-v1",
                    "input_sha256": metadata["input_sha256"],
                    "scope_sha256": metadata["scope_sha256"],
                    "frame_ids": [metadata["frame_ids"][0]],
                    "evidence_ids": [],
                    "output": {
                        "response": "hold",
                        "reason": "fixture only",
                        "confidence": 0.1,
                        "task_action": "hold",
                        "flight_plan": None,
                    },
                }
            )
        return {
            "raw_output": raw,
            "generated_tokens": 4,
            "input_tokens": 100,
            "preprocessing_s": 0.001,
            "generation_s": 0.002,
            "peak_allocated_bytes": 0,
            "max_new_tokens_reached": False,
        }


def _run_fixture(tmp_path, monkeypatch, output_kind, *, multi=False):
    _case, manifest = _fixture(tmp_path)
    if multi:
        document = json.loads(manifest.read_text(encoding="utf-8"))
        second = {**document["cases"][0], "case_id": "fixture-two", "prompt": "另一个任务"}
        document["cases"].append(second)
        manifest.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    args = _args(tmp_path, manifest)
    _FakeBackend.instances = 0
    _FakeBackend.output_kind = output_kind
    monkeypatch.setattr(probe, "require_d_storage", lambda path: path.resolve())
    monkeypatch.setattr(probe, "configure_d_cache", lambda _path: None)
    monkeypatch.setattr(probe, "prepare_model_snapshot", lambda *_args: (tmp_path, {"files": []}))
    code = probe.run_probe(args, backend_factory=_FakeBackend)
    report = json.loads((args.output_directory / "report.json").read_text(encoding="utf-8"))
    return code, report, args


def test_raw_invalid_answer_preserved_without_repair(tmp_path, monkeypatch):
    code, report, args = _run_fixture(tmp_path, monkeypatch, "invalid")
    assert code == 0 and report["native_generations_completed"] == 1
    assert report["strict_intents_accepted"] == 0
    assert (args.output_directory / "fixture-one" / "raw-output.txt").read_text(
        encoding="utf-8"
    ) == "unrepaired fixture answer"
    assert report["hard_deadline_enforced"] is False
    assert report["execution_authority"] is False
    assert report["candidate_promoted"] is False


def test_cuda_failure_is_not_native_inference_success(tmp_path, monkeypatch):
    code, report, _args_value = _run_fixture(tmp_path, monkeypatch, "cuda_failure")
    assert code == 1 and report["vlm_inference_performed"] is False
    assert report["native_generations_completed"] == 0
    assert report["case_results"][0]["native_generation_completed"] is False


def test_shared_strict_parser_acceptance_is_separate(tmp_path, monkeypatch):
    code, report, _args_value = _run_fixture(tmp_path, monkeypatch, "valid")
    assert code == 0 and report["strict_intents_accepted"] == 1
    assert report["case_results"][0]["parsed_intent"]["execution_authority"] is False


def test_actual_late_completion_rejected_without_repair(tmp_path, monkeypatch):
    counter = iter([0.0, 0.001, 31.0])
    monkeypatch.setattr(probe.time, "perf_counter", lambda: next(counter))
    code, report, _args_value = _run_fixture(tmp_path, monkeypatch, "valid")
    assert code == 0 and report["native_generations_completed"] == 1
    assert report["strict_intents_accepted"] == 0
    assert report["case_results"][0]["inference_latency_ms"] == 31000.0
    assert report["case_results"][0]["raw_output_sha256"]
    assert "deadline" in report["case_results"][0]["failure"]
    assert report["hard_deadline_enforced"] is False


def test_multiple_cases_share_one_load_but_different_input_scope(tmp_path, monkeypatch):
    code, report, _args_value = _run_fixture(tmp_path, monkeypatch, "valid", multi=True)
    assert code == 0 and _FakeBackend.instances == 1
    assert report["strict_intents_accepted"] == 2
    first, second = report["case_results"]
    assert first["input_sha256"] != second["input_sha256"]


def test_c_drive_storage_rejected():
    with pytest.raises(ValueError, match="D:"):
        probe.require_d_storage(probe.Path("C:/probe-cache-that-is-not-written"))


def test_existing_output_never_overwritten(tmp_path, monkeypatch):
    _case, manifest = _fixture(tmp_path)
    args = _args(tmp_path, manifest)
    args.output_directory.mkdir()
    monkeypatch.setattr(probe, "require_d_storage", lambda path: path.resolve())
    with pytest.raises(FileExistsError):
        probe.run_probe(args, backend_factory=_FakeBackend)


def test_evidence_writer_exclusive(tmp_path):
    path = tmp_path / "report.json"
    probe.write_json(path, {"first": True})
    with pytest.raises(FileExistsError):
        probe.write_json(path, {"second": True})
