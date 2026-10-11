from __future__ import annotations

import json

from scripts import convert_slow_system_lora as conversion
from scripts import eval_slow_system as evaluator
from scripts import train_slow_system_lora as trainer


def test_probe_reports_cached_runtime_without_network(tmp_path):
    root = tmp_path / "llama-cpp"
    bin_dir = root / "b11552"
    bin_dir.mkdir(parents=True)
    (bin_dir / "llama-server.exe").write_bytes(b"server")
    (bin_dir / "llama-quantize.exe").write_bytes(b"quantize")
    tools = conversion.probe_local_tools(root)
    assert tools.convert_lora_to_gguf is None
    assert tools.convert_hf_to_gguf is None
    assert tools.gguf_python_module is None
    assert tools.llama_server and tools.llama_server.endswith("llama-server.exe")
    assert tools.llama_quantize and tools.llama_quantize.endswith("llama-quantize.exe")


def test_conversion_plan_is_a_nonexecuting_merge_then_quantize_plan(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    tools = conversion.probe_local_tools(root)
    plan = conversion.conversion_plan(
        tools,
        base=tmp_path / "base",
        adapter=tmp_path / "adapter",
        output=tmp_path / "out",
    )
    assert plan["network_access"] is False
    assert plan["gpu_work_performed"] is False
    assert plan["hf_conversion_available"] is False
    assert any("merge_and_unload" in command for command in plan["commands"])
    assert any("MISSING: convert_hf_to_gguf.py" in command for command in plan["commands"])
    assert plan["commands"][-1].endswith("Q4_K_M") is False  # no quantizer in this fixture
    assert any("MISSING: llama-quantize" in command for command in plan["commands"])


def test_conversion_plan_serializes_as_utf8_json(tmp_path):
    output = tmp_path / "plan.json"
    tools = conversion.probe_local_tools(tmp_path / "none")
    payload = json.dumps(conversion.conversion_plan(tools), ensure_ascii=False, sort_keys=True)
    output.write_text(payload + "\n", encoding="utf-8", newline="\n")
    assert json.loads(output.read_text(encoding="utf-8"))["schema"].endswith("-v1")
    assert b"\r\n" not in output.read_bytes()


def test_trainer_quantization_option_plumbing_is_cpu_only():
    options = trainer.TrainOptions(quantization="auto")
    assert trainer.resolve_quantization("auto", bnb_available=False) == "bf16"
    assert trainer.resolve_quantization("auto", bnb_available=True) == "4bit"
    kwargs = trainer.model_load_kwargs(options, bnb_available=False)
    assert kwargs["local_files_only"] is True
    assert kwargs["device_map"] == {"": "cuda:0"}
    assert "load_in_4bit" not in kwargs


def test_trainer_run_passes_cli_options_to_mock_backend(tmp_path, monkeypatch):
    seen = {}

    class Optimizer:
        def state_dict(self):
            return {}

    class Torch:
        class optim:
            @staticmethod
            def AdamW(parameters, lr):
                return Optimizer()

        class cuda:
            @staticmethod
            def max_memory_allocated():
                return 0

            @staticmethod
            def max_memory_reserved():
                return 0

        @staticmethod
        def save(value, path):
            path.write_bytes(b"mock")

    class Model:
        def parameters(self):
            return []

        def save_pretrained(self, path, **kwargs):
            path.mkdir(parents=True, exist_ok=True)

    def backend(snapshot, options):
        seen["snapshot"] = snapshot
        seen["options"] = options
        return Torch, Model(), object(), ["q_proj"], "bf16"

    monkeypatch.setattr(trainer, "admit_train_rows", lambda dataset: ([], "dataset-hash"))
    monkeypatch.setattr(trainer, "local_snapshot", lambda model: tmp_path / "snapshot")
    args = type("Args", (), {
        "epochs": 1, "max_steps": 0, "accumulation": 2, "rank": 3, "alpha": 6,
        "learning_rate": 2e-4, "seed": 7, "max_input_tokens": 512,
        "max_new_tokens": 64, "checkpoint_every": 10, "max_side": 320,
        "quantization": "bf16", "vision_projector": True,
        "dataset": tmp_path / "dataset", "model": tmp_path / "model",
        "output": tmp_path / "output", "resume": False,
    })()
    report = trainer.run(args, backend_factory=backend)
    assert report["completed"] is True
    assert seen["options"].rank == 3
    assert seen["options"].vision_projector is True
    assert seen["options"].max_side == 320


def test_evaluator_metrics_cover_json_validity_and_distractor_rejection():
    rows = [
        {"sample_id": "a", "world": "w", "expected": "confirm_target", "valid": True, "decision": "confirm_target", "latency_s": 0.1},
        {"sample_id": "b", "world": "w", "expected": "reject_track", "valid": True, "decision": "reject_track", "latency_s": 0.2},
        {"sample_id": "c", "world": "w", "expected": "reject_track", "valid": False, "decision": None, "latency_s": 0.3},
    ]
    metrics = evaluator.decision_metrics(rows)
    assert metrics["json_validity"] == 2 / 3
    assert metrics["decision_accuracy"] == 2 / 3
    assert metrics["confirm_precision"] == 1.0
    assert metrics["confirm_recall"] == 1.0
    assert metrics["distractor_rejection_rate"] == 0.5

