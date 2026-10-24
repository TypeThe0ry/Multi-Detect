import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "golden_checker", ROOT / "scripts" / "check_golden_replay.py"
)
checker = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(checker)


def test_checker_reports_monkeypatched_hold_duration_mutation(monkeypatch, tmp_path):
    from multidetect.sim_teacher.primitives import ActionPrimitive

    episode = tmp_path / "count-cones" / "campaign" / "episode-0000"
    episode.mkdir(parents=True)
    fixture = ROOT / "tests" / "fixtures" / "onboard_replay"
    (episode / "episode.json").write_bytes((fixture / "episode.json").read_bytes())
    (episode / "steps.jsonl").write_bytes((fixture / "steps.jsonl").read_bytes())
    digest = checker.sha256_steps(episode)
    original = checker.fingerprint(checker.replay(episode))
    monkeypatch.setattr(checker, "MANIFEST", tmp_path / "manifest.json")
    monkeypatch.setattr(checker, "BASELINE", tmp_path / "baseline.json")
    checker.MANIFEST.write_text(
        __import__("json").dumps(
            {"episodes": [{"id": "fixture", "path": "unused", "sha256": digest}]}
        ),
        encoding="utf-8",
    )
    checker.BASELINE.write_text(
        __import__("json").dumps({"episodes": {"fixture": {"fingerprint": original}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(checker, "resolve_episode", lambda *_args, **_kwargs: episode)
    hold = ActionPrimitive.hold.__func__

    def altered_hold(cls, seconds):
        return hold(cls, seconds + 0.5)

    monkeypatch.setattr(ActionPrimitive, "hold", classmethod(altered_hold))
    code, summary = checker.check()
    assert code == 1
    assert summary["results"][0]["divergence"]["type"] == "action_changed"


def test_all_missing_data_is_a_warning_and_success(monkeypatch):
    monkeypatch.setattr(
        checker, "resolve_episode", lambda *_args, **_kwargs: Path("missing-golden")
    )
    code, summary = checker.check()
    assert code == 0
    assert summary["missing"] == summary["episodes"]
    assert all("缺少架次数据" in row["message"] for row in summary["results"])


@pytest.mark.optional
def test_golden_replay_is_optional_when_data_is_absent(capsys):
    manifest = checker.load_manifest()
    paths = [checker.resolve_episode(row["path"]) for row in manifest["episodes"]]
    if any(not (path / "steps.jsonl").is_file() for path in paths):
        pytest.skip("golden benchmark data is not installed")
    code, summary = checker.check()
    assert code == 0, summary
