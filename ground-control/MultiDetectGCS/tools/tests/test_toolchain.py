from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_compile_commands_symlink_is_gated_by_host_platform() -> None:
    toolchain = (REPO_ROOT / "cmake" / "Toolchain.cmake").read_text(encoding="utf-8")

    assert "CMAKE_EXPORT_COMPILE_COMMANDS AND NOT CMAKE_HOST_WIN32" in toolchain
    assert "CMAKE_EXPORT_COMPILE_COMMANDS AND NOT WIN32" not in toolchain
