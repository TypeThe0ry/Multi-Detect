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


def test_report_uses_configured_labels_and_records_baseline_hashes(tmp_path: Path, capsys):
    import hashlib
    import json
    from argparse import Namespace

    out = tmp_path / "r21id-round"
    out.mkdir()
    baseline = tmp_path / "r18-round" / "eval-v3.json"
    baseline.parent.mkdir()
    baseline_v1 = tmp_path / "r18-round" / "eval-v1.json"
    payload = {"metrics": {"identity_accuracy": 0.9, "distractor_rejection_rate": 0.98}, "size_buckets": {"<12": {"true_target_recall": 0.5}}}
    for path in (baseline, baseline_v1):
        path.write_text(json.dumps(payload), encoding="utf-8")
    candidate_payload = {"metrics": {**payload["metrics"], "size_buckets": {"<12": {"true_target_recall": 0.6}}}}
    for path in (out / "eval-v1.json", out / "eval-v3.json"):
        path.write_text(json.dumps(candidate_payload), encoding="utf-8")
    args = Namespace(
        out=out, dry_run=False, r12_baseline=baseline, r12_v1_baseline=baseline_v1,
        baseline_label="r18", candidate_label="r21id",
    )
    Round(args).write_report()
    report = (out / "r15-report.md").read_text(encoding="utf-8")
    manifest = json.loads((out / "report-manifest.json").read_text(encoding="utf-8"))
    assert "| 指标 | r18 | r21id |" in report
    assert "建议替换 r18" in report
    assert manifest["baseline_label"] == "r18"
    assert manifest["candidate_label"] == "r21id"
    assert manifest["baseline_json"]["v3"] == str(baseline.resolve())
    assert manifest["baseline_json"]["v3_sha256"] == hashlib.sha256(baseline.read_bytes()).hexdigest()
    assert not capsys.readouterr().out


def test_report_warns_when_later_round_baseline_keeps_default_label(tmp_path: Path, capsys):
    import json
    from argparse import Namespace

    out = tmp_path / "out"
    out.mkdir()
    baseline = tmp_path / "r20-round" / "eval-v3.json"
    baseline.parent.mkdir()
    baseline_v1 = tmp_path / "r20-round" / "eval-v1.json"
    payload = {"metrics": {"identity_accuracy": 0.9, "distractor_rejection_rate": 0.98}, "size_buckets": {"<12": {"true_target_recall": 0.5}}}
    for path in (baseline, baseline_v1, out / "eval-v1.json", out / "eval-v3.json"):
        path.write_text(json.dumps(payload), encoding="utf-8")
    Round(Namespace(out=out, dry_run=False, r12_baseline=baseline, r12_v1_baseline=baseline_v1,
                    baseline_label="r12", candidate_label="r15")).write_report()
    report = (out / "r15-report.md").read_text(encoding="utf-8")
    assert "警告：基线标签仍为 r12" in capsys.readouterr().out
    assert str(baseline.resolve()) in report
def test_each_collect_attempt_first_removes_the_workers_leftover_containers(monkeypatch, tmp_path):
    import scripts.run_r15_round as r15

    calls = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return types.SimpleNamespace(
            returncode=1, stdout="abc123\n" if command[:2] == ["docker", "ps"] else ""
        )

    monkeypatch.setattr(r15.subprocess, "run", fake_run)
    monkeypatch.setattr(r15, "_docker_daemon_reachable", lambda: True)
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


def test_docker_down_aborts_collect_without_consuming_episode_attempt(monkeypatch, tmp_path):
    import json
    import pytest
    import scripts.run_r15_round as r15

    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    world = "gen_r19_w0000"
    runtime = {
        "schema_version": 1,
        "vehicle": {"model": "px4_multirotor_standard", "spawn_pose": {"z_up_m": 0.2, "pitch_rad": 0}},
        "sensors": [],
    }
    (scenarios / f"{world}.runtime_input.json").write_text(json.dumps(runtime))
    (scenarios / f"{world}.truth.json").write_text("{}")
    (scenarios / f"{world}.scenario.json").write_text(json.dumps({
        "channels": {"runtime_input": {"path": f"{world}.runtime_input.json"}, "truth": {"path": f"{world}.truth.json"}}
    }))
    index = scenarios / "index.json"
    index.write_text(json.dumps({"worlds": [{"world_id": world}]}))
    args = Namespace(
        out=tmp_path / "round", episodes=tmp_path / "episodes", scenarios=scenarios,
        r15_index=index, dry_run=False, min_free_gb=0, min_free_memory_gb=0,
        collector_min_free_gb=None, low_disk=False, steps_per_episode=1, workers=1,
        retries=3, docker_wait_minutes=1, teacher_target="true",
    )
    monkeypatch.setattr(r15, "_docker_daemon_reachable", lambda: False)
    monkeypatch.setattr(r15, "_wait_for_docker", lambda _minutes: False)
    monkeypatch.setattr(r15, "_remove_worker_containers", lambda _prefix: pytest.fail("cleanup must not run"))
    runner = r15.Round(args)
    monkeypatch.setattr(runner, "_collection_airframe", lambda _scenario: ("x500_mono_cam", None, runtime))
    import scripts.collect_sim_teacher_episodes as collector
    monkeypatch.setattr(collector, "docker_command", lambda *_a, **_kw: ["docker"])

    with pytest.raises(RuntimeError, match="Docker daemon 不可用"):
        runner.collect()
    state = json.loads(runner.state_path.read_text())
    assert state["stages"]["collect"]["status"] == "docker_unavailable"
    assert state["episodes"][f"{world}:0"] == {"status": "pending", "failure": "docker_unavailable"}


def test_docker_recovery_mid_wait_runs_episode_without_losing_attempt(monkeypatch, tmp_path):
    import scripts.run_r15_round as r15

    runs = []
    health = iter([False, True, True])
    monkeypatch.setattr(r15, "_docker_daemon_reachable", lambda: next(health))
    monkeypatch.setattr(r15, "_wait_for_docker", lambda _minutes: True)
    monkeypatch.setattr(r15, "_remove_worker_containers", lambda _prefix: None)

    def fail_attempt(command, **_kwargs):
        runs.append(command)
        output_dir = Path(command[-1])
        output_dir.mkdir(parents=True)
        (output_dir / "partial.txt").write_text(str(len(runs)))
        return type("Completed", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(r15.subprocess, "run", fail_attempt)
    runner = Round.__new__(Round)
    runner.a = Namespace(retries=0, docker_wait_minutes=20)
    result = runner._collect_one(
        "w:0", ["python", "collect.py", "--container-prefix", "worker", "--out", str(tmp_path)], tmp_path
    )
    assert result == {"status": "failed", "attempts": 1}
    assert len(runs) == 1
    first_dir = Path(runs[0][-1])
    assert first_dir.name == "attempt-0"
    assert (first_dir / "partial.txt").read_text() == "1"


def test_docker_recovery_after_attempt_uses_new_preserved_output_directory(monkeypatch, tmp_path):
    import json
    import scripts.run_r15_round as r15

    runs = []
    health = iter([True, False, True, True])
    monkeypatch.setattr(r15, "_docker_daemon_reachable", lambda: next(health))
    monkeypatch.setattr(r15, "_wait_for_docker", lambda _minutes: True)
    monkeypatch.setattr(r15, "_remove_worker_containers", lambda _prefix: None)

    def fail_attempt(command, **_kwargs):
        runs.append(command)
        output_dir = Path(command[-1])
        output_dir.mkdir(parents=True)
        if len(runs) == 1:
            (output_dir / "partial.txt").write_text("partial")
        else:
            episode = output_dir / "episode-test" / "episode.json"
            episode.parent.mkdir(parents=True)
            episode.write_text('{"status":"complete"}')
        return type(
            "Completed",
            (),
            {"returncode": 0 if len(runs) == 2 else 1, "stdout": "", "stderr": ""},
        )()

    monkeypatch.setattr(r15.subprocess, "run", fail_attempt)
    runner = Round.__new__(Round)
    runner.a = Namespace(retries=0, docker_wait_minutes=20)
    result = runner._collect_one(
        "w:0", ["python", "collect.py", "--container-prefix", "worker", "--out", str(tmp_path)], tmp_path
    )

    assert result["status"] == "complete"
    previous, retry = (Path(command[-1]) for command in runs)
    assert previous != retry
    assert previous.name == "attempt-0" and retry.name == "attempt-1"
    assert (previous / "partial.txt").read_text() == "partial"
    assert json.loads((retry / "episode-test" / "episode.json").read_text())["status"] == "complete"


def test_repeated_docker_flaps_abort_after_bounded_outages(monkeypatch, tmp_path):
    import pytest
    import scripts.run_r15_round as r15

    monkeypatch.setattr(r15, "_docker_daemon_reachable", lambda: True)
    monkeypatch.setattr(r15, "_wait_for_docker", lambda _minutes: True)
    monkeypatch.setattr(r15, "_remove_worker_containers", lambda _prefix: None)

    def fail_and_drop_daemon(command, **_kwargs):
        return type("Completed", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    health = iter([True, False] * 6)
    monkeypatch.setattr(r15, "_docker_daemon_reachable", lambda: next(health))
    monkeypatch.setattr(r15.subprocess, "run", fail_and_drop_daemon)
    runner = Round.__new__(Round)
    runner.a = Namespace(retries=20, docker_wait_minutes=20)
    with pytest.raises(r15.DockerUnavailable, match="repeatedly flapped"):
        runner._collect_one(
            "w:0", ["python", "collect.py", "--container-prefix", "worker", "--out", str(tmp_path)], tmp_path
        )


def test_normal_collector_failures_consume_configured_attempts(monkeypatch, tmp_path):
    import scripts.run_r15_round as r15

    calls = []
    monkeypatch.setattr(r15, "_docker_daemon_reachable", lambda: True)
    monkeypatch.setattr(r15, "_remove_worker_containers", lambda _prefix: None)
    monkeypatch.setattr(r15.subprocess, "run", lambda command, **_kw: (
        calls.append(command) or type("Completed", (), {"returncode": 1, "stdout": "", "stderr": ""})()
    ))
    monkeypatch.setattr(r15.time, "sleep", lambda _seconds: None)
    runner = Round.__new__(Round)
    runner.a = Namespace(retries=2, docker_wait_minutes=20)
    result = runner._collect_one(
        "w:0", ["python", "collect.py", "--container-prefix", "worker", "--out", str(tmp_path)], tmp_path
    )
    assert result == {"status": "failed", "attempts": 3}
    assert len(calls) == 3


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


def _identity(
    sample_id: str,
    decision: str,
    label_type: str,
    *,
    world: str = "w0",
    image: str | None = None,
    task: str = "Find the backpack.",
) -> dict:
    return {
        "sample_id": sample_id,
        "assistant_target": {"decision": decision},
        "label_type": label_type,
        "world": world,
        "image_path": image or f"{sample_id}.jpg",
        "task_text": task,
    }


def test_full_replay_trims_unpaired_counterfactual_positives_before_paired_or_true():
    from scripts.run_r15_round import trim_counterfactual_positives

    r12 = [_identity(f"r12-p{i}", "confirm_target", "true_target") for i in range(4)] + [
        _identity(f"r12-n{i}", "reject_track", "distractor") for i in range(6)
    ]
    true = [
        _identity(f"t{i}", "approach_track", "true_target", world=f"w{i % 2}") for i in range(3)
    ]
    paired = [
        _identity(
            f"cp{i}", "confirm_target", "counterfactual", image=f"f{i}.jpg", task="Find the box."
        )
        for i in range(2)
    ]
    paired_rejects = [
        _identity(
            f"cn{i}",
            "reject_track",
            "counterfactual_negative",
            image=f"f{i}.jpg",
            task="Find the box.",
        )
        for i in range(2)
    ]
    unpaired = [
        _identity(f"cu{i}", "approach_track", "proposal_counterfactual", world=f"w{i % 2}")
        for i in range(6)
    ]
    rows = r12 + true + paired + paired_rejects + unpaired
    protected = {row["sample_id"] for row in r12}

    kept, report = trim_counterfactual_positives(rows, protected, 0.5, seed=7)
    again, _ = trim_counterfactual_positives(rows, protected, 0.5, seed=7)
    kept_ids = {row["sample_id"] for row in kept}
    # 15 positives vs 8 negatives; a 0.5 cap allows 8 positives, so 7 must go:
    # all six unpaired renamed-task positives, then one paired one.
    assert report["dropped"] == 7
    assert report["unpaired_counterfactual"] == 6 and report["paired_counterfactual"] == 1
    assert protected <= kept_ids
    assert {row["sample_id"] for row in true + paired_rejects} <= kept_ids
    assert [row["sample_id"] for row in again] == [row["sample_id"] for row in kept]

    kept, report = trim_counterfactual_positives(rows, protected, 0.6, seed=7)
    # cap 0.6 allows 12 positives: only 3 of the 6 unpaired go, spread over both worlds.
    assert report["dropped"] == 3 and report["unpaired_counterfactual"] == 3
    assert {k.split("|")[0] for k in report["by_stratum"]} == {"w0", "w1"}
    assert {row["sample_id"] for row in paired} <= {row["sample_id"] for row in kept}


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    import json

    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _merge_args(tmp_path: Path) -> Namespace:
    return Namespace(
        merged=tmp_path / "merged",
        r12_dataset=tmp_path / "r12",
        additions=tmp_path / "add",
        r12_replay_fraction=1.0,
        max_positive_ratio_delta=0.02,
        max_negative_ratio_delta=0.02,
        seed=20261016,
    )


def _r12_dataset(tmp_path: Path) -> None:
    import json

    r12 = tmp_path / "r12"
    r12.mkdir()
    train = [_identity(f"r12-p{i}", "confirm_target", "true_target") for i in range(40)] + [
        _identity(f"r12-n{i}", "reject_track", "distractor") for i in range(60)
    ]
    _write_jsonl(r12 / "train.jsonl", train)
    for split in ("val", "test"):
        _write_jsonl(r12 / f"{split}.jsonl", [])
    (r12 / "manifest.json").write_text(
        json.dumps(
            {"counts": {"train": {"decisions": {"confirm_target": 40, "reject_track": 60}}}}
        ),
        encoding="utf-8",
    )


def test_merge_with_full_replay_rebalances_positive_heavy_additions(tmp_path: Path):
    import json

    from scripts.run_r15_round import merge_cli

    _r12_dataset(tmp_path)
    add = tmp_path / "add"
    add.mkdir()
    # Same shape as r19: about half positives, most of them renamed-task rows.
    rows = [_identity(f"t{i}", "approach_track", "true_target") for i in range(5)]
    rows += [_identity(f"c{i}", "approach_track", "counterfactual") for i in range(15)]
    rows += [_identity(f"n{i}", "reject_track", "distractor") for i in range(20)]
    _write_jsonl(add / "train.jsonl", rows)
    for split in ("val", "test"):
        _write_jsonl(add / f"{split}.jsonl", [])

    merge_cli(_merge_args(tmp_path))  # 60/140 = 0.429 before rebalancing

    manifest = json.loads((tmp_path / "merged" / "manifest.json").read_text(encoding="utf-8"))
    assert abs(manifest["merged_positive_ratio"] - 0.40) <= 0.02
    rebalance = manifest["counts"]["train"]["rebalance"]
    assert rebalance["r12_negative_top_up"] == 0
    assert rebalance["dropped"] == rebalance["unpaired_counterfactual"] > 0
    merged = (tmp_path / "merged" / "train.jsonl").read_text(encoding="utf-8")
    assert all(f'"t{i}"' in merged for i in range(5))
    assert all(f'"r12-p{i}"' in merged for i in range(40))


def test_merge_gate_still_fails_when_true_positives_alone_tilt_the_mix(tmp_path: Path):
    import pytest

    from scripts.run_r15_round import merge_cli

    _r12_dataset(tmp_path)
    add = tmp_path / "add"
    add.mkdir()
    rows = [_identity(f"t{i}", "approach_track", "true_target") for i in range(20)]
    rows += [_identity(f"n{i}", "reject_track", "distractor") for i in range(10)]
    _write_jsonl(add / "train.jsonl", rows)
    for split in ("val", "test"):
        _write_jsonl(add / f"{split}.jsonl", [])

    with pytest.raises(SystemExit, match="positive ratio"):
        merge_cli(_merge_args(tmp_path))
