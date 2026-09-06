from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from multidetect.release_gate_manifest import load_parity_hashes
from multidetect.release_parity import (
    CONTRACT_GROUPS,
    DEFAULT_FILE_LIST,
    PARITY_FIELDS,
    ReleaseParityError,
    build_release_parity_document,
    compute_parity,
    hash_contract,
    normalize_file_list,
    normalize_relative_path,
    normalize_source_bytes,
    serialize_release_parity,
    write_release_parity,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_MANIFEST = REPO_ROOT / "configs" / "product-version.json"


def _groups(*, order: bool = False) -> dict[str, list[str]]:
    """Create a small, complete file-list mapping for isolated tests."""

    values: dict[str, list[str]] = {}
    for index, group in enumerate(CONTRACT_GROUPS):
        names = [f"src/{group}-{index}.txt", f"src/{group}-{index}-b.txt"]
        values[group] = list(reversed(names)) if order else names
    return values


def _root_with_sources(tmp_path: Path, groups: dict[str, list[str]]) -> Path:
    root = tmp_path / "repo"
    (root / "configs").mkdir(parents=True)
    (root / "configs" / "product-version.json").write_text(
        VERSION_MANIFEST.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )
    for group_index, paths in enumerate(groups.values()):
        for path_index, relative in enumerate(paths):
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(f"{group_index}:{path_index}\r\nline\rline\n".encode())
    return root


def _create_directory_junction(link: Path, target: Path) -> bool:
    """Create a Windows junction when symlink privilege is unavailable."""

    if os.name != "nt":
        return False
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and link.is_dir()


def test_default_file_list_is_explicit_and_resolves_from_repository() -> None:
    assert tuple(DEFAULT_FILE_LIST) == CONTRACT_GROUPS
    assert all(DEFAULT_FILE_LIST[group] for group in CONTRACT_GROUPS)
    document = build_release_parity_document(REPO_ROOT, version_manifest_path=VERSION_MANIFEST)
    assert document["schema_version"] == 1
    assert document["product"] == "Multi-Detect"
    assert document["release_label"] == "0.3.0-pre.8"
    assert set(document["source_files"]) == set(CONTRACT_GROUPS)
    assert set(document["parity"]) == set(PARITY_FIELDS)
    assert "src/multidetect/approach_live.py" in document["source_files"]["px4_mission_path"]
    assert (
        "src/multidetect/fixed_wing_aim_control.py" in document["source_files"]["px4_mission_path"]
    )
    assert (
        "configs/schemas/camera-calibration.schema.json"
        in document["source_files"]["core_algorithm"]
    )
    assert "src/multidetect/terrain_dem.py" in document["source_files"]["core_algorithm"]
    assert "src/multidetect/target_image_anchor.py" in document["source_files"]["core_algorithm"]
    assert "scripts/gazebo_sensor_stream.py" in document["source_files"]["core_algorithm"]
    assert "src/multidetect/backends/gazebo_sim.py" in document["source_files"]["core_algorithm"]
    assert (
        "configs/schemas/target-event-replay-manifest.schema.json"
        in document["source_files"]["core_algorithm"]
    )
    assert (
        "configs/schemas/raw-replay-telemetry-sidecar.schema.json"
        in document["source_files"]["task_contract"]
    )
    assert (
        "configs/schemas/camera-mount-survey.schema.json"
        in document["source_files"]["task_contract"]
    )
    assert (
        "configs/schemas/camera-mount-measurement-preflight.schema.json"
        in document["source_files"]["task_contract"]
    )
    assert (
        "configs/schemas/camera-mount-equipment-calibration.schema.json"
        in document["source_files"]["task_contract"]
    )
    assert "src/multidetect/camera_mount_survey.py" in document["source_files"]["task_contract"]
    assert "src/multidetect/field_evidence_bundle.py" in document["source_files"]["task_contract"]
    assert "src/multidetect/backends/raw_replay.py" in document["source_files"]["task_contract"]
    assert "src/multidetect/replay.py" in document["source_files"]["task_contract"]
    assert "src/multidetect/operator_link.py" in document["source_files"]["task_contract"]
    assert "src/multidetect/operator_status.py" in document["source_files"]["task_contract"]
    assert "configs/schemas/local-dem-grid.schema.json" in document["source_files"]["safety_policy"]
    assert "src/multidetect/simulator/stack.py" in document["source_files"]["safety_policy"]
    assert {
        "scripts/prepare_field_link.ps1",
        "scripts/verify_field_link_address_state.py",
        "scripts/inspect_field_link_interfaces.ps1",
        "scripts/run_field_link_preflight.ps1",
        "scripts/run_field_link_preflight.py",
        "scripts/verify_field_link_preflight.py",
        "src/multidetect/field_link_preflight.py",
        "src/multidetect/field_link_address_state.py",
        "configs/schemas/field-link-address-state.schema.json",
    } <= set(document["source_files"]["safety_policy"])
    for paths in document["source_files"].values():
        assert paths == sorted(paths)
        assert all(not path.startswith(("artifacts/", "build/")) for path in paths)


def test_canonical_release_parity_document_matches_current_sources() -> None:
    """Keep the checked-in parity contract synchronized with its source files.

    The parity file is intentionally checked in so release preparation can be
    audited without regenerating it first.  That also means a source change
    must update the canonical hashes in the same change; otherwise the
    release gate could consume stale evidence while local generation reports a
    different contract.
    """

    canonical_path = REPO_ROOT / "configs" / "release-parity.json"
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    generated = build_release_parity_document(
        REPO_ROOT,
        file_list=canonical["source_files"],
        version_manifest_path=VERSION_MANIFEST,
    )
    assert canonical == generated


def test_file_order_is_canonical_and_hash_is_stable(tmp_path: Path) -> None:
    groups = _groups()
    root = _root_with_sources(tmp_path, groups)
    reversed_groups = _groups(order=True)
    first = build_release_parity_document(
        root,
        file_list=groups,
        version_manifest_path=root / "configs/product-version.json",
    )
    second = build_release_parity_document(
        root,
        file_list=reversed_groups,
        version_manifest_path=root / "configs/product-version.json",
    )
    assert first == second
    assert compute_parity(root, groups) == compute_parity(root, reversed_groups)


def test_content_change_changes_only_the_affected_contract_hash(tmp_path: Path) -> None:
    groups = _groups()
    root = _root_with_sources(tmp_path, groups)
    before = compute_parity(root, groups)
    (root / groups["core_algorithm"][0]).write_text("changed\n", encoding="utf-8", newline="\n")
    after = compute_parity(root, groups)
    assert before["core_algorithm_hash"] != after["core_algorithm_hash"]
    for field in PARITY_FIELDS:
        if field != "core_algorithm_hash":
            assert before[field] == after[field]


def test_line_endings_are_normalized_but_other_bytes_are_preserved() -> None:
    assert normalize_source_bytes(b"a\r\nb\rc\n") == b"a\nb\nc\n"
    assert normalize_source_bytes(b"\x00\xff\r\n") == b"\x00\xff\n"


@pytest.mark.parametrize(
    "value",
    (
        "../outside.txt",
        "src/../../outside.txt",
        "/absolute.txt",
        "C:/absolute.txt",
        "\\\\server\\share\\file.txt",
        "build/output.txt",
        "artifacts/evidence.json",
        "fixtures/input.json",
        "src/artifact.json",
        "src/fixture-data.txt",
        "src/build.json",
        "",
    ),
)
def test_path_escape_and_generated_source_paths_are_rejected(value: str) -> None:
    with pytest.raises(ReleaseParityError):
        normalize_relative_path(value)


def test_empty_or_unknown_file_groups_are_rejected() -> None:
    groups = _groups()
    groups["ui_contract"] = []
    with pytest.raises(ReleaseParityError, match="must not be empty"):
        normalize_file_list(groups)

    groups = _groups()
    groups["unknown"] = ["src/unknown.txt"]
    with pytest.raises(ReleaseParityError, match="unknown"):
        normalize_file_list(groups)


def test_source_path_must_exist_and_cannot_escape_via_link(tmp_path: Path) -> None:
    groups = _groups()
    root = _root_with_sources(tmp_path, groups)
    missing = _groups()
    missing["ui_contract"][0] = "src/missing.txt"
    with pytest.raises(ReleaseParityError, match="does not exist"):
        compute_parity(root, missing)

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "escape.txt"
    outside_file.write_text("outside", encoding="utf-8")
    link = root / "src" / "escape.txt"
    escaped_path = "src/escape.txt"
    try:
        link.symlink_to(outside_file)
    except (OSError, NotImplementedError):
        junction = root / "src" / "escape-dir"
        if not _create_directory_junction(junction, outside):
            pytest.skip("neither symlink nor Windows junction creation is available")
        escaped_path = "src/escape-dir/escape.txt"
    escaped = _groups()
    escaped["ui_contract"][0] = escaped_path
    with pytest.raises(ReleaseParityError, match="escapes"):
        compute_parity(root, escaped)


def test_output_path_cannot_be_a_declared_source(tmp_path: Path) -> None:
    groups = _groups()
    root = _root_with_sources(tmp_path, groups)
    groups["ui_contract"][0] = "configs/release-parity.json"
    (root / "configs" / "release-parity.json").write_text("old\n", encoding="utf-8")
    with pytest.raises(ReleaseParityError, match="output"):
        build_release_parity_document(
            root,
            file_list=groups,
            output_path="configs/release-parity.json",
        )


def test_document_serialization_and_atomic_write_are_byte_reproducible(tmp_path: Path) -> None:
    groups = _groups()
    root = _root_with_sources(tmp_path, groups)
    document = build_release_parity_document(root, file_list=groups)
    first = serialize_release_parity(document)
    second = serialize_release_parity(json.loads(first))
    assert first == second
    assert first.endswith("\n")
    assert "\r" not in first
    destination = tmp_path / "out" / "release-parity.json"
    write_release_parity(destination, document)
    assert destination.read_text(encoding="utf-8") == first
    with pytest.raises(ReleaseParityError, match="already exists"):
        write_release_parity(destination, document, overwrite=False)


def test_cli_generates_from_an_explicit_file_list(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    groups = _groups()
    root = _root_with_sources(tmp_path, groups)
    file_list = root / "configs" / "parity-files.json"
    file_list.write_text(
        json.dumps({"schema_version": 1, "source_files": groups}),
        encoding="utf-8",
    )
    output = root / "configs" / "release-parity.json"

    from multidetect.release_parity import main

    assert (
        main(
            [
                "--root",
                str(root),
                "--out",
                str(output),
                "--file-list-json",
                "configs/parity-files.json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "release parity written" in captured.err
    generated = json.loads(output.read_text(encoding="utf-8"))
    assert generated["source_files"] == {group: sorted(paths) for group, paths in groups.items()}
    assert generated["parity"] == compute_parity(root, groups)
    assert load_parity_hashes(output) == generated["parity"]


def test_hash_contract_is_independent_of_caller_path_order(tmp_path: Path) -> None:
    groups = _groups()
    root = _root_with_sources(tmp_path, groups)
    paths = groups["ui_contract"]
    assert hash_contract(root, "ui_contract", paths) == hash_contract(
        root, "ui_contract", reversed(paths)
    )
    with pytest.raises(ReleaseParityError, match="unknown contract"):
        hash_contract(root, "other", paths)


def test_direct_script_help_works_without_site_packages() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-S", "scripts/create_release_parity.py", "--help"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--file-list-json" in completed.stdout
    assert "artifact/fixture/build" in completed.stdout
