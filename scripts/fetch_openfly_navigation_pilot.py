#!/usr/bin/env python3
"""Create-only, bounded import of original OpenFly TRAIN observations/planned actions.

This does not run OpenFly's future-history dataset builder, normalize action gold,
infer sensor time from image filenames, download weights, or authorize execution.
Raw pose, annotation, RGB and pinned source code are retained independently.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urljoin, urlsplit

import requests

DATASET = "IPEC-COMMUNITY/OpenFly"
REVISION = "a12316d56a4e35a32ad626fb725ed7089937a1c4"
CODE_REPOSITORY = "SHAILAB-IPEC/OpenFly-Platform"
CODE_REVISION = "c075075497a7122bad82f5b76b9be926ad5a81b3"
ANNOTATION_BYTES = 405_815_796
ANNOTATION_SHA256 = "1a4c5a5ed1561ea5b3b79b6c25da7648ce4085b7d8489d62022a9bd1244100df"
MAX_ANNOTATION_BYTES = 450 * 1024 * 1024
MAX_PILOT_BYTES = 128 * 1024 * 1024
MAX_IMAGES = 256
MAX_EPISODES = 3
MAX_METADATA_CANDIDATES = 32
MAX_METADATA_BYTES = 2 * 1024 * 1024
CODE_PATHS = (
    "tool_ws/src/traj_gen/include/navi/pathsearch.hpp",
    "tool_ws/src/traj_gen/src/traj_gen.cc",
    "scripts/sim/airsim_bridge.py",
)
CODE_LICENSE_PATHS = ("LICENSE",)
DATASET_CARD_PATH = "README.md"
_ANNOTATION_KEYS = {"image_path", "gpt_instruction", "action", "index_list", "pos", "yaw"}
_INDEX = re.compile(r"[0-9]{8}_[0-9]{6}_([0-9]+)\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEADERS = {
    "content-length",
    "content-type",
    "etag",
    "last-modified",
    "x-repo-commit",
    "x-linked-etag",
    "x-linked-size",
    "content-encoding",
}


class PilotError(ValueError):
    """Source or bounded import contract failed closed."""


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PilotError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise PilotError(f"non-finite JSON constant: {value}")


_DECODER = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_constant)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
        )
        + "\n"
    ).encode("utf-8")


def _repository_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "%" in value
        or ":" in value
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise PilotError("unsafe repository path")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(p in ("", ".", "..") for p in value.split("/")):
        raise PilotError("unsafe repository path")
    return value


def dataset_url(repository_path: str) -> str:
    path = _repository_path(repository_path)
    if path not in ("Annotation/train.json", DATASET_CARD_PATH) and not path.startswith("Image/"):
        raise PilotError(
            "only original train annotation, pinned card and episode Image paths allowed"
        )
    return f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/{quote(path)}"


def _allowed_url(url: str) -> None:
    parsed = urlsplit(url)
    hosts = {
        "huggingface.co",
        "raw.githubusercontent.com",
        "api.github.com",
        "us.aws.cdn.hf.co",
        "eu.aws.cdn.hf.co",
        "cas-bridge.xethub.hf.co",
        "cdn-lfs.hf.co",
        "cdn-lfs-us-1.hf.co",
    }
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise PilotError("unapproved HTTPS source/redirect host")


def _response_headers(headers: Mapping) -> dict[str, str]:
    # Signed redirect URLs/cookies/authorization are intentionally NOT persisted.
    return {str(k).lower(): str(v) for k, v in headers.items() if str(k).lower() in _HEADERS}


class OfficialClient:
    def __init__(self) -> None:
        self.session = requests.Session()

    def stream(self, url: str):
        trail = []
        for _ in range(6):
            _allowed_url(url)
            response = self.session.get(
                url,
                stream=True,
                allow_redirects=False,
                timeout=(15, 60),
                headers={"Accept-Encoding": "identity", "User-Agent": "Multi-Detect-pilot/1"},
            )
            trail.append(
                {"host": urlsplit(url).hostname, "headers": _response_headers(response.headers)}
            )
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise PilotError("redirect missing Location")
                url = urljoin(url, location)
                continue
            response.raise_for_status()
            if response.status_code != 200:
                response.close()
                raise PilotError("full file retrieval requires HTTP 200")
            return response, trail
        raise PilotError("too many redirects")

    def metadata(self, url: str) -> tuple[object, bytes, list]:
        response, trail = self.stream(url)
        try:
            pieces, total = [], 0
            for chunk in response.iter_content(65536):
                total += len(chunk)
                if total > MAX_METADATA_BYTES:
                    raise PilotError("metadata exceeds size cap")
                pieces.append(chunk)
            raw = b"".join(pieces)
            return _DECODER.decode(raw.decode("utf-8")), raw, trail
        finally:
            response.close()

    def tree(self, path: str) -> tuple[list, bytes, list]:
        path = _repository_path(path)
        url = (
            f"https://huggingface.co/api/datasets/{DATASET}/tree/{REVISION}/"
            f"{quote(path)}?limit=1000"
        )
        document, raw, trail = self.metadata(url)
        if not isinstance(document, list) or len(document) >= 1000:
            raise PilotError("bounded episode tree must be complete, without pagination")
        return document, raw, trail

    def root_tree(self) -> tuple[list, bytes, list]:
        document, raw, trail = self.metadata(
            f"https://huggingface.co/api/datasets/{DATASET}/tree/{REVISION}?limit=1000"
        )
        if not isinstance(document, list) or len(document) >= 1000:
            raise PilotError("bounded dataset root tree must be complete, without pagination")
        return document, raw, trail


def _file_binding(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path.resolve()), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def _create_bytes(path: Path, raw: bytes) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return _file_binding(path)


def _metadata_entry(entry: Mapping) -> tuple[int, str, str]:
    size, oid = entry.get("size"), entry.get("oid")
    if type(size) is not int or size <= 0 or not isinstance(oid, str) or not _HEX40.fullmatch(oid):
        raise PilotError("invalid official file metadata")
    lfs = entry.get("lfs")
    if lfs is not None:
        if (
            not isinstance(lfs, dict)
            or lfs.get("size") != size
            or not isinstance(lfs.get("oid"), str)
            or not _HEX64.fullmatch(lfs["oid"])
        ):
            raise PilotError("invalid official LFS metadata")
        return size, lfs["oid"], "sha256-lfs"
    return size, oid, "sha1-git-blob"


def download_file(
    client: OfficialClient,
    url: str,
    path: Path,
    *,
    size: int,
    expected: str,
    digest_kind: str,
    cap: int,
    cache: Path | None = None,
) -> dict:
    _allowed_url(url)
    parsed_url = urlsplit(url)
    prefix = f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/"
    code_urls = {
        f"https://raw.githubusercontent.com/{CODE_REPOSITORY}/{CODE_REVISION}/{p}"
        for p in (*CODE_PATHS, *CODE_LICENSE_PATHS)
    }
    code_prefix = f"https://raw.githubusercontent.com/{CODE_REPOSITORY}/{CODE_REVISION}/"
    if url.startswith(code_prefix) and re.fullmatch(
        r"configs/env_[A-Za-z0-9_]+\.yaml", url[len(code_prefix) :]
    ):
        code_urls.add(url)
    if parsed_url.query or (
        url not in code_urls
        and (not url.startswith(prefix) or dataset_url(url[len(prefix) :]) != url)
    ):
        raise PilotError("initial download must use pinned dataset/code path")
    if type(size) is not int or not 0 < size <= cap:
        raise PilotError("file exceeds authorized size cap")
    if path.exists() or path.with_suffix(path.suffix + ".partial").exists():
        raise FileExistsError(path)
    if digest_kind not in ("sha256-lfs", "sha1-git-blob"):
        raise PilotError("unknown digest kind")
    digest = hashlib.sha256() if digest_kind == "sha256-lfs" else hashlib.sha1()
    if digest_kind == "sha1-git-blob":
        digest.update(b"blob " + str(size).encode("ascii") + b"\0")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    print(
        json.dumps({"event": "source_file_started", "file": path.name, "expected_bytes": size}),
        flush=True,
    )
    response = None
    cached_stream = None
    trail = []
    try:
        if cache is not None:
            if cache.is_symlink() or not cache.is_file() or cache.stat().st_size != size:
                raise PilotError("invalid annotation cache")
            cached_stream = cache.open("rb")
            chunks = iter(lambda: cached_stream.read(1024 * 1024), b"")
        else:
            response, trail = client.stream(url)
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) != size):
                raise PilotError("Content-Length differs from pinned metadata")
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise PilotError("unexpected content encoding")
            chunks = response.iter_content(1024 * 1024)
        count = 0
        progress_threshold = 32 * 1024 * 1024
        with partial.open("xb") as target:
            for chunk in chunks:
                count += len(chunk)
                if count > size or count > cap:
                    raise PilotError("stream exceeds authorized size cap")
                digest.update(chunk)
                target.write(chunk)
                if count >= progress_threshold:
                    print(
                        json.dumps(
                            {"event": "source_file_progress", "file": path.name, "bytes": count}
                        ),
                        flush=True,
                    )
                    progress_threshold += 32 * 1024 * 1024
            target.flush()
            os.fsync(target.fileno())
        if count != size or digest.hexdigest() != expected:
            raise PilotError("download/cache size or official digest mismatch")
        # Publish without replacing another actor's output; failed .partial is retained.
        os.link(partial, path)
        partial.unlink()
        record = _file_binding(path)
        record.update(
            {
                "source_url": url,
                "official_digest": expected,
                "official_digest_kind": digest_kind,
                "response_chain": trail,
                "cache_source": _file_binding(cache) if cache is not None else None,
            }
        )
        print(
            json.dumps({"event": "source_file_verified", "file": path.name, "bytes": count}),
            flush=True,
        )
        return record
    finally:
        if response is not None:
            response.close()
        if cached_stream is not None:
            cached_stream.close()


def iter_annotation_records(path: Path) -> Iterator[tuple[int, dict]]:
    """Parse the pinned top-level array once, bounded memory, no future-history construction."""
    with path.open("r", encoding="utf-8") as stream:
        buffer, eof = "", False

        def fill() -> None:
            nonlocal buffer, eof
            chunk = stream.read(65536)
            buffer += chunk
            eof = not chunk

        def trim() -> None:
            nonlocal buffer
            while True:
                buffer = buffer.lstrip()
                if buffer or eof:
                    return
                fill()

        trim()
        if not buffer.startswith("["):
            raise PilotError("annotation must be a top-level array")
        buffer = buffer[1:]
        index = 0
        trim()
        while not buffer.startswith("]"):
            while True:
                try:
                    record, end = _DECODER.raw_decode(buffer)
                    break
                except json.JSONDecodeError as exc:
                    if eof or len(buffer) > MAX_METADATA_BYTES:
                        raise PilotError("invalid/truncated/oversized annotation record") from exc
                    fill()
            if not isinstance(record, dict):
                raise PilotError("annotation record must be an object")
            yield index, record
            index += 1
            buffer = buffer[end:]
            trim()
            if buffer.startswith(","):
                buffer = buffer[1:]
                trim()
                if buffer.startswith("]"):
                    raise PilotError("annotation trailing comma")
            elif not buffer.startswith("]"):
                raise PilotError("annotation missing array delimiter")
        buffer = buffer[1:]
        trim()
        if buffer:
            raise PilotError("annotation trailing bytes")


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _position(value: object) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(_number(x) for x in value)


def validate_annotation(record: dict) -> list[int]:
    if set(record) != _ANNOTATION_KEYS:
        raise PilotError("unexpected annotation schema")
    _repository_path(record["image_path"])
    if not record["image_path"].startswith("env_") or len(record["image_path"].split("/")) != 4:
        raise PilotError("unexpected episode path")
    if not isinstance(record["gpt_instruction"], str) or not record["gpt_instruction"].strip():
        raise PilotError("missing original instruction")
    arrays = [record[key] for key in ("action", "index_list", "pos", "yaw")]
    if (
        not all(isinstance(value, list) for value in arrays)
        or not arrays[0]
        or len({len(value) for value in arrays}) != 1
    ):
        raise PilotError("annotation parallel lists do not align")
    if any(type(x) is not int or not 0 <= x <= 9 for x in record["action"]):
        raise PilotError("unknown original action code")
    if (
        record["action"][-1] != 0
        or 0 in record["action"][:-1]
        or not all(_position(x) for x in record["pos"])
        or not all(_number(x) for x in record["yaw"])
    ):
        raise PilotError("invalid terminal STOP or non-finite pose/yaw")
    ids = []
    for name in record["index_list"]:
        match = _INDEX.fullmatch(name) if isinstance(name, str) else None
        if match is None:
            raise PilotError("unsafe/unknown image index")
        ids.append(int(match[1]))
    if any(b <= a for a, b in zip(ids, ids[1:], strict=False)):
        raise PilotError("annotation imageid order must strictly increase")
    return ids


def parse_pose(raw: bytes) -> tuple[list[dict], dict]:
    text = raw.decode("utf-8")
    records, cursor = [], 0
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor == len(text):
            break
        try:
            value, cursor = _DECODER.raw_decode(text, cursor)
        except json.JSONDecodeError as exc:
            raise PilotError("invalid concatenated pose JSON") from exc
        records.append(value)
    if (
        len(records) < 2
        or not isinstance(records[-1], dict)
        or set(records[-1]) != {"aim_landmark"}
    ):
        raise PilotError("pose must retain final offline-only aim_landmark")
    actions = []
    for index, wrapper in enumerate(records[:-1]):
        if not isinstance(wrapper, dict) or set(wrapper) != {"action"}:
            raise PilotError("invalid pose action wrapper")
        action = wrapper["action"]
        if (
            not isinstance(action, dict)
            or set(action) != {"imageid", "pos", "type", "value", "yaw"}
            or type(action["imageid"]) is not int
            or action["imageid"] != index
            or not _position(action["pos"])
            or not _number(action["yaw"])
            or not _number(action["value"])
            or not isinstance(action["type"], str)
        ):
            raise PilotError("invalid/out-of-order/non-finite raw pose")
        actions.append(action)
    if (
        actions[-1]["type"] != "stop"
        or actions[-1]["value"] != 0
        or any(a["type"] == "stop" for a in actions[:-1])
    ):
        raise PilotError("complete raw episode must end in original STOP")
    return actions, records[-1]


def episode_metadata(tree: list, image_path: str) -> tuple[dict, list[tuple[int, dict]]]:
    prefix = "Image/" + _repository_path(image_path) + "/"
    pose = None
    images = []
    for entry in tree:
        if not isinstance(entry, dict) or entry.get("type") != "file":
            raise PilotError("unexpected episode tree entry")
        path = _repository_path(entry["path"])
        if not path.startswith(prefix) or "/" in path[len(prefix) :]:
            raise PilotError("episode tree escapes selected train episode")
        _metadata_entry(entry)
        basename = path[len(prefix) :]
        if basename == "pose.jsonl":
            if pose is not None:
                raise PilotError("duplicate pose file")
            pose = entry
        elif basename.endswith(".png") and (match := _INDEX.fullmatch(basename[:-4])):
            images.append((int(match[1]), entry))
        else:
            raise PilotError("unknown episode payload")
    images.sort(key=lambda item: item[0])
    if pose is None or not images or [i for i, _ in images] != list(range(len(images))):
        raise PilotError("complete episode requires all contiguous raw RGB imageids and pose")
    return pose, images


def align_episode(annotation: dict, actions: list[dict], images: list[tuple[int, dict]]) -> dict:
    ids = validate_annotation(annotation)
    if len(actions) != len(images) or ids[-1] != len(actions) - 1:
        raise PilotError("raw RGB/pose/annotation terminal alignment mismatch")
    mismatches = []
    for step, imageid in enumerate(ids):
        if imageid >= len(actions):
            raise PilotError("annotation imageid absent from raw episode")
        if images[imageid][1]["path"].rsplit("/", 1)[1] != annotation["index_list"][step] + ".png":
            raise PilotError("annotation filename does not exactly match raw RGB imageid")
        if (
            actions[imageid]["pos"] != annotation["pos"][step]
            or actions[imageid]["yaw"] != annotation["yaw"][step]
        ):
            mismatches.append({"annotation_step": step, "raw_imageid": imageid})
    transitions = []
    for before, after in zip(actions, actions[1:], strict=False):
        delta = [b - a for a, b in zip(before["pos"], after["pos"], strict=True)]
        dyaw = math.atan2(
            math.sin(after["yaw"] - before["yaw"]), math.cos(after["yaw"] - before["yaw"])
        )
        transitions.append(
            {
                "imageid": before["imageid"],
                "next_imageid": after["imageid"],
                "raw_type": before["type"],
                "raw_value": before["value"],
                "observed_native_position_delta": delta,
                "observed_native_yaw_delta": dyaw,
            }
        )
    return {
        "annotation_image_ids": ids,
        "pose_image_ids": list(range(len(actions))),
        "annotation_pose_yaw_exact_match": not mismatches,
        "annotation_pose_yaw_mismatches": mismatches,
        "raw_transitions": transitions,
        "aggregate_action_alignment_verified": False,
        "position_unit": "unknown_not_normalized",
        "yaw_unit": "native_not_normalized",
        "timestamps_s": None,
        "dt_s": None,
        "note": "Filename timestamps are identifiers only; raw deltas are diagnostics, not gold.",
    }


def _rgb_info(path: Path) -> dict:
    from PIL import Image

    with Image.open(path) as image:
        if image.format != "PNG" or image.mode != "RGB" or image.width * image.height > 16_777_216:
            raise PilotError("unexpected RGB PNG payload or excessive decoded pixels")
        image.load()
        return {"encoding": "png", "mode": "RGB", "width": image.width, "height": image.height}


def fetch_pilot(
    output_dir: Path,
    *,
    episodes: int = 3,
    annotation_cache: Path | None = None,
    client: OfficialClient | None = None,
) -> dict:
    if type(episodes) is not int or not 1 <= episodes <= MAX_EPISODES:
        raise PilotError("episodes must be 1..3")
    out = output_dir.absolute()
    if out.exists() or out.is_symlink():
        raise FileExistsError("output directory must be new")
    owned_sources = [
        Path(__file__).resolve(),
        Path(__file__).resolve().parents[1] / "tests/test_fetch_openfly_navigation_pilot.py",
    ]
    source_files = [_file_binding(path) for path in owned_sources]
    client = client or OfficialClient()
    out.mkdir(parents=True, exist_ok=False)
    dataset_root, root_raw, _ = client.root_tree()
    cards = [e for e in dataset_root if e.get("path") == DATASET_CARD_PATH]
    if len(cards) != 1 or cards[0].get("type") != "file":
        raise PilotError("pinned dataset card missing")
    card_size, card_digest, card_kind = _metadata_entry(cards[0])
    metadata_records = [_create_bytes(out / "metadata/dataset-root-tree.json", root_raw)]
    card = download_file(
        client,
        dataset_url(DATASET_CARD_PATH),
        out / "license/dataset/README.md",
        size=card_size,
        expected=card_digest,
        digest_kind=card_kind,
        cap=MAX_METADATA_BYTES,
    )
    card["repository_path"] = DATASET_CARD_PATH
    card["repository"] = DATASET
    card["revision"] = REVISION
    card_raw = Path(card["path"]).read_bytes()
    if card_raw != b"---\nlicense: apache-2.0\n---\n":
        # Preserve a source declaration, not an invented license equivalence.
        if not re.fullmatch(rb"---\r?\nlicense: apache-2\.0\r?\n---\r?\n?", card_raw):
            raise PilotError("pinned dataset card does not declare expected apache-2.0")
    annotation_tree, tree_raw, _ = client.tree("Annotation")
    entries = [e for e in annotation_tree if e.get("path") == "Annotation/train.json"]
    if len(entries) != 1 or _metadata_entry(entries[0]) != (
        ANNOTATION_BYTES,
        ANNOTATION_SHA256,
        "sha256-lfs",
    ):
        raise PilotError("pinned original train annotation identity changed")
    metadata_records.append(_create_bytes(out / "metadata/annotation-tree.json", tree_raw))
    annotation = download_file(
        client,
        dataset_url("Annotation/train.json"),
        out / "raw/Annotation/train.json",
        size=ANNOTATION_BYTES,
        expected=ANNOTATION_SHA256,
        digest_kind="sha256-lfs",
        cap=MAX_ANNOTATION_BYTES,
        cache=annotation_cache,
    )
    annotation["repository_path"] = "Annotation/train.json"
    selected = []
    selected_paths = set()
    skipped_non_airsim_records = 0
    total = 0
    for index, record in iter_annotation_records(Path(annotation["path"])):
        total += 1
        if not str(record.get("image_path", "")).startswith("env_airsim_"):
            skipped_non_airsim_records += 1
            continue
        if (
            len(selected) < MAX_METADATA_CANDIDATES
            and record.get("image_path") not in selected_paths
        ):
            validate_annotation(record)
            selected.append((index, record))
            selected_paths.add(record["image_path"])
    if len(selected) < episodes:
        raise PilotError("original train lacks requested distinct AirSim candidate episodes")
    # Fetch metadata for whole episodes BEFORE downloading RGB; never truncate an episode.
    plans = []
    rejected_records = []
    for index, record in selected:
        tree, raw, _ = client.tree("Image/" + record["image_path"])
        tree_binding = _create_bytes(out / f"metadata/episode-{index}-tree.json", raw)
        metadata_records.append(tree_binding)
        try:
            pose, images = episode_metadata(tree, record["image_path"])
        except PilotError as exc:
            rejected_records.append(
                {
                    "annotation_index": index,
                    "image_path": record["image_path"],
                    "reason": str(exc),
                    "metadata": tree_binding,
                }
            )
            continue
        plans.append((index, record, pose, images))
        if len(plans) == episodes:
            break
    if len(plans) != episodes:
        raise PilotError("candidate budget lacks requested complete original train AirSim episodes")
    image_count = sum(len(images) for _, _, _, images in plans)
    pilot_bytes = sum(
        pose["size"] + sum(e["size"] for _, e in images) for _, _, pose, images in plans
    )
    if image_count > MAX_IMAGES or pilot_bytes > MAX_PILOT_BYTES:
        raise PilotError(
            "complete train prefix exceeds authorized RGB/pose budget; use fewer episodes"
        )
    code_api = (
        f"https://api.github.com/repos/{CODE_REPOSITORY}/git/trees/{CODE_REVISION}?recursive=1"
    )
    code_tree, code_raw, _ = client.metadata(code_api)
    if (
        not isinstance(code_tree, dict)
        or code_tree.get("sha") != CODE_REVISION
        or code_tree.get("truncated") is not False
    ):
        raise PilotError("pinned official code tree identity mismatch")
    metadata_records.append(_create_bytes(out / "metadata/code-tree.json", code_raw))
    code_sources = []
    generation_paths = list(CODE_PATHS) + sorted(
        {"configs/" + record["image_path"].split("/")[0] + ".yaml" for _, record, _, _ in plans}
    )
    for repository_path in generation_paths:
        matches = [e for e in code_tree["tree"] if e.get("path") == repository_path]
        if len(matches) != 1 or matches[0].get("type") != "blob":
            raise PilotError("pinned generation code missing")
        entry = matches[0]
        size, digest, kind = _metadata_entry({"size": entry["size"], "oid": entry["sha"]})
        url = (
            f"https://raw.githubusercontent.com/{CODE_REPOSITORY}/{CODE_REVISION}/{repository_path}"
        )
        code_source = download_file(
            client,
            url,
            out / "code" / repository_path,
            size=size,
            expected=digest,
            digest_kind=kind,
            cap=MAX_METADATA_BYTES,
        )
        code_source["repository_path"] = repository_path
        code_sources.append(code_source)
    license_sources = [card]
    for repository_path in CODE_LICENSE_PATHS:
        matches = [e for e in code_tree["tree"] if e.get("path") == repository_path]
        if len(matches) != 1 or matches[0].get("type") != "blob":
            raise PilotError("pinned code license missing")
        entry = matches[0]
        size, digest, kind = _metadata_entry({"size": entry["size"], "oid": entry["sha"]})
        license_source = download_file(
            client,
            f"https://raw.githubusercontent.com/{CODE_REPOSITORY}/{CODE_REVISION}/{repository_path}",
            out / "license/code" / repository_path,
            size=size,
            expected=digest,
            digest_kind=kind,
            cap=MAX_METADATA_BYTES,
        )
        license_source.update(
            {
                "repository_path": repository_path,
                "repository": CODE_REPOSITORY,
                "revision": CODE_REVISION,
            }
        )
        license_sources.append(license_source)
    imported = []
    for index, record, pose_entry, image_entries in plans:
        size, digest, kind = _metadata_entry(pose_entry)
        pose = download_file(
            client,
            dataset_url(pose_entry["path"]),
            out / "raw" / pose_entry["path"],
            size=size,
            expected=digest,
            digest_kind=kind,
            cap=MAX_PILOT_BYTES,
        )
        pose["repository_path"] = pose_entry["path"]
        actions, _ = parse_pose(Path(pose["path"]).read_bytes())
        alignment = align_episode(record, actions, image_entries)
        extracted = _create_bytes(out / f"selected/annotation-{index}.json", _canonical(record))
        extracted["representation"] = "canonical-extracted-not-original-byte-range"
        images = []
        for imageid, entry in image_entries:
            size, digest, kind = _metadata_entry(entry)
            image = download_file(
                client,
                dataset_url(entry["path"]),
                out / "raw" / entry["path"],
                size=size,
                expected=digest,
                digest_kind=kind,
                cap=MAX_PILOT_BYTES,
            )
            image.update(
                {
                    "repository_path": entry["path"],
                    "imageid": imageid,
                    "rgb": _rgb_info(Path(image["path"])),
                }
            )
            images.append(image)
        imported.append(
            {
                "annotation_index": index,
                "image_path": record["image_path"],
                "scene": record["image_path"].split("/")[0],
                "split": "train",
                "annotation_record": extracted,
                "pose": pose,
                "images": images,
                "alignment": alignment,
                "source_action_semantics": "planned_not_executed",
                "landmark_truth_model_input_allowed": False,
            }
        )
        print(
            json.dumps(
                {
                    "event": "complete_episode_imported",
                    "annotation_index": index,
                    "raw_rgb_count": len(images),
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
    if source_files != [_file_binding(path) for path in owned_sources]:
        raise PilotError("fetcher/test source changed before sealing")
    manifest = {
        "contract": "multidetect.openfly-navigation-pilot-v1",
        "dataset": {
            "repository": DATASET,
            "revision": REVISION,
            "split": "train",
            "license": "apache-2.0",
            "license_source": f"https://huggingface.co/datasets/{DATASET}/blob/{REVISION}/README.md",
        },
        "code": {"repository": CODE_REPOSITORY, "revision": CODE_REVISION},
        "selection": {
            "method": "first_complete_distinct_airsim_raw_episodes_in_original_train_file_order",
            "eligibility": "env_airsim_*; only this renderer currently has reviewed source units",
            "selection_not_conditioned_on": ["model_results", "action_labels", "prompt_targets"],
            "skipped_non_airsim_records": skipped_non_airsim_records,
            "max_distinct_metadata_candidates": MAX_METADATA_CANDIDATES,
            "metadata_candidates_available": len(selected),
            "metadata_candidates_checked": len(plans) + len(rejected_records),
            "annotation_record_count": total,
            "episodes": episodes,
            "raw_rgb_count": image_count,
            "rgb_pose_bytes": pilot_bytes,
            "max_episodes": MAX_EPISODES,
            "max_images": MAX_IMAGES,
            "max_rgb_pose_bytes": MAX_PILOT_BYTES,
        },
        "annotation": annotation,
        "code_sources": code_sources,
        "license_sources": license_sources,
        "source_files": source_files,
        "metadata_records": metadata_records,
        "episodes": imported,
        "rejected_records": rejected_records,
        "source_planned_not_executed": True,
        "training_ready": False,
        "action_labels_normalized": False,
        "dt_available": False,
        "flight_execution_evidence": False,
        "optimizer_performed": False,
        "weights_downloaded": False,
        "seen_unseen_downloaded": False,
    }
    _create_bytes(out / "manifest.json", _canonical(manifest))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--episodes", type=int, default=3)
    parser.add_argument("--annotation-cache", type=Path)
    args = parser.parse_args(argv)
    cache = args.annotation_cache
    if cache is None:
        candidate = (
            Path(__file__).resolve().parents[1]
            / f"artifacts/cache/openfly/{REVISION}/Annotation/train.json"
        )
        if candidate.is_file():
            cache = candidate
    try:
        manifest = fetch_pilot(args.output_dir, episodes=args.episodes, annotation_cache=cache)
    except Exception as exc:
        # Partial raw files remain for diagnosis; they are never complete training manifests.
        print(
            json.dumps(
                {
                    "event": "openfly_pilot_failed_partial",
                    "error_type": type(exc).__name__,
                    "error": str(exc)
                    if isinstance(exc, PilotError)
                    else "source import failed; partial retained",
                    "training_ready": False,
                }
            ),
            flush=True,
        )
        return 1
    print(
        json.dumps(
            {
                "event": "openfly_pilot_sealed",
                "manifest": _file_binding(args.output_dir / "manifest.json"),
                "selection": manifest["selection"],
                "training_ready": False,
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
