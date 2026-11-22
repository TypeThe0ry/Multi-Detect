from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.realworld import wide_collect_ab as ab
from scripts.realworld import wide_collect_batch as batch


@pytest.fixture
def ab_batch(tmp_path):
    ab_root = tmp_path / "ab"
    ab_root.mkdir()
    spec_path = tmp_path / "ab.json"
    template = {"site": "rw_farm_iowa", "altitude_m": 85, "time_of_day": "noon",
                "weather": "clear", "image": "example/simulator:horizon"}
    spec = {"batch_id": "ab", "output_root": str(ab_root), "gpu": 3, "slots": [5],
            "airframe": batch.DELTA_AIRFRAME,
            "jobs": [dict(template, seed=seed, gimbal_bridge_stabilize=enabled)
                     for seed, enabled in ((201, True), (202, False), (203, True))]}
    spec_path.write_text(json.dumps(spec))
    job_ids = [job["job_id"] for job in batch.expand(spec)["jobs"]]
    final_spec = tmp_path / "plans" / "final.json"
    final_root = tmp_path / "final"
    args = ["--ab-spec", str(spec_path), "--final-spec", str(final_spec),
            "--final-batch-id", "final", "--final-output-root", str(final_root),
            "--final-seeds", "301", "302"]
    return spec, job_ids, ab_root, final_spec, final_root, args


def row(job_id, enabled, p95, **details):
    return {"job_id": job_id, "bridge_stabilize": enabled, "accepted": False,
            "horizon_acceptance": {"feasible_error": {"p95_deg": p95}}, **details}


def write_results(root, *rows):
    path = root / "results.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in rows))
    return path


def test_choose_groups_batch_results_by_bridge_stabilize(ab_batch, capsys):
    spec, (on_a, off, on_b), ab_root, final_spec, final_root, args = ab_batch
    results = write_results(ab_root, row(on_a, True, 60), row(off, False, 9),
                            row(on_b, True, 7), row(on_a, True, 5),
                            row(on_a, True, 90, voided_reason="load_spike"),
                            row(on_a, True, 80, bridge_stabilize_check={"passed": False}),
                            row(on_a, True, None), row(on_a, True, float("inf")),
                            row(on_a, True, "99"), row(on_a, True, -1),
                            row(off, "false", 0), row(off, True, 0), row("unrelated", False, 0),
                            {**row(off, None, 0), "gimbal_bridge_stabilize": False})
    before = results.read_bytes()
    assert ab.main(["choose", *args]) == 0
    out, err = capsys.readouterr()
    assert out == "choice=on\n"
    assert "on: 2 jobs, mean feasible horizon P95 6 deg" in err
    assert "off: 1 jobs, mean feasible horizon P95 9 deg" in err
    assert (ab_root / "CHOICE.txt").read_text() == "choice=on\n"
    assert results.read_bytes() == before
    assert batch.read_object(final_spec) == dict(
        spec, batch_id="final", output_root=str(final_root),
        jobs=[dict(spec["jobs"][0], seed=seed, gimbal_bridge_stabilize=True)
              for seed in (301, 302)],
    )
    assert not final_root.exists()


@pytest.mark.parametrize(("on_p95", "off_p95", "expected"), [(9, 3, False), (3, 3, True)])
def test_select_prefers_lower_mean_and_keeps_on_tiebreak(tmp_path, on_p95, off_p95, expected):
    results = write_results(tmp_path, row("a", True, on_p95), row("b", False, off_p95))
    assert ab.select_mode(results, {"a": True, "b": False})[0] is expected


@pytest.mark.parametrize("corrupt", [False, True])
def test_incomplete_comparison_does_not_publish_choice(ab_batch, capsys, corrupt):
    _, (on_a, off, _), ab_root, final_spec, _, args = ab_batch
    results = write_results(ab_root, row(on_a, True, 5), row(off, False, None))
    if corrupt:
        with results.open("a") as stream:
            stream.write('{"job_id":')
    with pytest.raises(SystemExit) as error:
        ab.main(["choose", *args])
    assert error.value.code == 2
    expected = "results.jsonl:3" if corrupt else "both modes (on=1, off=0)"
    assert expected in capsys.readouterr().err
    assert not final_spec.exists()
    assert not (ab_root / "CHOICE.txt").exists()


def test_existing_final_plan_cannot_mix_bridge_modes(ab_batch, capsys):
    spec, (on_a, off, _), ab_root, final_spec, final_root, args = ab_batch
    write_results(ab_root, row(on_a, True, 5), row(off, False, 9))
    final_root.mkdir()
    off_jobs = [dict(spec["jobs"][0], seed=seed, gimbal_bridge_stabilize=False)
                for seed in (301, 302)]
    existing = batch.expand(dict(spec, batch_id="final", output_root=str(final_root), jobs=off_jobs))
    (final_root / "manifest.json").write_text(json.dumps(existing))
    with pytest.raises(SystemExit) as error:
        ab.main(["choose", *args])
    assert error.value.code == 2
    assert "different batch plan" in capsys.readouterr().err
    assert not final_spec.exists()
    assert not (ab_root / "CHOICE.txt").exists()


def test_run_cli_passes_generated_spec_and_propagates_exit(ab_batch, tmp_path, monkeypatch):
    _, (on_a, off, _), ab_root, final_spec, _, args = ab_batch
    write_results(ab_root, row(on_a, True, 5), row(off, False, 9))
    systemctl = tmp_path / "systemctl"
    systemctl.write_text('#!/bin/sh\n[ "$*" = "is-active -- test-ab" ] || exit 1\n'
                         'echo failed\nexit 3\n')
    runner = tmp_path / "runner"
    choice_path = str(ab_root / "CHOICE.txt")
    runner.write_text(f"#!{sys.executable}\n"
                      "import json, sys\nfrom pathlib import Path\n"
                      f"assert sys.argv[1:] == [{str(final_spec)!r}]\n"
                      "spec = json.loads(Path(sys.argv[1]).read_text())\n"
                      "assert all(job['gimbal_bridge_stabilize'] for job in spec['jobs'])\n"
                      f"assert Path({choice_path!r}).read_text() == 'choice=on\\n'\n"
                      "raise SystemExit(75)\n")
    for executable in (systemctl, runner):
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path), prepend=":")
    result = subprocess.run([sys.executable, str(Path(ab.__file__).resolve()), "run", *args,
                             "--wait-unit", "test-ab", "--runner", str(runner)], cwd=tmp_path,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 75, result.stderr
    assert result.stdout == "choice=on\n"


def test_waits_through_active_unit_states(monkeypatch):
    unit_states = iter([(3, "activating"), (0, "active"), (3, "deactivating"), (4, "unknown")])
    waits = []

    def status(command, **kwargs):
        rc, state = next(unit_states)
        return subprocess.CompletedProcess(command, rc, stdout=state, stderr="")

    monkeypatch.setattr(ab.subprocess, "run", status)
    monkeypatch.setattr(ab.time, "sleep", waits.append)
    ab.wait_for_unit("test-ab")
    assert len(waits) == 3
