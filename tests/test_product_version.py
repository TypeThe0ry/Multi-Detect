from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import multidetect
from multidetect.product_version import ProductVersion, load_product_version

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_MANIFEST = REPO_ROOT / "configs" / "product-version.json"


def test_product_version_manifest_is_the_prerelease_source_of_truth() -> None:
    raw = json.loads(VERSION_MANIFEST.read_text(encoding="utf-8"))
    version = load_product_version(VERSION_MANIFEST)

    assert raw == {
        "schema_version": 1,
        "product": "Multi-Detect",
        "release_label": "0.3.0-pre.9",
        "numeric": "0.3.0",
        "python": "0.3.0rc9",
        "channel": "prerelease",
    }
    assert version.release_label == "0.3.0-pre.9"
    assert version.numeric == "0.3.0"
    assert version.python == "0.3.0rc9"
    assert multidetect.__version__ == version.python
    assert multidetect.PRODUCT_VERSION == version


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"release_label": "0.3.0"}, "invalid prerelease label"),
        ({"release_label": "0.3.0-pre.10"}, "prerelease sequences do not match"),
        ({"python": "0.3.0"}, "invalid Python prerelease version"),
        ({"numeric": "0.3"}, "invalid numeric version"),
        ({"channel": "nightly"}, "invalid release channel"),
    ],
)
def test_product_version_rejects_divergent_surfaces(
    updates: dict[str, object], message: str
) -> None:
    raw = json.loads(VERSION_MANIFEST.read_text(encoding="utf-8"))
    raw.update(updates)

    with pytest.raises(ValueError, match=message):
        ProductVersion.from_mapping(raw)


@pytest.mark.parametrize(
    "updates",
    [
        {"schema_version": True},
        {"numeric": 300},
        {"channel": ["prerelease"]},
        {"unexpected": "second-source"},
    ],
)
def test_product_version_rejects_type_coercion_and_unknown_fields(
    updates: dict[str, object],
) -> None:
    raw = json.loads(VERSION_MANIFEST.read_text(encoding="utf-8"))
    raw.update(updates)

    with pytest.raises(ValueError):
        ProductVersion.from_mapping(raw)


def test_python_metadata_resolves_version_dynamically_from_the_manifest() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")

    project_block = pyproject.split("[project]", 1)[1].split("[project.optional-dependencies]", 1)[
        0
    ]
    assert 'dynamic = ["version"]' in project_block
    assert re.search(r"^version\s*=", project_block, flags=re.MULTILINE) is None
    assert 'version = { attr = "multidetect.product_version.__version__" }' in pyproject
    lock_project_block = lock.split('name = "multi-detect"', 1)[1].split(
        "[package.optional-dependencies]", 1
    )[0]
    assert 'source = { editable = "." }' in lock_project_block
    assert re.search(r"^version\s*=", lock_project_block, flags=re.MULTILINE) is None
    assert 'version = "0.1.0"' not in lock


def test_qgc_uses_numeric_version_and_separate_human_release_label() -> None:
    options = (
        REPO_ROOT / "ground-control" / "MultiDetectGCS" / "cmake" / "CustomOptions.cmake"
    ).read_text(encoding="utf-8")
    overrides = (
        REPO_ROOT
        / "ground-control"
        / "MultiDetectGCS"
        / "custom"
        / "cmake"
        / "CustomOverrides.cmake"
    ).read_text(encoding="utf-8")
    git_cmake = (
        REPO_ROOT / "ground-control" / "MultiDetectGCS" / "cmake" / "modules" / "Git.cmake"
    ).read_text(encoding="utf-8")

    assert 'set(QGC_APP_RELEASE_LABEL ""' in options
    assert "configs/product-version.json" in overrides
    assert "string(JSON _multidetect_product_name GET" in overrides
    assert "string(JSON _multidetect_release_label GET" in overrides
    assert "string(JSON _multidetect_numeric_version GET" in overrides
    assert "_multidetect_version_member_count" in overrides
    assert "must contain exactly six fields" in overrides
    assert "_multidetect_version_schema_type" in overrides
    assert '"${_multidetect_numeric_version}"' in overrides
    assert '"${_multidetect_release_label}"' in overrides
    assert "MultiDetectGCS-v${_multidetect_release_label}-windows-amd64.exe" in overrides
    assert "_multidetect_label_sequence" in overrides
    assert "_multidetect_python_sequence" in overrides
    assert 'set(QGC_APP_VERSION "${QGC_APP_VERSION_OVERRIDE}")' in git_cmake
    assert 'set(QGC_APP_VERSION_STR "v${QGC_APP_RELEASE_LABEL}-${QGC_GIT_HASH}")' in git_cmake


def test_windows_build_paths_use_label_and_skip_configure_checks_cache() -> None:
    build_script = (REPO_ROOT / "scripts" / "build_multidetect_windows.ps1").read_text(
        encoding="utf-8"
    )

    assert "Read-ProductVersionManifest" in build_script
    assert "configs\\product-version.json" in build_script
    assert "$Version = $productVersion.ReleaseLabel" in build_script
    assert "$numericVersion = $productVersion.Numeric" in build_script
    assert '"build\\windows-v{0}-{1}" -f $Version' in build_script
    assert '"portable-v{0}" -f $Version' in build_script
    assert '"$Configuration\\MultiDetectGCS.exe"' in build_script
    assert "[string]$ReleaseGateManifest = " in build_script
    assert "[switch]$RequireReleaseGate" in build_script
    assert "$productVersion.Channel -eq 'release'" in build_script
    assert "E7 release-gate evidence is incomplete" in build_script
    assert "Final release is blocked by E7 release gate" in build_script
    assert "'--report-only'" in build_script
    assert "contains unsupported field(s)" in build_script
    assert "schema_version must be an integer" in build_script
    assert "must be a non-empty string" in build_script
    assert "Patch      : $patchExe" in build_script
    assert 'call "$qtCmake" -S "$sourceDir"' in build_script
    assert '"-DPATCH_EXECUTABLE=$patchExe"' in build_script
    assert '"-DCMAKE_ASM_COMPILER=ml64.exe"' in build_script
    assert "$env:USERPROFILE\\Qt\\6.11.1\\msvc2022_64" in build_script
    assert "Get-ChildItem -LiteralPath $userProfilesRoot -Directory" in build_script
    assert "Users\\admin" not in build_script
    assert "function Get-NativeExitCode" in build_script
    assert "Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue" in build_script
    assert "[int]$LASTEXITCODE" not in build_script
    assert "$LASTEXITCODE -ne 0" not in build_script
    assert "function Get-Sha256Hex" in build_script
    assert "SHA256]::Create()" in build_script
    assert "Get-FileHash" not in build_script
    assert "Assert-CMakeCacheValue -CachePath $cachePath" in build_script
    for cache_key in (
        "CMAKE_HOME_DIRECTORY",
        "CMAKE_BUILD_TYPE",
        "QGC_APP_VERSION_OVERRIDE",
        "QGC_APP_RELEASE_LABEL",
        "MULTIDETECT_PRODUCT_VERSION_MANIFEST",
        "PATCH_EXECUTABLE",
    ):
        assert f"-Name '{cache_key}'" in build_script
