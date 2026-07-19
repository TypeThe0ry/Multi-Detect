#!/usr/bin/env python3
"""Fetch a reproducible public pedestrian-tracking sequence without downloading its full ZIP.

The MathWorks PedestrianTrackingDataset archive is a MOT-style frame sequence with
person identities.  The archive supports HTTP byte ranges, so this helper retrieves
only the selected PNG frames plus ``gt.txt`` and ``seqinfo.ini``, then writes a
local MP4 for ordinary recorded-video tooling.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import requests

DEFAULT_SOURCE_URL = "https://ssd.mathworks.com/supportfiles/vision/data/PedestrianTrackingDataset.zip"
_EOCD_SIGNATURE = b"PK\x05\x06"
_CENTRAL_SIGNATURE = b"PK\x01\x02"
_LOCAL_SIGNATURE = b"PK\x03\x04"
_FRAME_NAME = re.compile(r"^PedestrianTracking/img1/(?P<frame>\d{6})\.png$")


@dataclass(frozen=True, slots=True)
class RemoteZipEntry:
    name: str
    method: int
    compressed_size: int
    uncompressed_size: int
    local_header_offset: int
    flag_bits: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download a public MOT-style pedestrian sequence through HTTP range requests."
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--frame-start", type=int, default=1)
    parser.add_argument("--frame-end", type=int)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--video-fps", type=float, help="override source frame rate for MP4 output")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.frame_start <= 0:
        raise ValueError("frame-start must be positive")
    if args.frame_end is not None and args.frame_end < args.frame_start:
        raise ValueError("frame-end must be greater than or equal to frame-start")
    if not 1 <= args.workers <= 16:
        raise ValueError("workers must be between 1 and 16")
    if args.video_fps is not None and args.video_fps <= 0.0:
        raise ValueError("video-fps must be positive")

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    metadata = _remote_metadata(args.source_url)
    archive_size = int(metadata["content_length"])
    entries = _read_central_directory(args.source_url, archive_size)
    by_name = {entry.name: entry for entry in entries}
    required_names = ("PedestrianTracking/gt/gt.txt", "PedestrianTracking/seqinfo.ini")
    missing = [name for name in required_names if name not in by_name]
    if missing:
        raise RuntimeError("public archive is missing required entries: " + ", ".join(missing))

    frame_entries = _select_frame_entries(
        entries,
        frame_start=args.frame_start,
        frame_end=args.frame_end,
    )
    if not frame_entries:
        raise RuntimeError("selected frame range did not match any PNG frames")

    gt_path = out / "gt.txt"
    sequence_path = out / "seqinfo.ini"
    gt_path.write_bytes(_read_entry(args.source_url, by_name[required_names[0]]))
    sequence_path.write_bytes(_read_entry(args.source_url, by_name[required_names[1]]))

    frames_dir = out / "img1"
    frames_dir.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        pending = {
            executor.submit(_download_frame, args.source_url, entry, frames_dir): entry
            for entry in frame_entries
        }
        for future in concurrent.futures.as_completed(pending):
            future.result()

    fps = args.video_fps or _sequence_frame_rate(sequence_path)
    video_path = out / "pedestrian_tracking.mp4"
    _build_video(frames_dir, video_path, fps=fps)
    files = [gt_path, sequence_path, video_path, *sorted(frames_dir.glob("*.png"))]
    document = {
        "event": "public_pedestrian_tracking_downloaded",
        "source_url": args.source_url,
        "archive": metadata,
        "frame_range": [args.frame_start, _frame_number(frame_entries[-1])],
        "frame_count": len(frame_entries),
        "video": {
            "path": str(video_path),
            "fps": fps,
            "sha256": _sha256(video_path),
        },
        "ground_truth": {"path": str(gt_path), "sha256": _sha256(gt_path)},
        "sequence": {"path": str(sequence_path), "sha256": _sha256(sequence_path)},
        "frames_sha256": {path.name: _sha256(path) for path in files if path.suffix == ".png"},
    }
    source_path = out / "source.json"
    source_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    return 0


def _remote_metadata(url: str) -> dict[str, str]:
    response = requests.head(url, headers={"User-Agent": "Multi-Detect/1.0"}, timeout=60)
    response.raise_for_status()
    content_length = response.headers.get("content-length")
    if content_length is None or not content_length.isdigit() or int(content_length) <= 0:
        raise RuntimeError("public archive did not provide a positive Content-Length")
    return {
        "content_length": content_length,
        "etag": response.headers.get("etag", ""),
        "last_modified": response.headers.get("last-modified", ""),
    }


def _read_central_directory(url: str, archive_size: int) -> tuple[RemoteZipEntry, ...]:
    trailer_start = max(0, archive_size - 131_072)
    trailer = _fetch_range(url, trailer_start, archive_size - 1)
    eocd_offset = trailer.rfind(_EOCD_SIGNATURE)
    if eocd_offset < 0 or eocd_offset + 22 > len(trailer):
        raise RuntimeError("public archive has no readable ZIP end-of-central-directory record")
    (
        _,
        _disk,
        _central_disk,
        _disk_entries,
        entry_count,
        central_size,
        central_offset,
        comment_size,
    ) = struct.unpack_from("<4s4H2LH", trailer, eocd_offset)
    if comment_size != len(trailer) - eocd_offset - 22:
        raise RuntimeError("public archive has an invalid ZIP end-of-central-directory comment")
    if entry_count == 0xFFFF or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
        raise RuntimeError("ZIP64 public archives are not supported by this downloader")
    central = _fetch_range(url, central_offset, central_offset + central_size - 1)
    entries: list[RemoteZipEntry] = []
    cursor = 0
    while cursor < len(central):
        if central[cursor : cursor + 4] != _CENTRAL_SIGNATURE:
            raise RuntimeError("public archive has an invalid ZIP central-directory entry")
        (
            _signature,
            _version_made,
            _version_needed,
            flag_bits,
            method,
            _modified_time,
            _modified_date,
            _crc,
            compressed_size,
            uncompressed_size,
            name_size,
            extra_size,
            comment_size,
            _disk_start,
            _internal_attributes,
            _external_attributes,
            local_header_offset,
        ) = struct.unpack_from("<4s6H3L5H2L", central, cursor)
        name_start = cursor + 46
        name_end = name_start + name_size
        if name_end + extra_size + comment_size > len(central):
            raise RuntimeError("public archive central-directory entry exceeds its payload")
        encoding = "utf-8" if flag_bits & 0x800 else "cp437"
        name = central[name_start:name_end].decode(encoding, errors="strict")
        _validate_archive_name(name)
        entries.append(
            RemoteZipEntry(
                name=name,
                method=method,
                compressed_size=compressed_size,
                uncompressed_size=uncompressed_size,
                local_header_offset=local_header_offset,
                flag_bits=flag_bits,
            )
        )
        cursor = name_end + extra_size + comment_size
    if len(entries) != entry_count:
        raise RuntimeError("public archive central-directory entry count is inconsistent")
    return tuple(entries)


def _select_frame_entries(
    entries: tuple[RemoteZipEntry, ...],
    *,
    frame_start: int,
    frame_end: int | None,
) -> tuple[RemoteZipEntry, ...]:
    selected = []
    for entry in entries:
        match = _FRAME_NAME.fullmatch(entry.name)
        if match is None:
            continue
        frame = int(match.group("frame"))
        if frame >= frame_start and (frame_end is None or frame <= frame_end):
            selected.append(entry)
    return tuple(sorted(selected, key=_frame_number))


def _frame_number(entry: RemoteZipEntry) -> int:
    match = _FRAME_NAME.fullmatch(entry.name)
    if match is None:
        raise ValueError("entry is not a selected pedestrian frame")
    return int(match.group("frame"))


def _download_frame(url: str, entry: RemoteZipEntry, frames_dir: Path) -> None:
    target = frames_dir / Path(entry.name).name
    payload = _read_entry(url, entry)
    if len(payload) != entry.uncompressed_size:
        raise RuntimeError(f"downloaded frame has unexpected size: {entry.name}")
    temporary = target.with_suffix(target.suffix + ".partial")
    temporary.write_bytes(payload)
    temporary.replace(target)


def _read_entry(url: str, entry: RemoteZipEntry) -> bytes:
    header = _fetch_range(url, entry.local_header_offset, entry.local_header_offset + 29)
    (
        signature,
        _version_needed,
        _flag_bits,
        method,
        _modified_time,
        _modified_date,
        _crc,
        _compressed_size,
        _uncompressed_size,
        name_size,
        extra_size,
    ) = struct.unpack("<4s5H3L2H", header)
    if signature != _LOCAL_SIGNATURE:
        raise RuntimeError(f"public archive entry has an invalid local header: {entry.name}")
    if method != entry.method:
        raise RuntimeError(f"public archive entry compression mismatch: {entry.name}")
    payload_start = entry.local_header_offset + 30 + name_size + extra_size
    if entry.compressed_size == 0:
        compressed = b""
    else:
        compressed = _fetch_range(url, payload_start, payload_start + entry.compressed_size - 1)
    if entry.method == 0:
        payload = compressed
    elif entry.method == 8:
        payload = zlib.decompress(compressed, -zlib.MAX_WBITS)
    else:
        raise RuntimeError(
            f"public archive uses unsupported ZIP compression method {entry.method}: {entry.name}"
        )
    if len(payload) != entry.uncompressed_size:
        raise RuntimeError(f"public archive entry size mismatch: {entry.name}")
    return payload


def _fetch_range(url: str, start: int, end: int) -> bytes:
    if start < 0 or end < start:
        raise ValueError("invalid HTTP range")
    response = requests.get(
        url,
        headers={"Range": f"bytes={start}-{end}", "User-Agent": "Multi-Detect/1.0"},
        timeout=120,
    )
    response.raise_for_status()
    if response.status_code != 206:
        raise RuntimeError("public archive does not honor HTTP byte-range requests")
    expected_length = end - start + 1
    if len(response.content) != expected_length:
        raise RuntimeError("public archive returned an incomplete HTTP range")
    return response.content


def _sequence_frame_rate(path: Path) -> float:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    try:
        fps = float(values["frameRate"])
    except (KeyError, ValueError) as exc:
        raise RuntimeError("public sequence metadata is missing a valid frameRate") from exc
    if fps <= 0.0:
        raise RuntimeError("public sequence frameRate must be positive")
    return fps


def _build_video(frames_dir: Path, target: Path, *, fps: float) -> None:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - dependency-specific.
        raise RuntimeError("OpenCV is required to write the public test video") from exc
    frames = sorted(frames_dir.glob("*.png"))
    if not frames:
        raise RuntimeError("cannot create a video without downloaded frames")
    first = cv2.imread(str(frames[0]), cv2.IMREAD_COLOR)
    if first is None:
        raise RuntimeError("cannot decode first downloaded public frame")
    height, width = first.shape[:2]
    writer = cv2.VideoWriter(
        str(target),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not open MP4 output for the public test video")
    try:
        for path in frames:
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None or image.shape[:2] != (height, width):
                raise RuntimeError(f"downloaded public frame is not decodable: {path.name}")
            writer.write(image)
    finally:
        writer.release()
    if not target.is_file() or target.stat().st_size <= 0:
        raise RuntimeError("OpenCV did not produce a non-empty public test video")


def _validate_archive_name(name: str) -> None:
    path = Path(name)
    if not name or path.is_absolute() or ".." in path.parts:
        raise RuntimeError("public archive includes an unsafe entry name")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
