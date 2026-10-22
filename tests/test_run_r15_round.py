from __future__ import annotations

import shutil
import sys
import types
from argparse import Namespace
from pathlib import Path

from scripts.run_r15_round import (
    Round,
    compare_metrics,
    distribution,
    merge_without_copies,
    stratified_replay,
)
from scripts.train_slow_system_lora import initialize_adapter, resolve_adapter_weights


def row(sample_id: str, decision: str, side: int = 10, category: str = "backpack") -> dict:
    return {
        "sample_id": sample_id,
        "task_category": category,
        "candidate_short_side_px": side,
        "assistant_target": {"decision": decision},
        "candidate_box": [0, 0, side, side],
    }


def test_merge_rejects_positive_copy_with_new_id_and_keeps_hard_negative():
    base = [row("base-pos", "confirm_target"), row("base-neg", "reject_track", 20)]
    additions = [row("renamed-copy", "confirm_target"), row("new-neg", "reject_track", 8)]
    merged = merge_without_copies(base, additions)
    assert [item["sample_id"] for item in merged] == ["base-pos", "base-neg", "new-neg"]
    stats = distribution(merged)
    assert stats["backpack|<12|negative"] == 1
    assert stats["backpack|<12|positive"] == 1
    assert stats["backpack|20-32|negative"] == 1


def test_acceptance_rules_require_all_four_gates():
    old = {
        "v1_identity_accuracy": 0.90,
        "v3_identity_accuracy": 0.785,
        "v3_under12_recall": 0.46,
        "v3_distractor_rejection": 0.905,
    }
    good = {
        "v1_identity_accuracy": 0.90,
        "v3_identity_accuracy": 0.79,
        "v3_under12_recall": 0.56,
        "v3_distractor_rejection": 0.89,
    }
    ok, reasons = compare_metrics(old, good)
    assert ok and not reasons
    bad = dict(good, v3_under12_recall=0.55, v3_distractor_rejection=0.88)
    ok, reasons = compare_metrics(old, bad)
    assert not ok
    assert any("<12" in reason for reason in reasons)
    assert any("干扰物" in reason for reason in reasons)


def test_replay_is_stratified_and_bounded():
    rows = [row(f"p{i}", "confirm_target", 8 if i % 2 else 24) for i in range(20)]
    rows += [row(f"n{i}", "reject_track", 8 if i % 2 else 24) for i in range(20)]
    replay = stratified_replay(rows, 0.25)
    assert len(replay) >= 4
    assert {item["assistant_target"]["decision"] for item in replay} == {
        "confirm_target",
        "reject_track",
    }
    assert {item["candidate_short_side_px"] for item in replay} == {8, 24}


def test_adapter_path_resolver_accepts_directory(tmp_path: Path):
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    weights = adapter / "adapter_model.safetensors"
    weights.write_bytes(b"synthetic")
    assert resolve_adapter_weights(adapter) == weights


def test_initialize_adapter_loads_weights_without_optimizer(monkeypatch, tmp_path: Path):
    adapter = tmp_path / "adapter_model.safetensors"
    adapter.write_bytes(b"synthetic")
    calls = {}
    peft = types.ModuleType("peft")
    peft.set_peft_model_state_dict = lambda model, weights: (
        calls.update(model=model, weights=weights)
        or types.SimpleNamespace(unexpected_keys=[], missing_keys=[])
    )
    safe = types.ModuleType("safetensors.torch")
    safe.load = lambda payload: {"lora": payload}
    monkeypatch.setitem(sys.modules, "peft", peft)
    monkeypatch.setitem(sys.modules, "safetensors", types.ModuleType("safetensors"))
    monkeypatch.setitem(sys.modules, "safetensors.torch", safe)
    initialize_adapter("model", adapter)
    assert calls["weights"]["lora"] == b"synthetic"


def test_stage_resume_writes_manifest_and_skips(capsys):
    out = Path.cwd() / ".r15-test-work" / "round"
    shutil.rmtree(out.parent, ignore_errors=True)
    args = Namespace(out=out, dry_run=False, min_free_gb=0, min_free_memory_gb=0)
    runner = Round(args)
    output = out / "tiny.txt"
    runner.run(
        "tiny",
        [sys.executable, "-c", f"open(r'{output}', 'w', encoding='utf-8').write('ok')"],
        estimate="1 秒",
        disk="1 KiB",
        outputs=(output,),
    )
    assert output.read_text() == "ok"
    manifest = out / "manifests" / "tiny.json"
    assert manifest.is_file()
    runner = Round(args)
    runner.run(
        "tiny",
        [sys.executable, "-c", "raise SystemExit(99)"],
        estimate="1 秒",
        disk="1 KiB",
        outputs=(output,),
    )
    assert "skip" in capsys.readouterr().out
    shutil.rmtree(out.parent, ignore_errors=True)


def test_each_collect_attempt_first_removes_the_workers_leftover_containers(monkeypatch, tmp_path):
    import scripts.run_r15_round as r15

    calls = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return types.SimpleNamespace(
            returncode=1, stdout="abc123\n" if command[:2] == ["docker", "ps"] else ""
        )

    monkeypatch.setattr(r15.subprocess, "run", fake_run)
    monkeypatch.setattr(r15.time, "sleep", lambda _s: None)
    round_ = Round.__new__(Round)
    round_.a = Namespace(retries=1)
    cmd = ["python", "collect.py", "--container-prefix", "r15-worker-1", "--out", str(tmp_path)]
    assert round_._collect_one("w:0", cmd, tmp_path)["status"] == "failed"
    docker = [c for c in calls if c[0] == "docker"]
    assert (
        docker
        == [
            ["docker", "ps", "-aq", "--filter", "name=^r15-worker-1-"],
            ["docker", "rm", "-f", "abc123"],
        ]
        * 2
    )
    # The removal precedes each collector attempt.
    assert [c[0] for c in calls] == ["docker", "docker", "python", "docker", "docker", "python"]


def test_merge_tops_up_r12_negatives_instead_of_dropping_new_positives():
    from scripts.run_r15_round import top_up_negatives

    rows = [row(f"p{i}", "confirm_target", 8) for i in range(6)] + [
        row(f"n{i}", "reject_track", 20) for i in range(4)
    ]
    pool = [row(f"r{i}", "reject_track", 10 + i) for i in range(20)] + [
        row("extra-pos", "confirm_target", 30)
    ]
    topped = top_up_negatives(rows, pool, 0.41)
    positives = sum(1 for r in topped if r["assistant_target"]["decision"] == "confirm_target")
    negatives = sum(1 for r in topped if r["assistant_target"]["decision"] == "reject_track")
    assert positives == 6  # every new positive kept, none added from the pool
    assert positives / (positives + negatives) <= 0.41
    assert len({r["sample_id"] for r in topped}) == len(topped)


def test_initialize_adapter_ignores_frozen_base_weights_but_not_missing_lora(
    monkeypatch, tmp_path: Path
):
    import pytest

    adapter = tmp_path / "adapter_model.safetensors"
    adapter.write_bytes(b"synthetic")
    missing = ["base_model.model.model.visual.patch_embed.proj.weight"]
    peft = types.ModuleType("peft")
    peft.set_peft_model_state_dict = lambda model, weights: types.SimpleNamespace(
        unexpected_keys=[], missing_keys=list(missing)
    )
    safe = types.ModuleType("safetensors.torch")
    safe.load = lambda payload: {"lora": payload}
    monkeypatch.setitem(sys.modules, "peft", peft)
    monkeypatch.setitem(sys.modules, "safetensors", types.ModuleType("safetensors"))
    monkeypatch.setitem(sys.modules, "safetensors.torch", safe)
    initialize_adapter("model", adapter)  # base weights are never in an adapter
    missing.append("base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_A.weight")
    with pytest.raises(ValueError, match="does not match"):
        initialize_adapter("model", adapter)


def test_identity_summary_reads_the_metrics_block_and_size_buckets():
    from scripts.run_r15_round import compare_metrics, identity_summary

    payload = {
        "metrics": {
            "identity_accuracy": 0.9,
            "distractor_rejection_rate": 0.73,
            "size_buckets": {"<12": {"true_target_recall": 0.92}},
        }
    }
    assert identity_summary(payload) == {
        "identity_accuracy": 0.9,
        "distractor_rejection": 0.73,
        "under12_recall": 0.92,
    }
    approved, reasons = compare_metrics(
        {
            "v1_identity_accuracy": 0.928,
            "v1_distractor_rejection": 0.95,
            "v3_identity_accuracy": 0.785,
            "v3_under12_recall": 0.46,
            "v3_distractor_rejection": 0.905,
        },
        {
            "v1_identity_accuracy": 0.825,
            "v1_distractor_rejection": 0.73,
            "v3_identity_accuracy": 0.901,
            "v3_under12_recall": 0.92,
            "v3_distractor_rejection": 0.836,
        },
    )
    assert not approved
    assert any("v1 真实飞行集干扰物拒绝" in r for r in reasons)
    assert not any("<12 px" in r for r in reasons)  # 46% -> 92% is a real improvement


def _annotate_round(tmp_path: Path, **overrides) -> Round:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    episodes = repo / "artifacts" / "r19-round" / "episodes"
    for world in ("gen_r19_w0000", "gen_r19_w0001", "gen_r15_w0000"):
        (episodes / world).mkdir(parents=True, exist_ok=True)
    index = tmp_path / "index.json"
    index.write_text(
        '{"worlds": [{"world_id": "gen_r19_w0000"}, {"world_id": "gen_r19_w0001"},'
        ' {"world_id": "gen_r19_w0002"}]}',
        encoding="utf-8",
    )
    args = dict(
        out=tmp_path / "out",
        dry_run=False,
        r15_index=index,
        episodes=episodes,
        extra_episodes=[],
        source_root=None,
        annotate_device="cpu",
    )
    args.update(overrides)
    return Round(Namespace(**args))


def test_annotate_caches_the_index_worlds_keyed_from_the_episodes_repository(tmp_path: Path):
    runner = _annotate_round(tmp_path)
    cmd = runner.annotate_command()
    campaigns = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--campaign"]
    episodes = tmp_path / "repo" / "artifacts" / "r19-round" / "episodes"
    # gen_r19_* from the index, not the old hard-coded gen_r15_* glob; a world
    # that was never collected is simply absent.
    assert campaigns == [str(episodes / "gen_r19_w0000"), str(episodes / "gen_r19_w0001")]
    # build_sft keys frames as artifacts/r19-round/... relative to the repository.
    assert cmd[cmd.index("--source-root") + 1] == str((tmp_path / "repo").resolve())


def test_annotate_rejects_a_source_root_whose_keys_build_sft_never_looks_up(tmp_path: Path):
    import pytest

    wrong = tmp_path / "repo" / "artifacts"
    runner = _annotate_round(tmp_path, source_root=wrong)
    with pytest.raises(RuntimeError, match="不一致"):
        runner.annotate_command()
    explicit = _annotate_round(tmp_path, source_root=tmp_path / "repo")
    assert "--source-root" in explicit.annotate_command()


def test_annotate_refuses_when_no_round_world_was_collected(tmp_path: Path):
    import pytest

    runner = _annotate_round(tmp_path)
    runner.a.r15_index.write_text('{"worlds": [{"world_id": "gen_x_w0000"}]}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="找不到"):
        runner.annotate_command()
