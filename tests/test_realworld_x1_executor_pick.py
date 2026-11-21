import subprocess

import pytest

from scripts.realworld import x1_executor_pick as picker


@pytest.mark.parametrize(
    ("catalog", "agy_error", "codex_exit", "expected", "diagnostic"),
    [
        ("gemini-4-neon-high\nclaude-sonnet-5-5-high", "", 0,
         "agy:claude-sonnet-5-5-high", "exact ID listed"),
        ("| claude-sonnet-5-5-higher |\n| gemini-4-neon-high |", "", 0,
         "agy:gemini-4-neon-high", "exact ID absent"),
        ("claude-sonnet-5-5-medium\ngemini-4-neon-xhigh", "", 0,
         "codex:gpt-6.2-sol", "model availability unverified"),
        ("claude-sonnet-5-5-high", "Not logged in", 0,
         "codex:gpt-6.2-sol", "authentication required or rejected"),
        ("", "quota exceeded", 0,
         "codex:gpt-6.2-sol", "quota or rate limit"),
        ("", "listen: operation not permitted", 0,
         "codex:gpt-6.2-sol", "blocked by environment permissions"),
        ("", "Not logged in", 1, None, "No X1 executor passed"),
    ],
)
def test_selection(monkeypatch, capsys, catalog, agy_error, codex_exit, expected, diagnostic):
    calls = []
    monkeypatch.setattr(picker, "gemini_model_ids", lambda: (set(), "ACP model catalog unavailable"))

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == picker.CHECK_TIMEOUT
        if argv == ("agy", "models"):
            return subprocess.CompletedProcess(argv, bool(agy_error), catalog, agy_error)
        assert argv == ("codex", "login", "status")
        return subprocess.CompletedProcess(argv, codex_exit, "", "PRIVATE_PROBE_OUTPUT")

    monkeypatch.setattr(picker.subprocess, "run", run)
    assert picker.main([]) == (0 if expected else 1)
    output = capsys.readouterr()
    assert output.out == (f"{expected}\n" if expected else "")
    assert diagnostic in output.err
    assert "PRIVATE_PROBE_OUTPUT" not in output.out + output.err
    assert calls == ([("agy", "models")] if expected and expected.startswith("agy:") else
                     [("agy", "models"), ("codex", "login", "status")])


def test_missing_cli_and_timeout(monkeypatch, capsys):
    monkeypatch.setattr(picker, "gemini_model_ids", lambda: (set(), "ACP model catalog unavailable"))
    def run(argv, **kwargs):
        if argv == ("agy", "models"):
            raise FileNotFoundError
        assert argv == ("codex", "login", "status")
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output="PRIVATE_PROBE_OUTPUT")

    monkeypatch.setattr(picker.subprocess, "run", run)
    assert picker.main([]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "CLI not found on PATH" in output.err
    assert "check timed out" in output.err
    assert "PRIVATE_PROBE_OUTPUT" not in output.err


def test_standalone_gemini_is_tried_if_agy_cannot_start(monkeypatch, capsys):
    def run(argv, **kwargs):
        if argv == ("agy", "models"):
            raise FileNotFoundError
        assert argv == ("gemini", "--acp", "--extensions", "none")
        assert '"method": "session/new"' in kwargs["input"]
        assert "session/prompt" not in kwargs["input"]
        return subprocess.CompletedProcess(argv, 0, '{"id":2,"result":{"models":{"availableModels":'
                                           '[{"modelId":"gemini-4-neon-high"}]}}}', '')

    monkeypatch.setattr(picker.subprocess, "run", run)
    assert picker.main([]) == 0
    assert capsys.readouterr().out == "gemini:gemini-4-neon-high\n"
