"""Explicit V4 resource admission around frozen navigation/context semantics.

Only prefix resource policy, streaming I/O and composition are NEW here.
CRC, raw context, native/reset, camera/extrinsic and coordinate calculations
remain the frozen implementation. Neither historical reproduction nor a
resource budget grants current writer ownership or flight permission.
"""

from __future__ import annotations

import copy
import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from . import teacher_live_navigation_checkpoint as frozen
from .onboard_executed_trajectory import EvidenceBinding
from .serialization import canonical_json, require_int, require_real, strict_json_loads

CONTRACT = "multidetect.teacher-SIM-live-navigation-checkpoint-v2"
PREFIX_CONTRACT = "multidetect.teacher-live-dual-committed-prefix-v2"
CHUNK_BYTES = 1024 * 1024
MAXIMUM_REPORT_BYTES = 32 * 1024 * 1024


def _save_json(root, path, value, ledger=None):
    """Reserve exact serialized bytes BEFORE the main-owned write."""
    raw = canonical_json(value).encode() + b"\n"
    frozen._require(len(raw) <= MAXIMUM_REPORT_BYTES, "Finite checkpoint artifact exhausted")
    reservation = None
    if ledger is not None:
        reservation = ledger.reserve_write("evidence", path, maximum_bytes=len(raw))
    source = frozen._save(root, path, raw, pointer="")
    if reservation is not None:
        from scripts.onboard_teacher_sortie_artifact_ledger_v1 import LedgerFileBinding

        ledger.commit_write(
            reservation,
            LedgerFileBinding(
                Path(path).absolute().as_posix(),
                source.bytes,
                source.sha256,
            ),
        )
    return source


def _ledger_scope(ledger, capture, clock):
    if ledger is None:
        return
    from scripts.onboard_teacher_sortie_artifact_ledger_v1 import TeacherSortieArtifactLedger

    frozen._require(
        type(ledger) is TeacherSortieArtifactLedger
        and ledger.budget.canonical_hash == capture.budget.canonical_hash
        and ledger._clock == clock.monotonic,
        "Exact original lifecycle artifact ledger/clock required",
    )
    ledger._scope()


def _budget_from_source(root, source, identities):
    from scripts.onboard_teacher_sortie_budget_v1 import TeacherSortieLifecycleBudget

    raw = frozen._bound_bytes(root, source, 65536, identities)
    return TeacherSortieLifecycleBudget.from_mapping(strict_json_loads(raw))


def _writer(capture, clock):
    from scripts.onboard_teacher_telemetry_capture_v4 import TeacherTelemetryCaptureV4

    frozen._require(
        type(capture) is TeacherTelemetryCaptureV4, "Exact NEW lifecycle-budget V4 writer required"
    )
    capture._open()
    frozen._require(
        capture._owner == threading.get_ident()
        and not capture._busy
        and capture._clock == clock.monotonic
        and capture.clock_id == clock.session_id == capture.budget.clock_id
        and capture.max_records == capture.budget.record_limit,
        "Original main writer/live clock/lifecycle budget differs",
    )
    capture._now()
    return capture.budget


def _selected_records(records, started_at_s):
    recent, anchors = [], {}
    for record in records:
        kind = record["message_type"]
        if record["source_accepted"] is not True or kind not in frozen.NAVIGATION_MESSAGES:
            continue
        at = record["received_at_s"]
        if at >= started_at_s - 0.15:
            recent.append(record)
        if kind in {"HOME_POSITION", "GPS_GLOBAL_ORIGIN"} and at <= started_at_s:
            anchors[kind] = record
    by_index = {r["record_index"]: r for r in [*recent, *anchors.values()]}
    return [by_index[index] for index in sorted(by_index)]


def capture_teacher_committed_prefix_v2(
    capture,
    *,
    artifact_root,
    output_directory,
    clock,
    navigation_before,
    artifact_ledger=None,
):
    """Export exact open V4 budget + finite relevant navigation source closure."""
    budget = _writer(capture, clock)
    _ledger_scope(artifact_ledger, capture, clock)
    root, directory = Path(artifact_root).resolve(), Path(output_directory).resolve()
    frozen._require(
        directory.is_relative_to(root) and not directory.exists(),
        "Fresh contained lifecycle prefix export required",
    )
    start = require_real(navigation_before["started_at_s"], "native window start", minimum=0)
    frozen._require(
        budget.started_at_s <= start <= clock.monotonic(),
        "Original navigation window outside lifecycle",
    )
    directory.mkdir()
    budget_source = _save_json(
        root, directory / "lifecycle-budget.json", budget.to_mapping(), artifact_ledger
    )
    now = clock.monotonic()
    snapshot_directory = directory / "snapshot"
    if artifact_ledger is not None:
        snapshot_directory = capture.directory / f"NEW-live-snapshot-{len(capture._snapshots):04d}"
        frozen._require(
            not any(
                snapshot_directory.is_relative_to(p)
                for p in (artifact_ledger.camera_directory, artifact_ledger.evidence_directory)
            ),
            "Telemetry snapshots must not overlap artifact ledger scopes",
        )
    rows = capture.materialize_snapshot(
        list(frozen.REQUIRED_MESSAGES),
        snapshot_directory,
        now_s=now,
    )
    _writer(capture, clock)
    prefixes = {}
    for stage in ("raw", "projection"):
        path = Path(capture._streams[stage].name)
        stat, counts = path.stat(), capture._commits[stage]
        frozen._require(
            stat.st_size == capture._sizes[stage]
            and counts["attempted"] == counts["fsync_returned"] == counts["verified"],
            "Partial/unverified V4 journal tail cannot qualify",
        )
        prefixes[stage] = {
            "path": path.as_posix(),
            "prefix_bytes": capture._sizes[stage],
            "prefix_sha256": capture._digests[stage].hexdigest(),
            "file_device": stat.st_dev,
            "file_inode": stat.st_ino,
            "append_allowed": True,
            "whole_file_immutable_claimed": False,
            "commits": copy.deepcopy(counts),
        }
    selected = {kind: [] for kind in frozen.NAVIGATION_MESSAGES}
    for record in _selected_records(capture._records, start):
        projection = strict_json_loads(
            capture._read_slice("projection", record["projection_record"])
        )
        frozen._require(
            projection["raw_record"] == record["raw_record"],
            "Original V4 navigation raw/projection proof differs",
        )
        selected[record["message_type"]].append(
            {
                **projection["row"],
                **{
                    k: copy.deepcopy(record[k])
                    for k in (
                        "raw_record",
                        "projection_record",
                        "raw_committed_at_s",
                        "projection_committed_at_s",
                    )
                },
                "dispatch_ready_at_s": record["projection_committed_at_s"],
            }
        )
    before, utc, after = clock.monotonic(), clock.utc_now(), clock.monotonic()
    frozen._require(
        isinstance(utc, datetime) and utc.tzinfo is not None and 0 <= after - before <= 0.05,
        "Actual bounded UTC anchor required",
    )
    report = {
        "contract": PREFIX_CONTRACT,
        "writer_closed": False,
        "writer_live_at_export": True,
        "clock_id": capture.clock_id,
        "transaction_id": capture.transaction_id,
        "local_origin_id": capture.local_origin_id,
        "maximum_packets": budget.record_limit,
        "record_count": len(capture._records),
        "exported_at_s": after,
        "committed_prefixes": prefixes,
        "budget_source": budget_source.to_mapping(),
        "budget_sha256": budget.canonical_hash,
        "writer_lifecycle": capture.lifecycle_mapping(),
        "selection_window_started_at_s": start,
        "snapshot_rows": rows,
        "selected_navigation_rows": selected,
        "snapshot_manifest": frozen._binding(
            root,
            {
                "path": (snapshot_directory / "snapshot-manifest.json").as_posix(),
                "bytes": (snapshot_directory / "snapshot-manifest.json").stat().st_size,
                "sha256": hashlib.sha256(
                    (snapshot_directory / "snapshot-manifest.json").read_bytes()
                ).hexdigest(),
            },
            pointer="",
        ).to_mapping(),
        "UTC_anchor": {
            "before_monotonic_s": before,
            "actual_UTC": utc.isoformat(),
            "after_monotonic_s": after,
            "external_synchronization_claimed": False,
        },
        "claims": {"execution_authority": False, "source_authenticated": False, "sealed": False},
    }
    raw = canonical_json(report).encode() + b"\n"
    frozen._require(len(raw) <= MAXIMUM_REPORT_BYTES, "Bounded NEW prefix report exhausted")
    source = _save_json(root, directory / "committed-prefix.json", report, artifact_ledger)
    _read_prefixes_v2(root, report)
    _writer(capture, clock)
    return source


class _VerifiedPrefixSlices:
    """Constant-memory, bounded original offset reads for frozen pure helpers."""

    def __init__(self, path, size, device, inode):
        self.path, self.size, self.device, self.inode = path, size, device, inode

    def __len__(self):
        return self.size

    def __getitem__(self, item):
        frozen._require(
            isinstance(item, slice) and item.step in (None, 1),
            "Only bounded original prefix slices supported",
        )
        start, stop = item.start or 0, self.size if item.stop is None else item.stop
        frozen._require(
            type(start) is int
            and type(stop) is int
            and 0 <= start <= stop <= self.size
            and stop - start <= 65536,
            "Original slice is outside finite committed prefix",
        )
        before = self.path.stat()
        frozen._require(
            (before.st_dev, before.st_ino) == (self.device, self.inode)
            and before.st_size >= self.size,
            "Prefix replaced/truncated before slice",
        )
        with self.path.open("rb") as stream:
            stream.seek(start)
            raw = stream.read(stop - start)
        after = self.path.stat()
        frozen._require(
            (after.st_dev, after.st_ino) == (self.device, self.inode)
            and after.st_size >= before.st_size
            and len(raw) == stop - start,
            "Prefix slice changed/replaced/truncated",
        )
        return raw

    def startswith(self, magic):
        return self[: len(magic)] == magic


def _read_prefixes_v2(root, document):
    budget = _budget_from_source(root, EvidenceBinding.from_mapping(document["budget_source"]), {})
    frozen._require(
        document["contract"] == PREFIX_CONTRACT
        and document["writer_closed"] is False
        and document["writer_live_at_export"] is True
        and document["clock_id"] == budget.clock_id
        and document["maximum_packets"] == budget.record_limit
        and document["budget_sha256"] == budget.canonical_hash
        and budget.started_at_s <= document["exported_at_s"] < budget.deadline_s,
        "Exact original lifecycle/prefix resource binding differs",
    )
    count = require_int(
        document["record_count"],
        "original committed records",
        minimum=9,
        maximum=budget.record_limit,
    )
    lifecycle = document["writer_lifecycle"]
    frozen._require(
        lifecycle["contract"] == "multidetect.onboard-teacher-lifecycle-telemetry-v4"
        and lifecycle["budget"] == budget.to_mapping()
        and lifecycle["budget_sha256"] == budget.canonical_hash
        and lifecycle["record_limit"] == budget.record_limit
        and lifecycle["telemetry_byte_limit"] == budget.telemetry_byte_limit
        and lifecycle["records_attempted"] == count,
        "V4 original writer lifecycle differs",
    )
    total, result = 0, {}
    for stage in ("raw", "projection"):
        prefix = document["committed_prefixes"][stage]
        original = Path(prefix["path"])
        path = original.resolve()
        frozen._require(
            path.is_relative_to(root) and path.is_file() and not original.is_symlink(),
            "Contained original V4 journal required",
        )
        length = require_int(
            prefix["prefix_bytes"],
            "original prefix bytes",
            minimum=1,
            maximum=budget.telemetry_byte_limit,
        )
        total += length
        before = path.stat()
        frozen._require(
            (before.st_dev, before.st_ino) == (prefix["file_device"], prefix["file_inode"])
            and before.st_size >= length
            and prefix["append_allowed"] is True
            and prefix["whole_file_immutable_claimed"] is False,
            "V4 prefix identity differs",
        )
        digest, remaining = hashlib.sha256(), length
        with path.open("rb") as stream:
            while remaining:
                raw = stream.read(min(CHUNK_BYTES, remaining))
                frozen._require(bool(raw), "V4 committed prefix truncated")
                digest.update(raw)
                remaining -= len(raw)
        after = path.stat()
        frozen._require(
            (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
            and after.st_size >= before.st_size
            and digest.hexdigest() == prefix["prefix_sha256"],
            "V4 committed prefix changed/replaced/truncated",
        )
        commits = prefix["commits"]
        frozen._require(
            set(commits) == {"attempted", "fsync_returned", "verified"}
            and all(type(n) is int and n >= 1 for n in commits.values())
            and commits["attempted"] == commits["fsync_returned"] == commits["verified"],
            "Unverified lifecycle prefix commits",
        )
        result[stage] = (
            SimpleNamespace(
                path=path.relative_to(root).as_posix(), bytes=length, sha256=prefix["prefix_sha256"]
            ),
            _VerifiedPrefixSlices(path, length, before.st_dev, before.st_ino),
        )
    frozen._require(
        total <= lifecycle["bytes_charged"] <= budget.telemetry_byte_limit,
        "Committed resource bytes exceed original lifecycle charge",
    )
    frozen._require(
        result["raw"][1].startswith(b"MDTEACHER-RAW-V3\n"), "Original V3 wire codec differs"
    )
    _verify_selection(root, document, result, budget)
    return result


def _verify_selection(root, prefix, journals, budget):
    """Recompute finite selected indices from original committed projection bytes."""
    from scripts.onboard_teacher_telemetry_capture_v3 import PROJECTION_MAGIC

    path = root / journals["projection"][0].path
    length = journals["projection"][0].bytes
    rows, anchors, offset, count = [], {}, 0, 0
    start = prefix["selection_window_started_at_s"]
    # Original JSONL header is independently bound by the full prefix SHA.
    with path.open("rb") as stream:
        header = stream.readline(65537)
        frozen._require(header == PROJECTION_MAGIC, "Projection wire header differs")
        offset = len(header)
        while offset < length:
            raw = stream.readline(min(budget.maximum_projection_record_bytes + 1, length - offset))
            frozen._require(
                raw.endswith(b"\n") and len(raw) <= budget.maximum_projection_record_bytes,
                "Original projection record bound/termination differs",
            )
            projection = strict_json_loads(raw)
            row = projection["row"]
            frozen._require(
                row["record_index"] == count and count < budget.record_limit,
                "Absolute original projection index changed",
            )
            if (
                row["source_accepted"] is True
                and row["message"]["message_type"] in frozen.NAVIGATION_MESSAGES
            ):
                record = {
                    "record_index": row["record_index"],
                    "message_type": row["message"]["message_type"],
                    "received_at_s": row["received_at_s"],
                    "source_accepted": True,
                    "projection_record": {
                        "offset": offset,
                        "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    },
                }
                if record["received_at_s"] >= start - 0.15:
                    rows.append(record)
                    frozen._require(
                        len(rows) <= 16384, "Finite active navigation selection exhausted"
                    )
                if (
                    record["message_type"] in {"HOME_POSITION", "GPS_GLOBAL_ORIGIN"}
                    and record["received_at_s"] <= start
                ):
                    anchors[record["message_type"]] = record
            offset += len(raw)
            count += 1
    frozen._require(
        offset == length and count == prefix["record_count"],
        "Original projection prefix count differs",
    )
    expected = _selected_records([*rows, *anchors.values()], start)
    supplied = [r for entries in prefix["selected_navigation_rows"].values() for r in entries]
    by_index = {r["record_index"]: r for r in supplied}
    frozen._require(
        len(by_index) == len(supplied) and set(by_index) == {r["record_index"] for r in expected},
        "Current navigation source selection omitted/added original records",
    )
    for row in expected:
        supplied_row = by_index[row["record_index"]]
        proof = supplied_row["projection_record"]
        frozen._require(
            all(proof[k] == v for k, v in row["projection_record"].items())
            and supplied_row["received_at_s"] == row["received_at_s"],
            "Selected navigation original source offsets/time differ",
        )


@dataclass(frozen=True, slots=True)
class LiveTeacherNavigationCheckpointV2(frozen.LiveTeacherNavigationCheckpoint):
    @property
    def budget(self):
        from scripts.onboard_teacher_sortie_budget_v1 import TeacherSortieLifecycleBudget

        return TeacherSortieLifecycleBudget.from_mapping(self.to_mapping()["lifecycle_budget"])


def _derive(root, inputs, input_source, now, clock_id, stage=None):
    """NEW composition only; all measurement interpretation stays frozen."""
    identities = {}

    def read(name, maximum=128 * 1024):
        source = EvidenceBinding.from_mapping(inputs[name])
        return strict_json_loads(frozen._bound_bytes(root, source, maximum, identities)), source

    prefix, _ = read("prefix_source", MAXIMUM_REPORT_BYTES)
    budget = _budget_from_source(
        root, EvidenceBinding.from_mapping(prefix["budget_source"]), identities
    )
    frozen._require(
        prefix["clock_id"] == clock_id and budget.remaining_s(now, clock_id=clock_id) > 0,
        "Checkpoint outside original lifecycle clock/deadline",
    )
    journals = _read_prefixes_v2(root, prefix)
    if stage is not None:
        stage("prefix_hash_and_complete_selection")
    messages, times, message_sources = frozen._snapshot_messages(
        root, prefix, journals, now, identities
    )
    if stage is not None:
        stage("nine_original_context_packets")
    profile, profile_source = read("profile_source")
    runtime, runtime_source = read("runtime_source")
    world_source = EvidenceBinding.from_mapping(inputs["world_source"])
    wind = frozen._configured_wind(root, world_source, runtime_source)
    models = {k: EvidenceBinding.from_mapping(v) for k, v in inputs["model_sources"].items()}
    plan = inputs["plan"]
    frozen._require(
        profile["airframe_profile_id"] == "px4_multirotor_gazebo_v1"
        and profile["model_name"] == "x500_mono_cam"
        and profile["vehicle_model"] == "px4_multirotor_standard"
        and runtime["scenario_id"] == plan["docker_plan"]["scenario_id"]
        and runtime["vehicle"]["airframe_profile_id"] == profile["airframe_profile_id"],
        "Actual source-bound x500 profile/runtime differs",
    )
    phases, inspects, parameters, update, native_sources = frozen._native_navigation(
        root,
        inputs,
        prefix,
        plan,
        inputs["owner_token"],
        now,
        identities,
    )
    if stage is not None:
        stage("profile_wind_native_parameters_and_inspect")
    frozen._require(
        prefix["selection_window_started_at_s"] == phases[0][2][0],
        "Selected source window differs from actual native listener",
    )
    geometry, camera_sources = frozen._navigation_geometry(
        root,
        prefix,
        journals,
        inputs["camera_updates"],
        phases,
        world_source,
        models,
        now,
        identities,
        messages,
    )
    if stage is not None:
        stage("camera_native_coordinate_math")
    resource, resource_source = read("resource_source", 65536)
    frozen._require(
        resource["clock_id"] == clock_id
        and resource["transaction_id"] == prefix["transaction_id"]
        and 0 <= now - resource["observed_at_s"] < 2
        and resource["airframe_profile_hash"] == frozen.airframe_profile_hash(profile)
        and resource["camera_available"] is True
        and all(
            resource[k] is False
            for k in (
                "geofence_valid",
                "perception_available",
                "map_cache_available",
                "dem_available",
            )
        )
        and resource["wind_source"] == "software-SIM-configured"
        and (resource["wind_north_mps"], resource["wind_east_mps"])
        == (wind.wind_north_mps, wind.wind_east_mps)
        and resource["configured_wind"] == wind.to_mapping(),
        "Actual limited camera/configured-wind resource source differs",
    )
    context = frozen._ground_context(
        messages,
        times,
        profile,
        profile_source,
        resource_source,
        resource,
        prefix["UTC_anchor"],
        now,
        prefix["transaction_id"],
    )
    if stage is not None:
        stage("source_bound_observed_context")
    docker = plan["docker_plan"]
    attestations = [
        frozen.attest_owned_px4_container(
            inspect[0],
            expected_container_id=inputs["container_id"],
            expected_image=docker["image"],
            vehicle_class="multirotor",
            scenario_id=docker["scenario_id"],
            scenario_manifest_sha256=docker["scenario_manifest_hash"],
            expected_model="gz_x500_mono_cam",
            expected_purpose_label="simulator-docker",
            control_port=docker["ports"]["control"]["host"],
            telemetry_qualified=True,
            expected_readonly_mounts={v["container"]: v["host"] for v in docker["mounts"].values()},
        )
        for inspect in inspects
    ]
    frozen._require(
        attestations[0] == attestations[1], "Owned runtime changed inside current window"
    )
    attestation = attestations[-1]
    oldest = min(
        context.captured_at_s, geometry["oldest_camera_at_s"], phases[-1][2][0], inspects[-1][2][0]
    )
    expires = min(oldest + 2, budget.deadline_s)
    frozen._require(now < expires, "Original current context/camera/native/lifecycle expired")
    receipt = {
        "contract": "multidetect.onboard-teacher-observed-context-v2",
        "schema_version": 2,
        "context": context.to_mapping(),
        "context_sha256": context.canonical_hash,
        "source_references": [
            s.to_mapping() for s in [*message_sources, profile_source, resource_source]
        ],
        "qualification_details": {
            "source_file_bytes_SHA_verified": True,
            "source_identity_before_after_equal": True,
            "qualified_plain_MC_disarmed_on_ground": True,
            "clock_id": clock_id,
            "accepted_system_component": [1, 1],
            "message_received_at_s": times,
            "resource_observed_at_s": resource["observed_at_s"],
            "oldest_observation_at_s": context.captured_at_s,
            "endurance_reserve_already_deducted": False,
            "maximum_packets": budget.record_limit,
            "original_record_indices_preserved": True,
            "unknown_values_fabricated": False,
            "UTC_anchor": prefix["UTC_anchor"],
            "bridge": "NEW-live-committed-prefix-v2",
            "lifecycle_budget_sha256": budget.canonical_hash,
        },
        "claims": {
            k: False
            for k in (
                "execution_authority",
                "hardware_control_enabled",
                "mission_approved",
                "source_authenticated",
                "physical_performance_claimed",
            )
        },
    }
    sources = [
        input_source,
        *native_sources,
        *camera_sources,
        *message_sources,
        profile_source,
        runtime_source,
        world_source,
        *models.values(),
    ]
    by_path = {s.path: s for s in sources}
    unique = [by_path[path] for path in sorted(by_path)]
    frozen._require(
        len(unique) <= 64 and all(s.bytes <= 128 * 1024 for s in unique),
        "Origin source closure exceeded unchanged static consumer bounds",
    )
    origin = {
        "contract": frozen.ORIGIN_CONTRACT,
        "schema_version": 1,
        "runtime_kind": "SIM",
        "vehicle_class": "multirotor",
        "observed_context_sha256": context.canonical_hash,
        "airframe_profile_id": context.airframe_profile_id,
        "airframe_profile_sha256": context.airframe_profile_hash,
        "runtime_attestation_sha256": attestation.canonical_hash,
        "container_id": attestation.container_id,
        "image": attestation.image,
        "scenario_id": attestation.scenario_id,
        "scenario_manifest_sha256": attestation.scenario_manifest_sha256,
        "world_source": world_source.to_mapping(),
        "world_reference": geometry["reference"],
        "reference_point": "PX4_navigation_reference_point",
        "verified_home_enu_m": geometry["home_enu_m"],
        "home_position": context.home_position.to_mapping(),
        "clock_id": clock_id,
        "observed_at_s": context.captured_at_s,
        "qualified_at_s": now,
        "expires_at_s": expires,
        "horizontal_uncertainty_m": geometry["uncertainty_m"],
        "vertical_uncertainty_m": geometry["uncertainty_m"],
        "source_evidence": [s.to_mapping() for s in unique],
        "verification": {
            k: True
            for k in (
                "PX4_navigation_reference_point_identified",
                "Gazebo_world_ENU_pose_observed",
                "pose_to_navigation_reference_extrinsic_bound",
                "EKF_reset_continuity_verified",
                "WGS84_home_mapping_checked",
            )
        },
    }
    frozen.ExplicitSimOriginBinding.from_mapping(origin)
    _read_prefixes_v2(root, prefix)
    for path, identity in identities.items():
        stat = (root / path).stat()
        frozen._require(
            (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino) == identity,
            "Immutable lifecycle checkpoint source changed during check",
        )
    if stage is not None:
        stage("attestation_origin_and_final_source_recheck")
    return {
        "observed_context_receipt": receipt,
        "runtime_attestation": attestation.to_mapping(),
        "origin": origin,
        "origin_continuity_token": geometry["continuity_token"],
        "inspected_at_s": inspects[-1][2][0],
        "expires_at_s": expires,
        "record_indices": {k: r["record_index"] for k, r in prefix["snapshot_rows"].items()},
        "committed_prefixes": prefix["committed_prefixes"],
        "camera_source": camera_sources[-1].to_mapping(),
        "native_parameters": parameters,
        "parameter_update": update,
        "maximum_coordinate_residual_m": geometry["maximum_residual_m"],
        "configured_wind": wind.to_mapping(),
        "lifecycle_budget": budget.to_mapping(),
        "lifecycle_budget_sha256": budget.canonical_hash,
    }


def _check_generation(prefix, capture):
    frozen._prefix_writer_identity(prefix, capture)
    for stage in ("raw", "projection"):
        part = prefix["committed_prefixes"][stage]
        stat = Path(capture._streams[stage].name).stat()
        frozen._require(
            stat.st_size == capture._sizes[stage] == part["prefix_bytes"]
            and capture._commits[stage] == part["commits"]
            and capture._digests[stage].hexdigest() == part["prefix_sha256"],
            "Known committed source generation changed; NEW checkpoint required",
        )
    frozen._require(
        prefix["writer_lifecycle"] == capture.lifecycle_mapping(),
        "Current writer lifecycle charges/generation differ",
    )


def _qualify_teacher_live_navigation_checkpoint_v2(
    *,
    artifact_root,
    output_directory,
    prefix_source,
    profile_source,
    runtime_source,
    world_source,
    model_sources,
    camera_updates,
    parameter_baseline,
    navigation_before,
    navigation_after,
    owned_inspect_before,
    owned_inspect_after,
    plan,
    container_id,
    owner_token,
    now_s,
    clock_id,
    live_capture,
    live_clock,
    artifact_ledger=None,
    _stage=None,
):
    budget = _writer(live_capture, live_clock)
    _ledger_scope(artifact_ledger, live_capture, live_clock)
    root, directory = Path(artifact_root).resolve(), Path(output_directory).resolve()
    frozen._require(
        directory.is_relative_to(root) and not directory.exists(),
        "Fresh lifecycle checkpoint output required",
    )
    now = require_real(now_s, "actual lifecycle qualification time", minimum=budget.started_at_s)
    frozen._require(
        clock_id == budget.clock_id and 0 <= live_clock.monotonic() - now <= 0.05,
        "Qualification must use actual original live clock",
    )
    prefix = strict_json_loads(frozen._bound_bytes(root, prefix_source, MAXIMUM_REPORT_BYTES, {}))
    _check_generation(prefix, live_capture)
    wind = frozen._configured_wind(root, world_source, runtime_source)
    profile = strict_json_loads(frozen._bound_bytes(root, profile_source, 65536, {}))
    directory.mkdir()
    resource = {
        "clock_id": clock_id,
        "transaction_id": prefix["transaction_id"],
        "observed_at_s": now,
        "airframe_profile_hash": frozen.airframe_profile_hash(profile),
        "camera_available": True,
        "geofence_valid": False,
        "perception_available": False,
        "map_cache_available": False,
        "dem_available": False,
        "wind_north_mps": wind.wind_north_mps,
        "wind_east_mps": wind.wind_east_mps,
        "wind_source": "software-SIM-configured",
        "configured_wind": wind.to_mapping(),
        "battery_packet_sha256": prefix["snapshot_rows"]["BATTERY_STATUS"]["packet_sha256"],
    }
    resource_source = _save_json(
        root, directory / "limited-resources.json", resource, artifact_ledger
    )
    inputs = {
        "prefix_source": prefix_source.to_mapping(),
        "profile_source": profile_source.to_mapping(),
        "runtime_source": runtime_source.to_mapping(),
        "world_source": world_source.to_mapping(),
        "model_sources": {k: v.to_mapping() for k, v in model_sources.items()},
        "camera_updates": camera_updates,
        "parameter_baseline": parameter_baseline,
        "navigation_before": navigation_before,
        "navigation_after": navigation_after,
        "owned_inspect_before": owned_inspect_before,
        "owned_inspect_after": owned_inspect_after,
        "plan": plan,
        "container_id": container_id,
        "owner_token": owner_token,
        "resource_source": resource_source.to_mapping(),
    }
    input_source = _save_json(root, directory / "checkpoint-inputs.json", inputs, artifact_ledger)
    if _stage is not None:
        _stage("initial_source_and_inputs_persistence")
    derived = _derive(root, inputs, input_source, now, clock_id, _stage)
    frozen._require(
        derived["lifecycle_budget_sha256"] == budget.canonical_hash,
        "Derived checkpoint does not belong to live original budget",
    )
    origin_source = _save_json(
        root, directory / "navigation-origin.json", derived["origin"], artifact_ledger
    )
    document = {
        "contract": CONTRACT,
        "clock_id": clock_id,
        "qualified_at_s": now,
        "inputs": inputs,
        "input_source": input_source.to_mapping(),
        "origin_source": origin_source.to_mapping(),
        **derived,
        "claims": {
            k: False
            for k in (
                "execution_authority",
                "Grant_created",
                "Mission_approved",
                "ARM_sent",
                "flight_executed",
                "map_dem_geofence_qualified",
                "physical_performance_claimed",
                "final_release_allowed",
            )
        },
    }
    source = _save_json(
        root, directory / "live-navigation-checkpoint.json", document, artifact_ledger
    )
    checkpoint = LiveTeacherNavigationCheckpointV2(
        canonical_json(document), source, live_capture, live_clock
    )
    validate_live_teacher_navigation_checkpoint_v2(
        checkpoint,
        artifact_root=root,
        now_s=live_clock.monotonic(),
        clock_id=clock_id,
    )
    if _stage is not None:
        _stage("checkpoint_persistence_and_current_source_revalidation")
    return checkpoint


def qualify_teacher_live_navigation_checkpoint_v2(
    *,
    artifact_root,
    output_directory,
    prefix_source,
    profile_source,
    runtime_source,
    world_source,
    model_sources,
    camera_updates,
    parameter_baseline,
    navigation_before,
    navigation_after,
    owned_inspect_before,
    owned_inspect_after,
    plan,
    container_id,
    owner_token,
    now_s,
    clock_id,
    live_capture,
    live_clock,
    artifact_ledger=None,
):
    """Current qualifier plus finite profiling; diagnostics are never admission."""
    inputs = locals()
    root, directory = Path(artifact_root).resolve(), Path(output_directory).resolve()
    existed = directory.exists()
    start = live_clock.monotonic()
    profiling = {
        "contract": "multidetect.teacher-live-checkpoint-stage-timing-v2",
        "clock_id": clock_id,
        "started_at_s": start,
        "finished_at_s": None,
        "stages": [],
        "semantic_check_completed": False,
        "error": None,
        "diagnostic_not_an_admission_receipt": True,
        "diagnostic_write_included_in_return_age_check": True,
        "claims": {"execution_authority": False, "flight_executed": False},
    }
    previous = start

    def stage(name):
        nonlocal previous
        finished = live_clock.monotonic()
        frozen._require(
            finished >= previous and len(profiling["stages"]) < 16,
            "Actual finite profiling clock/stage bound differs",
        )
        profiling["stages"].append(
            {
                "stage": name,
                "started_at_s": previous,
                "finished_at_s": finished,
                "duration_s": finished - previous,
            }
        )
        previous = finished

    checkpoint, primary = None, None
    try:
        checkpoint = _qualify_teacher_live_navigation_checkpoint_v2(**inputs, _stage=stage)
        profiling["semantic_check_completed"] = True
    except BaseException as error:
        primary = error
        profiling["error"] = f"{type(error).__name__}: {error}"[:1024]
        raise
    finally:
        profiling["finished_at_s"] = live_clock.monotonic()
        if not existed and directory.is_relative_to(root) and directory.is_dir():
            try:
                _save_json(
                    root, directory / "qualification-timing.json", profiling, artifact_ledger
                )
            except BaseException as error:
                if primary is None:
                    raise
                primary.add_note(
                    f"Timing original persistence also failed: {type(error).__name__}: {error}"
                )
    # Diagnostic serialization/fsync/ledger costs remain inside the ACTUAL
    # return-time freshness gate, not excluded from the original two seconds.
    _writer(live_capture, live_clock)
    frozen._require(
        live_clock.monotonic() < checkpoint.expires_at_s,
        "Original evidence expired during diagnostic persistence",
    )
    return checkpoint


def reproduce_teacher_live_navigation_checkpoint_v2(
    checkpoint,
    *,
    artifact_root,
    clock_id,
    expected_origin_continuity_token=None,
):
    """Readonly historical source reproduction; NEVER renew or call writer APIs."""
    frozen._require(
        type(checkpoint) is LiveTeacherNavigationCheckpointV2,
        "Typed NEW lifecycle checkpoint required",
    )
    root = Path(artifact_root).resolve()
    document = strict_json_loads(
        frozen._bound_bytes(root, checkpoint.checkpoint_source, 2 * 1024 * 1024, {})
    )
    frozen._require(
        document == checkpoint.to_mapping()
        and document["contract"] == CONTRACT
        and document["clock_id"] == clock_id,
        "Original lifecycle checkpoint differs",
    )
    input_source = EvidenceBinding.from_mapping(document["input_source"])
    inputs = strict_json_loads(frozen._bound_bytes(root, input_source, 128 * 1024, {}))
    frozen._require(inputs == document["inputs"], "Original lifecycle inputs differ")
    result = _derive(root, inputs, input_source, document["qualified_at_s"], clock_id)
    frozen._require(
        all(document[k] == value for k, value in result.items()),
        "Lifecycle checkpoint semantic/hash derivation differs",
    )
    origin = strict_json_loads(frozen._bound_bytes(root, checkpoint.origin_source, 128 * 1024, {}))
    frozen._require(origin == result["origin"], "Original origin source differs")
    if expected_origin_continuity_token is not None:
        frozen._require(
            checkpoint.origin_continuity_token == expected_origin_continuity_token,
            "Original plan HOME/reference/reset token changed",
        )
    return checkpoint


def validate_live_teacher_navigation_checkpoint_v2(
    checkpoint,
    *,
    artifact_root,
    now_s,
    clock_id,
    expected_origin_continuity_token=None,
):
    """Always actual main-owned current lease, even at original qualified_at."""
    frozen._require(
        type(checkpoint) is LiveTeacherNavigationCheckpointV2,
        "Typed NEW lifecycle checkpoint required",
    )
    frozen._require(
        checkpoint._live_capture is not None and checkpoint._live_clock is not None,
        "Loaded historical receipt cannot become current",
    )
    budget = _writer(checkpoint._live_capture, checkpoint._live_clock)
    now = require_real(now_s, "actual current checkpoint time", minimum=budget.started_at_s)
    frozen._require(
        clock_id == budget.clock_id
        and 0 <= checkpoint._live_clock.monotonic() - now <= 0.05
        and checkpoint.qualified_at_s <= now < checkpoint.expires_at_s,
        "Original current clock/checkpoint lifetime differs",
    )
    root = Path(artifact_root).resolve()
    prefix_source = EvidenceBinding.from_mapping(checkpoint.to_mapping()["inputs"]["prefix_source"])
    prefix = strict_json_loads(frozen._bound_bytes(root, prefix_source, MAXIMUM_REPORT_BYTES, {}))
    _check_generation(prefix, checkpoint._live_capture)
    reproduce_teacher_live_navigation_checkpoint_v2(
        checkpoint,
        artifact_root=root,
        clock_id=clock_id,
        expected_origin_continuity_token=expected_origin_continuity_token,
    )
    _writer(checkpoint._live_capture, checkpoint._live_clock)
    _check_generation(prefix, checkpoint._live_capture)
    final = checkpoint._live_clock.monotonic()
    frozen._require(
        now <= final < checkpoint.expires_at_s,
        "Original evidence expired while source originals were read",
    )
    return checkpoint


__all__ = [
    "LiveTeacherNavigationCheckpointV2",
    "capture_teacher_committed_prefix_v2",
    "qualify_teacher_live_navigation_checkpoint_v2",
    "reproduce_teacher_live_navigation_checkpoint_v2",
    "validate_live_teacher_navigation_checkpoint_v2",
]
