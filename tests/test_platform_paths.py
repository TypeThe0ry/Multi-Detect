from pathlib import Path

from scripts.platform_paths import default_llama_server, pythonpath_value


def test_pythonpath_uses_native_separator():
    value = pythonpath_value(Path("/repo"))
    assert value == "/repo/src:/repo"


def test_llama_server_environment_override(monkeypatch, tmp_path):
    configured = tmp_path / "custom-llama-server"
    monkeypatch.setenv("LLAMA_SERVER", str(configured))
    assert default_llama_server(tmp_path) == configured


def test_llama_server_prefers_linux_binary(monkeypatch, tmp_path):
    monkeypatch.delenv("LLAMA_SERVER", raising=False)
    cache = tmp_path / "artifacts/cache/llama-cpp/b11552"
    cache.mkdir(parents=True)
    binary = cache / "llama-server"
    binary.touch()
    (cache / "llama-server.exe").touch()
    assert default_llama_server(tmp_path) == binary
