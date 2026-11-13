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
