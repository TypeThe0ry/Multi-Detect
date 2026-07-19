from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "download_public_pedestrian_tracking.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("download_public_pedestrian_tracking", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _entry(module, name: str):
    return module.RemoteZipEntry(
        name=name,
        method=0,
        compressed_size=1,
        uncompressed_size=1,
        local_header_offset=0,
        flag_bits=0,
    )


def test_public_pedestrian_downloader_selects_requested_contiguous_frame_range() -> None:
    module = _load_module()
    entries = (
        _entry(module, "PedestrianTracking/img1/000003.png"),
        _entry(module, "PedestrianTracking/gt/gt.txt"),
        _entry(module, "PedestrianTracking/img1/000001.png"),
        _entry(module, "PedestrianTracking/img1/000002.png"),
    )

    selected = module._select_frame_entries(entries, frame_start=2, frame_end=3)

    assert [item.name for item in selected] == [
        "PedestrianTracking/img1/000002.png",
        "PedestrianTracking/img1/000003.png",
    ]
    assert module._frame_number(selected[-1]) == 3


def test_public_pedestrian_downloader_reads_sequence_fps(tmp_path: Path) -> None:
    module = _load_module()
    sequence = tmp_path / "seqinfo.ini"
    sequence.write_text("[Sequence]\nframeRate=1\n", encoding="utf-8")

    assert module._sequence_frame_rate(sequence) == pytest.approx(1.0)


def test_public_pedestrian_downloader_rejects_unsafe_archive_paths() -> None:
    module = _load_module()

    with pytest.raises(RuntimeError, match="unsafe"):
        module._validate_archive_name("../outside.png")
