"""Extract small original nav-reference assets from a never-started image copy.

Tiny local Docker diagnostic, no SIM launch, network port, sensor, or flight.
Only the freshly created container with this invocation's ID/name/owner label
is removed. TAR replies and exact files are retained without TAR extraction.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from scripts.onboard_teacher_native_prefix import capture_native_prefix  # noqa: E402
from scripts.run_onboard_teacher_gazebo_observation_v2 import _json_new, _write_new  # noqa: E402
from scripts.run_simulator_docker import PINNED_IMAGE  # noqa: E402

CONTRACT = "multidetect.teacher-Gazebo-navref-asset-extraction-v1"
OWNER_LABEL = "multidetect.navref-extraction-owner"
PURPOSE = "teacher-navref-never-started-extraction"
FILES = {
    "x500-model.sdf": "/opt/px4-gazebo/share/gz/models/x500/model.sdf",
    "x500-base-model.sdf": "/opt/px4-gazebo/share/gz/models/x500_base/model.sdf",
    "px4-rc-gzsim": "/opt/px4-gazebo/etc/init.d-posix/px4-rc.gzsim",
}
MAXIMUM_ARCHIVE_BYTES = 256 * 1024


def _owned_created(record, *, identity, name, owner):
    config = record.get("Config") or {}
    labels = config.get("Labels") or {}
    state = record.get("State") or {}
    if (
        record.get("Id") != identity
        or record.get("Name") != "/" + name
        or config.get("Image") != PINNED_IMAGE
        or labels.get(OWNER_LABEL) != owner
        or labels.get("multidetect.purpose") != PURPOSE
        or state.get("Status") != "created"
        or state.get("Running") is not False
    ):
        raise ValueError("exact owned never-started container identity required")


def _single_tar_file(raw, expected_name):
    if type(raw) is not bytes or not 0 < len(raw) <= MAXIMUM_ARCHIVE_BYTES:
        raise ValueError("bounded original Docker TAR reply required")
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            members = archive.getmembers()
            if (
                len(members) != 1
                or not members[0].isfile()
                or members[0].name != expected_name
                or not 0 < members[0].size <= 128 * 1024
            ):
                raise ValueError("exact single regular asset required; no path/link extraction")
            # TAR readers normally stop at the first zero header and ignore
            # concatenated archives/junk. Only zero padding may follow the
            # exact one logical member; retain/refuse any hidden nonzero tail.
            if any(raw[archive.offset :]):
                raise ValueError("nonzero trailing Docker TAR bytes are unsupported")
            source = archive.extractfile(members[0])
            if source is None:
                raise ValueError("missing original TAR file body")
            payload = source.read(128 * 1024 + 1)
            if len(payload) != members[0].size:
                raise ValueError("TAR body differs from declared exact asset")
            return payload
    except tarfile.TarError as exc:
        raise ValueError("invalid original Docker TAR reply") from exc


def extract_navref_assets(output_directory, *, repository_root=ROOT, native=None):
    root = Path(repository_root).resolve(strict=True)
    directory = Path(output_directory).resolve()
    if not directory.is_relative_to(root / "artifacts/diagnostics") or directory.exists():
        raise ValueError("fresh contained diagnostic directory required")
    runner = capture_native_prefix if native is None else native
    directory.mkdir(exist_ok=False)
    owner = uuid4().hex
    name = "multidetect-navref-" + owner[:16]
    report = {
        "contract": CONTRACT,
        "status": "planned",
        "image": PINNED_IMAGE,
        "container_id": None,
        "container_name": name,
        "owner_token": owner,
        "native_results": [],
        "assets": {},
        "never_started_identity_verified": False,
        "owned_container_absent_after_cleanup": False,
        "injected_native_used": native is not None,
        "error": None,
        "cleanup_error": None,
        "claims": {
            key: False
            for key in (
                "external_SIM_runtime_executed",
                "source_qualified_origin",
                "Mission_transferred",
                "ARM_sent",
                "flight_executed",
                "model_loaded",
                "optimizer_performed",
                "candidate_promoted",
                "physical_performance_claimed",
                "hil_real_evidence",
                "outdoor_gnss_evidence",
                "final_release_allowed",
            )
        },
    }
    _json_new(directory / "intent.json", report)

    def command(args, *, allow_failure=False):
        row = {"argv": ["docker", *args], "output": {}}
        report["native_results"].append(row)
        result = runner(
            row["argv"],
            timeout_s=15,
            prefix_bytes=MAXIMUM_ARCHIVE_BYTES,
            output_limit_bytes=2 * MAXIMUM_ARCHIVE_BYTES,
        )
        streams = result.pop("streams")
        row.update(result)
        for label in ("stdout", "stderr"):
            item = dict(streams[label])
            payload = item.pop("prefix")
            row["output"][label] = {
                **item,
                "source": _write_new(
                    directory / f"native-{len(report['native_results']) - 1:02d}.{label}.bin",
                    payload,
                ),
            }
        if not result["complete_output_observed"] or not result["reader_threads_exited"]:
            raise RuntimeError("native output incomplete; original prefixes retained")
        if not all(streams[label]["all_bytes_retained"] for label in ("stdout", "stderr")):
            raise RuntimeError("small diagnostic output exceeded exact retention bound")
        if not allow_failure and result["native_exit_code"] != 0:
            raise RuntimeError("native Docker diagnostic failed")
        return result["native_exit_code"], streams["stdout"]["prefix"]

    def inspect_owned():
        _, raw = command(["inspect", report["container_id"]])
        records = json.loads(raw)
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise ValueError("one original Docker inspection object required")
        _owned_created(records[0], identity=report["container_id"], name=name, owner=owner)

    try:
        command(["image", "inspect", PINNED_IMAGE])  # No implicit pull of absent image.
        _, raw = command(
            [
                "create",
                "--name",
                name,
                "--label",
                OWNER_LABEL + "=" + owner,
                "--label",
                "multidetect.purpose=" + PURPOSE,
                "--network",
                "none",
                PINNED_IMAGE,
            ]
        )
        identity = raw.decode("ascii").strip()
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("full native Docker container ID required")
        report["container_id"] = identity
        inspect_owned()
        report["never_started_identity_verified"] = True
        for label, path in FILES.items():
            inspect_owned()
            _, raw = command(["cp", identity + ":" + path, "-"])
            payload = _single_tar_file(raw, path.rsplit("/", 1)[1])
            report["assets"][label] = {
                "image_path": path,
                "source": _write_new(directory / label, payload),
                "archive_sha256": hashlib.sha256(raw).hexdigest(),
            }
        inspect_owned()
        report["status"] = "original-image-assets-recorded"
    except Exception as error:
        report.update(status="asset-extraction-failed", error=f"{type(error).__name__}: {error}")
    finally:
        if report["container_id"] is not None:
            try:
                inspect_owned()
                command(["rm", report["container_id"]])  # no -f; never running, exact owned ID
                code, raw = command(["inspect", report["container_id"]], allow_failure=True)
                last = report["native_results"][-1]
                stderr_path = Path(last["output"]["stderr"]["source"]["path"])
                stderr = stderr_path.read_bytes().lower()
                report["owned_container_absent_after_cleanup"] = (
                    code == 1
                    and raw.strip() == b"[]"
                    and b"no such" in stderr
                    and report["container_id"].encode() in stderr
                )
                if not report["owned_container_absent_after_cleanup"]:
                    raise RuntimeError("exact container absence not proven after cleanup")
            except Exception as error:
                report["cleanup_error"] = f"{type(error).__name__}: {error}"
                report["status"] = "asset-extraction-failed"
        report["receipt"] = _json_new(directory / "extraction.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    report = extract_navref_assets(args.output_directory)
    print(json.dumps({k: report[k] for k in ("status", "error", "cleanup_error", "receipt")}))
    return 0 if report["status"] == "original-image-assets-recorded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
