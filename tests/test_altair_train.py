from scripts.altair import train_altair as ta

NAMES = {
    "q": "model.language_model.layers.3.self_attn.q_proj",
    "o": "model.language_model.layers.3.self_attn.o_proj",
    "mlp": "model.language_model.layers.12.mlp.down_proj",
    "vis": "model.visual.blocks.0.attn.qkv",
    "merger": "model.visual.merger.linear_fc1",
}


def hits(preset):
    rx = ta.PRESETS[preset][0]
    return {k for k, v in NAMES.items() if rx.fullmatch(v)}


def test_preset_targets():
    assert hits("A") == {"q"}
    assert hits("B") == {"q", "o"}
    assert hits("C") == {"q", "o", "mlp"}  # never the vision tower


def test_argv_defaults_and_override():
    preset, argv = ta.build_argv(["--dataset", "d", "--learning-rate", "5e-5"])
    assert preset == "C"
    assert argv[argv.index("--rank") + 1] == "32"
    assert argv[argv.index("--alpha") + 1] == "64"
    # argparse keeps the last occurrence, so the user's flag wins
    assert argv[len(argv) - 1 - argv[::-1].index("--learning-rate") + 1] == "5e-5"


def test_preset_a_rank():
    _, argv = ta.build_argv(["--preset", "A"])
    assert argv[argv.index("--rank") + 1] == "16"
    assert "--preset" not in argv


class _T:
    def __init__(self, v):
        self.v = v

    def abs(self):
        return self

    def float(self):
        return self

    def mean(self):
        return self.v

    def __float__(self):
        return float(self.v)


def test_backfill_lora_stat(tmp_path):
    import json

    (tmp_path / "checkpoint").mkdir()
    (tmp_path / "checkpoint/adapter_model.safetensors").write_bytes(b"x")
    rp = tmp_path / "training-report.json"
    rp.write_text(json.dumps({"lora_b_mean_abs": None}))
    load = lambda _p: {"a.lora_B.weight": _T(0.2), "a.lora_A.weight": _T(9.0), "b.lora_B.weight": _T(0.4)}
    assert abs(ta.backfill_lora_stat(tmp_path, load) - 0.3) < 1e-9
    assert abs(json.loads(rp.read_text())["lora_b_mean_abs"] - 0.3) < 1e-9


def test_backfill_keeps_existing_and_missing(tmp_path):
    import json

    assert ta.backfill_lora_stat(tmp_path) is None
    (tmp_path / "training-report.json").write_text(json.dumps({"lora_b_mean_abs": 0.5}))
    assert ta.backfill_lora_stat(tmp_path) == 0.5


def test_output_dir():
    assert str(ta.output_dir(["--output", "/x", "--a", "1"])) == "/x"
    assert ta.output_dir(["--a"]) is None
