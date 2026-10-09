"""Extract exact metadata from a NEVER-started owned pinned image container.

No flight, network, model, configuration change or automatic image pull. The
image is the source; live PX4 firmware/parameter equivalence is not implied.
CLI requires --execute; default does not create outputs or Docker objects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import math
import re
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from scripts.run_onboard_teacher_gazebo_observation_v2 import (  # noqa: E402
    FALSE_CLAIMS,
    _json_new,
    _write_new,
)
from scripts.run_simulator_docker import PINNED_IMAGE  # noqa: E402

FILES = ("all_events.json.xz", "parameters.json.xz", "component_general.json.xz")
CONTRACT = "multidetect.teacher-pinned-PX4-metadata-extraction-v1"
LABEL = "multidetect.teacher-metadata-owner"
MAX_COMPRESSED = 4 * 1024 * 1024
MAX_DECOMPRESSED = 16 * 1024 * 1024


def _expand(raw, *, memory_limit_mib=64):
    if not 0 < len(raw) <= MAX_COMPRESSED:
        raise ValueError("Bounded nonempty compressed metadata required")
    if type(memory_limit_mib) is not int or memory_limit_mib not in {64, 128}:
        raise ValueError("Explicit 64/128 MiB metadata decoder memory limit required")
    decoder = lzma.LZMADecompressor(memlimit=memory_limit_mib * 1024 * 1024)
    expanded = decoder.decompress(raw, max_length=MAX_DECOMPRESSED + 1)
    if len(expanded) > MAX_DECOMPRESSED or not decoder.eof or decoder.unused_data:
        raise ValueError("Metadata expansion incomplete/oversized/concatenated")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate metadata JSON field")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError(f"Nonfinite metadata JSON: {value}")

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Nonfinite metadata JSON number")
        return result

    document = json.loads(
        expanded, object_pairs_hook=unique, parse_constant=invalid, parse_float=finite_float
    )
    if type(document) is not dict:
        raise ValueError("Metadata JSON object required")
    return expanded, document


def extract_metadata(output_directory, *, execute=False, runner=None, memory_limit_mib=64):
    if type(execute) is not bool:
        raise TypeError("Explicit boolean execute required")
    if type(memory_limit_mib) is not int or memory_limit_mib not in {64, 128}:
        raise ValueError("Explicit 64/128 MiB metadata decoder memory limit required")
    directory = Path(output_directory).resolve()
    directory.relative_to(ROOT / "artifacts/diagnostics")
    if directory.exists():
        raise ValueError("Fresh metadata output required")
    report = {
        "contract": CONTRACT,
        "status": "planned",
        "image": PINNED_IMAGE,
        "execute_requested": execute,
        "metadata_decoder_memory_limit_mib": memory_limit_mib,
        "injected_runner": runner is not None,
        "files": {},
        "native_commands": [],
        "container_started": False,
        "cleanup_verified": False,
        "actual_live_firmware_verified": False,
        "actual_parameter_values_observed": False,
        "error": None,
        "cleanup_error": None,
        "claims": dict(FALSE_CLAIMS),
    }
    if not execute:
        return report
    directory.mkdir(exist_ok=False)
    token = uuid4().hex
    name = f"multidetect-metadata-{token[:16]}"
    identity = None
    creation_attempted = False

    def run(args):
        result = (
            subprocess.run(["docker", *args], capture_output=True, timeout=15, check=False)
            if runner is None
            else runner(["docker", *args], timeout_s=15)
        )
        if type(result.returncode) is not int:
            raise RuntimeError("Native Docker exit missing")
        stdout = b"" if result.stdout is None else result.stdout
        stderr = b"" if result.stderr is None else result.stderr
        if type(stdout) is not bytes or type(stderr) is not bytes:
            raise TypeError("Original native output bytes required")
        if len(stdout) + len(stderr) > 256 * 1024:
            raise ValueError("Docker metadata diagnostic budget exceeded")
        report["native_commands"].append(
            {
                "args": args,
                "native_exit_code": result.returncode,
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                "stderr": stderr.decode("utf-8", "replace")[:4096],
            }
        )
        return result

    def inspect(target):
        result = run(["container", "inspect", target])
        if result.returncode == 1 and re.search(
            rf"No such (?:container|object):\s*{re.escape(target)}(?:\s|$)",
            result.stderr.decode("utf-8", "replace"),
            re.IGNORECASE,
        ):
            return None
        if result.returncode != 0:
            raise RuntimeError("Docker absence/identity unproven")
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError("Single native container inspect required")
        return rows[0]

    def owned(row):
        if row is None:
            raise RuntimeError("Owned metadata container missing")
        cid = row.get("Id", "")
        config = row.get("Config") or {}
        if (
            re.fullmatch(r"[0-9a-f]{64}", cid) is None
            or row.get("Name") != "/" + name
            or config.get("Image") != PINNED_IMAGE
            or (config.get("Labels") or {}).get(LABEL) != token
            or row.get("State", {}).get("Running") is not False
            or row.get("State", {}).get("Status") != "created"
            or row.get("State", {}).get("StartedAt") != "0001-01-01T00:00:00Z"
            or (identity is not None and identity != cid)
        ):
            raise RuntimeError("Exact NEVER-started container identity unavailable; not touched")
        return cid

    try:
        if inspect(name) is not None:
            raise RuntimeError("Pre-existing container refused")
        image = run(["image", "inspect", PINNED_IMAGE])
        if image.returncode != 0:
            raise RuntimeError("Pinned local image unavailable; no pull")
        report["image_inspect"] = _write_new(directory / "image-inspect.json", image.stdout)
        creation_attempted = True
        created = run(
            [
                "create",
                "--name",
                name,
                "--label",
                f"{LABEL}={token}",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--entrypoint",
                "/bin/false",
                PINNED_IMAGE,
            ]
        )
        if created.returncode != 0:
            raise RuntimeError("Owned native create failed")
        returned_id = created.stdout.decode("ascii").strip()
        if re.fullmatch(r"[0-9a-f]{64}", returned_id) is None:
            raise RuntimeError("Exact created-container native ID missing")
        identity = returned_id
        owned(inspect(identity))
        report["container_id"] = identity
        for filename in FILES:
            target = directory / filename
            copied = run(["cp", f"{identity}:/opt/px4-gazebo/etc/extras/{filename}", str(target)])
            if copied.returncode != 0 or target.is_symlink() or not target.is_file():
                raise RuntimeError("Original image metadata copy failed")
            size = target.stat().st_size
            if not 0 < size <= MAX_COMPRESSED:
                raise ValueError("Copied metadata exceeds bound")
            original = target.read_bytes()
            expanded, document = _expand(original, memory_limit_mib=memory_limit_mib)
            decoded_path = directory / filename.removesuffix(".xz")
            decoded = _write_new(decoded_path, expanded)
            report["files"][filename] = {
                "image_path": f"/opt/px4-gazebo/etc/extras/{filename}",
                "compressed": {
                    "path": target.as_posix(),
                    "bytes": len(original),
                    "sha256": hashlib.sha256(original).hexdigest(),
                },
                "expanded": decoded,
                "top_level_keys": sorted(document),
                "source": "fixed_image_build_metadata_not_live_parameter_values",
            }
        report["status"] = "pinned-image-metadata-extracted"
    except BaseException as error:
        report.update(
            status="metadata-extraction-failed", error=f"{type(error).__name__}: {error}"[:2048]
        )
    finally:
        try:
            if creation_attempted:
                row = inspect(identity or name)
                if row is not None:
                    identity = owned(row)
                    removed = run(["rm", identity])
                    if removed.returncode != 0:
                        raise RuntimeError("Owned created-container removal failed")
                    if inspect(identity) is not None:
                        raise RuntimeError("Owned container absence unproven")
                report["cleanup_verified"] = True
            else:
                report["cleanup_not_required_no_create_attempt"] = True
        except BaseException as error:
            report["cleanup_error"] = f"{type(error).__name__}: {error}"[:2048]
            report["status"] = "metadata-extraction-failed"
        try:
            for row in report["files"].values():
                for kind in ("compressed", "expanded"):
                    binding = row[kind]
                    path = Path(binding["path"])
                    if path.is_symlink() or path.resolve().parent != directory:
                        raise ValueError("Final metadata source path drift")
                    before = path.stat()
                    if before.st_size != binding["bytes"]:
                        raise ValueError("Final metadata source size drift")
                    raw = path.read_bytes()
                    after = path.stat()
                    if (
                        (before.st_size, before.st_mtime_ns, before.st_ino)
                        != (after.st_size, after.st_mtime_ns, after.st_ino)
                        or len(raw) != binding["bytes"]
                        or hashlib.sha256(raw).hexdigest() != binding["sha256"]
                    ):
                        raise ValueError("Final metadata source hash/identity drift")
            report["final_metadata_sources_reverified"] = True
        except BaseException as error:
            report["final_metadata_sources_reverified"] = False
            report["source_verification_error"] = f"{type(error).__name__}: {error}"[:2048]
            report["status"] = "metadata-extraction-failed"
        report["receipt"] = _json_new(directory / "metadata-extraction.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--memory-limit-mib", type=int, choices=(64, 128), default=64)
    args = parser.parse_args(argv)
    report = extract_metadata(
        args.output_directory, execute=args.execute, memory_limit_mib=args.memory_limit_mib
    )
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["status"] in {"planned", "pinned-image-metadata-extracted"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
