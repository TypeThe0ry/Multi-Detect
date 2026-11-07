from pathlib import Path

from multidetect.host_paths import executable_name, host_path
from multidetect.autonomy.onboard_policy_parts.config import llama_server_path


def test_host_path_platform_defaults_and_environment_override(monkeypatch):
    monkeypatch.delenv("TEST_HOST_PATH", raising=False)
    assert host_path(
        "TEST_HOST_PATH", r"D:\\external\\cache", Path(".tmp/cache"), platform="nt"
    ) == Path(r"D:\\external\\cache")
    assert host_path(
        "TEST_HOST_PATH", r"D:\\external\\cache", Path(".tmp/cache"), platform="posix"
    ) == Path(".tmp/cache")

    monkeypatch.setenv("TEST_HOST_PATH", "/configured/cache")
    assert host_path(
        "TEST_HOST_PATH", r"D:\\external\\cache", Path(".tmp/cache"), platform="posix"
    ) == Path("/configured/cache")


def test_llama_server_default_is_platform_specific():
    root = Path("/repo")
    assert (
        llama_server_path(root, platform="nt")
        == root / "artifacts/cache/llama-cpp/b11552/llama-server.exe"
    )
    assert (
        llama_server_path(root, platform="posix")
        == root / "artifacts/cache/llama-cpp/b11552/llama-server"
    )


def test_executable_name_adds_windows_extension_only():
    assert executable_name("llama-server", platform="nt") == "llama-server.exe"
    assert executable_name("llama-server.exe", platform="posix") == "llama-server"
